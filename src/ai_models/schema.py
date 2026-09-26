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

-- ---------------------------------------------------------------------------
-- Multi-provider extension (C3-ext v2). Fixed platform-defined provider set —
-- Admin selects among these; registering an arbitrary fifth provider is
-- explicitly deferred.
-- ---------------------------------------------------------------------------

-- ONE row per platform-defined provider. The transport (URL / auth style /
-- catalogue mode) lives here and in src/ai_models/providers.py — never
-- hardcoded in a calling module.
CREATE TABLE IF NOT EXISTS ai_providers (
    provider_key    TEXT    PRIMARY KEY,   -- openrouter | anthropic | deepseek | sarvam
    label           TEXT    NOT NULL,
    kind            TEXT    NOT NULL,      -- openai_compatible | anthropic | sarvam
    chat_url        TEXT    NOT NULL,
    catalog_url     TEXT,                  -- NULL when the provider has no catalogue endpoint
    catalog_mode    TEXT    NOT NULL,      -- 'live' | 'static'
    auth_style      TEXT    NOT NULL,      -- 'bearer' | 'x-api-key'
    static_models   TEXT,                  -- JSON list, used when catalog_mode='static'
    is_enabled      INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

-- ONE credential binding per provider. The real secret lives in C4's vault;
-- this table only binds a provider to a vault credential_id and caches the
-- live connection status. Never stores a secret.
CREATE TABLE IF NOT EXISTS ai_provider_credentials (
    provider_key     TEXT    PRIMARY KEY,
    credential_id    INTEGER,
    status           TEXT    NOT NULL DEFAULT 'unconfigured',  -- live|invalid|unreachable|unconfigured
    status_detail    TEXT,
    last_checked_at  TEXT,
    updated_at       TEXT,
    updated_by       TEXT
);

-- Per-provider model catalogue cache. Live where the provider exposes a
-- catalogue endpoint; the seeded static list where it does not. The registry
-- degrades to this cache (never an empty dropdown) when a live pull fails.
CREATE TABLE IF NOT EXISTS ai_provider_models (
    provider_key      TEXT    NOT NULL,
    model_id          TEXT    NOT NULL,
    name              TEXT,
    context_length    INTEGER,
    prompt_price      REAL,
    completion_price  REAL,
    modality          TEXT,
    source            TEXT    NOT NULL DEFAULT 'live',  -- live|static
    fetched_at        TEXT,
    PRIMARY KEY (provider_key, model_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_provider_models_provider
    ON ai_provider_models(provider_key);

-- The source for the "Recent Fallbacks" panel. Records WHICH provider the
-- fallback actually resolved to, not merely that one occurred. Also emitted
-- (best-effort) to C4's vault_security_events.
CREATE TABLE IF NOT EXISTS ai_fallback_events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    touchpoint_key    TEXT    NOT NULL,
    leg               TEXT    NOT NULL,   -- the leg attempted first: primary|fallback
    primary_provider  TEXT,
    primary_model     TEXT,
    resolved_provider TEXT    NOT NULL,
    resolved_model    TEXT    NOT NULL,
    reason            TEXT,
    latency_ms        INTEGER,
    actor             TEXT,
    client_id         INTEGER,
    created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_fallback_events_created
    ON ai_fallback_events(created_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for pre-existing databases.

    ``CREATE TABLE IF NOT EXISTS`` never adds a column to an existing table,
    so the multi-provider columns on ``ai_model_assignments`` must be added
    via PRAGMA + ALTER (the established repo pattern). Existing rows default
    to OpenRouter — the only provider that existed before this extension.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_model_assignments)")}
    if "primary_provider" not in cols:
        conn.execute("ALTER TABLE ai_model_assignments ADD COLUMN primary_provider TEXT")
    if "fallback_provider" not in cols:
        conn.execute("ALTER TABLE ai_model_assignments ADD COLUMN fallback_provider TEXT")
    conn.execute(
        "UPDATE ai_model_assignments SET primary_provider = 'openrouter' "
        "WHERE primary_provider IS NULL"
    )
    conn.execute(
        "UPDATE ai_model_assignments SET fallback_provider = 'openrouter' "
        "WHERE fallback_provider IS NULL"
    )
    conn.commit()


def init_ai_models_schema(conn: sqlite3.Connection) -> None:
    """Create the AI model registry tables if missing. Idempotent."""
    conn.executescript(AI_MODELS_SCHEMA)
    _migrate(conn)
    conn.commit()
