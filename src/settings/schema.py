"""SQLite schema for the C3 System Settings (base) module.

Shares db/poc.db with src/db.py, src/auth/schema.py and src/clients/schema.py
(same pattern: separate CREATE TABLE statements in the same file).
init_settings_schema() is idempotent and safe to call every app start.

Design notes (see C3 build prompt §3a):
- FirmProfile, OnboardingDefaults, RetentionSetting and the session-timeout
  value are all stored in a generic `settings` key/value table — these are
  flat platform-wide settings with no relational shape in this build.
- CredentialConnectionStatus is a separate table but stores ONLY a masked
  reference and a live status — the secret itself is never stored here
  (handed to C4; a stub until C4 exists).
- NotificationPreference is split: a platform-default list (with a
  firm-mandatory flag that blocks override) plus a per-user override table.
"""

from __future__ import annotations

import sqlite3

SETTINGS_SCHEMA = """
-- Platform-wide flat settings (firm profile, onboarding defaults,
-- retention, session timeout, etc.). key/value only — no relational
-- shape needed in this build.
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT,
    updated_at   TEXT,
    updated_by   TEXT
);

-- Platform-default notification preferences. `firm_mandatory` blocks any
-- per-user override (the toggle renders locked, component 3.4).
CREATE TABLE IF NOT EXISTS notification_preferences (
    pref_key       TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    kind           TEXT NOT NULL,          -- grouping (e.g. 'recon', 'admin')
    firm_default   INTEGER NOT NULL DEFAULT 0,   -- platform default on/off
    firm_mandatory INTEGER NOT NULL DEFAULT 0    -- blocked-override flag
);

-- Per-user override of a notification preference. Absence = follow the
-- platform default. Never stores a row for a firm-mandatory pref.
CREATE TABLE IF NOT EXISTS notification_user_overrides (
    user_id      INTEGER NOT NULL,
    pref_key     TEXT    NOT NULL,
    enabled      INTEGER NOT NULL,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (user_id, pref_key)
);

-- Credential / API connections. Stores ONLY a masked reference + live
-- status. The secret is handed to C4 (stub) and never persisted here.
CREATE TABLE IF NOT EXISTS credential_connections (
    connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    service       TEXT    NOT NULL,
    masked_ref    TEXT    NOT NULL,   -- e.g. "sk-••••1234"
    status        TEXT    NOT NULL DEFAULT 'connected',  -- connected|expired|needs_reauth
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
"""


def init_settings_schema(conn: sqlite3.Connection) -> None:
    """Create C3 settings tables if missing. Idempotent."""
    conn.executescript(SETTINGS_SCHEMA)
    conn.commit()