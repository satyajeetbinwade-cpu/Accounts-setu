"""SQLite schema for the F1 auth layer.

Shares db/poc.db with src/db.py (same file, separate CREATE TABLE
statements) so the PoC keeps a single database file. init_auth_schema()
is idempotent and safe to call every app start, same pattern as
src/db.py's SCHEMA.

Design notes (see F1 build prompt):
- Role is data, not a hardcoded enum — seeded but editable/creatable.
- Permission is action-level (e.g. "users.create"), not screen-level.
- RolePermission = role defaults; UserPermissionOverride = per-user
  grant/revoke exceptions layered on top.
- PerClientTeamAssignment scopes default visibility across every module.
- SecurityEvent and AuthChangeLog are LOCAL stubs: the F1 prompt calls
  for retrofitting SecurityEvent into C4's vault/event sink once C4
  exists, and role/permission changes into F4's real edit-history
  service once F4 exists. Until then both log locally in this schema.
"""

from __future__ import annotations

import sqlite3

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    role_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    description  TEXT,
    is_system    INTEGER NOT NULL DEFAULT 0,  -- seeded role; still editable/deactivatable
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    permission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,   -- action-level, e.g. "users.create"
    module        TEXT    NOT NULL,          -- which module/screen this action belongs to
    description   TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id       INTEGER NOT NULL REFERENCES roles(role_id),
    permission_id INTEGER NOT NULL REFERENCES permissions(permission_id),
    PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    NOT NULL UNIQUE,
    display_name   TEXT    NOT NULL,
    email          TEXT,
    password_salt  TEXT    NOT NULL,
    password_hash  TEXT    NOT NULL,
    role_id        INTEGER NOT NULL REFERENCES roles(role_id),
    -- ACCESS role ('admin' | 'user'): the data-visibility axis, distinct from
    -- role_id, which is F1's screen/permission role. 'admin' sees every client
    -- firm-wide; 'user' sees only the clients it owns. Derived from the role
    -- name (Admin/Partner -> admin) and kept in sync on every role change.
    role           TEXT    NOT NULL DEFAULT 'user',
    is_active      INTEGER NOT NULL DEFAULT 1,
    is_end_client  INTEGER NOT NULL DEFAULT 0,  -- shared End-Client login flag (F2 retrofit target)
    end_client_id  INTEGER,  -- which client (end_clients.client_id) this shared login belongs to
    -- Set when a password was issued by anyone other than its owner (seeded,
    -- or reset by an Admin). The app blocks every other screen until it is
    -- replaced, so a credential transmitted or displayed once can't stay live.
    must_change_password INTEGER NOT NULL DEFAULT 0,
    -- Brute-force state, reset by any successful sign-in.
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until   TEXT,
    created_at     TEXT    NOT NULL,
    last_login_at  TEXT
);

-- NOTE: idx_users_role is created in _migrate_users() below, not here: on a
-- pre-existing users table "CREATE INDEX IF NOT EXISTS ... (role)" would fail
-- on the column the ALTER has not added yet.

-- Per-user exceptions on top of role defaults. effect: 'grant' adds a
-- permission the role doesn't have; 'revoke' removes one the role does.
CREATE TABLE IF NOT EXISTS user_permission_overrides (
    user_id       INTEGER NOT NULL REFERENCES users(user_id),
    permission_id INTEGER NOT NULL REFERENCES permissions(permission_id),
    effect        TEXT    NOT NULL CHECK (effect IN ('grant', 'revoke')),
    reason        TEXT,
    granted_by    TEXT,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (user_id, permission_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_token TEXT    PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(user_id),
    created_at    TEXT    NOT NULL,
    expires_at    TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    is_revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Which staff work on which End-Client. Scopes default visibility
-- platform-wide (e.g. the client selector in the sidebar).
CREATE TABLE IF NOT EXISTS per_client_team_assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    client_id    INTEGER NOT NULL,  -- end_clients.client_id (no FK: auth boots before F2)
    assigned_by  TEXT,
    created_at   TEXT    NOT NULL,
    UNIQUE (user_id, client_id)
);

CREATE INDEX IF NOT EXISTS idx_team_client ON per_client_team_assignments(client_id);

-- SecurityEvent stub. Failed logins and permission escalations only for
-- this build; retrofit to C4's vault/event sink once C4 exists.
CREATE TABLE IF NOT EXISTS security_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT    NOT NULL,   -- 'login_failed' | 'login_success' | 'permission_denied' | 'escalation'
    username    TEXT,
    user_id     INTEGER,
    detail      TEXT,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_security_events_created ON security_events(created_at);

-- Local change log for role/permission/role-assignment changes. Retrofit
-- to F4's Universal Edit & Version History once F4 exists (see F1's
-- prompt: "F1: role changes, permission grants/revokes" retrofit item).
CREATE TABLE IF NOT EXISTS auth_change_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type   TEXT    NOT NULL,  -- 'role' | 'user_role' | 'role_permission' | 'user_override'
    record_ref    TEXT    NOT NULL,  -- e.g. role name or username
    field         TEXT,
    old_value     TEXT,
    new_value     TEXT,
    reason        TEXT,
    changed_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);
"""


_NEW_USER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("role", "TEXT NOT NULL DEFAULT 'user'"),
    ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    ("failed_login_count", "INTEGER NOT NULL DEFAULT 0"),
    ("locked_until", "TEXT"),
)

# F1 role names that carry firm-wide (admin) data visibility.
_ADMIN_ROLE_NAMES = ("Admin", "Partner")


def _migrate_users(conn: sqlite3.Connection) -> None:
    """Bring a pre-silo database onto the ownership model.

    ``CREATE TABLE IF NOT EXISTS`` never adds a column to an existing table,
    so each new column is ALTERed in individually (the established pattern in
    this repo). Every existing user is then given an explicit access role
    derived from its F1 role, so no user is left with an unknown-visibility
    default.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for name, decl in _NEW_USER_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")

    placeholders = ", ".join("?" for _ in _ADMIN_ROLE_NAMES)
    # Re-derived on EVERY boot rather than only where the column happens to be
    # blank: ALTER TABLE ... NOT NULL DEFAULT 'user' fills existing rows with
    # 'user' (so a blank-check would never fire), and the access role is by
    # definition a projection of the F1 role. Re-deriving is what keeps a
    # promotion or demotion from leaving a stale value behind.
    conn.execute(
        f"""
        UPDATE users
           SET role = CASE
                 WHEN is_end_client = 1 THEN 'user'
                 WHEN role_id IN (SELECT role_id FROM roles WHERE name IN ({placeholders}))
                 THEN 'admin' ELSE 'user'
               END
        """,
        _ADMIN_ROLE_NAMES,
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
    conn.commit()


def init_auth_schema(conn: sqlite3.Connection) -> None:
    """Create auth tables if missing, then migrate an older database onto the
    access-role model. Idempotent."""
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
    _migrate_users(conn)
