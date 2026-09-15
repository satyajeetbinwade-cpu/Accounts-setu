"""Low-level CRUD for the C2 filing tables. Mirrors src/rules/db.py's
style: functions take a connection, do one thing, and commit individually.

src/filing/service.py is the only module that should import this one
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
# Filing records (state machine)
# ---------------------------------------------------------------------------


def create_filing_record(
    conn: sqlite3.Connection, *, client_id: int, obligation: str, period: str,
    package_document_id: Optional[int], created_by: str,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO filing_records
            (client_id, obligation, period, package_document_id, status, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?)
        """,
        (client_id, obligation, period, package_document_id, created_by, now, now),
    )
    conn.commit()
    return cur.lastrowid


def get_filing_record(conn: sqlite3.Connection, filing_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM filing_records WHERE filing_id = ?", (filing_id,))


def list_filing_records(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, status: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM filing_records WHERE 1=1"
    params: list[Any] = []
    if client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def set_filing_sent(
    conn: sqlite3.Connection, filing_id: int, *, sent_by: str, sent_at: str,
) -> None:
    conn.execute(
        "UPDATE filing_records SET status = 'sent', sent_by = ?, sent_at = ?, updated_at = ? "
        "WHERE filing_id = ?",
        (sent_by, sent_at, _now(), filing_id),
    )
    conn.commit()


def set_filing_outcome(
    conn: sqlite3.Connection, filing_id: int, *, status: str, outcome_note: Optional[str],
) -> None:
    """status must be one of 'acknowledged' | 'failed' | 'ambiguous'."""
    conn.execute(
        "UPDATE filing_records SET status = ?, outcome_note = ?, updated_at = ? WHERE filing_id = ?",
        (status, outcome_note, _now(), filing_id),
    )
    conn.commit()


def set_filing_acknowledgement(
    conn: sqlite3.Connection, filing_id: int, *, acknowledgement_document_id: int,
    confirmed_by: str,
) -> None:
    conn.execute(
        "UPDATE filing_records SET status = 'acknowledged', "
        "acknowledgement_document_id = ?, acknowledgement_confirmed_by = ?, "
        "acknowledgement_confirmed_at = ?, updated_at = ? WHERE filing_id = ?",
        (acknowledgement_document_id, confirmed_by, _now(), _now(), filing_id),
    )
    conn.commit()


def attach_package_document(
    conn: sqlite3.Connection, filing_id: int, package_document_id: int,
) -> None:
    conn.execute(
        "UPDATE filing_records SET package_document_id = ?, updated_at = ? WHERE filing_id = ?",
        (package_document_id, _now(), filing_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Filing calendar entries
# ---------------------------------------------------------------------------


def list_calendar_entries(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM filing_calendar_entries"
    params: tuple = ()
    if client_id is not None:
        sql += " WHERE client_id = ?"
        params = (client_id,)
    sql += " ORDER BY due_date, obligation"
    return _rows_to_dicts(conn, sql, params)


def upsert_calendar_entry(
    conn: sqlite3.Connection, *, client_id: int, obligation: str, period: str, due_date: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO filing_calendar_entries (client_id, obligation, period, due_date, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(client_id, obligation, period) DO UPDATE SET
            due_date = excluded.due_date
        """,
        (client_id, obligation, period, due_date, _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT entry_id FROM filing_calendar_entries WHERE client_id = ? AND obligation = ? AND period = ?",
        (client_id, obligation, period),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Per-client source toggles
# ---------------------------------------------------------------------------


def list_toggles(conn: sqlite3.Connection, *, client_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM per_client_source_toggles"
    params: tuple = ()
    if client_id is not None:
        sql += " WHERE client_id = ?"
        params = (client_id,)
    sql += " ORDER BY source"
    return _rows_to_dicts(conn, sql, params)


def get_toggle(conn: sqlite3.Connection, client_id: int, source: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn,
        "SELECT * FROM per_client_source_toggles WHERE client_id = ? AND source = ?",
        (client_id, source),
    )


def upsert_toggle(conn: sqlite3.Connection, *, client_id: int, source: str, mode: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO per_client_source_toggles (client_id, source, mode, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(client_id, source) DO UPDATE SET
            mode = excluded.mode,
            updated_at = excluded.updated_at
        """,
        (client_id, source, mode, _now(), _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT toggle_id FROM per_client_source_toggles WHERE client_id = ? AND source = ?",
        (client_id, source),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Audit log + escalations
# ---------------------------------------------------------------------------


def log_audit(
    conn: sqlite3.Connection, *, filing_id: int, entry_type: str, action: str,
    detail: Optional[str], actor: str,
) -> None:
    conn.execute(
        "INSERT INTO filing_audit_log (filing_id, entry_type, action, detail, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (filing_id, entry_type, action, detail, actor, _now()),
    )
    conn.commit()


def list_audit_log(conn: sqlite3.Connection, filing_id: int, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM filing_audit_log WHERE filing_id = ? ORDER BY id DESC LIMIT ?",
        (filing_id, limit),
    )


def insert_escalation(
    conn: sqlite3.Connection, *, filing_id: int, event_type: str, message: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO filing_escalations (filing_id, event_type, message, created_at) "
        "VALUES (?, ?, ?, ?)",
        (filing_id, event_type, message, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_escalations(conn: sqlite3.Connection, *, filing_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM filing_escalations"
    params: tuple = ()
    if filing_id is not None:
        sql += " WHERE filing_id = ?"
        params = (filing_id,)
    sql += " ORDER BY escalation_id DESC"
    return _rows_to_dicts(conn, sql, params)
