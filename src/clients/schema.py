"""SQLite schema for the F2 Client Profile & Master Data module.

Shares db/poc.db with src/db.py and src/auth/schema.py (same pattern:
separate CREATE TABLE statements in the same file). init_clients_schema()
is idempotent and safe to call every app start.

Design notes (see the F2 build prompt + its 27-Sep-2026 revision):
- EndClient is FLAT. The original "multiple GSTINs/branches under one
  End-Client" model was REVERSED: every GST registration is now its own
  independent EndClient row, so a company with three state-wise GSTINs is
  three clients in the roster, not one client with three branches. There is
  no branch sub-entity, no per-branch status, and no rollup across sibling
  registrations (explicitly deferred).
- EndClient therefore carries `gstin` directly (single, optional), plus a
  primary contact block (email/phone/address — the former branch address
  folds in here; `state` is no longer a separate field, it is part of the
  free-text address). The contact block is OPTIONAL: it is collected when the
  client has it, but never blocks a save.
- Validation that needs more than one column (the legal name is the only
  required one) lives in src/clients/service.py, not as DB constraints, so the
  UI gets one plain-language message per rule.
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
    client_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    legal_name               TEXT    NOT NULL,
    pan                      TEXT,
    tan                      TEXT,
    gstin                    TEXT,                        -- ONE registration per client
    assigned_team            TEXT,                        -- free-text description; per-client staff
                                                          -- assignment for module scoping still lives
                                                          -- in F1's per_client_team_assignments
    primary_contact_email    TEXT,
    primary_contact_phone    TEXT,
    primary_contact_address  TEXT,                        -- includes the state (free text this phase)
    is_active                INTEGER NOT NULL DEFAULT 1,  -- soft-delete only, never hard-deleted
    created_at               TEXT    NOT NULL,
    created_by               TEXT
);

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

-- Legacy local stub (superseded by F4). Retained-but-unwritten, like
-- src/auth/schema.py's auth_change_log.
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


# Columns the flat EndClient gained when the branch model was removed.
_NEW_CLIENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tan", "TEXT"),
    ("gstin", "TEXT"),
    ("primary_contact_email", "TEXT"),
    ("primary_contact_phone", "TEXT"),
    ("primary_contact_address", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-revision database onto the flat model.

    ``CREATE TABLE IF NOT EXISTS`` never adds columns to an existing table, so
    each new EndClient column is ALTERed in individually (the established
    pattern in this repo). The old per-branch data is then FOLDED IN \u2014 the
    primary branch's GSTIN becomes the client's single GSTIN and its address
    (plus state) becomes the primary contact address \u2014 and only once that
    data is safely on the client row is ``gstin_branches`` dropped.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(end_clients)")}
    for name, decl in _NEW_CLIENT_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE end_clients ADD COLUMN {name} {decl}")

    if _table_exists(conn, "gstin_branches"):
        # Only fill a BLANK GSTIN: a client already carrying one is authoritative.
        conn.execute(
            """
            UPDATE end_clients
               SET gstin = (
                     SELECT b.gstin FROM gstin_branches b
                      WHERE b.client_id = end_clients.client_id
                        AND b.is_active = 1
                      ORDER BY b.is_primary DESC, b.branch_id
                      LIMIT 1
                   )
             WHERE (gstin IS NULL OR TRIM(gstin) = '')
               AND EXISTS (SELECT 1 FROM gstin_branches b WHERE b.client_id = end_clients.client_id)
            """
        )

        for (client_id,) in conn.execute("SELECT client_id FROM end_clients").fetchall():
            current = conn.execute(
                "SELECT primary_contact_address FROM end_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            if current and (current[0] or "").strip():
                continue
            folded = _folded_address(conn, client_id)
            if folded:
                conn.execute(
                    "UPDATE end_clients SET primary_contact_address = ? WHERE client_id = ?",
                    (folded, client_id),
                )

        # The GSTINBranch entity is deleted in full \u2014 the client row above is
        # now the single source of truth for the registration.
        conn.execute("DROP TABLE gstin_branches")

    conn.commit()


def _folded_address(conn: sqlite3.Connection, client_id: int) -> str | None:
    """The legacy branch address + state, joined into one free-text line."""
    rows = conn.execute(
        "SELECT address, state FROM gstin_branches WHERE client_id = ? "
        "ORDER BY is_primary DESC, branch_id",
        (client_id,),
    ).fetchall()
    for address, state in rows:
        parts = [p.strip() for p in (address, state) if p and p.strip()]
        if parts:
            return ", ".join(parts)
    return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def init_clients_schema(conn: sqlite3.Connection) -> None:
    """Create F2 client tables if missing, then fold a pre-revision
    branch-based database onto the flat model. Idempotent."""
    conn.executescript(CLIENTS_SCHEMA)
    _migrate(conn)
