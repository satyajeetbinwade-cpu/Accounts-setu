"""SQLite schema for the central AI model registry.

Shares db/poc.db with every other module (same pattern: separate CREATE
TABLE statements in one file). init_ai_models_schema() is idempotent and
safe to call every app start.

Why this module exists
----------------------
Before this, the model choice was scattered across three unrelated places:

  * ``config/ai_config.yaml`` (``models.complex`` / ``models.simple``) —
    read by src/ai_analysis.py, editable only by editing the file.
  * a C3 setting (``ingestion_ai.llm.model``) — editable from Settings →
    AI Ingestion, but only for the ingestion layer.
  * ``invoice_ai_touchpoints`` — F3-B's own local table, holding
    human-readable labels like "Claude Sonnet (vision)" rather than real
    OpenRouter model ids.

There was no single place to see or change "which model does this module
use", and no way to pick from the actual OpenRouter catalogue. This module
is that single place.

Tables
------
``ai_model_assignments``
    ONE row per AI touchpoint across every module — the single source of
    truth for "which model does this touchpoint use". ``primary_model`` and
    ``fallback_model`` hold real OpenRouter model ids.

``ai_model_catalog``
    A local cache of OpenRouter's public model list (``/api/v1/models``).
    Cached rather than fetched per render because the list is ~450 models
    and the endpoint is a third-party network call; refreshed explicitly
    from the UI. Never authoritative — the registry works without it.

``ai_model_change_log``
    LOCAL stub, retrofits to F4 later (same pattern every other module in
    this build order uses).
"""

from __future__ import annotations

import sqlite3

AI_MODELS_SCHEMA = """
-- ONE row per AI touchpoint, across every module. This is the single
-- source of truth for model assignment; module-specific tables (e.g.
-- F3-B's invoice_ai_touchpoints) mirror it rather than compete with it.
CREATE TABLE IF NOT EXISTS ai_model_assignments (
    touchpoint_key  TEXT    PRIMARY KEY,
    label           TEXT    NOT NULL,
    category        TEXT    NOT NULL,   -- 'Ingestion' | 'Analysis' | 'Extraction'
    description     TEXT,
    primary_model   TEXT,               -- real OpenRouter model id
    fallback_model  TEXT,               -- real OpenRouter model id
    is_active       INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT,
    updated_by      TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_model_assignments_category
    ON ai_model_assignments(category, sort_order);

-- Local cache of OpenRouter's public model catalogue. Refreshed explicitly
-- from the UI; the registry degrades to the bundled fallback list when this
-- is empty or stale.
CREATE TABLE IF NOT EXISTS ai_model_catalog (
    model_id        TEXT    PRIMARY KEY,
    name            TEXT,
    context_length  INTEGER,
    prompt_price    REAL,               -- USD per 1M input tokens
    completion_price REAL,              -- USD per 1M output tokens
    modality        TEXT,               -- e.g. 'text->text', 'text+image->text'
    fetched_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_model_catalog_name ON ai_model_catalog(name);

-- LOCAL stub — retrofits to F4 later.
CREATE TABLE IF NOT EXISTS ai_model_change_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    touchpoint_key  TEXT,
    action          TEXT    NOT NULL,
    detail          TEXT,
    actor           TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);
"""


def init_ai_models_schema(conn: sqlite3.Connection) -> None:
    """Create the AI model registry tables if missing. Idempotent."""
    conn.executescript(AI_MODELS_SCHEMA)
    conn.commit()
