"""SQLite schema for F6 — Source File Ingestion & Format Registry.

Shares db/poc.db with src/db.py and every other module's schema.py.
init_f6_schema() is idempotent and safe to call every app start.

Design notes (F6 build prompt §3 "Data model"), mapped onto sqlite tables:

- SourceFormat -> f6_source_formats: a logical format family (gstr2b, ims,
  gstr1, purchase_register, sales_register [provisioned], tds_26as
  [provisioned]). Carries the slot it fills (portal/books) and whether it
  is firm-wide-only or may be client-scoped.

- FormatVersion -> f6_format_versions: one registered layout. Carries its
  FormatFingerprint (hash + retained plaintext, so a near-miss can be
  diffed — §4), its parse_config (JSON: sheet scope, header offsets,
  flattening rules, column mapping, aggregation rules), status
  (active/superseded/quarantined), provenance (seeded/
  ai_proposed_confirmed), scope (firm/client), confirming user + timestamp.
  Soft-delete only, non-retroactive: superseding never touches historical
  runs, which keep pointing at the version that actually parsed them
  (matched_version_id on f6_ingestion_runs is never rewritten).

- FieldMappingRule -> f6_field_mapping_rules: one rule per canonical
  field, scoped to a FormatVersion. kind in
  direct/aggregate/derived/unavailable. Carries its own confidence (only
  meaningful when AI-proposed) and human_confirmed flag.

- IngestionRun -> f6_ingestion_runs: one row per upload attempt. Records
  the path taken (A/B/C), matched version, row accounting (read/parsed/
  excluded with reasons), validation results (§8's guardrail outcomes,
  JSON), status, and the confirming operator for Path B.

- NormalizedRow: this PoC does NOT persist a full canonical-row table in
  sqlite (matching the existing pattern in src/ingestion_ai — canonical
  data is returned as a DataFrame with row-level provenance embedded
  inline as an `original_row` JSON column + `source_sheet`/`source_row`
  columns, not written row-by-row to the database). Persisting every row
  a second time here would double storage for no benefit this build; the
  provenance is on the DataFrame the caller (Module 2) receives.

- MappingProposal -> f6_mapping_proposals: the AI output for a Path B
  run, retained verbatim (proposal_json) plus what a human edited/
  rejected (edits_json) — survives promotion, never deleted.

- Confirmations/promotions/quarantines are logged through F4's Universal
  Edit & Version History (already built in this repo — see service.py),
  not a local stub table, since F4 already exists.
"""

from __future__ import annotations

import sqlite3

F6_SCHEMA = """
-- SourceFormat: a logical format family.
CREATE TABLE IF NOT EXISTS f6_source_formats (
    source_format_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT    NOT NULL UNIQUE,   -- 'gstr2b' | 'ims' | 'gstr1' | 'purchase_register' | ...
    label           TEXT    NOT NULL,
    slot            TEXT    NOT NULL,          -- 'portal' | 'books'
    scope_kind      TEXT    NOT NULL,          -- 'firm_only' | 'client_scoped' | 'either'
    provisioned_only INTEGER NOT NULL DEFAULT 0,  -- seeded but not consumed (e.g. sales_register, tds_26as)
    description     TEXT,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

-- FormatVersion: one registered layout of a SourceFormat.
CREATE TABLE IF NOT EXISTS f6_format_versions (
    version_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_format_id    INTEGER NOT NULL REFERENCES f6_source_formats(source_format_id),
    version_number      INTEGER NOT NULL,      -- 1, 2, 3... per source_format (+ per client if client-scoped)
    fingerprint_hash     TEXT    NOT NULL,
    fingerprint_plaintext TEXT   NOT NULL,      -- JSON: {"sheet_signature": [...], "column_signature": [...]}
    parse_config        TEXT    NOT NULL,      -- JSON parse config the generic parser reads
    status              TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'superseded' | 'quarantined'
    provenance          TEXT    NOT NULL,      -- 'seeded' | 'ai_proposed_confirmed'
    scope               TEXT    NOT NULL,      -- 'firm' | 'client'
    client_id           INTEGER,               -- NULL for firm-wide
    specimen_file_hash  TEXT,                  -- hash of the confirming specimen (file itself NOT retained, per DPDP decision)
    confirmed_by         TEXT,
    confirmed_at         TEXT,
    superseded_by_version_id INTEGER,          -- NULL unless a newer firm-wide version replaced this one
    quarantine_reason   TEXT,
    quarantined_by       TEXT,
    quarantined_at        TEXT,
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_f6_format_versions_format
    ON f6_format_versions(source_format_id, status);
CREATE INDEX IF NOT EXISTS idx_f6_format_versions_fingerprint
    ON f6_format_versions(fingerprint_hash);

-- FieldMappingRule: one rule per canonical field for a given FormatVersion.
CREATE TABLE IF NOT EXISTS f6_field_mapping_rules (
    rule_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES f6_format_versions(version_id),
    canonical_field TEXT    NOT NULL,
    kind            TEXT    NOT NULL,   -- 'direct' | 'aggregate' | 'derived' | 'unavailable'
    source_columns  TEXT    NOT NULL,   -- JSON list of flattened source column labels
    transform       TEXT,              -- e.g. 'flatten_two_row_header' | 'parse_date:%d/%m/%Y' | 'strip_trailing_period'
    confidence      REAL,              -- 0-100, only meaningful when AI-proposed
    human_confirmed INTEGER NOT NULL DEFAULT 0,
    rationale       TEXT
);

CREATE INDEX IF NOT EXISTS idx_f6_field_mapping_rules_version
    ON f6_field_mapping_rules(version_id);

-- IngestionRun: one row per upload attempt.
CREATE TABLE IF NOT EXISTS f6_ingestion_runs (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    period              TEXT    NOT NULL,
    slot                TEXT    NOT NULL,       -- 'portal' | 'books'
    source_format_key   TEXT,                   -- resolved SourceFormat.key, NULL if never resolved (Path C dead file)
    filename            TEXT    NOT NULL,
    file_hash           TEXT    NOT NULL,
    path_taken          TEXT    NOT NULL,       -- 'A' | 'B' | 'C'
    matched_version_id  INTEGER,                -- FK f6_format_versions, NULL on Path B/C
    row_count_read      INTEGER NOT NULL DEFAULT 0,
    row_count_parsed    INTEGER NOT NULL DEFAULT 0,
    row_count_excluded  INTEGER NOT NULL DEFAULT 0,
    exclusion_reasons   TEXT,                   -- JSON list of {reason, count}
    validation_results  TEXT,                   -- JSON list of {check, result, detail}
    recognised_not_parsed TEXT,                 -- JSON list of sheet names deferred (e.g. ISD/IMPG on 2B)
    status              TEXT    NOT NULL,       -- 'parsed' | 'blocked' | 'rejected' | 'pending_confirmation'
    rejection_reason    TEXT,
    operator            TEXT,                   -- who confirmed, on Path B
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_f6_ingestion_runs_client_period
    ON f6_ingestion_runs(client_id, period, slot);
CREATE INDEX IF NOT EXISTS idx_f6_ingestion_runs_file_hash
    ON f6_ingestion_runs(client_id, period, slot, file_hash);

-- MappingProposal: the AI output for a Path B run, retained verbatim.
CREATE TABLE IF NOT EXISTS f6_mapping_proposals (
    proposal_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES f6_ingestion_runs(run_id),
    slot            TEXT    NOT NULL,
    proposal_json   TEXT    NOT NULL,   -- verbatim model output (schema-validated before use)
    edits_json      TEXT,               -- what the human changed/rejected, with reasons
    model_used      TEXT,
    skill_version   TEXT    NOT NULL,   -- versioned instruction-set id, for replay
    closest_version_id INTEGER,         -- FK f6_format_versions, NULL if no similar version existed
    decision        TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'confirmed' | 'rejected'
    decided_by       TEXT,
    decided_at        TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_f6_mapping_proposals_run
    ON f6_mapping_proposals(run_id);
"""


def init_f6_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(F6_SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """PRAGMA-based column adds for pre-existing DBs. No columns added yet
    beyond the initial CREATE TABLE — kept as a hook for later additions,
    per the repo's established migration pattern (src/db.py)."""
    return
