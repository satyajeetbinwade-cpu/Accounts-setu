"""SQLite schema for the F2 Client Profile & Master Data module.

Shares db/poc.db with src/db.py and src/auth/schema.py (same pattern:
separate CREATE TABLE statements in the same file). init_clients_schema()
is idempotent and safe to call every app start.

Design notes (see F2 build prompt):
- EndClient carries client-level data only (legal name, PAN, assigned
  team). No client-level health/status rollup is stored anywhere — F2's
  health summary is a structural placeholder only.
- GSTINBranch carries its OWN independent status field per branch
  (deliberate: GST filings are GSTIN-specific in India, and a
  client-level rollup would need a retrofit once Module 2 exists).
- ContactDirectoryEntry is internal-reference only, never linked to a
  login row (the End-Client login is a single shared credential).
- ChartOfAccounts / HistoricalSnapshot are manual entry only this phase
  (no Tally-sync path). HistoricalSnapshot is stored per fiscal year
  even though its consumer (Module 4) isn't built yet.
- client_edit_log is a local stub for the GSTIN/PAN reason-capture
  requirement; retrofit to F4's Universal Edit & Version History once
  F4 exists (same pattern as src/auth/schema.py's auth_change_log).
"""

from __future__ import annotations

import sqlite3

CLIENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS end_clients (
    client_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    legal_name     TEXT    NOT NULL,
    pan            TEXT,
    assigned_team  TEXT,                        -- free-text description; per-client staff
                                                  -- assignment for module scoping still lives
                                                  -- in F1's per_client_team_assignments
    is_active      INTEGER NOT NULL DEFAULT 1,   -- soft-delete only, never hard-deleted
    created_at     TEXT    NOT NULL,
    created_by     TEXT
);

-- Multiple per EndClient. Each branch/GSTIN carries its OWN independent
-- status field \u2014 deliberately NOT rolled up to the client level.
CREATE TABLE IF NOT EXISTS gstin_branches (
    branch_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL REFERENCES end_clients(client_id),
    gstin          TEXT    NOT NULL,
    branch_name    TEXT,
    address        TEXT,
    state          TEXT,
    status         TEXT    NOT NULL DEFAULT 'Not Started',  -- per-branch, independent
    is_primary     INTEGER NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 1,   -- Deactivate (always available)
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gstin_branches_client ON gstin_branches(client_id);

-- Internal reference only \u2014 never linked to a login row.
CREATE TABLE IF NOT EXISTS contact_directory (
    contact_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL REFERENCES end_clients(client_id),
    name           TEXT    NOT NULL,
    role_title     TEXT,
    email          TEXT,
    phone          TEXT,
    notes          TEXT,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contact_directory_client ON contact_directory(client_id);

-- Manual entry only this phase. No Tally-sync path.
CREATE TABLE IF NOT EXISTS chart_of_accounts (
    coa_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL REFERENCES end_clients(client_id),
    code           TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    account_type   TEXT,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coa_client ON chart_of_accounts(client_id);

-- Prior year closing/audited figures, per fiscal year. Consumer (Module 4)
-- isn't built yet \u2014 stored anyway per the F2 prompt.
CREATE TABLE IF NOT EXISTS historical_snapshots (
    snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL REFERENCES end_clients(client_id),
    fiscal_year    TEXT    NOT NULL,
    line_item      TEXT    NOT NULL,
    amount         TEXT,
    notes          TEXT,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_client ON historical_snapshots(client_id);

-- Local stub: captures the mandatory reason for GSTIN/PAN edits. Retrofit
-- to F4's Universal Edit & Version History once F4 exists \u2014 same
-- pattern as src/auth/schema.py's auth_change_log.
CREATE TABLE IF NOT EXISTS client_edit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER,
    branch_id      INTEGER,
    field          TEXT    NOT NULL,
    old_value      TEXT,
    new_value      TEXT,
    reason         TEXT    NOT NULL,
    changed_by     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);
"""


def init_clients_schema(conn: sqlite3.Connection) -> None:
    """Create F2 client tables if missing. Idempotent."""
    conn.executescript(CLIENTS_SCHEMA)
    conn.commit()
