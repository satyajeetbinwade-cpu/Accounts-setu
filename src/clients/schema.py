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
    -- The standard user who created this client. THE data-silo key: a
    -- standard user's every client query is filtered on it, and a direct
    -- lookup by id that doesn't match is refused. Set from the session on
    -- creation, never from request input. NULL = unowned, which is admin-only.
    owner_user_id            INTEGER,
    primary_contact_email    TEXT,
    primary_contact_phone    TEXT,
    primary_contact_address  TEXT,                        -- includes the state (free text this phase)
    is_active                INTEGER NOT NULL DEFAULT 1,  -- soft-delete only, never hard-deleted
    created_at               TEXT    NOT NULL,
    created_by               TEXT
);

-- NOTE: the owner indexes are created in _migrate() below, not here.
-- "CREATE INDEX IF NOT EXISTS" on a pre-existing table would fail on the
-- missing column before the ALTER runs, so the indexes follow the ALTERs.

-- Internal reference only \u2014 never linked to a login row.
CREATE TABLE IF NOT EXISTS contact_directory (
    contact_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL REFERENCES end_clients(client_id),
    owner_user_id  INTEGER,                              -- mirrors the parent client's owner
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
    owner_user_id  INTEGER,                              -- mirrors the parent client's owner
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
    owner_user_id  INTEGER,                              -- mirrors the parent client's owner
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


# Columns the flat EndClient gained when the branch model was removed, plus
# the ownership column that makes per-user data silos possible.
_NEW_CLIENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tan", "TEXT"),
    ("gstin", "TEXT"),
    ("primary_contact_email", "TEXT"),
    ("primary_contact_phone", "TEXT"),
    ("primary_contact_address", "TEXT"),
    ("owner_user_id", "INTEGER"),
)

# Client child tables that mirror the parent's owner. Kept as (table, parent
# key) so the backfill below can be written once instead of four times.
_OWNER_CHILD_TABLES: tuple[tuple[str, str], ...] = (
    ("contact_directory", "client_id"),
    ("chart_of_accounts", "client_id"),
    ("historical_snapshots", "client_id"),
)


def _backfill_owner_columns(conn: sqlite3.Connection) -> None:
    """Give every pre-existing client an owner and mirror it onto children.

    Everything that exists before the silo model belongs to the firm, so it is
    assigned to the first Admin — no data is lost and no client is left
    unowned (an unowned row would be invisible to standard users *and* to a
    non-Admin admin-less database, which is a data-loss bug, not a safe
    default). A database with no Admin yet keeps NULL owners; every read path
    treats NULL as admin-only, so nothing leaks while waiting for a seed.
    """
    if not _table_exists(conn, "users"):
        # A database that has client tables but no auth layer yet (a legacy
        # fixture, or init order in a test). Owners stay NULL, and every read
        # path treats NULL as admin-only — so nothing leaks while it waits.
        return

    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "role" not in user_cols:
        # Users table exists but has not been migrated onto the access-role
        # model yet. Same reasoning: leave owners NULL rather than guess.
        return

    admin_id = conn.execute(
        "SELECT MIN(user_id) FROM users WHERE role = 'admin' AND is_active = 1"
    ).fetchone()[0]
    if admin_id is None:
        return

    conn.execute(
        "UPDATE end_clients SET owner_user_id = ? WHERE owner_user_id IS NULL",
        (admin_id,),
    )
    for table, parent_key in _OWNER_CHILD_TABLES:
        conn.execute(
            f"""
            UPDATE {table}
               SET owner_user_id = (
                     SELECT c.owner_user_id FROM end_clients c
                      WHERE c.client_id = {table}.{parent_key}
                   )
             WHERE owner_user_id IS NULL
            """
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

    for table, _parent_key in _OWNER_CHILD_TABLES:
        child_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if child_cols and "owner_user_id" not in child_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_user_id INTEGER")

    # Indexes last: they reference the columns the ALTERs above just added, and
    # `CREATE INDEX IF NOT EXISTS` cannot run before them on a legacy database.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_end_clients_owner ON end_clients(owner_user_id)")
    for table, _parent_key in _OWNER_CHILD_TABLES:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_owner ON {table}(owner_user_id)"
        )

    _backfill_owner_columns(conn)

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
