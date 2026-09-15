"""SQLite schema for the C4 Security & Credential Vault module.

Shares db/poc.db with src/db.py, src/auth/schema.py, src/clients/schema.py,
src/settings/schema.py and src/rules/schema.py (same pattern: separate
CREATE TABLE statements in the same file). init_vault_schema() is
idempotent and safe to call every app start.

Design notes (see C4 build prompt §3c):
- EncryptedCredential holds REAL encryption-at-rest (Fernet/AES via the
  `cryptography` package — see src/vault/crypto.py). Only the ciphertext
  and a masked reference are stored; the plaintext secret is decrypted
  in-process, at the point of use, and never written back to any table
  or log.
- SecurityEvent (vault-owned) is the REAL sink C4 exists to provide. F1's
  auth_events security_events table and C3-ext's future fallback log are
  retrofit targets: this table is additive to (not a replacement for)
  auth.security_events for THIS build, but the C4 UI reads from here as
  the canonical source going forward (see retrofit note in service.py).
- DPDPDeletionRequest is a legally-scoped exception workflow, Partner-only
  approval, distinct from routine C3 retention settings.
- BackupDRStatus is intentionally NOT a real table with fabricated values
  — the build prompt is explicit that no backup status should be
  fabricated. It is rendered as a structural placeholder string by the UI
  only; nothing is persisted for it here.
- credential_rotation_history is append-only, no value column ever, per
  the "old value never shown, new value never redisplayed" business rule.
"""

from __future__ import annotations

import sqlite3

VAULT_SCHEMA = """
-- Encrypted secrets vault. `ciphertext` is Fernet-encrypted bytes (stored
-- as base64 text); `masked_ref` is the ONLY human-visible representation
-- (e.g. "sk-\u2022\u2022\u20223456"). Never a plaintext column, ever.
CREATE TABLE IF NOT EXISTS encrypted_credentials (
    credential_id INTEGER PRIMARY KEY AUTOINCREMENT,
    service       TEXT    NOT NULL,        -- e.g. "Tally", "GST Portal", "TRACES", "OpenRouter"
    label         TEXT,                    -- optional free-text disambiguator
    masked_ref    TEXT    NOT NULL,
    ciphertext    TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'connected',  -- connected|expired|needs_reauth
    client_id     INTEGER,                 -- NULL = firm-wide credential, not client-scoped
    created_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_credentials_service ON encrypted_credentials(service);

-- Rotation history \u2014 timestamp + who rotated ONLY, never a value
-- column (old or new), per the build prompt's explicit rule.
CREATE TABLE IF NOT EXISTS credential_rotation_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES encrypted_credentials(credential_id),
    rotated_by    TEXT    NOT NULL,
    rotated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rotation_credential ON credential_rotation_history(credential_id);

-- SecurityEvent \u2014 the real, canonical sink C4 exists to provide.
-- Logged for every credential RETRIEVAL/USE (not just changes), plus
-- failed logins / permission escalations / validation-block overrides /
-- filing sends once those modules retrofit to it. `client_id` supports
-- data-isolation testing at the data layer (a query can be scoped to a
-- single client_id and never see another's rows).
CREATE TABLE IF NOT EXISTS vault_security_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT    NOT NULL,   -- 'credential_created' | 'credential_retrieved' | 'credential_rotated'
                                       -- | 'credential_removed' | 'dpdp_request_submitted'
                                       -- | 'dpdp_request_approved' | 'dpdp_request_rejected'
    actor        TEXT,
    client_id    INTEGER,            -- data-isolation scoping; NULL = firm-wide / not client-specific
    detail       TEXT,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vault_events_created ON vault_security_events(created_at);
CREATE INDEX IF NOT EXISTS idx_vault_events_type ON vault_security_events(event_type);
CREATE INDEX IF NOT EXISTS idx_vault_events_client ON vault_security_events(client_id);

-- DPDP Act deletion-request workflow. Partner-only approval; reject
-- leaves data untouched but is still fully logged with its reason.
CREATE TABLE IF NOT EXISTS dpdp_deletion_requests (
    request_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER,
    client_label   TEXT    NOT NULL,   -- denormalized display label at submit time
    scope          TEXT    NOT NULL,   -- free-text description of data scope requested
    legal_basis    TEXT    NOT NULL,
    deletable_items    TEXT NOT NULL,  -- JSON array \u2014 items considered deletable
    exempt_items       TEXT NOT NULL,  -- JSON array \u2014 statutorily-exempt items (not deleted)
    status         TEXT    NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    submitted_by   TEXT    NOT NULL,
    submitted_at   TEXT    NOT NULL,
    decided_by     TEXT,
    decided_at     TEXT,
    decision_reason TEXT,
    executed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_dpdp_status ON dpdp_deletion_requests(status);
"""


def init_vault_schema(conn: sqlite3.Connection) -> None:
    """Create C4 vault tables if missing. Idempotent."""
    conn.executescript(VAULT_SCHEMA)
    conn.commit()
