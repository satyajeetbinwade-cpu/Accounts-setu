"""SQLite schema for the F5 Data Integrity & Validation Layer.

Shares db/poc.db with src/db.py and every other module's schema.py (same
pattern: separate CREATE TABLE statements in the same file).
init_f5_schema() is idempotent and safe to call every app start.

Design notes (see F5 build prompt "Data Model"):
- ValidationBlock is explicitly a GATE, not a Flagged Item — it blocks
  the affected data from being trusted downstream until resolved
  (override or fix-and-rerun upstream). Structural validity (malformed
  GSTIN/PAN/dates/duplicates on already-mapped CANONICAL data) is a
  SECOND, DISTINCT gate from F3-AI's mapping-confidence queue — never
  merged with it.
- SyncHealthStatus is per client per source (tally / gst_portal / traces
  / bank), append-only so history is retained over time; the LATEST row
  per (client, source) is the current state. States: not_attempted /
  attempted_failed / succeeded.
- ManualControlTotal is the interim completeness-check input: the
  uploader manually enters the source system's own control total
  (count/amount) alongside the ingested canonical data, since C2's live
  connection doesn't exist yet.
- ReconOfReconResult — the data model and calling interface exist NOW,
  dormant. Module 2 doesn't exist yet in this repo (build step 9), so no
  row is ever written here yet; the UI's "Reconciliation-of-the-
  reconciliation" tab reads this table and renders the dormant
  placeholder when it's empty (always, this build).
- validation_change_log is a LOCAL stub, retrofit to F4's Universal Edit
  & Version History once F4 exists (same pattern as rule_change_log /
  client_edit_log / document_edit_log). Every override writes an Audit
  Trail Entry here, and — where the override changed data state — an
  Edit History Entry too (both rows in this one table, distinguished by
  `entry_kind`).
- escalation_events records persistent/consecutive sync failures that
  escalate directly to a Partner (not just passive dashboard visibility)
  — the local stand-in for a real Partner notification/escalation sink
  until C3/C4's notification delivery exists for real push/email.
"""

from __future__ import annotations

import sqlite3

F5_SCHEMA = """
-- ValidationBlock — a GATE, not a Flagged Item. Blocks only the affected
-- data (never the client's entire processing) until overridden or the
-- upstream data is fixed and re-ingested.
CREATE TABLE IF NOT EXISTS validation_blocks (
    block_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL,
    recon_type      TEXT    NOT NULL,   -- 'GST' | 'TDS'
    source_type     TEXT    NOT NULL,   -- e.g. 'tally', 'gstr2b', 'form26as'
    source_file     TEXT    NOT NULL,
    issue_type      TEXT    NOT NULL,   -- 'malformed_gstin' | 'invalid_pan' | 'invalid_tan' |
                                        -- 'impossible_date' | 'duplicate'
    severity        TEXT    NOT NULL,   -- 'cosmetic' | 'minor' | 'gstin_pan'
    field_name      TEXT,
    raw_value       TEXT,
    description     TEXT    NOT NULL,   -- plain-language: what failed, why
    row_count       INTEGER NOT NULL DEFAULT 1,  -- how many rows this block represents
    status          TEXT    NOT NULL DEFAULT 'open',  -- 'open' | 'overridden'
    created_at      TEXT    NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT,
    override_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_validation_blocks_client ON validation_blocks(client_id, status);

-- SyncHealthStatus — per client per source, append-only (history
-- retained). The most recent row per (client_id, source) is the current
-- state. For the manual-upload path (the only path this phase) these are
-- REAL entries; for API sources this stays dormant until C2 exists.
CREATE TABLE IF NOT EXISTS sync_health_status (
    health_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL,
    source          TEXT    NOT NULL,   -- 'tally' | 'gst_portal' | 'traces' | 'bank'
    status          TEXT    NOT NULL,   -- 'not_attempted' | 'attempted_failed' | 'succeeded'
    detail          TEXT,
    checked_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_health_client_source ON sync_health_status(client_id, source, checked_at);

-- ManualControlTotal — interim completeness-check input (manual entry of
-- the source system's own control total) vs what was actually ingested.
CREATE TABLE IF NOT EXISTS manual_control_totals (
    control_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         INTEGER NOT NULL,
    period            TEXT    NOT NULL,
    recon_type        TEXT    NOT NULL,
    source_type       TEXT    NOT NULL,
    source_file       TEXT    NOT NULL,
    control_count     INTEGER NOT NULL,   -- e.g. "portal shows 47 records"
    control_amount    REAL,               -- e.g. "...₹X total"
    ingested_count    INTEGER NOT NULL,
    ingested_amount   REAL,
    result            TEXT    NOT NULL,   -- 'match' | 'mismatch'
    entered_by        TEXT    NOT NULL,
    entered_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_control_totals_client ON manual_control_totals(client_id, period, source_type);

-- ReconOfReconResult — data model + calling interface built NOW, dormant.
-- Module 2 (build step 9) is what actually wires and tests the real
-- check (matched+unmatched+excluded sums tying back to ingested totals).
-- No consumer writes here yet in this build; the table exists so the
-- interface is stable when Module 2 lands.
CREATE TABLE IF NOT EXISTS recon_of_recon_results (
    result_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         INTEGER NOT NULL,
    run_id            INTEGER,
    matched_sum       REAL,
    unmatched_sum     REAL,
    excluded_sum      REAL,
    ingested_total    REAL,
    ties_out          INTEGER,   -- boolean; NULL until a real check runs
    created_at        TEXT    NOT NULL
);

-- Escalation events — consecutive/persistent sync failures that escalate
-- directly to a Partner (not just passive dashboard visibility). Local
-- stand-in for a real notification-delivery mechanism.
CREATE TABLE IF NOT EXISTS escalation_events (
    escalation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id         INTEGER NOT NULL,
    source            TEXT    NOT NULL,
    consecutive_fails INTEGER NOT NULL,
    message           TEXT    NOT NULL,
    created_at        TEXT    NOT NULL
);

-- Local change log — retrofit to F4's Universal Edit & Version History
-- once F4 exists. Every override writes an Audit Trail Entry; where the
-- override changed a data state (e.g. cleared a block), also an Edit
-- History Entry, both rows here distinguished by entry_kind.
CREATE TABLE IF NOT EXISTS validation_change_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id     INTEGER,
    entry_kind   TEXT    NOT NULL,   -- 'audit_trail' | 'edit_history'
    action       TEXT    NOT NULL,   -- e.g. 'override', 'run_check', 'control_total_entered'
    detail       TEXT,
    reason       TEXT,
    actor        TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);
"""


def init_f5_schema(conn: sqlite3.Connection) -> None:
    """Create F5 tables if missing. Idempotent."""
    conn.executescript(F5_SCHEMA)
    conn.commit()
