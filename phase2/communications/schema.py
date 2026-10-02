"""SQLite schema for the Communications module.

Shares db/poc.db with the rest of the platform. Follows the same pattern
as src/clients/schema.py and src/auth/schema.py.

Tables:
- followup_providers       — Email/WhatsApp provider config
- followup_templates       — Reusable message templates
- communication_log        — History of all sent communications
- client_followup_prefs    — Per-client follow-up contact preferences
"""

from __future__ import annotations

import sqlite3

COMMUNICATIONS_SCHEMA = """
-- Communication providers (SMTP, SendGrid, Twilio, WATI, Interakt)
CREATE TABLE IF NOT EXISTS followup_providers (
    provider_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT    NOT NULL CHECK(channel IN ('email', 'whatsapp')),
    provider_type TEXT    NOT NULL,  -- 'smtp', 'sendgrid', 'twilio', 'wati', 'interakt'
    label         TEXT,
    config_json   TEXT    NOT NULL DEFAULT '{}',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Reusable message templates
CREATE TABLE IF NOT EXISTS followup_templates (
    template_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    channel         TEXT    NOT NULL CHECK(channel IN ('email', 'whatsapp', 'both')),
    subject_template TEXT,
    body_template   TEXT    NOT NULL,
    variables       TEXT    NOT NULL DEFAULT '[]',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- History of all sent communications
CREATE TABLE IF NOT EXISTS communication_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL,
    channel         TEXT    NOT NULL CHECK(channel IN ('email', 'whatsapp')),
    template_name   TEXT,
    recipient_email TEXT,
    recipient_phone TEXT,
    recipient_name  TEXT,
    subject         TEXT,
    message_body    TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'sent', 'failed', 'read')),
    provider_ref    TEXT,
    error_message   TEXT,
    sent_at         TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_comms_log_client ON communication_log(client_id);
CREATE INDEX IF NOT EXISTS idx_comms_log_status ON communication_log(status);
CREATE INDEX IF NOT EXISTS idx_comms_log_sent_at ON communication_log(sent_at);

-- Per-client follow-up contact preferences
CREATE TABLE IF NOT EXISTS client_followup_prefs (
    pref_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES end_clients(client_id),
    contact_id  INTEGER,
    email       TEXT,
    phone       TEXT,
    preferred_channel TEXT NOT NULL DEFAULT 'email'
                        CHECK(preferred_channel IN ('email', 'whatsapp', 'both')),
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(client_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_followup_prefs_client ON client_followup_prefs(client_id);
"""


def init_communications_schema(conn: sqlite3.Connection) -> None:
    """Create communications tables if missing. Idempotent."""
    conn.executescript(COMMUNICATIONS_SCHEMA)
    conn.commit()
