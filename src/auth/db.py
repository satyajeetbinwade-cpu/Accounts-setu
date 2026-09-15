"""Low-level CRUD for the F1 auth tables. Mirrors src/db.py's style:
functions take a connection, do one thing, and mostly don't commit (the
one exception being session/security-event writes, which are always
independent of any larger transaction — same pattern as src/db.py's
review_state functions).

src/auth/service.py is the only module that should import this one
directly; everything else should go through service.py.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.auth import security

SESSION_LIFETIME_HOURS = 8  # F1: hardcoded DEFAULT \u2014 the LIVE value now comes from C3 (see _session_lifetime_hours below)


def _session_lifetime_hours() -> float:
    """Retrofit (C3 \u2192 F1): read the configured session timeout LIVE from
    C3 instead of F1's hardcoded default. Falls back to
    SESSION_LIFETIME_HOURS if C3 is unavailable or unset. Lazily imported to
    avoid a module-initialisation cycle."""
    try:
        from src.settings import service as settings_service

        return settings_service.get_session_timeout_hours()
    except Exception:  # noqa: BLE001 \u2014 never break session creation over a config read
        return SESSION_LIFETIME_HOURS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def list_roles(conn: sqlite3.Connection, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM roles"
    if not include_inactive:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY name"
    rows = conn.execute(sql).fetchall()
    cols = [d[0] for d in conn.execute(sql).description]
    return [dict(zip(cols, r)) for r in rows]


def get_role(conn: sqlite3.Connection, role_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM roles WHERE role_id = ?", (role_id,)).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM roles WHERE role_id = ?", (role_id,)).description]
    return dict(zip(cols, row))


def get_role_by_name(conn: sqlite3.Connection, name: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM roles WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM roles WHERE name = ?", (name,)).description]
    return dict(zip(cols, row))


def create_role(conn: sqlite3.Connection, name: str, description: str) -> int:
    cur = conn.execute(
        "INSERT INTO roles (name, description, is_system, is_active, created_at) VALUES (?, ?, 0, 1, ?)",
        (name, description, _now()),
    )
    conn.commit()
    return cur.lastrowid


def update_role(conn: sqlite3.Connection, role_id: int, *, name: str, description: str) -> None:
    conn.execute(
        "UPDATE roles SET name = ?, description = ? WHERE role_id = ?",
        (name, description, role_id),
    )
    conn.commit()


def set_role_active(conn: sqlite3.Connection, role_id: int, is_active: bool) -> None:
    conn.execute(
        "UPDATE roles SET is_active = ? WHERE role_id = ?", (int(is_active), role_id)
    )
    conn.commit()


def role_user_count(conn: sqlite3.Connection, role_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role_id = ? AND is_active = 1", (role_id,)
    ).fetchone()[0]


def delete_role(conn: sqlite3.Connection, role_id: int) -> None:
    """Hard-delete a role. Caller (service layer) must confirm no active
    users are assigned to it first — deletion is blocked at that level,
    per F1's business rule."""
    conn.execute("DELETE FROM role_permissions WHERE role_id = ?", (role_id,))
    conn.execute("DELETE FROM roles WHERE role_id = ?", (role_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def list_permissions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM permissions ORDER BY module, code").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM permissions ORDER BY module, code").description]
    return [dict(zip(cols, r)) for r in rows]


def role_permission_codes(conn: sqlite3.Connection, role_id: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT p.code FROM role_permissions rp
        JOIN permissions p ON p.permission_id = rp.permission_id
        WHERE rp.role_id = ?
        """,
        (role_id,),
    ).fetchall()
    return {r[0] for r in rows}


def set_role_permissions(conn: sqlite3.Connection, role_id: int, permission_ids: list[int]) -> None:
    """Replace a role's permission set wholesale."""
    conn.execute("DELETE FROM role_permissions WHERE role_id = ?", (role_id,))
    for pid in permission_ids:
        conn.execute(
            "INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?, ?)",
            (role_id, pid),
        )
    conn.commit()


def permission_holders(conn: sqlite3.Connection, permission_code: str) -> dict[str, list[str]]:
    """Return {'roles': [...], 'users_via_override': [...]} for a permission code."""
    roles = conn.execute(
        """
        SELECT r.name FROM roles r
        JOIN role_permissions rp ON rp.role_id = r.role_id
        JOIN permissions p ON p.permission_id = rp.permission_id
        WHERE p.code = ? AND r.is_active = 1
        ORDER BY r.name
        """,
        (permission_code,),
    ).fetchall()
    override_users = conn.execute(
        """
        SELECT u.username FROM users u
        JOIN user_permission_overrides upo ON upo.user_id = u.user_id
        JOIN permissions p ON p.permission_id = upo.permission_id
        WHERE p.code = ? AND upo.effect = 'grant' AND u.is_active = 1
        ORDER BY u.username
        """,
        (permission_code,),
    ).fetchall()
    return {
        "roles": [r[0] for r in roles],
        "users_via_override": [u[0] for u in override_users],
    }


def permission_holder_users(conn: sqlite3.Connection, permission_code: str) -> list[dict[str, Any]]:
    """Individual active users who currently hold a permission, via role
    default or an override, with their role and current override effect.
    Used by the inline per-user override controls (retrofit item 2).

    Each row: {user_id, username, display_name, role_name,
              via_role (bool), override_effect (None|'grant'|'revoke')}.
    """
    rows = conn.execute(
        """
        SELECT u.user_id, u.username, u.display_name, r.name AS role_name,
               CASE WHEN rp.permission_id IS NOT NULL THEN 1 ELSE 0 END AS via_role,
               upo.effect AS override_effect
        FROM users u
        JOIN roles r ON r.role_id = u.role_id
        LEFT JOIN role_permissions rp
               ON rp.role_id = u.role_id
              AND rp.permission_id = (SELECT permission_id FROM permissions WHERE code = ?)
        LEFT JOIN user_permission_overrides upo
               ON upo.user_id = u.user_id
              AND upo.permission_id = (SELECT permission_id FROM permissions WHERE code = ?)
        WHERE u.is_active = 1
        ORDER BY u.username
        """,
        (permission_code, permission_code),
    ).fetchall()
    cols = ["user_id", "username", "display_name", "role_name", "via_role", "override_effect"]
    return [dict(zip(cols, r)) for r in rows]


def user_override_rows(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.code, p.description, upo.effect, upo.reason, upo.created_at
        FROM user_permission_overrides upo
        JOIN permissions p ON p.permission_id = upo.permission_id
        WHERE upo.user_id = ?
        ORDER BY p.code
        """,
        (user_id,),
    ).fetchall()
    cols = ["code", "description", "effect", "reason", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def set_user_override(
    conn: sqlite3.Connection,
    user_id: int,
    permission_id: int,
    effect: str,
    *,
    reason: Optional[str],
    granted_by: str,
) -> None:
    conn.execute(
        """
        INSERT INTO user_permission_overrides
            (user_id, permission_id, effect, reason, granted_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, permission_id) DO UPDATE SET
            effect = excluded.effect,
            reason = excluded.reason,
            granted_by = excluded.granted_by,
            created_at = excluded.created_at
        """,
        (user_id, permission_id, effect, reason, granted_by, _now()),
    )
    conn.commit()


def clear_user_override(conn: sqlite3.Connection, user_id: int, permission_id: int) -> None:
    conn.execute(
        "DELETE FROM user_permission_overrides WHERE user_id = ? AND permission_id = ?",
        (user_id, permission_id),
    )
    conn.commit()


def effective_permission_codes(conn: sqlite3.Connection, user_id: int, role_id: int) -> set[str]:
    codes = role_permission_codes(conn, role_id)
    rows = conn.execute(
        """
        SELECT p.code, upo.effect FROM user_permission_overrides upo
        JOIN permissions p ON p.permission_id = upo.permission_id
        WHERE upo.user_id = ?
        """,
        (user_id,),
    ).fetchall()
    for code, effect in rows:
        if effect == "grant":
            codes.add(code)
        elif effect == "revoke":
            codes.discard(code)
    return codes


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def list_users(conn: sqlite3.Connection, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = """
        SELECT u.*, r.name AS role_name FROM users u
        JOIN roles r ON r.role_id = u.role_id
    """
    if not include_inactive:
        sql += " WHERE u.is_active = 1"
    sql += " ORDER BY u.username"
    rows = conn.execute(sql).fetchall()
    cols = [d[0] for d in conn.execute(sql).description]
    return [dict(zip(cols, r)) for r in rows]


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT u.*, r.name AS role_name FROM users u
        JOIN roles r ON r.role_id = u.role_id
        WHERE u.username = ?
        """,
        (username,),
    ).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute(
        "SELECT u.*, r.name AS role_name FROM users u JOIN roles r ON r.role_id = u.role_id WHERE u.username = ?",
        (username,),
    ).description]
    return dict(zip(cols, row))


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT u.*, r.name AS role_name FROM users u
        JOIN roles r ON r.role_id = u.role_id
        WHERE u.user_id = ?
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute(
        "SELECT u.*, r.name AS role_name FROM users u JOIN roles r ON r.role_id = u.role_id WHERE u.user_id = ?",
        (user_id,),
    ).description]
    return dict(zip(cols, row))


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    email: Optional[str],
    password: str,
    role_id: int,
    is_end_client: bool = False,
    end_client_ref: Optional[str] = None,
) -> int:
    salt, pw_hash = security.hash_password(password)
    cur = conn.execute(
        """
        INSERT INTO users
            (username, display_name, email, password_salt, password_hash,
             role_id, is_active, is_end_client, end_client_ref, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            username, display_name, email, salt, pw_hash,
            role_id, int(is_end_client), end_client_ref, _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_user_role(conn: sqlite3.Connection, user_id: int, role_id: int) -> None:
    """Forward-only: this changes the CURRENT role. Past audit entries
    that recorded the role held at the time are untouched (see F1's
    business rule)."""
    conn.execute("UPDATE users SET role_id = ? WHERE user_id = ?", (role_id, user_id))
    conn.commit()


def set_user_active(conn: sqlite3.Connection, user_id: int, is_active: bool) -> None:
    conn.execute("UPDATE users SET is_active = ? WHERE user_id = ?", (int(is_active), user_id))
    if not is_active:
        conn.execute(
            "UPDATE sessions SET is_revoked = 1 WHERE user_id = ?", (user_id,)
        )
    conn.commit()


def set_user_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    salt, pw_hash = security.hash_password(password)
    conn.execute(
        "UPDATE users SET password_salt = ?, password_hash = ? WHERE user_id = ?",
        (salt, pw_hash, user_id),
    )
    conn.commit()


def touch_last_login(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET last_login_at = ? WHERE user_id = ?", (_now(), user_id))
    conn.commit()


def role_user_count_for(conn: sqlite3.Connection, role_id: int) -> int:
    """Count ACTIVE users assigned to a role (used by the Delete-vs-Deactivate
    UI to show 'N users assigned')."""
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role_id = ? AND is_active = 1", (role_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = security.new_session_token()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=_session_lifetime_hours())
    conn.execute(
        """
        INSERT INTO sessions (session_token, user_id, created_at, expires_at, last_seen_at, is_revoked)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (token, user_id, now.isoformat(), expires.isoformat(), now.isoformat()),
    )
    conn.commit()
    return token


def get_session(conn: sqlite3.Connection, token: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM sessions WHERE session_token = ?", (token,)).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM sessions WHERE session_token = ?", (token,)
    ).description]
    return dict(zip(cols, row))


def touch_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE session_token = ?", (_now(), token)
    )
    conn.commit()


def revoke_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("UPDATE sessions SET is_revoked = 1 WHERE session_token = ?", (token,))
    conn.commit()


def extend_session(conn: sqlite3.Connection, token: str) -> datetime:
    """Reset the session's expiry to the configured lifetime (LIVE from C3)
    from now and return the new expiry datetime. Used by the "stay signed
    in" action in the session-timeout warning (F1 retrofit item 5)."""
    new_expiry = datetime.now(timezone.utc) + timedelta(hours=_session_lifetime_hours())
    conn.execute(
        "UPDATE sessions SET expires_at = ?, last_seen_at = ? WHERE session_token = ?",
        (new_expiry.isoformat(), _now(), token),
    )
    conn.commit()
    return new_expiry


# ---------------------------------------------------------------------------
# Per-client team assignment
# ---------------------------------------------------------------------------


def assign_user_to_client(conn: sqlite3.Connection, user_id: int, client: str, assigned_by: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO per_client_team_assignments (user_id, client, assigned_by, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, client, assigned_by, _now()),
    )
    conn.commit()


def unassign_user_from_client(conn: sqlite3.Connection, user_id: int, client: str) -> None:
    conn.execute(
        "DELETE FROM per_client_team_assignments WHERE user_id = ? AND client = ?",
        (user_id, client),
    )
    conn.commit()


def clients_for_user(conn: sqlite3.Connection, user_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT client FROM per_client_team_assignments WHERE user_id = ? ORDER BY client",
        (user_id,),
    ).fetchall()
    return [r[0] for r in rows]


def team_for_client(conn: sqlite3.Connection, client: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT u.user_id, u.username, u.display_name, r.name AS role_name
        FROM per_client_team_assignments pcta
        JOIN users u ON u.user_id = pcta.user_id
        JOIN roles r ON r.role_id = u.role_id
        WHERE pcta.client = ?
        ORDER BY u.username
        """,
        (client,),
    ).fetchall()
    cols = ["user_id", "username", "display_name", "role_name"]
    return [dict(zip(cols, r)) for r in rows]


def all_assignments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT pcta.id, u.username, pcta.client, pcta.assigned_by, pcta.created_at
        FROM per_client_team_assignments pcta
        JOIN users u ON u.user_id = pcta.user_id
        ORDER BY pcta.client, u.username
        """
    ).fetchall()
    cols = ["id", "username", "client", "assigned_by", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Security events (local stub — retrofit to C4 once it exists)
# ---------------------------------------------------------------------------


def log_security_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    username: Optional[str] = None,
    user_id: Optional[int] = None,
    detail: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO security_events (event_type, username, user_id, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, username, user_id, detail, _now()),
    )
    conn.commit()


def list_security_events(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (limit,)
    ).description]
    return [dict(zip(cols, r)) for r in rows]


def failed_login_count_recent(conn: sqlite3.Connection, username: str, minutes: int = 15) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM security_events
        WHERE event_type = 'login_failed' AND username = ? AND created_at >= ?
        """,
        (username, cutoff),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Role/permission/role-assignment change capture — real F4 Edit History
# ---------------------------------------------------------------------------
# F4 retrofit: this used to write F1's local `auth_change_log` stub. Per the
# F4 build prompt's retrofit checklist ("F1 — role changes, permission
# grants/revokes. REPLACE the local stub with real F4 entries"), it now
# records a genuine, immutable F4 Edit History entry instead. The signature
# is kept identical so every existing call site is unchanged; the local
# `auth_change_log` table is retained (empty) for backward compatibility but
# is no longer written.


def log_change(
    conn: sqlite3.Connection,
    record_type: str,
    record_ref: str,
    *,
    field: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    reason: Optional[str] = None,
    changed_by: str,
) -> None:
    from src.f4 import service as f4  # local import avoids a hard circular dep

    # Normalize F1's legacy record types into F4's two record scopes so a
    # single View History trigger per record shows everything about it:
    # user-scoped (role changes, overrides, password/active) vs role-scoped.
    f4_record_type = {
        "user_role": "user",
        "user_override": "user",
    }.get(record_type, record_type)  # 'user' | 'role' | 'role_permission'

    f4.record_edit(
        record_type=f4_record_type,
        record_id=record_ref,
        field=field or "record",
        old_value=old_value,
        new_value=new_value,
        reason=reason,
        actor=changed_by,
    )


def list_change_log(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM auth_change_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM auth_change_log ORDER BY id DESC LIMIT ?", (limit,)
    ).description]
    return [dict(zip(cols, r)) for r in rows]
