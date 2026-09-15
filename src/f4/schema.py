"""SQLite schema for the F4 Universal Edit & Version History service.

Shares db/poc.db with every other module (same pattern: separate
CREATE TABLE statements in one file). init_f4_schema() is idempotent and
safe to call every app start.

Design notes (see the F4 build prompt "Data Model" + the locked
Information Architecture distinction between Audit Trail and Edit
History):

- EditHistoryEntry is the ONE cross-cutting record every editable field
  change writes into: (record_type, record_id, field, old_value,
  new_value, reason, who, when). It is IMMUTABLE once written \u2014 there is
  no UPDATE/DELETE path anywhere in db.py (mirrors the runs/match_results
  append-only convention). Even a same-minute correction is a NEW entry,
  never an overwrite.
- This is genuine FIELD-LEVEL EDIT HISTORY only: "a record's stored value
  changed by a person, with a reason" \u2014 deliberately DISTINCT from
  SECURITY/OPERATIONAL EVENTS (failed logins, permission escalations,
  credential access, AI fallbacks, validation-block overrides, filing
  sends). Those stay permanently in C4's SecurityEvent sink per the
  build prompt's resolution of the Open Conflict; they are NOT migrated
  here.
- FieldSensitivityFlag is NOT a table \u2014 it is a CODE-LEVEL attribute set
  at build time (see service.FIELD_SENSITIVITY / MANDATORY_REASON_FIELDS)
  on the PRD's named fields (GSTIN, PAN, rule thresholds, bank details =
  mandatory reason; everything else = optional/skippable). It is
  explicitly NOT an Admin-configurable setting.
"""

from __future__ import annotations

import sqlite3

F4_SCHEMA = """
-- EditHistoryEntry \u2014 immutable once written, permanently queryable, never
-- overwritten at the storage level. One row per field change (a bulk edit
-- of N records produces N rows, never one combined line).
CREATE TABLE IF NOT EXISTS edit_history_entries (
    entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type   TEXT    NOT NULL,   -- e.g. 'user', 'role', 'user_role', 'role_permission',
                                      --      'user_override', 'client', 'branch', 'rule',
                                      --      'taxonomy', 'regulatory_rule', 'statutory_due_date'
    record_id     TEXT    NOT NULL,   -- the edited record's stable reference (username, key, id-as-text)
    client_id     INTEGER,            -- nullable; populated where the record is client-scoped, so
                                      -- a per-client History view can filter cleanly
    field         TEXT    NOT NULL,   -- the changed field
    old_value     TEXT,               -- preserved verbatim
    new_value     TEXT,
    reason        TEXT,               -- nullable for optional/skippable fields with none given
    changed_by    TEXT    NOT NULL,   -- the person who made the edit (never 'system' \u2014 see prompt)
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edit_history_record ON edit_history_entries(record_type, record_id);
CREATE INDEX IF NOT EXISTS idx_edit_history_client ON edit_history_entries(client_id);
CREATE INDEX IF NOT EXISTS idx_edit_history_created ON edit_history_entries(created_at);
"""


def init_f4_schema(conn: sqlite3.Connection) -> None:
    """Create F4 tables if missing. Idempotent."""
    conn.executescript(F4_SCHEMA)
    conn.commit()