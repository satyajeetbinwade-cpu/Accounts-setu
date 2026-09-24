"""SQLite schema for the F3-AI Smart Document Ingestion module.

Shares db/poc.db with every other module (same pattern: separate
CREATE TABLE statements in one file). init_ingestion_ai_schema() is
idempotent and safe to call every app start.

Design notes (see the F3-AI build prompt):
- RawUpload is 1:1 with the F3 document it files into (document_id FK) —
  F3-AI doesn't keep a second document store; the raw-upload row exists
  only to carry mapping-specific state (source_type, status, the
  confirmed mapping) alongside the F3 document row.
- ColumnMappingProfile is keyed PER-CLIENT AND PER-SOURCE_TYPE on purpose
  (not a single global profile per source_type), even though in practice
  most clients share the same export shape for a given source_type —
  per the build prompt's explicit "build the schema with this pairing as
  the real key regardless" instruction.
- MappingConfidenceReport stores the full per-field inference result
  (mapped raw column, confidence, status, ambiguous candidates) as JSON,
  one row per upload — this is the audit trail the mapping-review screen
  reads and the human edits.
- No second Unified Review Queue is created here — a blocked upload adds
  a row to F3's existing `unified_review_queue_entries` table (reason
  "couldn't map"), reusing document_id as the link, per the explicit
  "do not build two separate queues" instruction.
"""

from __future__ import annotations

import sqlite3

INGESTION_AI_SCHEMA = """
-- One row per uploaded raw source file, 1:1 with its F3 document.
CREATE TABLE IF NOT EXISTS raw_uploads (
    upload_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents(document_id),
    client_id       INTEGER NOT NULL,
    source_type     TEXT    NOT NULL,   -- tally | gstr2b | ims | form26as | tds
    filename        TEXT    NOT NULL,
    -- 'pending' | 'blocked' | 'needs_confirm' | 'confirmed' | 'unrecognized'
    status          TEXT    NOT NULL DEFAULT 'pending',
    confirmed_mapping TEXT,             -- JSON canonical_field -> raw_column|null, set on confirm
    trusted_on_confirm INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    confirmed_at    TEXT,
    confirmed_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_uploads_document ON raw_uploads(document_id);
CREATE INDEX IF NOT EXISTS idx_raw_uploads_client ON raw_uploads(client_id, source_type);

-- Per-file, per-field inference detail. One row per raw_upload.
CREATE TABLE IF NOT EXISTS mapping_confidence_reports (
    report_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL REFERENCES raw_uploads(upload_id),
    field_mapping   TEXT    NOT NULL,   -- JSON list of per-canonical-field inference results
    c5_context_used INTEGER NOT NULL DEFAULT 0,
    routing_stub    INTEGER NOT NULL DEFAULT 1,  -- C3-ext not built yet in this repo — always 1 for now
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mapping_reports_upload ON mapping_confidence_reports(upload_id);

-- Confirmed, reusable mapping shape per client + source_type. A profile is
-- proposed (trusted=0) the first time a shape is confirmed, and only
-- becomes trusted (auto-proposed and eligible for silent reuse) via an
-- EXPLICIT "trust for future reuse" action on confirm — never implied.
CREATE TABLE IF NOT EXISTS column_mapping_profiles (
    profile_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL,
    source_type     TEXT    NOT NULL,
    column_map      TEXT    NOT NULL,   -- JSON canonical_field -> raw_column|null
    trusted         INTEGER NOT NULL DEFAULT 0,
    last_used_at    TEXT,
    created_by      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mapping_profiles_client_source
    ON column_mapping_profiles(client_id, source_type);

-- Unified AI ingestion layer: cached/learned mappings keyed by the SHAPE of
-- the file (client + source_type + header signature), not merely by client +
-- source_type. Two jobs share one row:
--   1. LLM response cache — a file shape seen before does not re-call the
--      model (the build prompt's explicit caching requirement).
--   2. Trusted-profile reuse — a mapping a human confirmed and trusted is
--      re-proposed on the next file of the SAME shape, skipping both the
--      model call and the manual step.
-- Keying on the header signature (not just client+source_type) is
-- deliberate: Tally/SUVIT/GSTN exports differ client to client AND can
-- change shape month to month for the same client, so a mapping learned for
-- one shape must never be silently applied to a differently-shaped file.
CREATE TABLE IF NOT EXISTS ingestion_shape_cache (
    shape_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER,                    -- F2 client_id; NULL = client unknown (folder-only)
    client_ref          TEXT    NOT NULL,           -- sidebar client folder/name the upload came from
    source_type         TEXT    NOT NULL,
    header_signature    TEXT    NOT NULL,           -- stable hash of the detected header set
    headers_json        TEXT    NOT NULL,           -- JSON list of detected raw headers
    mapping_json        TEXT    NOT NULL,           -- JSON canonical_field -> {raw_column, confidence, reason}
    classification_json TEXT,                       -- JSON {document_type, matches_declared, message}
    model_used          TEXT,
    confidence_floor    REAL,                       -- lowest confidence in the stored mapping
    trusted             INTEGER NOT NULL DEFAULT 0, -- human-trusted for reuse on this exact shape
    source              TEXT    NOT NULL DEFAULT 'ai',  -- 'ai' (model) | 'trusted_profile'
    times_used          INTEGER NOT NULL DEFAULT 0,
    created_by          TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    last_used_at        TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_shape_cache_lookup
    ON ingestion_shape_cache(client_ref, source_type, header_signature);

-- Per-upload record of what the unified ingestion layer produced, so the
-- Run screen and the Smart Ingestion screen read the SAME result object
-- rather than each re-deriving it.
CREATE TABLE IF NOT EXISTS ingestion_results (
    result_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_key          TEXT    NOT NULL,           -- "<client>/<period>/<source_type>/<filename>"
    client_ref          TEXT    NOT NULL,
    client_id           INTEGER,
    period              TEXT,
    source_type         TEXT    NOT NULL,
    filename            TEXT    NOT NULL,
    status              TEXT    NOT NULL,           -- ok | partial | blocked | unrecognized
    header_row          INTEGER,
    sheet_name          TEXT,
    sheet_ambiguous     INTEGER NOT NULL DEFAULT 0,
    headers_json        TEXT    NOT NULL,
    mapping_json        TEXT    NOT NULL,
    classification_json TEXT,
    unmapped_required_json TEXT NOT NULL,
    warnings_json       TEXT    NOT NULL,
    notes_json          TEXT    NOT NULL DEFAULT '[]',
    message             TEXT,           -- plain-language block reason (wrong slot / unrecognized)
    row_count_in        INTEGER NOT NULL DEFAULT 0,
    row_count_out       INTEGER NOT NULL DEFAULT 0,
    model_used          TEXT,
    llm_cached          INTEGER NOT NULL DEFAULT 0,
    -- §8 guardrail outcomes for this file, as JSON [{check,result,detail,
    -- affected_rows}]. Persisted so the confirm gate and the review screen
    -- read the SAME verdict the pipeline enforced.
    validation_json     TEXT    NOT NULL DEFAULT '[]',
    -- Metadata captured from the rows above the header (entity, report
    -- title, period, filter text, date-format evidence).
    metadata_json       TEXT    NOT NULL DEFAULT '{}',
    -- The head x rate matrix + per-column dispositions, for the UI cards.
    rate_matrix_json    TEXT    NOT NULL DEFAULT '[]',
    dispositions_json   TEXT    NOT NULL DEFAULT '[]',
    -- 'deterministic' | 'ai' | 'cache' — how this result was produced.
    ingestion_path      TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    created_by          TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_results_upload ON ingestion_results(upload_key);
"""


def init_ingestion_ai_schema(conn: sqlite3.Connection) -> None:
    """Create F3-AI tables if missing. Idempotent. Must run AFTER F3's
    init_documents_schema() since raw_uploads references documents."""
    conn.executescript(INGESTION_AI_SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for DBs created before a column existed.
    CREATE TABLE IF NOT EXISTS won't add a column to an existing table, so
    new columns are added here explicitly."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ingestion_results)")}
    if existing and "notes_json" not in existing:
        conn.execute("ALTER TABLE ingestion_results ADD COLUMN notes_json TEXT NOT NULL DEFAULT '[]'")
    if existing and "message" not in existing:
        conn.execute("ALTER TABLE ingestion_results ADD COLUMN message TEXT")
    # §8 / §5 / §7 additions — additive, so pre-existing DBs gain the columns
    # without a rebuild (CREATE TABLE IF NOT EXISTS will not add them).
    for column, ddl in (
        ("validation_json", "ALTER TABLE ingestion_results ADD COLUMN validation_json TEXT NOT NULL DEFAULT '[]'"),
        ("metadata_json", "ALTER TABLE ingestion_results ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"),
        ("rate_matrix_json", "ALTER TABLE ingestion_results ADD COLUMN rate_matrix_json TEXT NOT NULL DEFAULT '[]'"),
        ("dispositions_json", "ALTER TABLE ingestion_results ADD COLUMN dispositions_json TEXT NOT NULL DEFAULT '[]'"),
        ("ingestion_path", "ALTER TABLE ingestion_results ADD COLUMN ingestion_path TEXT NOT NULL DEFAULT ''"),
    ):
        if existing and column not in existing:
            conn.execute(ddl)
