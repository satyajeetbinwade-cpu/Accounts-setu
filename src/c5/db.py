"""Low-level CRUD for the C5 AI Instruction & Knowledge Library tables.
Mirrors src/rules/db.py's style: functions take a connection, do one thing,
and commit individually.

src/c5/service.py is the only module that should import this one directly.
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
# Touchpoints
# ---------------------------------------------------------------------------


def list_touchpoints(conn: sqlite3.Connection, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM instruction_touchpoints"
    if not include_inactive:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort_order, touchpoint_id"
    return _rows_to_dicts(conn, sql)


def get_touchpoint_by_key(conn: sqlite3.Connection, key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM instruction_touchpoints WHERE key = ?", (key,))


def upsert_touchpoint(
    conn: sqlite3.Connection, *, key: str, label: str, is_active: bool, sort_order: int,
) -> int:
    conn.execute(
        """
        INSERT INTO instruction_touchpoints (key, label, is_active, sort_order)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            label = excluded.label,
            is_active = excluded.is_active,
            sort_order = excluded.sort_order
        """,
        (key, label, int(is_active), sort_order),
    )
    conn.commit()
    row = conn.execute("SELECT touchpoint_id FROM instruction_touchpoints WHERE key = ?", (key,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def list_instructions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM instructions ORDER BY is_active DESC, updated_at DESC")


def get_instruction(conn: sqlite3.Connection, instruction_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM instructions WHERE instruction_id = ?", (instruction_id,))


def create_instruction(
    conn: sqlite3.Connection, *, title: str, explanation: str, scope: str,
    client_id: Optional[int], created_by: str,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO instructions (title, explanation, scope, client_id, is_active, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (title, explanation, scope, client_id, created_by, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_instruction(
    conn: sqlite3.Connection, instruction_id: int, *, title: str, explanation: str,
    scope: str, client_id: Optional[int],
) -> None:
    conn.execute(
        """
        UPDATE instructions SET title = ?, explanation = ?, scope = ?, client_id = ?, updated_at = ?
        WHERE instruction_id = ?
        """,
        (title, explanation, scope, client_id, _now(), instruction_id),
    )
    conn.commit()


def set_instruction_active(conn: sqlite3.Connection, instruction_id: int, is_active: bool) -> None:
    conn.execute(
        "UPDATE instructions SET is_active = ?, updated_at = ? WHERE instruction_id = ?",
        (int(is_active), _now(), instruction_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Instruction tags (many-to-many)
# ---------------------------------------------------------------------------


def set_instruction_tags(conn: sqlite3.Connection, instruction_id: int, touchpoint_ids: list[int]) -> None:
    conn.execute("DELETE FROM instruction_tags WHERE instruction_id = ?", (instruction_id,))
    for tid in touchpoint_ids:
        conn.execute(
            "INSERT OR IGNORE INTO instruction_tags (instruction_id, touchpoint_id) VALUES (?, ?)",
            (instruction_id, tid),
        )
    conn.commit()


def instruction_tag_keys(conn: sqlite3.Connection, instruction_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.key FROM instruction_tags it
        JOIN instruction_touchpoints t ON t.touchpoint_id = it.touchpoint_id
        WHERE it.instruction_id = ? ORDER BY t.sort_order
        """,
        (instruction_id,),
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Instruction versions (immutable, indefinite retention)
# ---------------------------------------------------------------------------


def insert_instruction_version(
    conn: sqlite3.Connection, *, instruction_id: int, title: str, explanation: str,
    scope: str, client_id: Optional[int], change_summary: str, changed_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO instruction_versions
            (instruction_id, title, explanation, scope, client_id, change_summary, changed_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (instruction_id, title, explanation, scope, client_id, change_summary, changed_by, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_instruction_versions(conn: sqlite3.Connection, instruction_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM instruction_versions WHERE instruction_id = ? ORDER BY version_id DESC",
        (instruction_id,),
    )


# ---------------------------------------------------------------------------
# Proposed instructions (always pending Admin confirmation)
# ---------------------------------------------------------------------------


def create_proposal(
    conn: sqlite3.Connection, *, title: str, explanation: str, scope: str,
    client_id: Optional[int], source: str, submitted_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO proposed_instructions (title, explanation, scope, client_id, source, status, submitted_by, submitted_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (title, explanation, scope, client_id, source, submitted_by, _now()),
    )
    conn.commit()
    return cur.lastrowid


def set_proposal_tags(conn: sqlite3.Connection, proposal_id: int, touchpoint_ids: list[int]) -> None:
    conn.execute("DELETE FROM proposed_instruction_tags WHERE proposal_id = ?", (proposal_id,))
    for tid in touchpoint_ids:
        conn.execute(
            "INSERT OR IGNORE INTO proposed_instruction_tags (proposal_id, touchpoint_id) VALUES (?, ?)",
            (proposal_id, tid),
        )
    conn.commit()


def proposal_tag_keys(conn: sqlite3.Connection, proposal_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.key FROM proposed_instruction_tags pt
        JOIN instruction_touchpoints t ON t.touchpoint_id = pt.touchpoint_id
        WHERE pt.proposal_id = ? ORDER BY t.sort_order
        """,
        (proposal_id,),
    ).fetchall()
    return [r[0] for r in rows]


def list_proposals(conn: sqlite3.Connection, status: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM proposed_instructions"
    params: tuple = ()
    if status is not None:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY submitted_at DESC"
    return _rows_to_dicts(conn, sql, params)


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM proposed_instructions WHERE proposal_id = ?", (proposal_id,))


def resolve_proposal(
    conn: sqlite3.Connection, proposal_id: int, *, status: str, resolved_by: str,
) -> None:
    conn.execute(
        "UPDATE proposed_instructions SET status = ?, resolved_by = ?, resolved_at = ? WHERE proposal_id = ?",
        (status, resolved_by, _now(), proposal_id),
    )
    conn.commit()