"""SQLite schema for the F3 Document & Data Repository module.

Shares db/poc.db with every other module (same pattern: separate
CREATE TABLE statements in one file). init_documents_schema() is
idempotent and safe to call every app start.

Design notes (see F3 build prompt):
- Document / DocumentVersion is file-level supersession history, distinct
  from F4's eventual field-level Edit History (which doesn't exist yet).
  A Document row always points at its current version; every prior
  upload for the same document stays in document_versions, newest first.
- EvidenceLink is bidirectionally inspectable and deliberately generic/
  polymorphic (`target_type` + `target_id` + a denormalized display
  label) even though most targets (Reconciliation Exception, Audit
  Query, Corrective Entry) don't exist as real tables yet \u2014 Modules
  2, 5 and 3 respectively. Nothing here assumes those tables exist.
- unified_review_queue_entries is ONE shared queue, also used by F3-AI
  (built next) for its "couldn't map" tag \u2014 `reason` is a free-text/
  open-ended column, not a hardcoded enum, per the build prompt's
  explicit "do not build two separate queues" instruction.
- Retention is INFINITE \u2014 no purge job anywhere in this schema, ever
  (firmer than C4's general DPDP-driven default; documented exception).
- Soft-delete only: `is_deleted` + `deleted_at` on documents; evidence
  links are never removed when a document is soft-deleted, so they keep
  resolving (the acceptance-criteria requirement) both before and after
  a restore.
- document_edit_log is a local stub for the reassignment reason-capture
  requirement (reuses F2's reason-capture pattern) \u2014 retrofit to F4's
  Universal Edit & Version History once F4 exists, same pattern as
  src/clients/schema.py's client_edit_log.
"""

from __future__ import annotations

import sqlite3

DOCUMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id              INTEGER NOT NULL,      -- REFERENCES end_clients(client_id)
    doc_type               TEXT    NOT NULL,       -- e.g. GSTR-2B, Form 26AS, Tally Export, Other
    period                 TEXT,                    -- free-text period label, e.g. "2026-08"
    current_version_id     INTEGER,                 -- REFERENCES document_versions(version_id)
    classification_source  TEXT    NOT NULL DEFAULT 'ai',   -- 'rule' | 'ai' \u2014 confidence-badge form
    classification_pct     INTEGER,                 -- AI confidence percent, nullable for rule-match
    review_status          TEXT    NOT NULL DEFAULT 'filed', -- 'filed' | 'pending_review'
    is_deleted             INTEGER NOT NULL DEFAULT 0,        -- soft-delete only, always recoverable
    deleted_at             TEXT,
    deleted_by             TEXT,
    created_at             TEXT    NOT NULL,
    created_by             TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_client ON documents(client_id);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_period ON documents(period);

-- File-level supersession history. Distinct from F4's eventual
-- field-level Edit History.
CREATE TABLE IF NOT EXISTS document_versions (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES documents(document_id),
    version_number  INTEGER NOT NULL,
    filename        TEXT    NOT NULL,
    file_ext        TEXT    NOT NULL,   -- validated at intake against the allowed-format list
    file_size       INTEGER NOT NULL,
    file_bytes      BLOB    NOT NULL,
    uploaded_by     TEXT    NOT NULL,
    uploaded_at     TEXT    NOT NULL,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id);

-- Bidirectionally inspectable, generic/polymorphic evidence linkage.
-- target_type is one of 'reconciliation_exception' | 'audit_query' |
-- 'corrective_entry' (Modules 2, 5, 3 respectively) \u2014 all structurally
-- real now, all empty until those modules exist.
CREATE TABLE IF NOT EXISTS evidence_links (
    link_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents(document_id),
    target_type   TEXT    NOT NULL,
    target_id     INTEGER,
    target_label  TEXT    NOT NULL,   -- denormalized display label for the "Used in" panel
    created_at    TEXT    NOT NULL,
    created_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_evidence_document ON evidence_links(document_id);
CREATE INDEX IF NOT EXISTS idx_evidence_target ON evidence_links(target_type, target_id);

-- ONE shared queue (also used by F3-AI's future "couldn't map" tag) \u2014
-- reason is open-ended text, never hardcoded to a single tag.
CREATE TABLE IF NOT EXISTS unified_review_queue_entries (
    entry_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        INTEGER NOT NULL REFERENCES documents(document_id),
    reason             TEXT    NOT NULL,   -- e.g. "couldn't classify" (F3) / "couldn't map" (F3-AI)
    status              TEXT    NOT NULL DEFAULT 'pending',  -- pending | resolved
    created_at          TEXT    NOT NULL,
    resolved_at         TEXT,
    resolved_by         TEXT,
    resolution_notes    TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON unified_review_queue_entries(status);
CREATE INDEX IF NOT EXISTS idx_queue_document ON unified_review_queue_entries(document_id);

-- Local stub: captures the mandatory reason for a misfiled-document
-- reassignment (reuses F2's reason-capture pattern exactly). Retrofit to
-- F4's Universal Edit & Version History once F4 exists.
CREATE TABLE IF NOT EXISTS document_edit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL,
    action         TEXT    NOT NULL,   -- 'reassign' | 'soft_delete' | 'restore'
    old_value      TEXT,
    new_value      TEXT,
    reason         TEXT,
    changed_by     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_edit_log_document ON document_edit_log(document_id);
"""


def init_documents_schema(conn: sqlite3.Connection) -> None:
    """Create F3 document tables if missing. Idempotent."""
    conn.executescript(DOCUMENTS_SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for pre-existing DBs.

    ``CREATE TABLE IF NOT EXISTS`` never adds a column to a table that
    already exists, so new columns must be added explicitly here (the same
    pattern src/db.py and src/ingestion_ai/schema.py use).
    """
    _add_column(conn, "documents", "notes", "TEXT")


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
