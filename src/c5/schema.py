"""SQLite schema for the C5 AI Instruction & Knowledge Library module.

Shares db/poc.db with the other module schemas (same pattern: separate
CREATE TABLE statements, idempotent init).

Design notes (see C5 build prompt §4.5):
- Instruction is deliberately NOT a rigid schema — a short required title
  plus a longer free-text explanation, because it captures judgment-shaped
  knowledge.
- InstructionVersion retains full history INDEFINITELY (not subject to the
  general retention policy — mirrors F3's evidentiary retention exception).
  Superseded instructions are marked inactive, never deleted.
- InstructionTouchpointTag names which AI touchpoint(s) an instruction
  applies to (ingestion, TDS classification, plus the placeholder
  touchpoints from C3-extended).
- InstructionScope is firm-wide or a specific End-Client (client_id).
- ProposedInstruction is submitted from other modules' AI review screens,
  always pending Admin confirmation before activation.
- proposed_instruction / proposed_instruction_tag are junction records
  because a proposal can carry multiple touchpoint tags.
"""

from __future__ import annotations

import sqlite3

C5_SCHEMA = """
-- The AI touchpoint vocabulary an instruction can be tagged for. Two are
-- live (ingestion_mapping, tds_classification); the rest are placeholders
-- from C3-extended, tagged at creation for future use.
CREATE TABLE IF NOT EXISTS instruction_touchpoints (
    touchpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT    NOT NULL UNIQUE,   -- e.g. 'ingestion_mapping'
    label         TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1, -- 0 = placeholder, not wired yet
    sort_order    INTEGER NOT NULL DEFAULT 0
);

-- A single judgment-shaped instruction. `is_active` is the active/inactive
-- toggle; superseded instructions are marked inactive, never deleted.
CREATE TABLE IF NOT EXISTS instructions (
    instruction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    explanation    TEXT    NOT NULL,
    scope          TEXT    NOT NULL DEFAULT 'firm',  -- 'firm' | 'client'
    client_id      INTEGER,                          -- set when scope='client'
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_by     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instructions_scope ON instructions(scope, client_id);

-- Which touchpoint(s) an instruction applies to (many-to-many).
CREATE TABLE IF NOT EXISTS instruction_tags (
    instruction_id INTEGER NOT NULL REFERENCES instructions(instruction_id),
    touchpoint_id  INTEGER NOT NULL REFERENCES instruction_touchpoints(touchpoint_id),
    PRIMARY KEY (instruction_id, touchpoint_id)
);

-- Immutable version history — retained INDEFINITELY (F3-style evidentiary
-- exception). Each edit appends a NEW row; nothing is ever deleted.
CREATE TABLE IF NOT EXISTS instruction_versions (
    version_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    instruction_id INTEGER NOT NULL REFERENCES instructions(instruction_id),
    title          TEXT    NOT NULL,
    explanation    TEXT    NOT NULL,
    scope          TEXT    NOT NULL,
    client_id      INTEGER,
    change_summary TEXT    NOT NULL,
    changed_by     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instruction_versions ON instruction_versions(instruction_id, version_id);

-- A proposal submitted from another module's AI review screen (F3-AI,
-- Module 2's TDS classification). Always pending Admin confirmation; never
-- auto-activates. Status: 'pending' | 'approved' | 'rejected'.
CREATE TABLE IF NOT EXISTS proposed_instructions (
    proposal_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    explanation    TEXT    NOT NULL,
    scope          TEXT    NOT NULL DEFAULT 'firm',
    client_id      INTEGER,
    source         TEXT    NOT NULL,   -- which AI review screen it came from
    status         TEXT    NOT NULL DEFAULT 'pending',
    submitted_by   TEXT    NOT NULL,
    submitted_at   TEXT    NOT NULL,
    resolved_by    TEXT,
    resolved_at    TEXT
);

-- Junction: touchpoint tags on a proposal.
CREATE TABLE IF NOT EXISTS proposed_instruction_tags (
    proposal_id    INTEGER NOT NULL REFERENCES proposed_instructions(proposal_id),
    touchpoint_id  INTEGER NOT NULL REFERENCES instruction_touchpoints(touchpoint_id),
    PRIMARY KEY (proposal_id, touchpoint_id)
);
"""


def init_c5_schema(conn: sqlite3.Connection) -> None:
    """Create C5 tables if missing. Idempotent."""
    conn.executescript(C5_SCHEMA)
    conn.commit()