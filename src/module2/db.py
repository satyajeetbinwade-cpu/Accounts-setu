"""Low-level CRUD for the Module 2 tables. Mirrors src/f5/db.py's style:
functions take a connection, do one thing, and commit individually.

src/module2/service.py is the only module that should import this one
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
# Reconciliation exceptions (generic Flagged Item shape)
# ---------------------------------------------------------------------------


def insert_exception(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO reconciliation_exceptions
            (client_id, run_id, result_id, fingerprint, sub_type, recon_type,
             source_module, item_type, classification, difference_type,
             confidence_score, confidence_band, confidence_source, recommendation,
             recommendation_reason, priority, assignee, due_date, status,
             resolution, resolution_note, resolved_by, resolved_at, evidence,
             match_reason, materiality_threshold, above_materiality, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec["client_id"], rec.get("run_id"), rec.get("result_id"), rec.get("fingerprint"),
            rec["sub_type"], rec["recon_type"], rec.get("source_module", "Module 2"),
            rec["item_type"], rec["classification"], rec.get("difference_type"),
            rec.get("confidence_score"), rec.get("confidence_band"),
            rec.get("confidence_source", "rule"), rec.get("recommendation"),
            rec.get("recommendation_reason"), rec.get("priority", "normal"),
            rec.get("assignee"), rec.get("due_date"), rec.get("status", "open"),
            rec.get("resolution"), rec.get("resolution_note"), rec.get("resolved_by"),
            rec.get("resolved_at"), rec.get("evidence"), rec.get("match_reason"),
            rec.get("materiality_threshold"), 1 if rec.get("above_materiality") else 0,
            rec.get("created_at") or _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_exceptions(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, run_id: Optional[int] = None,
    sub_type: Optional[str] = None, status: Optional[str] = None,
    classification: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reconciliation_exceptions WHERE 1=1"
    params: list[Any] = []
    if client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    if run_id is not None:
        sql += " AND run_id = ?"
        params.append(run_id)
    if sub_type is not None:
        sql += " AND sub_type = ?"
        params.append(sub_type)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    if classification is not None:
        sql += " AND classification = ?"
        params.append(classification)
    sql += " ORDER BY exception_id DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def get_exception(conn: sqlite3.Connection, exception_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn, "SELECT * FROM reconciliation_exceptions WHERE exception_id = ?", (exception_id,)
    )


def resolve_exception(
    conn: sqlite3.Connection, exception_id: int, *, resolution: str, note: Optional[str],
    actor: str, status: str = "resolved",
) -> None:
    conn.execute(
        """
        UPDATE reconciliation_exceptions
        SET status = ?, resolution = ?, resolution_note = ?, resolved_by = ?, resolved_at = ?
        WHERE exception_id = ?
        """,
        (status, resolution, note, actor, _now(), exception_id),
    )
    conn.commit()


def set_exception_priority(conn: sqlite3.Connection, exception_id: int, priority: str) -> None:
    conn.execute(
        "UPDATE reconciliation_exceptions SET priority = ? WHERE exception_id = ?",
        (priority, exception_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# AI recommendations (Recommendation half of the pair)
# ---------------------------------------------------------------------------


def insert_recommendation(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO ai_recommendations
            (exception_id, run_id, fingerprint, touchpoint, recommendation, reasoning,
             confidence_pct, model_used, routing_reason, routing_stub, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec.get("exception_id"), rec.get("run_id"), rec.get("fingerprint"),
            rec["touchpoint"], rec["recommendation"], rec["reasoning"],
            rec.get("confidence_pct"), rec.get("model_used"), rec.get("routing_reason"),
            1 if rec.get("routing_stub", True) else 0, rec.get("created_at") or _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_recommendations(
    conn: sqlite3.Connection, *, exception_id: Optional[int] = None,
    touchpoint: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ai_recommendations WHERE 1=1"
    params: list[Any] = []
    if exception_id is not None:
        sql += " AND exception_id = ?"
        params.append(exception_id)
    if touchpoint is not None:
        sql += " AND touchpoint = ?"
        params.append(touchpoint)
    sql += " ORDER BY recommendation_id DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


# ---------------------------------------------------------------------------
# AI outcomes (Outcome half — STUB, never written this build)
# ---------------------------------------------------------------------------


def list_outcomes(conn: sqlite3.Connection, *, exception_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ai_outcomes WHERE 1=1"
    params: list[Any] = []
    if exception_id is not None:
        sql += " AND exception_id = ?"
        params.append(exception_id)
    sql += " ORDER BY outcome_id DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


# ---------------------------------------------------------------------------
# Matching key configs
# ---------------------------------------------------------------------------


def upsert_matching_key_config(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    conn.execute(
        """
        INSERT INTO matching_key_configs
            (sub_type, source_key, label, match_keys, tolerance_absolute, tolerance_percent,
             date_tolerance_days, fuzzy_threshold, materiality_threshold, is_active, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sub_type, source_key) DO UPDATE SET
            label = excluded.label,
            match_keys = excluded.match_keys,
            tolerance_absolute = excluded.tolerance_absolute,
            tolerance_percent = excluded.tolerance_percent,
            date_tolerance_days = excluded.date_tolerance_days,
            fuzzy_threshold = excluded.fuzzy_threshold,
            materiality_threshold = excluded.materiality_threshold,
            is_active = excluded.is_active,
            updated_at = excluded.updated_at
        """,
        (
            rec["sub_type"], rec["source_key"], rec["label"], rec["match_keys"],
            rec.get("tolerance_absolute"), rec.get("tolerance_percent"),
            rec.get("date_tolerance_days"), rec.get("fuzzy_threshold"),
            rec.get("materiality_threshold"), 1 if rec.get("is_active", True) else 0,
            rec.get("updated_at") or _now(),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT config_id FROM matching_key_configs WHERE sub_type = ? AND source_key = ?",
        (rec["sub_type"], rec["source_key"]),
    ).fetchone()
    return row[0]


def list_matching_key_configs(
    conn: sqlite3.Connection, *, sub_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM matching_key_configs WHERE 1=1"
    params: list[Any] = []
    if sub_type is not None:
        sql += " AND sub_type = ?"
        params.append(sub_type)
    sql += " ORDER BY sub_type, config_id"
    return _rows_to_dicts(conn, sql, tuple(params))


def get_matching_key_config(
    conn: sqlite3.Connection, sub_type: str, source_key: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn,
        "SELECT * FROM matching_key_configs WHERE sub_type = ? AND source_key = ?",
        (sub_type, source_key),
    )


# ---------------------------------------------------------------------------
# Eligible credit figures (2A)
# ---------------------------------------------------------------------------


def insert_eligible_credit(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO eligible_credit_figures
            (client_id, run_id, period, total_itc_claimed, matched_itc, at_risk_itc,
             blocked_credit_itc, reverse_charge_itc, eligible_credit, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec["client_id"], rec.get("run_id"), rec["period"],
            rec.get("total_itc_claimed", 0.0), rec.get("matched_itc", 0.0),
            rec.get("at_risk_itc", 0.0), rec.get("blocked_credit_itc", 0.0),
            rec.get("reverse_charge_itc", 0.0), rec.get("eligible_credit", 0.0),
            rec.get("computed_at") or _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def latest_eligible_credit(
    conn: sqlite3.Connection, *, client_id: int, period: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    sql = "SELECT * FROM eligible_credit_figures WHERE client_id = ?"
    params: list[Any] = [client_id]
    if period is not None:
        sql += " AND period = ?"
        params.append(period)
    sql += " ORDER BY figure_id DESC LIMIT 1"
    return _row_to_dict(conn, sql, tuple(params))


# ---------------------------------------------------------------------------
# TDS chain stages (2B)
# ---------------------------------------------------------------------------


def insert_chain_stage(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO tds_chain_stages
            (exception_id, stage_key, stage_label, stage_state, detail, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec["exception_id"], rec["stage_key"], rec["stage_label"], rec["stage_state"],
            rec.get("detail"), rec["sort_order"], rec.get("created_at") or _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_chain_stages(conn: sqlite3.Connection, exception_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM tds_chain_stages WHERE exception_id = ? ORDER BY sort_order",
        (exception_id,),
    )


# ---------------------------------------------------------------------------
# Change log (local stub — retrofit to F4)
# ---------------------------------------------------------------------------


def log_change(
    conn: sqlite3.Connection, *, exception_id: Optional[int], entry_kind: str, action: str,
    detail: Optional[str], reason: Optional[str], actor: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO module2_change_log
            (exception_id, entry_kind, action, detail, reason, actor, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (exception_id, entry_kind, action, detail, reason, actor, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_change_log(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM module2_change_log ORDER BY id DESC LIMIT ?", (limit,)
    )