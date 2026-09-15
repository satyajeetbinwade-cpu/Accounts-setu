"""SQLite schema for the C2 Portal Connect & Filing module.

Shares db/poc.db with every other module (same pattern: separate CREATE
TABLE statements in one file). init_filing_schema() is idempotent and
safe to call every app start.

Design notes (see C2 build prompt §7):
- FilingRecord is the state-machine core: what was sent, when, by whom,
  and the portal's acknowledgement. Status is one of 'prepared' ->
  'sent' -> 'acknowledged' | 'failed' | 'ambiguous'. 'ambiguous' is a
  FIRST-CLASS state (never folded into failed or pending) because the
  spec treats a partial/ambiguous portal failure as near-emergency.
- FilingCalendarEntry is materialized per client per return type per
  period, with its due date computed from C1's statutory_due_dates
  (the "Statutory Due Dates" rule category added to C1 as part of this
  build step).
- The filing package and the acknowledgement receipt are both stored in
  F3's repository (documents/document_versions) and linked back here via
  F3's evidence_links (target_type = 'filing_record'); this module holds
  only the foreign document ids, never a parallel attachment store.
- PerClientSourceToggle persists the intended API-vs-manual mode, but
  its *availability* is read LIVE from C4's credential status — a source
  stays dormant/unavailable until a connected credential exists in C4.
- filing_audit_log is a local stub for the who/when/why audit trail on
  every Send and outcome (retrofit to F4 once built). filing_escalations
  records Manager+Partner escalation on failed/ambiguous filings.
"""

from __future__ import annotations

import sqlite3

FILING_SCHEMA = """
-- The filing record: what was sent, when, by whom, and the portal's
-- acknowledgement. This is the state-machine core.
-- status: 'prepared' (package uploaded, not yet sent)
--         'sent'      (explicit human Send action logged)
--         'acknowledged' | 'failed' | 'ambiguous' (final outcomes)
CREATE TABLE IF NOT EXISTS filing_records (
    filing_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id                INTEGER NOT NULL,   -- REFERENCES end_clients(client_id)
    obligation               TEXT    NOT NULL,   -- return type, e.g. 'GSTR-3B' (from statutory_due_dates)
    period                   TEXT    NOT NULL,   -- e.g. '2026-08'
    package_document_id      INTEGER,             -- the uploaded filing package (F3 document)
    status                   TEXT    NOT NULL DEFAULT 'prepared',
    sent_by                  TEXT,
    sent_at                  TEXT,
    outcome_note             TEXT,
    acknowledgement_document_id INTEGER,          -- portal receipt, routed through F3
    acknowledgement_confirmed_by TEXT,
    acknowledgement_confirmed_at TEXT,
    created_by               TEXT    NOT NULL,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_filing_records_client ON filing_records(client_id);
CREATE INDEX IF NOT EXISTS idx_filing_records_status ON filing_records(status);

-- Materialized calendar entry: per client per return type per period.
-- due_date is computed from C1's statutory_due_dates at materialization.
CREATE TABLE IF NOT EXISTS filing_calendar_entries (
    entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    obligation   TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    due_date     TEXT    NOT NULL,   -- ISO date
    created_at   TEXT    NOT NULL,
    UNIQUE (client_id, obligation, period)
);

CREATE INDEX IF NOT EXISTS idx_calendar_client ON filing_calendar_entries(client_id);
CREATE INDEX IF NOT EXISTS idx_calendar_due ON filing_calendar_entries(due_date);

-- Per-client per-source API-vs-manual toggle. The intended mode is stored
-- here; AVAILABILITY is read LIVE from C4's credential status (dormant
-- until a connected credential exists). Manual upload is ALWAYS available
-- regardless of this toggle's state.
CREATE TABLE IF NOT EXISTS per_client_source_toggles (
    toggle_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    source       TEXT    NOT NULL,   -- 'gst_portal' | 'traces'
    mode         TEXT    NOT NULL DEFAULT 'manual',  -- 'manual' | 'api'
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (client_id, source)
);

-- Local audit-trail stub (retrofit to F4). Logs the who/when/why of every
-- prepare, send, outcome recording, and acknowledgement confirmation —
-- "arguably the single most important entry type in the platform" (PRD).
CREATE TABLE IF NOT EXISTS filing_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id    INTEGER NOT NULL,
    entry_type   TEXT    NOT NULL,   -- 'prepare' | 'send' | 'outcome' | 'acknowledge'
    action       TEXT    NOT NULL,
    detail       TEXT,
    actor        TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_filing_audit ON filing_audit_log(filing_id);

-- Escalation record: a failed or ambiguous filing escalates to BOTH
-- Manager and Partner (not passive dashboard visibility). Local
-- stand-in for a real notification-delivery mechanism.
CREATE TABLE IF NOT EXISTS filing_escalations (
    escalation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id       INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,   -- 'failed' | 'ambiguous'
    message         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_filing_escalations ON filing_escalations(filing_id);
"""


def init_filing_schema(conn: sqlite3.Connection) -> None:
    """Create C2 filing tables if missing. Idempotent."""
    conn.executescript(FILING_SCHEMA)
    conn.commit()
