"""SQLite schema for Module 2 — Reconciliation Engine (2A GST / 2B TDS / 2C Other).

Shares db/poc.db with src/db.py and every other module's schema.py (same
pattern: separate CREATE TABLE statements in the same file).
init_module2_schema() is idempotent and safe to call every app start.

Design notes (see the Module 2 build prompt "Data Model"):

- ReconciliationException is built against the GENERIC Flagged Item shape
  from Step 3's Information Architecture (id, type, source module, evidence,
  classification/recommendation, confidence, priority, assignee, due date,
  status) — because Module 8 (built next) consumes this generically. Module 8
  does NOT exist in this repo yet, so this table is the interim single point
  of human action; routing into Module 8's queue is a documented retrofit
  point (see service.py's `route_to_module8_stub()`).

- AIRecommendation / AIOutcome pair: the Recommendation half is built and
  populated now (per fuzzy match and per IMS recommendation). The Outcome
  half is a STUB until Module 9 exists to log human review outcomes — no
  outcome data is ever fabricated. `ai_outcomes` exists as an empty,
  interface-stable table.

- MatchingKeyConfig is per reconciliation sub-type (GST, TDS, and each of
  the six 2C sources), reading tolerance/key definitions from C1. It is
  genuinely data-driven: a new 2C source is addable via configuration, not
  a new code path.

- EligibleCreditFigure (2A) is a live, recalculated-on-each-run figure,
  surfaced directly within Module 2's own screen (not routed through
  Module 7, which doesn't exist).

- TdsChainStage (2B) records the six-step chain trace per TDS exception:
  applicability -> rate -> deduction -> deposit -> return -> 26AS reflection,
  each stage marked complete / gap / timing-lag.

- module2_change_log is a LOCAL stub, retrofit to F4's Universal Edit &
  Version History once F4 exists (same pattern as rule_change_log /
  client_edit_log / f5's validation_change_log).
"""

from __future__ import annotations

import sqlite3

MODULE2_SCHEMA = """
-- ReconciliationException — the GENERIC Flagged Item shape (Step 3 IA).
-- Module 8 (built next) consumes this generically; until it exists this is
-- the interim single point of human action. `sub_type` distinguishes the
-- reconciliation sub-scope: '2A' (GST), '2B' (TDS), '2C' (Other).
CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
    exception_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        INTEGER NOT NULL,
    run_id           INTEGER,             -- the matching run that produced it
    result_id        INTEGER,             -- the match_results row it came from
    fingerprint      TEXT,                -- stable identity, carries across runs
    sub_type         TEXT    NOT NULL,    -- '2A' | '2B' | '2C'
    recon_type       TEXT    NOT NULL,    -- 'GST' | 'TDS' | 'OTHER'
    source_module    TEXT    NOT NULL DEFAULT 'Module 2',
    item_type        TEXT    NOT NULL,    -- generic Flagged Item "type"
    classification   TEXT    NOT NULL,    -- Matched / Not in Books / Not in Portal / Amount Difference
    difference_type  TEXT,
    confidence_score REAL,
    confidence_band  TEXT,
    confidence_source TEXT   NOT NULL DEFAULT 'rule',  -- 'rule' | 'ai' (Foundation 3.1)
    recommendation   TEXT,                -- e.g. IMS Accept/Reject/Pending, or NULL
    recommendation_reason TEXT,           -- always-visible reasoning, never a tooltip
    priority         TEXT    NOT NULL DEFAULT 'normal',  -- 'low' | 'normal' | 'high' | 'escalated'
    assignee         TEXT,
    due_date         TEXT,
    status           TEXT    NOT NULL DEFAULT 'open',    -- 'open' | 'resolved' | 'escalated'
    resolution       TEXT,                -- 'accepted' | 'rejected' | 'escalated'
    resolution_note  TEXT,
    resolved_by      TEXT,
    resolved_at      TEXT,
    evidence         TEXT,                -- JSON: books/portal records + match_reason
    match_reason     TEXT,                -- full text, always visible in the main view
    materiality_threshold REAL,           -- the threshold in force for this sub_type
    above_materiality INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recon_exceptions_client
    ON reconciliation_exceptions(client_id, status);
CREATE INDEX IF NOT EXISTS idx_recon_exceptions_run
    ON reconciliation_exceptions(run_id);
CREATE INDEX IF NOT EXISTS idx_recon_exceptions_fingerprint
    ON reconciliation_exceptions(fingerprint);

-- AIRecommendation — the Recommendation half of the AIRecommendation /
-- AIOutcome pair. Populated now for fuzzy matches and IMS recommendations.
-- Never auto-applied: always routes through human review.
CREATE TABLE IF NOT EXISTS ai_recommendations (
    recommendation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_id      INTEGER,
    run_id            INTEGER,
    fingerprint       TEXT,
    touchpoint        TEXT    NOT NULL,   -- 'fuzzy_match' | 'ims_recommendation' | 'tds_classification'
    recommendation    TEXT    NOT NULL,
    reasoning         TEXT    NOT NULL,   -- first-class, never a tooltip
    confidence_pct    INTEGER,
    model_used        TEXT,
    routing_reason    TEXT,
    routing_stub      INTEGER NOT NULL DEFAULT 1,  -- C3-ext not built yet in this repo
    created_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_recommendations_exception
    ON ai_recommendations(exception_id);

-- AIOutcome — the Outcome half. STUB until Module 9 exists to log human
-- review outcomes. Never fabricated: this table stays empty this build.
CREATE TABLE IF NOT EXISTS ai_outcomes (
    outcome_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id INTEGER,
    exception_id      INTEGER,
    human_decision    TEXT,               -- 'accepted' | 'rejected' | 'escalated'
    outcome_note      TEXT,
    decided_by        TEXT,
    decided_at        TEXT,
    created_at        TEXT    NOT NULL
);

-- MatchingKeyConfig — per reconciliation sub-type (GST, TDS, and each of
-- the six 2C sources). Genuinely data-driven: a new 2C source is addable
-- via configuration, not a new code path. Tolerance/key definitions are
-- read from C1 (rules) at run time; this table records the resolved
-- configuration actually used for a sub-type so it is auditable.
CREATE TABLE IF NOT EXISTS matching_key_configs (
    config_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_type         TEXT    NOT NULL,    -- '2A' | '2B' | '2C'
    source_key       TEXT    NOT NULL,    -- 'gst' | 'tds' | 'bank' | 'vendor_ledger' |
                                          -- 'form26as' | 'opening_balances' | 'loan_sheet' | 'salary'
    label            TEXT    NOT NULL,
    match_keys       TEXT    NOT NULL,    -- JSON array of key field names
    tolerance_absolute REAL,
    tolerance_percent  REAL,
    date_tolerance_days INTEGER,
    fuzzy_threshold  REAL,
    materiality_threshold REAL,
    is_active        INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT    NOT NULL,
    UNIQUE(sub_type, source_key)
);

-- EligibleCreditFigure (2A) — a live, recalculated-on-each-run figure,
-- surfaced directly within Module 2's own screen (not routed through
-- Module 7, which doesn't exist).
CREATE TABLE IF NOT EXISTS eligible_credit_figures (
    figure_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        INTEGER NOT NULL,
    run_id           INTEGER,
    period           TEXT    NOT NULL,
    total_itc_claimed REAL  NOT NULL DEFAULT 0,
    matched_itc      REAL    NOT NULL DEFAULT 0,
    at_risk_itc      REAL    NOT NULL DEFAULT 0,
    blocked_credit_itc REAL  NOT NULL DEFAULT 0,   -- Section 17(5)
    reverse_charge_itc REAL  NOT NULL DEFAULT 0,
    eligible_credit  REAL    NOT NULL DEFAULT 0,
    computed_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eligible_credit_client
    ON eligible_credit_figures(client_id, period);

-- TdsChainStage (2B) — the six-step chain trace per TDS exception:
-- applicability -> rate -> deduction -> deposit -> return -> 26AS reflection.
-- Each stage is marked complete / gap / timing-lag. A timing-lag read is
-- itself an AI-assisted judgment call (Foundation 3.1's amber token).
CREATE TABLE IF NOT EXISTS tds_chain_stages (
    stage_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_id     INTEGER NOT NULL,
    stage_key        TEXT    NOT NULL,    -- applicability|rate|deduction|deposit|return|reflection
    stage_label      TEXT    NOT NULL,
    stage_state      TEXT    NOT NULL,    -- 'complete' | 'gap' | 'timing_lag'
    detail           TEXT,
    sort_order       INTEGER NOT NULL,
    created_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tds_chain_exception
    ON tds_chain_stages(exception_id);

-- Local change log — retrofit to F4's Universal Edit & Version History
-- once F4 exists. Every exception resolution writes an Audit Trail Entry
-- here, and — where the resolution changed data state — an Edit History
-- Entry too (both rows in this one table, distinguished by entry_kind).
CREATE TABLE IF NOT EXISTS module2_change_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_id INTEGER,
    entry_kind   TEXT    NOT NULL,   -- 'audit_trail' | 'edit_history'
    action       TEXT    NOT NULL,
    detail       TEXT,
    reason       TEXT,
    actor        TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);
"""


def init_module2_schema(conn: sqlite3.Connection) -> None:
    """Create Module 2 tables if missing. Idempotent."""
    conn.executescript(MODULE2_SCHEMA)
    conn.commit()