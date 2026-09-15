"""SQLite schema for F3-B — Invoice Extraction & Digitalization.

Shares db/poc.db with every other module (same pattern: separate CREATE
TABLE statements in one file). init_invoice_extract_schema() is idempotent
and safe to call every app start.

Design notes (see the F3-B build prompt):
- InvoiceUpload is the module's own record. It carries an OPTIONAL
  document_id FK into F3 (src.documents) — provisioned structurally for
  the deferred "file each Confirmed upload as an F3 Document with an
  EvidenceLink" retrofit, but intentionally left NULL/unwired in this
  build (the module is standalone).
- ExtractedInvoiceField is one row per field per upload — the per-field
  confidence convention mirrors F3-AI exactly (never a single
  document-level score).
- InvoiceReviewQueueEntry is this module's OWN queue, NOT F3's Unified
  Review Queue (per the standalone-module scope decision). It holds every
  upload with at least one sub-threshold field, tagged with which
  field(s) need attention.
- TallyExportBatch is immutable once generated: re-exporting produces a
  NEW batch row, never an overwrite. There is deliberately no UPDATE path
  for a generated batch anywhere in db.py/service.py.
- invoice_extract_change_log is a LOCAL stub that retrofits to F4 later
  (same pattern every other module in this build order uses).
"""

from __future__ import annotations

import sqlite3

INVOICE_EXTRACT_SCHEMA = """
-- One row per uploaded invoice file. Standalone: document_id is an
-- optional, currently-unwired structural link into F3.
CREATE TABLE IF NOT EXISTS invoice_uploads (
    upload_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER,            -- optional F3 link (deferred retrofit; NULL this build)
    client_id       INTEGER NOT NULL,
    filename        TEXT    NOT NULL,
    file_ext        TEXT    NOT NULL,
    file_bytes      BLOB,               -- stored so the review screen can show a genuine source preview
    -- 'image' | 'pdf' | 'excel' | 'word'  (the four accepted source formats)
    source_format   TEXT    NOT NULL,
    -- 'image' | 'structured'  (which C3-ext touchpoint routed this upload)
    extraction_path TEXT    NOT NULL,
    batch_id        TEXT,               -- optional grouping id for one export run
    -- 'extracted' | 'needs_review' | 'confirmed' | 'exported'
    status          TEXT    NOT NULL DEFAULT 'extracted',
    duplicate_warning INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    confirmed_at    TEXT,
    confirmed_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_invoice_uploads_client ON invoice_uploads(client_id, status);
CREATE INDEX IF NOT EXISTS idx_invoice_uploads_batch ON invoice_uploads(batch_id);

-- One row per canonical field per upload. confidence is 0-100, or NULL
-- when the field is genuinely absent from the source ("not present",
-- distinct from a low-confidence extraction — never invented).
CREATE TABLE IF NOT EXISTS extracted_invoice_fields (
    field_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL REFERENCES invoice_uploads(upload_id),
    field_name      TEXT    NOT NULL,   -- canonical field key (see seed.CANONICAL_FIELDS)
    extracted_value TEXT,               -- AI-extracted value (NULL when not present)
    confidence      INTEGER,            -- 0-100, or NULL when not present
    source_location TEXT,               -- page / cell / row reference where extractable
    is_present      INTEGER NOT NULL DEFAULT 1,  -- 0 = genuinely absent from the source
    resolved        INTEGER NOT NULL DEFAULT 0,  -- reviewer has explicitly resolved this field
    resolved_value  TEXT,               -- reviewer's confirmed value (may differ from extracted)
    resolved_by     TEXT,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_extracted_fields_upload ON extracted_invoice_fields(upload_id);

-- This module's OWN review queue (NOT F3's Unified Review Queue).
CREATE TABLE IF NOT EXISTS invoice_review_queue_entries (
    entry_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL REFERENCES invoice_uploads(upload_id),
    flagged_fields  TEXT    NOT NULL,   -- JSON list of field names needing attention
    status          TEXT    NOT NULL DEFAULT 'open',  -- 'open' | 'resolved'
    created_at      TEXT    NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_invoice_queue_status ON invoice_review_queue_entries(status);

-- Immutable once generated. No UPDATE path exists anywhere.
CREATE TABLE IF NOT EXISTS tally_export_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT    NOT NULL,
    upload_ids      TEXT    NOT NULL,   -- JSON list of upload_ids included
    row_count       INTEGER NOT NULL,
    row_range       TEXT    NOT NULL,   -- e.g. "1-12"
    format_version  TEXT    NOT NULL,
    generated_by    TEXT    NOT NULL,
    generated_at    TEXT    NOT NULL
);

-- The two new AITouchpoint rows this build adds to C3-ext's
-- ModelAssignment table. C3-ext is not built in this repo yet, so this
-- module carries the rows locally (seeded ACTIVE, unlike C3-ext's own
-- greyed placeholder rows for unbuilt touchpoints) — the structural
-- provision that moves to C3-ext's table unchanged once it ships.
CREATE TABLE IF NOT EXISTS invoice_ai_touchpoints (
    touchpoint_key  TEXT    PRIMARY KEY,   -- invoice_extraction_visual | invoice_extraction_structured
    label           TEXT    NOT NULL,
    primary_model   TEXT    NOT NULL,
    primary_provider TEXT   NOT NULL,
    fallback_model  TEXT    NOT NULL,
    fallback_provider TEXT  NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

-- LOCAL stub — retrofits to F4 later (export history + review-edit actions).
CREATE TABLE IF NOT EXISTS invoice_extract_change_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER,
    action          TEXT    NOT NULL,   -- 'field_resolved' | 'export_generated' | 'upload_discarded'
    detail          TEXT,
    actor           TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);
"""


def init_invoice_extract_schema(conn: sqlite3.Connection) -> None:
    """Create F3-B tables if missing. Idempotent."""
    conn.executescript(INVOICE_EXTRACT_SCHEMA)
    conn.commit()
