"""Low-level CRUD for the F5 tables. Mirrors src/rules/db.py's style:
functions take a connection, do one thing, and commit individually.

src/f5/service.py is the only module that should import this one
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
# Validation blocks
# ---------------------------------------------------------------------------


def insert_block(
    conn: sqlite3.Connection, *, client_id: int, recon_type: str, source_type: str,
    source_file: str, issue_type: str, severity: str, field_name: Optional[str],
    raw_value: Optional[str], description: str, row_count: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO validation_blocks
            (client_id, recon_type, source_type, source_file, issue_type, severity,
             field_name, raw_value, description, row_count, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (client_id, recon_type, source_type, source_file, issue_type, severity,
         field_name, raw_value, description, row_count, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_blocks(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, status: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM validation_blocks WHERE 1=1"
    params: list[Any] = []
    if client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def get_block(conn: sqlite3.Connection, block_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM validation_blocks WHERE block_id = ?", (block_id,))


def override_block(conn: sqlite3.Connection, block_id: int, *, actor: str, reason: str) -> None:
    conn.execute(
        """
        UPDATE validation_blocks
        SET status = 'overridden', resolved_at = ?, resolved_by = ?, override_reason = ?
        WHERE block_id = ?
        """,
        (_now(), actor, reason, block_id),
    )
    conn.commit()


def clear_open_blocks_for_source(
    conn: sqlite3.Connection, *, client_id: int, source_type: str, source_file: str,
) -> None:
    """Remove any still-open blocks for this exact (client, source_type,
    source_file) before re-running the structural check, so a fixed file
    doesn't keep a stale block around forever."""
    conn.execute(
        "DELETE FROM validation_blocks WHERE client_id = ? AND source_type = ? "
        "AND source_file = ? AND status = 'open'",
        (client_id, source_type, source_file),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Sync health
# ---------------------------------------------------------------------------


def insert_sync_health(
    conn: sqlite3.Connection, *, client_id: int, source: str, status: str, detail: Optional[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO sync_health_status (client_id, source, status, detail, checked_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (client_id, source, status, detail, _now()),
    )
    conn.commit()
    return cur.lastrowid


def latest_sync_health(conn: sqlite3.Connection, client_id: int) -> list[dict[str, Any]]:
    """One row per source for this client — the most recent status."""
    rows = _rows_to_dicts(
        conn,
        "SELECT * FROM sync_health_status WHERE client_id = ? ORDER BY checked_at DESC",
        (client_id,),
    )
    seen: set[str] = set()
    latest: list[dict[str, Any]] = []
    for r in rows:
        if r["source"] not in seen:
            seen.add(r["source"])
            latest.append(r)
    return latest


def sync_health_history(conn: sqlite3.Connection, client_id: int, source: str, *, limit: int = 20) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM sync_health_status WHERE client_id = ? AND source = ? "
        "ORDER BY checked_at DESC LIMIT ?",
        (client_id, source, limit),
    )


def consecutive_failures(conn: sqlite3.Connection, client_id: int, source: str) -> int:
    """Count consecutive attempted_failed entries at the head of history
    (most recent first) — stops at the first non-failure."""
    history = sync_health_history(conn, client_id, source, limit=50)
    count = 0
    for row in history:
        if row["status"] == "attempted_failed":
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Manual control totals
# ---------------------------------------------------------------------------


def insert_control_total(
    conn: sqlite3.Connection, *, client_id: int, period: str, recon_type: str, source_type: str,
    source_file: str, control_count: int, control_amount: Optional[float], ingested_count: int,
    ingested_amount: Optional[float], result: str, entered_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO manual_control_totals
            (client_id, period, recon_type, source_type, source_file, control_count,
             control_amount, ingested_count, ingested_amount, result, entered_by, entered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (client_id, period, recon_type, source_type, source_file, control_count,
         control_amount, ingested_count, ingested_amount, result, entered_by, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_control_totals(conn: sqlite3.Connection, *, client_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM manual_control_totals"
    params: tuple = ()
    if client_id is not None:
        sql += " WHERE client_id = ?"
        params = (client_id,)
    sql += " ORDER BY entered_at DESC"
    return _rows_to_dicts(conn, sql, params)


# ---------------------------------------------------------------------------
# Reconciliation-of-the-reconciliation (dormant interface)
# ---------------------------------------------------------------------------


def list_recon_of_recon(conn: sqlite3.Connection, *, client_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM recon_of_recon_results"
    params: tuple = ()
    if client_id is not None:
        sql += " WHERE client_id = ?"
        params = (client_id,)
    sql += " ORDER BY created_at DESC"
    return _rows_to_dicts(conn, sql, params)


def insert_recon_of_recon(
    conn: sqlite3.Connection, *, client_id: int, run_id: Optional[int], matched_sum: Optional[float],
    unmatched_sum: Optional[float], excluded_sum: Optional[float], ingested_total: Optional[float],
    ties_out: Optional[bool],
) -> int:
    """Not called anywhere yet this build — Module 2 wires the real check.
    Present so the calling interface is stable when that lands."""
    cur = conn.execute(
        """
        INSERT INTO recon_of_recon_results
            (client_id, run_id, matched_sum, unmatched_sum, excluded_sum, ingested_total, ties_out, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (client_id, run_id, matched_sum, unmatched_sum, excluded_sum, ingested_total,
         None if ties_out is None else int(ties_out), _now()),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Escalation events
# ---------------------------------------------------------------------------


def insert_escalation(
    conn: sqlite3.Connection, *, client_id: int, source: str, consecutive_fails: int, message: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO escalation_events (client_id, source, consecutive_fails, message, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (client_id, source, consecutive_fails, message, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_escalations(conn: sqlite3.Connection, *, client_id: Optional[int] = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM escalation_events"
    params: list[Any] = []
    if client_id is not None:
        sql += " WHERE client_id = ?"
        params.append(client_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return _rows_to_dicts(conn, sql, tuple(params))


# ---------------------------------------------------------------------------
# Change log (local stub — retrofit to F4)
# ---------------------------------------------------------------------------


def log_change(
    conn: sqlite3.Connection, *, block_id: Optional[int], entry_kind: str, action: str,
    detail: Optional[str], reason: Optional[str], actor: str,
) -> None:
    conn.execute(
        "INSERT INTO validation_change_log (block_id, entry_kind, action, detail, reason, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (block_id, entry_kind, action, detail, reason, actor, _now()),
    )
    conn.commit()


def list_change_log(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM validation_change_log ORDER BY created_at DESC LIMIT ?", (limit,),
    )
