"""Public API for the F1 auth layer — every other module should import
from here, not from src.auth.db directly.

Covers: DB init + seed, login/session lifecycle, the two-layer permission
model (role defaults + per-user overrides), role/user/team CRUD with the
business rules from the F1 build prompt (role deletion blocked while
users are assigned, forward-only role changes, security events on failed
logins rather than silent lockout, etc).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.auth import db as adb
from src.auth import security
from src.auth.schema import init_auth_schema
from src.auth.seed import run_seed

FAILED_LOGIN_ESCALATION_THRESHOLD = 5  # repeated failures -> escalation event, never a silent lockout


class AuthError(Exception):
    """Raised for expected auth failures (bad credentials, blocked action)."""


def init_auth(db_path=None) -> None:
    """Create auth tables and seed first-run data. Call once at app start,
    alongside src.db.init_db()."""
    conn = recon_db.get_connection(db_path)
    try:
        init_auth_schema(conn)
        run_seed(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# Login / session lifecycle
# ---------------------------------------------------------------------------


def login(username: str, password: str, *, db_path=None) -> str:
    """Authenticate and return a new session token, or raise AuthError.

    Every failed attempt produces a SecurityEvent (never a silent
    lockout); repeated failures also log an 'escalation' event.
    """
    conn = _connect(db_path)
    try:
        user = adb.get_user_by_username(conn, username)
        if user is None or not user["is_active"]:
            adb.log_security_event(conn, "login_failed", username=username, detail="unknown or inactive user")
            raise AuthError("Invalid username or password.")

        if not security.verify_password(password, user["password_salt"], user["password_hash"]):
            adb.log_security_event(
                conn, "login_failed", username=username, user_id=user["user_id"],
                detail="wrong password",
            )
            recent_failures = adb.failed_login_count_recent(conn, username)
            if recent_failures >= FAILED_LOGIN_ESCALATION_THRESHOLD:
                adb.log_security_event(
                    conn, "escalation", username=username, user_id=user["user_id"],
                    detail=f"{recent_failures} failed logins in the last 15 minutes",
                )
            raise AuthError("Invalid username or password.")

        token = adb.create_session(conn, user["user_id"])
        adb.touch_last_login(conn, user["user_id"])
        adb.log_security_event(conn, "login_success", username=username, user_id=user["user_id"])
        return token
    finally:
        conn.close()


def logout(session_token: str, *, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        adb.revoke_session(conn, session_token)
    finally:
        conn.close()


def session_seconds_remaining(session_token: Optional[str], *, db_path=None) -> Optional[float]:
    """Seconds until the session expires, or None if no valid session.
    Used by the front-end session-timeout warning (retrofit item 5)."""
    if not session_token:
        return None
    conn = _connect(db_path)
    try:
        from datetime import datetime, timezone

        sess = adb.get_session(conn, session_token)
        if sess is None or sess["is_revoked"]:
            return None
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(sess["expires_at"])
        remaining = (expires - now).total_seconds()
        return remaining if remaining >= 0 else None
    finally:
        conn.close()


def extend_session(session_token: str, *, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        adb.extend_session(conn, session_token)
    finally:
        conn.close()


def current_user(session_token: Optional[str], *, db_path=None) -> Optional[dict[str, Any]]:
    """Resolve a session token to the user dict, or None if missing/expired/
    revoked/deactivated. Touches last_seen_at on success."""
    if not session_token:
        return None
    conn = _connect(db_path)
    try:
        from datetime import datetime, timezone

        sess = adb.get_session(conn, session_token)
        if sess is None or sess["is_revoked"]:
            return None
        if sess["expires_at"] < datetime.now(timezone.utc).isoformat():
            return None
        user = adb.get_user_by_id(conn, sess["user_id"])
        if user is None or not user["is_active"]:
            return None
        adb.touch_session(conn, session_token)
        return user
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------


def effective_permissions(user: dict[str, Any], *, db_path=None) -> set[str]:
    conn = _connect(db_path)
    try:
        return adb.effective_permission_codes(conn, user["user_id"], user["role_id"])
    finally:
        conn.close()


def has_permission(user: Optional[dict[str, Any]], code: str, *, db_path=None) -> bool:
    """No sensitive action anywhere in the platform should execute without
    this returning True first."""
    if user is None:
        return False
    return code in effective_permissions(user, db_path=db_path)


def require_permission(user: Optional[dict[str, Any]], code: str, *, db_path=None) -> None:
    if not has_permission(user, code, db_path=db_path):
        conn = _connect(db_path)
        try:
            adb.log_security_event(
                conn, "permission_denied",
                username=user.get("username") if user else None,
                user_id=user.get("user_id") if user else None,
                detail=f"denied: {code}",
            )
        finally:
            conn.close()
        raise AuthError(f"You don't have permission to do this ({code}).")


def is_admin(user: Optional[dict[str, Any]]) -> bool:
    return bool(user) and user.get("role_name") == "Admin"


# ---------------------------------------------------------------------------
# Role management
# ---------------------------------------------------------------------------


def list_roles(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.list_roles(conn)
    finally:
        conn.close()


def list_permissions(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.list_permissions(conn)
    finally:
        conn.close()


def role_permission_codes(role_id: int, *, db_path=None) -> set[str]:
    conn = _connect(db_path)
    try:
        return adb.role_permission_codes(conn, role_id)
    finally:
        conn.close()


def permission_holders(code: str, *, db_path=None) -> dict[str, list[str]]:
    conn = _connect(db_path)
    try:
        return adb.permission_holders(conn, code)
    finally:
        conn.close()


def permission_holder_users(code: str, *, db_path=None) -> list[dict[str, Any]]:
    """All active users with their effective hold on a permission, for the
    inline per-user override controls (retrofit item 2).

    Each row gains a computed `effective` bool: holds via role (unless
    revoked) or holds via an explicit grant override. Also includes
    `permission_id` for direct override calls.
    """
    conn = _connect(db_path)
    try:
        rows = adb.permission_holder_users(conn, code)
        perm_id = None
        for p in adb.list_permissions(conn):
            if p["code"] == code:
                perm_id = p["permission_id"]
                break
        for r in rows:
            override = r["override_effect"]
            via_role = bool(r["via_role"])
            r["permission_id"] = perm_id
            r["effective"] = (override == "grant") or (override != "revoke" and via_role)
        return rows
    finally:
        conn.close()


def create_role(name: str, description: str, *, actor: str, db_path=None) -> int:
    conn = _connect(db_path)
    try:
        if adb.get_role_by_name(conn, name) is not None:
            raise AuthError(f"A role named '{name}' already exists.")
        role_id = adb.create_role(conn, name, description)
        adb.log_change(conn, "role", name, field="created", new_value=description, changed_by=actor)
        return role_id
    finally:
        conn.close()


def update_role(role_id: int, *, name: str, description: str, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        before = adb.get_role(conn, role_id)
        adb.update_role(conn, role_id, name=name, description=description)
        adb.log_change(
            conn, "role", name, field="name/description",
            old_value=f"{before['name']} / {before['description']}" if before else None,
            new_value=f"{name} / {description}", changed_by=actor,
        )
    finally:
        conn.close()


def set_role_active(role_id: int, is_active: bool, *, actor: str, db_path=None) -> None:
    """Deactivating a role leaves existing users' access untouched until
    manually reassigned, per F1's business rule — this only flips the
    role's own active flag."""
    conn = _connect(db_path)
    try:
        role = adb.get_role(conn, role_id)
        adb.set_role_active(conn, role_id, is_active)
        adb.log_change(
            conn, "role", role["name"] if role else str(role_id),
            field="is_active", old_value=str(not is_active), new_value=str(is_active),
            changed_by=actor,
        )
    finally:
        conn.close()


def role_assigned_user_count(role_id: int, *, db_path=None) -> int:
    conn = _connect(db_path)
    try:
        return adb.role_user_count_for(conn, role_id)
    finally:
        conn.close()


def delete_role(role_id: int, *, actor: str, db_path=None) -> None:
    """Blocked while any user is currently assigned to this role — the
    caller should deactivate instead. Raises AuthError if blocked."""
    conn = _connect(db_path)
    try:
        count = adb.role_user_count(conn, role_id)
        if count > 0:
            raise AuthError(
                f"Can't delete this role — {count} active user(s) are still assigned to it. "
                "Deactivate it instead, or reassign those users first."
            )
        role = adb.get_role(conn, role_id)
        adb.delete_role(conn, role_id)
        adb.log_change(conn, "role", role["name"] if role else str(role_id), field="deleted", changed_by=actor)
    finally:
        conn.close()


def set_role_permissions(role_id: int, permission_ids: list[int], *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        before = adb.role_permission_codes(conn, role_id)
        adb.set_role_permissions(conn, role_id, permission_ids)
        role = adb.get_role(conn, role_id)
        after_codes = {
            p["code"] for p in adb.list_permissions(conn) if p["permission_id"] in permission_ids
        }
        adb.log_change(
            conn, "role_permission", role["name"] if role else str(role_id),
            field="permissions",
            old_value=", ".join(sorted(before)),
            new_value=", ".join(sorted(after_codes)),
            changed_by=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


def list_users(*, include_inactive: bool = True, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.list_users(conn, include_inactive=include_inactive)
    finally:
        conn.close()


def get_user(user_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.get_user_by_id(conn, user_id)
    finally:
        conn.close()


def create_user(
    *,
    username: str,
    display_name: str,
    email: Optional[str],
    password: str,
    role_id: int,
    actor: str,
    is_end_client: bool = False,
    end_client_ref: Optional[str] = None,
    db_path=None,
) -> int:
    weak = security.validate_password_strength(password)
    if weak:
        raise AuthError(weak)
    conn = _connect(db_path)
    try:
        if adb.get_user_by_username(conn, username) is not None:
            raise AuthError(f"Username '{username}' is already taken.")
        user_id = adb.create_user(
            conn,
            username=username, display_name=display_name, email=email,
            password=password, role_id=role_id,
            is_end_client=is_end_client, end_client_ref=end_client_ref,
        )
        adb.log_change(conn, "user", username, field="created", changed_by=actor)
        return user_id
    finally:
        conn.close()


def update_user_role(user_id: int, new_role_id: int, *, actor: str, db_path=None) -> None:
    """Forward-only role change — see F1's business rule: this never
    rewrites what a past action says about the role held at the time,
    since that's recorded wherever the action itself was logged, not here."""
    conn = _connect(db_path)
    try:
        user = adb.get_user_by_id(conn, user_id)
        old_role = user["role_name"] if user else None
        new_role = adb.get_role(conn, new_role_id)
        adb.update_user_role(conn, user_id, new_role_id)
        adb.log_change(
            conn, "user_role", user["username"] if user else str(user_id),
            field="role", old_value=old_role,
            new_value=new_role["name"] if new_role else str(new_role_id),
            changed_by=actor,
        )
    finally:
        conn.close()


def set_user_active(user_id: int, is_active: bool, *, actor: str, db_path=None) -> None:
    """Deactivation is immediate; historic actions remain fully visible
    wherever audit history is shown (nothing here touches past logs)."""
    conn = _connect(db_path)
    try:
        user = adb.get_user_by_id(conn, user_id)
        adb.set_user_active(conn, user_id, is_active)
        adb.log_change(
            conn, "user", user["username"] if user else str(user_id),
            field="is_active", old_value=str(not is_active), new_value=str(is_active),
            changed_by=actor,
        )
        if not is_active:
            # Deactivation is a security-relevant event (surfaced in the
            # "Recent security events" panel — retrofit item 7).
            adb.log_security_event(
                conn, "deactivation",
                username=user["username"] if user else None,
                user_id=user_id,
                detail=f"deactivated by {actor}",
            )
    finally:
        conn.close()


def reset_user_password(user_id: int, new_password: str, *, actor: str, db_path=None) -> None:
    weak = security.validate_password_strength(new_password)
    if weak:
        raise AuthError(weak)
    conn = _connect(db_path)
    try:
        user = adb.get_user_by_id(conn, user_id)
        adb.set_user_password(conn, user_id, new_password)
        adb.log_change(
            conn, "user", user["username"] if user else str(user_id),
            field="password", new_value="(reset)", changed_by=actor,
        )
    finally:
        conn.close()


def user_overrides(user_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.user_override_rows(conn, user_id)
    finally:
        conn.close()


def set_user_override(
    user_id: int, permission_id: int, effect: str, *, reason: Optional[str], actor: str, db_path=None
) -> None:
    conn = _connect(db_path)
    try:
        adb.set_user_override(conn, user_id, permission_id, effect, reason=reason, granted_by=actor)
        user = adb.get_user_by_id(conn, user_id)
        perm = next((p for p in adb.list_permissions(conn) if p["permission_id"] == permission_id), None)
        adb.log_change(
            conn, "user_override", user["username"] if user else str(user_id),
            field=perm["code"] if perm else str(permission_id),
            new_value=effect, reason=reason, changed_by=actor,
        )
    finally:
        conn.close()


def clear_user_override(user_id: int, permission_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        adb.clear_user_override(conn, user_id, permission_id)
        user = adb.get_user_by_id(conn, user_id)
        adb.log_change(
            conn, "user_override", user["username"] if user else str(user_id),
            field=str(permission_id), new_value="(cleared)", changed_by=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-client team assignment
# ---------------------------------------------------------------------------


def assign_user_to_client(user_id: int, client: str, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        adb.assign_user_to_client(conn, user_id, client, actor)
    finally:
        conn.close()


def unassign_user_from_client(user_id: int, client: str, *, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        adb.unassign_user_from_client(conn, user_id, client)
    finally:
        conn.close()


def clients_for_user(user_id: int, *, db_path=None) -> list[str]:
    conn = _connect(db_path)
    try:
        return adb.clients_for_user(conn, user_id)
    finally:
        conn.close()


def team_for_client(client: str, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.team_for_client(conn, client)
    finally:
        conn.close()


def all_assignments(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.all_assignments(conn)
    finally:
        conn.close()


def visible_clients(user: dict[str, Any], all_clients: list[str], *, db_path=None) -> list[str]:
    """Scope a client list down to what this user should see.

    Admin and Partner see everything firm-wide (Partner-level access is
    an explicit, single-Admin-action grant per the PRD — visibility
    follows from role, not a separate flag in this PoC). Everyone else
    is scoped to their PerClientTeamAssignment rows; if a user has no
    assignments at all, fall back to showing everything rather than
    silently hiding all clients (avoids a confusing empty PoC for
    freshly-seeded roles with no assignments yet).
    """
    if user.get("role_name") in ("Admin", "Partner"):
        return all_clients
    assigned = set(clients_for_user(user["user_id"], db_path=db_path))
    if not assigned:
        return all_clients
    return [c for c in all_clients if c in assigned]


# ---------------------------------------------------------------------------
# Security events / change log (read-only surfacing)
# ---------------------------------------------------------------------------


def list_security_events(limit: int = 200, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.list_security_events(conn, limit=limit)
    finally:
        conn.close()


def list_change_log(limit: int = 200, *, db_path=None) -> list[dict[str, Any]]:
    """F4 retrofit: role/permission changes are now real F4 Edit History
    entries, so this reads F4's cross-cutting log (filtered to F1's own
    record types) and normalizes it to the legacy row shape the existing
    UI still renders."""
    from src.f4 import service as f4  # local import avoids a hard circular dep

    f1_record_types = {"role", "user_role", "role_permission", "user_override", "user"}
    rows = [
        r for r in f4.recent_history(limit=max(limit * 3, limit), db_path=db_path)
        if r["record_type"] in f1_record_types
    ][:limit]
    return [
        {
            "record_type": r["record_type"],
            "record_ref": r["record_id"],
            "field": r["field"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "reason": r["reason"],
            "changed_by": r["changed_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
