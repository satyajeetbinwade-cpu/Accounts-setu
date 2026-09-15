"""Low-level CRUD for the F4 edit-history table. Mirrors src/rules/db.py's
style: functions take a connection, do one thing, and commit individually.

src/f4/service.py is the only module that should import this one directly.

Deliberately append-only: there is NO update_* / delete_* function here for
edit_history_entries. An entry, once written, is never mutated at the
storage level (F4 build prompt: "immutable once written, permanently
queryable, never overwritten").
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


def insert_entry(
    conn: sqlite3.Connection, *, record_type: str, record_id: str, field: str,
    old_value: Optional[str], new_value: Optional[str], reason: Optional[str],
    changed_by: str, client_id: Optional[int] = None,
) -> int:
    """Append ONE immutable edit-history entry and return its entry_id."""
    cur = conn.execute(
        """
        INSERT INTO edit_history_entries
            (record_type, record_id, client_id, field, old_value, new_value, reason, changed_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (record_type, record_id, client_id, field, old_value, new_value, reason, changed_by, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_entries_for_record(
    conn: sqlite3.Connection, record_type: str, record_id: str,
    *, client_id: Optional[int] = None, firm_only: bool = False,
) -> list[dict[str, Any]]:
    """Newest first, per the History panel spec.

    ``firm_only`` restricts to firm-wide entries (``client_id IS NULL``) \u2014 so
    a firm-wide rule value's History doesn't mix in every client override.
    Otherwise, when ``client_id`` is given, entries are restricted to that
    client."""
    sql = "SELECT * FROM edit_history_entries WHERE record_type = ? AND record_id = ?"
    params: list[Any] = [record_type, record_id]
    if firm_only:
        sql += " AND client_id IS NULL"
    elif client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    sql += " ORDER BY entry_id DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def list_entries_for_client(conn: sqlite3.Connection, client_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM edit_history_entries WHERE client_id = ? ORDER BY entry_id DESC",
        (client_id,),
    )


def recent_entries(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM edit_history_entries ORDER BY entry_id DESC LIMIT ?",
        (limit,),
    )