"""Low-level CRUD for the F3 document tables. Mirrors src/clients/db.py's
style: functions take a connection, do one thing, and commit
individually.

src/documents/service.py is the only module that should import this one
directly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_to_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _row_to_dict(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = _rows_to_dicts(conn, sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Document + DocumentVersion
# ---------------------------------------------------------------------------


def create_document_with_version(
    conn: sqlite3.Connection, *, client_id: int, doc_type: str, period: Optional[str],
    filename: str, file_ext: str, file_size: int, file_bytes: bytes,
    classification_source: str, classification_pct: Optional[int], review_status: str,
    created_by: str,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO documents
            (client_id, doc_type, period, current_version_id, classification_source,
             classification_pct, review_status, is_deleted, created_at, created_by)
        VALUES (?, ?, ?, NULL, ?, ?, ?, 0, ?, ?)
        """,
        (client_id, doc_type, period, classification_source, classification_pct, review_status, now, created_by),
    )
    document_id = cur.lastrowid
    version_id = add_version(
        conn, document_id=document_id, version_number=1, filename=filename, file_ext=file_ext,
        file_size=file_size, file_bytes=file_bytes, uploaded_by=created_by, notes=None,
    )
    conn.execute("UPDATE documents SET current_version_id = ? WHERE document_id = ?", (version_id, document_id))
    conn.commit()
    return document_id


def add_version(
    conn: sqlite3.Connection, *, document_id: int, version_number: int, filename: str, file_ext: str,
    file_size: int, file_bytes: bytes, uploaded_by: str, notes: Optional[str],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO document_versions
            (document_id, version_number, filename, file_ext, file_size, file_bytes, uploaded_by, uploaded_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, version_number, filename, file_ext, file_size, file_bytes, uploaded_by, _now(), notes),
    )
    conn.commit()
    return cur.lastrowid


def next_version_number(conn: sqlite3.Connection, document_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) FROM document_versions WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    return (row[0] or 0) + 1


def list_versions(conn: sqlite3.Connection, document_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT version_id, document_id, version_number, filename, file_ext, file_size, "
        "uploaded_by, uploaded_at, notes FROM document_versions WHERE document_id = ? "
        "ORDER BY version_number DESC",
        (document_id,),
    )


def get_version_bytes(conn: sqlite3.Connection, version_id: int) -> Optional[bytes]:
    row = conn.execute("SELECT file_bytes FROM document_versions WHERE version_id = ?", (version_id,)).fetchone()
    return row[0] if row else None


def get_current_version(conn: sqlite3.Connection, document_id: int) -> Optional[dict[str, Any]]:
    """The document's current version row (filename/file_ext/file_size/bytes),
    joined off ``documents.current_version_id``. Returns None when no version
    exists yet."""
    return _row_to_dict(
        conn,
        "SELECT v.* FROM documents d "
        "JOIN document_versions v ON v.version_id = d.current_version_id "
        "WHERE d.document_id = ?",
        (document_id,),
    )


def set_document_notes(conn: sqlite3.Connection, document_id: int, notes: str) -> None:
    conn.execute("UPDATE documents SET notes = ? WHERE document_id = ?", (notes, document_id))
    conn.commit()


def set_document_period(conn: sqlite3.Connection, document_id: int, period: Optional[str]) -> None:
    """Re-file a document under a different period.

    The period is the folder key the reconciliation engine reads
    (``data/<client>/<period>/<source_type>/``), so correcting it is what
    makes a previously unfiled upload reachable by the Run/Reconcile
    pickers.
    """
    conn.execute("UPDATE documents SET period = ? WHERE document_id = ?", (period, document_id))
    conn.commit()


def discard_document(conn: sqlite3.Connection, document_id: int, actor: str) -> None:
    """Hard path for the Unified Review Queue's explicit 'discard' resolution.
    Removes the document row (and, via FK cascade-less explicit deletes, its
    versions, evidence links and queue rows)."""
    conn.execute("DELETE FROM evidence_links WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM document_versions WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM unified_review_queue_entries WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM document_edit_log WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
    conn.commit()


def get_document(conn: sqlite3.Connection, document_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM documents WHERE document_id = ?", (document_id,))


def list_documents(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, doc_type: Optional[str] = None,
    period: Optional[str] = None, include_deleted: bool = False, deleted_only: bool = False,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM documents WHERE 1=1"
    params: list[Any] = []
    if client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    if doc_type:
        sql += " AND doc_type = ?"
        params.append(doc_type)
    if period:
        sql += " AND period = ?"
        params.append(period)
    if deleted_only:
        sql += " AND is_deleted = 1"
    elif not include_deleted:
        sql += " AND is_deleted = 0"
    sql += " ORDER BY created_at DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def set_document_deleted(conn: sqlite3.Connection, document_id: int, *, is_deleted: bool, actor: str) -> None:
    if is_deleted:
        conn.execute(
            "UPDATE documents SET is_deleted = 1, deleted_at = ?, deleted_by = ? WHERE document_id = ?",
            (_now(), actor, document_id),
        )
    else:
        conn.execute(
            "UPDATE documents SET is_deleted = 0, deleted_at = NULL, deleted_by = NULL WHERE document_id = ?",
            (document_id,),
        )
    conn.commit()


def reassign_document_client(conn: sqlite3.Connection, document_id: int, new_client_id: int) -> None:
    conn.execute("UPDATE documents SET client_id = ? WHERE document_id = ?", (new_client_id, document_id))
    conn.commit()


def resolve_queue_entry_for_document(conn: sqlite3.Connection, document_id: int, *, resolved_by: str) -> None:
    conn.execute(
        "UPDATE unified_review_queue_entries SET status = 'resolved', resolved_at = ?, resolved_by = ? "
        "WHERE document_id = ? AND status = 'pending'",
        (_now(), resolved_by, document_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# EvidenceLink
# ---------------------------------------------------------------------------


def add_evidence_link(
    conn: sqlite3.Connection, *, document_id: int, target_type: str, target_id: Optional[int],
    target_label: str, created_by: Optional[str],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO evidence_links (document_id, target_type, target_id, target_label, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (document_id, target_type, target_id, target_label, _now(), created_by),
    )
    conn.commit()
    return cur.lastrowid


def list_evidence_links_for_document(conn: sqlite3.Connection, document_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM evidence_links WHERE document_id = ? ORDER BY created_at DESC", (document_id,)
    )


def list_documents_for_target(conn: sqlite3.Connection, target_type: str, target_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT d.* FROM documents d JOIN evidence_links e ON e.document_id = d.document_id "
        "WHERE e.target_type = ? AND e.target_id = ?",
        (target_type, target_id),
    )


# ---------------------------------------------------------------------------
# Unified Review Queue (shared with future F3-AI)
# ---------------------------------------------------------------------------


def add_queue_entry(conn: sqlite3.Connection, *, document_id: int, reason: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO unified_review_queue_entries (document_id, reason, status, created_at)
        VALUES (?, ?, 'pending', ?)
        """,
        (document_id, reason, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_queue_entries(conn: sqlite3.Connection, *, status: Optional[str] = "pending") -> list[dict[str, Any]]:
    sql = (
        "SELECT q.*, d.doc_type, d.client_id, d.period FROM unified_review_queue_entries q "
        "JOIN documents d ON d.document_id = q.document_id WHERE 1=1"
    )
    params: list[Any] = []
    if status:
        sql += " AND q.status = ?"
        params.append(status)
    sql += " ORDER BY q.created_at DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def resolve_queue_entry(conn: sqlite3.Connection, entry_id: int, *, resolved_by: str, notes: Optional[str]) -> None:
    conn.execute(
        "UPDATE unified_review_queue_entries SET status = 'resolved', resolved_at = ?, resolved_by = ?, "
        "resolution_notes = ? WHERE entry_id = ?",
        (_now(), resolved_by, notes, entry_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Edit log (local stub — retrofit to F4 once it exists)
# ---------------------------------------------------------------------------


def log_edit(
    conn: sqlite3.Connection, *, document_id: int, action: str, old_value: Optional[str],
    new_value: Optional[str], reason: Optional[str], changed_by: str,
) -> None:
    conn.execute(
        """
        INSERT INTO document_edit_log (document_id, action, old_value, new_value, reason, changed_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, action, old_value, new_value, reason, changed_by, _now()),
    )
    conn.commit()


def list_edit_log(conn: sqlite3.Connection, document_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM document_edit_log WHERE document_id = ? ORDER BY id DESC", (document_id,)
    )
