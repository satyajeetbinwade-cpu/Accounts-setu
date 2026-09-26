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
    primary_provider: Optional[str] = "openrouter",
    fallback_provider: Optional[str] = "openrouter",
) -> None:
    """Insert a touchpoint, or update its descriptive fields.

    Deliberately does NOT overwrite ``primary_model``/``fallback_model`` (or
    the provider legs, or ``is_active``) on an existing row: those are admin
    decisions, and a re-seed must never silently revert a model an admin chose
    or an activation an admin toggled (same additive-upsert rule every other
    seed in this build order follows). Model/provider changes go through
    ``set_assignment_legs()``; activation goes through ``set_assignment_active``.
    """
    conn.execute(
        """
        INSERT INTO ai_model_assignments
            (touchpoint_key, label, category, description, primary_model, fallback_model,
             primary_provider, fallback_provider, is_active, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(touchpoint_key) DO UPDATE SET
            label = excluded.label,
            category = excluded.category,
            description = excluded.description,
            sort_order = excluded.sort_order
        """,
        (
            touchpoint_key, label, category, description, primary_model, fallback_model,
            primary_provider, fallback_provider, 1 if is_active else 0, sort_order,
        ),
    )
    conn.commit()


def set_assignment_legs(
    conn: sqlite3.Connection,
    touchpoint_key: str,
    *,
    primary_provider: Optional[str],
    primary_model: Optional[str],
    fallback_provider: Optional[str],
    fallback_model: Optional[str],
    actor: str,
) -> None:
    """Set both legs (provider + model) of a touchpoint."""
    conn.execute(
        """
        UPDATE ai_model_assignments
           SET primary_provider = ?, primary_model = ?,
               fallback_provider = ?, fallback_model = ?,
               updated_at = ?, updated_by = ?
         WHERE touchpoint_key = ?
        """,
        (
            primary_provider, primary_model, fallback_provider, fallback_model,
            _now(), actor, touchpoint_key,
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
# Change log — RETROFITTED to F4 (the local table is retained but no longer
# written; F4 is the real, immutable Edit History sink).
# ---------------------------------------------------------------------------


def log_change(
    conn: sqlite3.Connection, *, touchpoint_key: Optional[str], action: str, detail: str, actor: str,
) -> None:
    """Record a model-routing change as a REAL F4 Edit History entry.

    The signature is kept identical so every existing call site is
    unchanged; the local ``ai_model_change_log`` table is retained (empty)
    for backward compatibility but is no longer written. F4's record_type is
    ``ai_model_assignment`` and record_id is the touchpoint key, so one View
    History trigger per touchpoint shows its full routing history.
    """
    from src.f4 import service as f4  # local import avoids a hard circular dep

    f4.record_edit(
        record_type="ai_model_assignment",
        record_id=touchpoint_key or "catalog",
        field=action,
        old_value=None,
        new_value=detail,
        reason=None,
        actor=actor,
    )


def list_change_log(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM ai_model_change_log ORDER BY log_id DESC LIMIT ?",
        (int(limit),),
    )


# ---------------------------------------------------------------------------
# Providers (the fixed platform-defined set)
# ---------------------------------------------------------------------------


def upsert_provider(
    conn: sqlite3.Connection,
    *,
    provider_key: str,
    label: str,
    kind: str,
    chat_url: str,
    catalog_url: Optional[str],
    catalog_mode: str,
    auth_style: str,
    static_models: Optional[str],
    is_enabled: int = 1,
    sort_order: int = 0,
) -> None:
    """Insert a provider, or refresh its transport description.

    Never overwrites ``is_enabled`` (an admin toggle) or ``static_models`` (an
    admin-maintained list) on an existing row — a re-seed must not undo either.
    """
    conn.execute(
        """
        INSERT INTO ai_providers
            (provider_key, label, kind, chat_url, catalog_url, catalog_mode,
             auth_style, static_models, is_enabled, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider_key) DO UPDATE SET
            label = excluded.label,
            kind = excluded.kind,
            chat_url = excluded.chat_url,
            catalog_url = excluded.catalog_url,
            catalog_mode = excluded.catalog_mode,
            auth_style = excluded.auth_style,
            sort_order = excluded.sort_order
        """,
        (
            provider_key, label, kind, chat_url, catalog_url, catalog_mode,
            auth_style, static_models, 1 if is_enabled else 0, sort_order,
        ),
    )
    conn.commit()


def list_providers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM ai_providers ORDER BY sort_order, provider_key")


def get_provider(conn: sqlite3.Connection, provider_key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn, "SELECT * FROM ai_providers WHERE provider_key = ?", (provider_key,)
    )


# ---------------------------------------------------------------------------
# Provider credentials (a binding to a C4 vault credential id — never a secret)
# ---------------------------------------------------------------------------


def get_provider_credential(conn: sqlite3.Connection, provider_key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn, "SELECT * FROM ai_provider_credentials WHERE provider_key = ?", (provider_key,)
    )


def set_provider_credential(
    conn: sqlite3.Connection,
    provider_key: str,
    *,
    credential_id: Optional[int],
    status: str,
    status_detail: Optional[str],
    updated_by: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ai_provider_credentials
            (provider_key, credential_id, status, status_detail, last_checked_at, updated_at, updated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider_key) DO UPDATE SET
            credential_id = excluded.credential_id,
            status = excluded.status,
            status_detail = excluded.status_detail,
            last_checked_at = excluded.last_checked_at,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (provider_key, credential_id, status, status_detail, _now(), _now(), updated_by),
    )
    conn.commit()


def set_provider_status(
    conn: sqlite3.Connection, provider_key: str, *, status: str, status_detail: Optional[str],
) -> None:
    conn.execute(
        "UPDATE ai_provider_credentials SET status = ?, status_detail = ?, last_checked_at = ? "
        "WHERE provider_key = ?",
        (status, status_detail, _now(), provider_key),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Provider model catalogue cache
# ---------------------------------------------------------------------------


def list_provider_models(conn: sqlite3.Connection, provider_key: str) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM ai_provider_models WHERE provider_key = ? ORDER BY name COLLATE NOCASE",
        (provider_key,),
    )


def replace_provider_models(
    conn: sqlite3.Connection, provider_key: str, models: list[dict[str, Any]], *, source: str,
) -> None:
    """Replace one provider's cached catalogue wholesale (retired ids must not
    linger — the cache is a mirror, not a history)."""
    fetched_at = _now()
    conn.execute("DELETE FROM ai_provider_models WHERE provider_key = ?", (provider_key,))
    conn.executemany(
        """
        INSERT INTO ai_provider_models
            (provider_key, model_id, name, context_length, prompt_price, completion_price,
             modality, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                provider_key, m["model_id"], m.get("name"), m.get("context_length"),
                m.get("prompt_price"), m.get("completion_price"), m.get("modality"),
                source, fetched_at,
            )
            for m in models
        ],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fallback events (the source for the Recent Fallbacks panel)
# ---------------------------------------------------------------------------


def insert_fallback_event(
    conn: sqlite3.Connection,
    *,
    touchpoint_key: str,
    leg: str,
    primary_provider: Optional[str],
    primary_model: Optional[str],
    resolved_provider: str,
    resolved_model: str,
    reason: Optional[str],
    latency_ms: Optional[int],
    actor: Optional[str],
    client_id: Optional[int],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ai_fallback_events
            (touchpoint_key, leg, primary_provider, primary_model, resolved_provider,
             resolved_model, reason, latency_ms, actor, client_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            touchpoint_key, leg, primary_provider, primary_model, resolved_provider,
            resolved_model, reason, latency_ms, actor, client_id, _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_fallback_events(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM ai_fallback_events ORDER BY event_id DESC LIMIT ?",
        (int(limit),),
    )
