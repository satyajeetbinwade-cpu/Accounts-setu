"""Low-level CRUD for F3-B — Invoice Extraction & Digitalization.

Mirrors src/documents/db.py and src/ingestion_ai/db.py: functions take a
connection, do one thing, and commit individually.

src/invoice_extract/service.py is the only module that should import this
one directly.

Immutability invariant: there is deliberately NO update/delete function
for ``tally_export_batches`` anywhere in this file — a generated export
batch is immutable by construction, so a re-export can only ever INSERT a
new row.
"""

from __future__ import annotations

import json
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
# InvoiceUpload
# ---------------------------------------------------------------------------


def create_upload(
    conn: sqlite3.Connection, *, client_id: int, filename: str, file_ext: str, source_format: str,
    extraction_path: str, batch_id: Optional[str], status: str, duplicate_warning: bool, actor: str,
    file_bytes: Optional[bytes] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO invoice_uploads
            (document_id, client_id, filename, file_ext, file_bytes, source_format, extraction_path,
             batch_id, status, duplicate_warning, created_at, created_by)
        VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (client_id, filename, file_ext, file_bytes, source_format, extraction_path, batch_id,
         status, int(duplicate_warning), _now(), actor),
    )
    conn.commit()
    return cur.lastrowid


def get_upload(conn: sqlite3.Connection, upload_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM invoice_uploads WHERE upload_id = ?", (upload_id,))


def get_upload_bytes(conn: sqlite3.Connection, upload_id: int) -> Optional[bytes]:
    row = conn.execute("SELECT file_bytes FROM invoice_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
    return row[0] if row else None


def list_uploads(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, status: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM invoice_uploads WHERE 1=1"
    params: list[Any] = []
    if client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if batch_id:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    sql += " ORDER BY created_at DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def find_duplicate(
    conn: sqlite3.Connection, *, invoice_number: Optional[str], vendor_gstin: Optional[str],
    exclude_upload_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Duplicate invoice detection (same invoice number + vendor GSTIN).
    Only a genuine match on BOTH keys counts. Returns the existing upload
    row (for the warning) or None. ``exclude_upload_id`` skips the upload
    currently being processed."""
    if not invoice_number or not vendor_gstin:
        return None
    sql = """
        SELECT u.* FROM invoice_uploads u
        JOIN extracted_invoice_fields f_no ON f_no.upload_id = u.upload_id
             AND f_no.field_name = 'invoice_number'
             AND LOWER(TRIM(COALESCE(f_no.resolved_value, f_no.extracted_value))) = LOWER(TRIM(?))
        JOIN extracted_invoice_fields f_gs ON f_gs.upload_id = u.upload_id
             AND f_gs.field_name = 'vendor_gstin'
             AND UPPER(TRIM(COALESCE(f_gs.resolved_value, f_gs.extracted_value))) = UPPER(TRIM(?))
    """
    params: list[Any] = [invoice_number, vendor_gstin]
    if exclude_upload_id is not None:
        sql += " WHERE u.upload_id <> ?"
        params.append(exclude_upload_id)
    sql += " ORDER BY u.upload_id DESC LIMIT 1"
    return _row_to_dict(conn, sql, tuple(params))


def set_upload_status(
    conn: sqlite3.Connection, upload_id: int, status: str, *,
    confirmed_by: Optional[str] = None,
) -> None:
    if confirmed_by is not None:
        conn.execute(
            "UPDATE invoice_uploads SET status = ?, confirmed_at = ?, confirmed_by = ? WHERE upload_id = ?",
            (status, _now(), confirmed_by, upload_id),
        )
    else:
        conn.execute("UPDATE invoice_uploads SET status = ? WHERE upload_id = ?", (status, upload_id))
    conn.commit()


def mark_duplicate_warning(conn: sqlite3.Connection, upload_id: int) -> None:
    conn.execute("UPDATE invoice_uploads SET duplicate_warning = 1 WHERE upload_id = ?", (upload_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# ExtractedInvoiceField
# ---------------------------------------------------------------------------


def insert_field(conn: sqlite3.Connection, *, upload_id: int, result: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO extracted_invoice_fields
            (upload_id, field_name, extracted_value, confidence, source_location, is_present)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (upload_id, result["field_name"], result.get("extracted_value"), result.get("confidence"),
         result.get("source_location"), int(result.get("is_present", True))),
    )
    conn.commit()
    return cur.lastrowid


def list_fields(conn: sqlite3.Connection, upload_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM extracted_invoice_fields WHERE upload_id = ? ORDER BY field_id", (upload_id,)
    )


def get_field(conn: sqlite3.Connection, upload_id: int, field_name: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn,
        "SELECT * FROM extracted_invoice_fields WHERE upload_id = ? AND field_name = ?",
        (upload_id, field_name),
    )


def resolve_field(
    conn: sqlite3.Connection, *, upload_id: int, field_name: str, resolved_value: Optional[str], actor: str,
) -> None:
    """Reviewer confirms/edits one field. Marking it resolved clears it from
    the threshold gate for this upload."""
    conn.execute(
        """
        UPDATE extracted_invoice_fields
        SET resolved = 1, resolved_value = ?, resolved_by = ?, resolved_at = ?
        WHERE upload_id = ? AND field_name = ?
        """,
        (resolved_value, actor, _now(), upload_id, field_name),
    )
    conn.commit()


def effective_value(field_row: dict[str, Any]) -> Optional[str]:
    """The value that should be used downstream (export / duplicate check):
    the reviewer's resolved value when present, else the extracted value."""
    resolved = field_row.get("resolved_value")
    if field_row.get("resolved") and resolved is not None:
        return resolved
    return field_row.get("extracted_value")


# ---------------------------------------------------------------------------
# InvoiceReviewQueueEntry (this module's OWN queue)
# ---------------------------------------------------------------------------


def create_queue_entry(
    conn: sqlite3.Connection, *, upload_id: int, flagged_fields: list[str],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO invoice_review_queue_entries (upload_id, flagged_fields, status, created_at)
        VALUES (?, ?, 'open', ?)
        """,
        (upload_id, json.dumps(flagged_fields), _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_open_queue_entry(conn: sqlite3.Connection, upload_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(
        conn,
        "SELECT * FROM invoice_review_queue_entries WHERE upload_id = ? AND status = 'open' ORDER BY entry_id DESC LIMIT 1",
        (upload_id,),
    )
    if row is not None:
        row["flagged_fields"] = json.loads(row["flagged_fields"])
    return row


def list_queue(
    conn: sqlite3.Connection, *, status: Optional[str] = "open",
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM invoice_review_queue_entries WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    rows = _rows_to_dicts(conn, sql, tuple(params))
    for r in rows:
        r["flagged_fields"] = json.loads(r["flagged_fields"])
    return rows


def resolve_queue_entry(conn: sqlite3.Connection, upload_id: int, *, resolved_by: str) -> None:
    conn.execute(
        """
        UPDATE invoice_review_queue_entries
        SET status = 'resolved', resolved_at = ?, resolved_by = ?
        WHERE upload_id = ? AND status = 'open'
        """,
        (_now(), resolved_by, upload_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# TallyExportBatch — INSERT-only (immutable by construction)
# ---------------------------------------------------------------------------


def insert_export_batch(
    conn: sqlite3.Connection, *, filename: str, upload_ids: list[int], row_count: int, row_range: str,
    format_version: str, generated_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO tally_export_batches
            (filename, upload_ids, row_count, row_range, format_version, generated_by, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (filename, json.dumps(upload_ids), row_count, row_range, format_version, generated_by, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_export_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows_to_dicts(conn, "SELECT * FROM tally_export_batches ORDER BY batch_id DESC")
    for r in rows:
        r["upload_ids"] = json.loads(r["upload_ids"])
    return rows


def get_export_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(conn, "SELECT * FROM tally_export_batches WHERE batch_id = ?", (batch_id,))
    if row is not None:
        row["upload_ids"] = json.loads(row["upload_ids"])
    return row


# ---------------------------------------------------------------------------
# AITouchpoint rows (the two new C3-ext ModelAssignment rows)
# ---------------------------------------------------------------------------


def list_touchpoints(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM invoice_ai_touchpoints ORDER BY sort_order")


def get_touchpoint(conn: sqlite3.Connection, key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM invoice_ai_touchpoints WHERE touchpoint_key = ?", (key,))


def set_touchpoint_models(
    conn: sqlite3.Connection, key: str, *, primary_model: str, fallback_model: str,
) -> None:
    conn.execute(
        "UPDATE invoice_ai_touchpoints SET primary_model = ?, fallback_model = ? WHERE touchpoint_key = ?",
        (primary_model, fallback_model, key),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Change log — LOCAL stub, retrofit to F4 later
# ---------------------------------------------------------------------------


def log_change(
    conn: sqlite3.Connection, *, upload_id: Optional[int], action: str, detail: str, actor: str,
) -> None:
    conn.execute(
        "INSERT INTO invoice_extract_change_log (upload_id, action, detail, actor, created_at) VALUES (?, ?, ?, ?, ?)",
        (upload_id, action, detail, actor, _now()),
    )
    conn.commit()


def list_change_log(conn: sqlite3.Connection, *, upload_id: Optional[int] = None) -> list[dict[str, Any]]:
    if upload_id is not None:
        return _rows_to_dicts(
            conn,
            "SELECT * FROM invoice_extract_change_log WHERE upload_id = ? ORDER BY log_id DESC",
            (upload_id,),
        )
    return _rows_to_dicts(conn, "SELECT * FROM invoice_extract_change_log ORDER BY log_id DESC LIMIT 200")