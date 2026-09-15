"""Low-level CRUD for the Module 8 Action Center tables. Mirrors
src/module2/db.py's style: functions take a connection, do one thing, and
commit individually.

src/action_center/service.py is the only module that should import this
one directly.
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
# Assignments (Module 8's overlay)
# ---------------------------------------------------------------------------


def get_assignment(
    conn: sqlite3.Connection, source_module: str, item_ref: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn,
        "SELECT * FROM action_center_assignments WHERE source_module = ? AND item_ref = ?",
        (source_module, item_ref),
    )


def upsert_assignment(
    conn: sqlite3.Connection, *, source_module: str, item_ref: str, client_id: Optional[int],
    assignee: Optional[str], due_date: Optional[str], age_escalated: bool = False,
    escalated_at: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO action_center_assignments
            (source_module, item_ref, client_id, assignee, due_date, age_escalated, escalated_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_module, item_ref) DO UPDATE SET
            client_id = COALESCE(excluded.client_id, action_center_assignments.client_id),
            assignee = excluded.assignee,
            due_date = excluded.due_date,
            age_escalated = excluded.age_escalated,
            escalated_at = excluded.escalated_at,
            updated_at = excluded.updated_at
        """,
        (
            source_module, item_ref, client_id, assignee, due_date,
            int(age_escalated), escalated_at, _now(),
        ),
    )
    conn.commit()


def list_assignments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM action_center_assignments")


def set_assignee_and_due(
    conn: sqlite3.Connection, *, source_module: str, item_ref: str, client_id: Optional[int],
    assignee: Optional[str], due_date: Optional[str],
) -> None:
    """Update ONLY the manual assignee + due date. Never touches priority
    (there is no priority column here — it is owned by the originating
    module), so a reassignment can never silently change priority."""
    conn.execute(
        """
        INSERT INTO action_center_assignments
            (source_module, item_ref, client_id, assignee, due_date, age_escalated, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(source_module, item_ref) DO UPDATE SET
            client_id = COALESCE(excluded.client_id, action_center_assignments.client_id),
            assignee = excluded.assignee,
            due_date = excluded.due_date,
            updated_at = excluded.updated_at
        """,
        (source_module, item_ref, client_id, assignee, due_date, _now()),
    )
    conn.commit()


def mark_age_escalated(conn: sqlite3.Connection, source_module: str, item_ref: str) -> None:
    conn.execute(
        "UPDATE action_center_assignments SET age_escalated = 1, escalated_at = ?, updated_at = ? "
        "WHERE source_module = ? AND item_ref = ?",
        (_now(), _now(), source_module, item_ref),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Local change log
# ---------------------------------------------------------------------------


def log_change(
    conn: sqlite3.Connection, *, source_module: str, item_ref: str, entry_kind: str,
    action: str, detail: Optional[str], reason: Optional[str], actor: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO action_center_change_log
            (source_module, item_ref, entry_kind, action, detail, reason, actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_module, item_ref, entry_kind, action, detail, reason, actor, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_change_log(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM action_center_change_log ORDER BY id DESC LIMIT ?", (limit,)
    )