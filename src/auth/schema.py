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
    is_active      INTEGER NOT NULL DEFAULT 1,
    is_end_client  INTEGER NOT NULL DEFAULT 0,  -- shared End-Client login flag (F2 retrofit target)
    end_client_ref TEXT,                        -- which client this shared login belongs to, once F2 exists
    created_at     TEXT    NOT NULL,
    last_login_at  TEXT
);

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
    client       TEXT    NOT NULL,
    assigned_by  TEXT,
    created_at   TEXT    NOT NULL,
    UNIQUE (user_id, client)
);

CREATE INDEX IF NOT EXISTS idx_team_client ON per_client_team_assignments(client);

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


def init_auth_schema(conn: sqlite3.Connection) -> None:
    """Create auth tables if missing. Idempotent."""
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
