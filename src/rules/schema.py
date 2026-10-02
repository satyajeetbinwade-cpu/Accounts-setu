"""SQLite schema for the C1 Rules, Taxonomy & Regulatory Configuration module.

Shares db/poc.db with src/db.py, src/auth/schema.py, src/clients/schema.py
and src/settings/schema.py (same pattern: separate CREATE TABLE statements
in the same file). init_rules_schema() is idempotent and safe to call every
app start.

Design notes (see C1 build prompt §4):
- RuleCategory / Rule / RuleVersion — effective-dating at TWO independent
  layers: a firm-wide default Rule Version AND a per-client override Rule
  Version, each independently date-versioned. This is deliberate, to avoid
  a Module 2 retrofit later.
- RuleVersion is append-only: an edit inserts a NEW row (forward-only),
  never UPDATEs a past version. Past transactions therefore keep the
  classification/calculation that was in effect when they were processed.
- TaxonomyEntry is PRE-SEEDED (never ships empty) with the Flagged Item
  subtypes and shared vocabulary locked in the Information Architecture
  (Step 3) document.
- RegulatoryRulesTable is PRE-SEEDED with known current rates as of a
  stated date, tagged "Seed data as of [date] — verify before relying on
  for filings."
- StatutoryDueDate is a NEW rule category added to support C2's filing
  calendar (C2 depends on this, built later).
- rule_change_log is a LOCAL stub, retrofit to F4's Universal Edit &
  Version History once F4 exists (same pattern as auth_change_log and
  client_edit_log).
"""

from __future__ import annotations

import sqlite3

RULES_SCHEMA = """
-- The five rule categories. `key` is a stable machine key (e.g. "tds_rates",
-- "gst_tolerance", "suspense", "aging", "statutory_due_dates").
CREATE TABLE IF NOT EXISTS rule_categories (
    category_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT    NOT NULL UNIQUE,
    label         TEXT    NOT NULL,
    description   TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

-- A single editable rule within a category. `key` is stable (e.g.
-- "194C.default_rate", "gst.amount_tolerance.absolute"). `high_impact`
-- marks wide-blast-radius rules (e.g. a firm-wide suspense convention)
-- that require the impact-confirmation banner before saving.
CREATE TABLE IF NOT EXISTS rules (
    rule_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id   INTEGER NOT NULL REFERENCES rule_categories(category_id),
    key           TEXT    NOT NULL UNIQUE,
    label         TEXT    NOT NULL,
    value_type    TEXT    NOT NULL,   -- 'number' | 'text' | 'boolean' | 'date'
    unit          TEXT,               -- e.g. '%', 'Rs', 'days', 'months'
    high_impact    INTEGER NOT NULL DEFAULT 0,
    description   TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_rules_category ON rules(category_id);

-- Effective-dated Rule Versions. TWO independent layers:
--   scope = 'firm'   -> the firm-wide default (client_id is NULL)
--   scope = 'client' -> a per-client override (client_id is set)
-- Each layer is independently date-versioned: editing a rule inserts a NEW
-- row with a new effective_from; the previous row is never mutated, so a
-- past transaction keeps the value that was in effect when it ran.
CREATE TABLE IF NOT EXISTS rule_versions (
    version_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       INTEGER NOT NULL REFERENCES rules(rule_id),
    scope         TEXT    NOT NULL,   -- 'firm' | 'client'
    client_id     INTEGER,            -- NULL for firm-wide; set for client override
    value         TEXT    NOT NULL,   -- stored as text; value_type governs parsing
    effective_from TEXT   NOT NULL,   -- ISO date; forward-only
    created_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_versions_rule ON rule_versions(rule_id, scope, client_id, effective_from);

-- Pre-seeded taxonomy: the Flagged Item subtypes and shared vocabulary
-- locked in the Information Architecture (Step 3) document. Does NOT ship
-- empty. Entries only rename or delete (no deactivate) — see C1's design
-- table (Delete vs Deactivate, Delete side only).
CREATE TABLE IF NOT EXISTS taxonomy_entries (
    entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT    NOT NULL,   -- e.g. 'flagged_item_subtype', 'difference_type'
    code          TEXT    NOT NULL UNIQUE,
    label         TEXT    NOT NULL,
    description   TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

-- Regulatory rules table (GST/TDS section rates, ITC eligibility, locking
-- rules). PRE-SEEDED with known current rates as of a stated date, tagged
-- "Seed data as of [date] — verify before relying on for filings."
-- `last_reviewed_at` drives the staleness signal ("not reviewed in N months").
CREATE TABLE IF NOT EXISTS regulatory_rules (
    reg_rule_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT    NOT NULL,   -- 'GST' | 'TDS'
    section       TEXT,               -- e.g. '194C' for TDS; NULL for GST slabs
    name          TEXT    NOT NULL,
    rate_or_rule  TEXT    NOT NULL,   -- e.g. '2.0%' or 'ITC blocked'
    effective_from TEXT   NOT NULL,
    effective_to  TEXT,               -- NULL => still in force. Lets one section
                                       -- carry successive dated rows (e.g. across
                                       -- the Income-tax Act 2025 recodification)
                                       -- without the newer row clobbering the older.
    single_transaction_threshold REAL, -- TDS only; structured (not parsed from notes)
    annual_aggregate_threshold   REAL, -- TDS only; structured (not parsed from notes)
    seed_as_of    TEXT    NOT NULL,   -- "Seed data as of [date]"
    last_reviewed_at TEXT,            -- NULL => never reviewed (seed-unverified)
    notes         TEXT
);

-- Old-Act TDS section code <-> Income-tax Act 2025 code (in force from
-- 2026-04-01). `new_code` is a PLACEHOLDER pending the CA firm's actual
-- confirmation (see notes on each seeded row) -- never a guessed real
-- citation. A section not yet in this table is treated as unchanged by the
-- recodification (old_code is used as its own canonical key).
CREATE TABLE IF NOT EXISTS tds_section_aliases (
    alias_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    old_code      TEXT    NOT NULL,   -- canonical key rates are stored under
    new_code      TEXT    NOT NULL,   -- Income-tax Act 2025 code (placeholder)
    effective_from TEXT   NOT NULL,   -- date new_code takes over, e.g. 2026-04-01
    notes         TEXT,
    UNIQUE(old_code, new_code)
);

-- Statutory due dates — NEW rule category for C2's filing calendar.
-- Structured so C2 can read the filing calendar directly from here.
CREATE TABLE IF NOT EXISTS statutory_due_dates (
    due_date_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation    TEXT    NOT NULL,   -- e.g. 'GSTR-1', 'GSTR-3B', 'TDS return'
    period        TEXT    NOT NULL,   -- e.g. 'monthly', 'quarterly', 'annual'
    due_day       INTEGER,            -- day of month (for monthly/quarterly)
    due_month     INTEGER,            -- month (1-12) for annual obligations
    grace_days    INTEGER NOT NULL DEFAULT 0,
    description   TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_statutory_due_dates_obligation
    ON statutory_due_dates(obligation);

-- Local change log stub — retrofit to F4's Universal Edit & Version
-- History once F4 exists (same pattern as auth_change_log / client_edit_log).
CREATE TABLE IF NOT EXISTS rule_change_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       INTEGER,
    scope         TEXT,               -- 'firm' | 'client'
    client_id     INTEGER,
    field         TEXT    NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);
"""


def init_rules_schema(conn: sqlite3.Connection) -> None:
    """Create C1 rules tables if missing. Idempotent."""
    conn.executescript(RULES_SCHEMA)
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created.

    ``CREATE TABLE IF NOT EXISTS`` won't add a column to a table that already
    exists, so new columns need an explicit, additive migration here (same
    pattern as src/db.py's _migrate). Safe to call on every connection.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(regulatory_rules)")}
    if "effective_to" not in cols:
        conn.execute("ALTER TABLE regulatory_rules ADD COLUMN effective_to TEXT")
    if "single_transaction_threshold" not in cols:
        conn.execute("ALTER TABLE regulatory_rules ADD COLUMN single_transaction_threshold REAL")
    if "annual_aggregate_threshold" not in cols:
        conn.execute("ALTER TABLE regulatory_rules ADD COLUMN annual_aggregate_threshold REAL")
    conn.commit()