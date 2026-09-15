"""Low-level CRUD for the central AI model registry.

Mirrors src/invoice_extract/db.py and src/ingestion_ai/db.py: functions
take a connection, do one thing, and commit individually.

src/ai_models/service.py is the only module that should import this one
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
# Assignments
# ---------------------------------------------------------------------------


def list_assignments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM ai_model_assignments ORDER BY category, sort_order, touchpoint_key"
    )


def get_assignment(conn: sqlite3.Connection, touchpoint_key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn, "SELECT * FROM ai_model_assignments WHERE touchpoint_key = ?", (touchpoint_key,)
    )


def upsert_assignment(
    conn: sqlite3.Connection,
    *,
    touchpoint_key: str,
    label: str,
    category: str,
    description: Optional[str],
    primary_model: Optional[str],
    fallback_model: Optional[str],
    is_active: bool,
    sort_order: int,
) -> None:
    """Insert a touchpoint, or update its descriptive fields.

    Deliberately does NOT overwrite ``primary_model``/``fallback_model`` on
    an existing row: those are admin decisions, and a re-seed must never
    silently revert a model an admin chose (same additive-upsert rule every
    other seed in this build order follows). Model changes go through
    ``set_assignment_models()``.
    """
    conn.execute(
        """
        INSERT INTO ai_model_assignments
            (touchpoint_key, label, category, description, primary_model, fallback_model,
             is_active, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(touchpoint_key) DO UPDATE SET
            label = excluded.label,
            category = excluded.category,
            description = excluded.description,
            is_active = excluded.is_active,
            sort_order = excluded.sort_order
        """,
        (
            touchpoint_key, label, category, description, primary_model, fallback_model,
            1 if is_active else 0, sort_order,
        ),
    )
    conn.commit()


def set_assignment_models(
    conn: sqlite3.Connection,
    touchpoint_key: str,
    *,
    primary_model: Optional[str],
    fallback_model: Optional[str],
    actor: str,
) -> None:
    conn.execute(
        """
        UPDATE ai_model_assignments
           SET primary_model = ?, fallback_model = ?, updated_at = ?, updated_by = ?
         WHERE touchpoint_key = ?
        """,
        (primary_model, fallback_model, _now(), actor, touchpoint_key),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Catalogue cache
# ---------------------------------------------------------------------------


def replace_catalog(conn: sqlite3.Connection, models: list[dict[str, Any]]) -> None:
    """Replace the cached catalogue wholesale.

    A full replace (not an upsert) is correct here: OpenRouter retires
    models, and a stale row would keep offering a model id that no longer
    resolves. The cache is a mirror, not a history.
    """
    fetched_at = _now()
    conn.execute("DELETE FROM ai_model_catalog")
    conn.executemany(
        """
        INSERT INTO ai_model_catalog
            (model_id, name, context_length, prompt_price, completion_price, modality, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                m["model_id"], m.get("name"), m.get("context_length"),
                m.get("prompt_price"), m.get("completion_price"), m.get("modality"), fetched_at,
            )
            for m in models
        ],
    )
    conn.commit()


def list_catalog(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM ai_model_catalog ORDER BY name COLLATE NOCASE"
    )


def catalog_fetched_at(conn: sqlite3.Connection) -> Optional[str]:
    row = _row_to_dict(conn, "SELECT MAX(fetched_at) AS fetched_at FROM ai_model_catalog")
    return (row or {}).get("fetched_at")


# ---------------------------------------------------------------------------
# Change log — LOCAL stub, retrofit to F4 later
# ---------------------------------------------------------------------------


def log_change(
    conn: sqlite3.Connection, *, touchpoint_key: Optional[str], action: str, detail: str, actor: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ai_model_change_log (touchpoint_key, action, detail, actor, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (touchpoint_key, action, detail, actor, _now()),
    )
    conn.commit()


def list_change_log(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM ai_model_change_log ORDER BY log_id DESC LIMIT ?",
        (int(limit),),
    )
