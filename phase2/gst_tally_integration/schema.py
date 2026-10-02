"""SQLite schema for the GST Portal & Tally Integration module.

Tables:
- gst_credentials     — Encrypted GST portal credentials (per client per GSTIN)
- tally_connections   — Tally connection configs (per client)
- sync_operations     — Queue of sync operations with status
- gst_returns         — Fetched GST return data
- tally_data_cache    — Cached Tally data for preview
"""

from __future__ import annotations

import sqlite3

GST_TALLY_SCHEMA = """
-- Encrypted GST portal credentials (one per GSTIN)
CREATE TABLE IF NOT EXISTS gst_credentials (
    credential_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        INTEGER NOT NULL REFERENCES end_clients(client_id),
    gstin            TEXT    NOT NULL,
    username         TEXT    NOT NULL,
    encrypted_password TEXT NOT NULL,
    is_active        INTEGER NOT NULL DEFAULT 1,
    last_synced_at   TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_gst_creds_client ON gst_credentials(client_id);
CREATE INDEX IF NOT EXISTS idx_gst_creds_gstin  ON gst_credentials(gstin);

-- Tally connection config (one per company per client)
CREATE TABLE IF NOT EXISTS tally_connections (
    conn_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL REFERENCES end_clients(client_id),
    host         TEXT    NOT NULL DEFAULT 'localhost',
    port         INTEGER NOT NULL DEFAULT 9000,
    company_name TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    last_synced_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tally_conn_client ON tally_connections(client_id);

-- Sync operations queue
CREATE TABLE IF NOT EXISTS sync_operations (
    op_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    direction    TEXT    NOT NULL CHECK(direction IN ('fetch', 'push')),
    source       TEXT    NOT NULL CHECK(source IN ('gst_portal', 'tally')),
    data_type    TEXT    NOT NULL,
    period       TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'previewing', 'confirmed',
                                     'completed', 'failed', 'cancelled')),
    summary_json TEXT    NOT NULL DEFAULT '{}',
    details_json TEXT    NOT NULL DEFAULT '{}',
    error_message TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_ops_client  ON sync_operations(client_id);
CREATE INDEX IF NOT EXISTS idx_sync_ops_status  ON sync_operations(status);
CREATE INDEX IF NOT EXISTS idx_sync_ops_created ON sync_operations(created_at);

-- Cached GST return data
CREATE TABLE IF NOT EXISTS gst_returns (
    return_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    gstin        TEXT    NOT NULL,
    return_type  TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    filing_status TEXT,
    filed_at     TEXT,
    raw_json     TEXT    NOT NULL DEFAULT '{}',
    synced_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_gst_returns_client ON gst_returns(client_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gst_returns_lookup
    ON gst_returns(client_id, gstin, return_type, period);

-- Cached Tally data
CREATE TABLE IF NOT EXISTS tally_data_cache (
    data_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    data_type    TEXT    NOT NULL,
    period       TEXT,
    raw_xml      TEXT,
    raw_json     TEXT    NOT NULL DEFAULT '{}',
    synced_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tally_cache_client ON tally_data_cache(client_id);
CREATE INDEX IF NOT EXISTS idx_tally_cache_type   ON tally_data_cache(data_type);
"""


def init_gst_tally_schema(conn: sqlite3.Connection) -> None:
    """Create GST/Tally tables if missing. Idempotent."""
    conn.executescript(GST_TALLY_SCHEMA)
    conn.commit()
