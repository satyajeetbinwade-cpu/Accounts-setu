"""SQLite schema for Module 8 — Action Center (Recon Exceptions only).

Shares db/poc.db with every other module (same pattern: separate CREATE
TABLE statements in one file). init_action_center_schema() is idempotent
and safe to call every app start.

Design notes (see the Module 8 build prompt):
- Module 8 is a WORKING QUEUE, not a dashboard, and it CREATES NOTHING
  itself — it aggregates and surfaces Flagged Items originated by other
  modules. This phase's only concrete source is Module 2's
  ReconciliationException, mapped onto the generic FlaggedItem interface
  in service.py. The queue's core rendering/filtering logic must contain
  ZERO Reconciliation-Exception-specific field names.
- The generic Flagged Item interface itself is NOT a table — it is the
  normalized, in-memory dict shape service.py produces (see
  `list_flagged_items`). Each item carries an `item_ref` of the form
  "<source_module>:<origin_id>" so the queue can address any source
  uniformly.
- action_center_assignments holds ONLY Module 8's OWN overlay state on top
  of an item: the manual assignee + due date, and the ageing/escalation
  bookkeeping. It deliberately never stores priority (that belongs to the
  originating module) — so reassignment can never silently change priority.
- action_center_change_log is a LOCAL stub for the audit trail; the REAL
  edit history for assignment/priority/due-date changes is written to F4
  (which now exists) at build time — see service.py's `assign_item`.
"""

from __future__ import annotations

import sqlite3

ACTION_CENTER_SCHEMA = """
-- Module 8's OWN overlay on a flagged item: manual assignee, due date, and
-- ageing/escalation bookkeeping. Keyed by the GENERIC (source_module,
-- item_ref) pair, never by an exception-specific id — a future Module
-- 1/4/5/6 source slots in with zero schema change.
CREATE TABLE IF NOT EXISTS action_center_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_module TEXT    NOT NULL,   -- e.g. 'Module 2' (generic origin tag)
    item_ref      TEXT    NOT NULL,   -- generic stable identity: "<source_module>:<origin_id>"
    client_id     INTEGER,            -- the item's client scope (for F5 staleness + views)
    assignee      TEXT,               -- manual assignee (overrides F1 default routing); NULL = unassigned
    due_date      TEXT,               -- ISO date; NULL = none set
    priority      TEXT,               -- NOT owned here — mirrored read-only for display only
    age_escalated INTEGER NOT NULL DEFAULT 0,  -- set once the age threshold triggers
    escalated_at  TEXT,
    updated_at    TEXT    NOT NULL,
    UNIQUE (source_module, item_ref)
);

CREATE INDEX IF NOT EXISTS idx_action_center_assignee
    ON action_center_assignments(assignee);
CREATE INDEX IF NOT EXISTS idx_action_center_client
    ON action_center_assignments(client_id);

-- Local change log — the audit trail for assignment/due-date/priority
-- actions. The REAL field-level edit history is written to F4 at build
-- time; this table is retained as the queue's own local trace.
CREATE TABLE IF NOT EXISTS action_center_change_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_module TEXT    NOT NULL,
    item_ref      TEXT    NOT NULL,
    entry_kind    TEXT    NOT NULL,   -- 'audit_trail' | 'edit_history'
    action        TEXT    NOT NULL,
    detail        TEXT,
    reason        TEXT,
    actor         TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_center_log_item
    ON action_center_change_log(source_module, item_ref);
"""


def init_action_center_schema(conn: sqlite3.Connection) -> None:
    """Create Module 8 tables if missing. Idempotent."""
    conn.executescript(ACTION_CENTER_SCHEMA)
    conn.commit()