"""F1 UI \u2014 login gate, session-timeout warning, and the User Management
(Identity & Access) console.

render_login_gate() is called first thing in app.py; it returns the
authenticated user dict, or renders a login form and stops the script if
no one is signed in.

render_admin_tab() is the Identity & Access console, visible to Admin
(and, for the read-only security events panel, Partner).

F1 retrofit additions (Phase 2 flow-mapping pass):
- item 6: failed-login notice + increasing backoff delay.
- item 5: session-timeout warning with "stay signed in".
- item 1: two-pane "Clients & Teams" screen (Manager+).
- item 2: inline per-user override controls in the Permission Lookup.
- item 3: visually distinct Deactivate vs Delete on roles.
- item 7: Recent Security Events panel (Admin + Partner).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.ui.theme import (
    render_info_banner,
    render_inline_reason,
    render_placeholder_badge,
    render_view_history,
)

SK_SESSION_TOKEN = "_auth_session_token"

# F1 retrofit item 5: warn this many seconds before the hardcoded
# inactivity timeout fires.
SESSION_WARN_SECONDS = 120

# F1 retrofit item 6: after attempt 4, add an increasing delay (1s, 2s,
# 4s capping at 4s) — friction, not a lockout.
_FAILED_LOGIN_DELAYS = {4: 1.0, 5: 2.0, 6: 4.0, 7: 4.0}
_FAILED_LOGIN_NOTICE_THRESHOLD = 4

# Foundation token class for each security event type's badge.
_EVENT_PILL_CLASS = {
    "login_failed": "danger",
    "login_success": "rule",
    "permission_denied": "danger",
    "escalation": "danger",
    "deactivation": "danger",
}


def _current_user() -> Optional[dict[str, Any]]:
    token = st.session_state.get(SK_SESSION_TOKEN)
    return auth.current_user(token)


# ---------------------------------------------------------------------------
# Login gate
# ---------------------------------------------------------------------------


def render_login_gate() -> dict[str, Any]:
    """Render the login form if not authenticated; otherwise return the
    current user dict. Call once at the very top of app.py."""
    user = _current_user()
    if user is not None:
        return user

    # If we were signed out due to inactivity, show an explicit message —
    # never a silent redirect (retrofit item 5).
    if st.session_state.pop("_signed_out_inactivity", False):
        st.warning("You were signed out due to inactivity.")

    st.title("Setu \u2014 Sign in")
    st.caption("Foundational identity layer (F1). Use your firm-issued credentials.")

    failed_attempts = st.session_state.get("_failed_login_attempts", 0)

    username = st.text_input("Username", key="login_username")
    password = st.text_input("Password", type="password", key="login_password")

    submitting = st.button("Sign in", type="primary", key="login_submit")

    if submitting:
        # F1 retrofit item 6: increasing delay from attempt 4 onward.
        delay = _FAILED_LOGIN_DELAYS.get(failed_attempts + 1, 0.0)
        if delay:
            with st.spinner("Checking credentials\u2026"):
                time.sleep(delay)
        try:
            token = auth.login(username, password)
            st.session_state[SK_SESSION_TOKEN] = token
            st.session_state["_failed_login_attempts"] = 0
            st.rerun()
        except auth.AuthError:
            st.session_state["_failed_login_attempts"] = failed_attempts + 1
            # UI Foundation: generic message in primary text color through
            # attempt 3; danger-coral notice from attempt 4 onward. Functional
            # delay/lockout logic is unchanged.
            st.markdown("Invalid username or password.")
            if st.session_state["_failed_login_attempts"] >= _FAILED_LOGIN_NOTICE_THRESHOLD:
                st.markdown(
                    '<span class="setu-inline-reason">\u26a0\ufe0f '
                    'Multiple failed attempts have been logged.</span>',
                    unsafe_allow_html=True,
                )

    st.info(
        "First run? Default admin login is **admin / ChangeMe123** \u2014 "
        "change this password immediately after signing in."
    )
    st.stop()  # never fall through to the rest of app.py while unauthenticated
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Session timeout warning (retrofit item 5)
# ---------------------------------------------------------------------------


def render_session_timeout_warning() -> None:
    """Call near the top of the authenticated flow. If the session is within
    SESSION_WARN_SECONDS of expiry, show a warning with a "stay signed in"
    action. If it has expired, clear the session and sign out. When
    ignored, the next render loop (via current_user() returning None) drops
    back to the login gate with the explicit inactivity message.

    NOTE (retrofit item 5): preserving in-progress work on timeout is
    explicitly OUT of scope — F1 has no long-running edit screens yet. The
    first module with a long-running edit screen (e.g. Corrective Entry
    Assistant) should adopt a save-before-timeout hook following this
    pattern rather than relying on silent loss.
    """
    token = st.session_state.get(SK_SESSION_TOKEN)
    if not token:
        return

    remaining = auth.session_seconds_remaining(token)
    if remaining is None:
        # Expired or invalid — sign out and redirect with explicit message.
        st.session_state.pop(SK_SESSION_TOKEN, None)
        st.session_state["_signed_out_inactivity"] = True
        st.rerun()

    if remaining <= SESSION_WARN_SECONDS:
        with st.container():
            st.warning(
                f"Your session will expire soon due to inactivity "
                f"(~{int(remaining)}s). Save any work you need to keep."
            )
            if st.button("Stay signed in", type="primary"):
                auth.extend_session(token)
                st.success("Session extended.")
                st.rerun()


def render_logout_control(user: dict[str, Any]) -> None:
    """Small sidebar block: who's signed in, their role, a link to their
    personal notification preferences (C3), and a sign-out button."""
    st.sidebar.markdown(f"**{user['display_name']}**")
    st.sidebar.caption(f"Role: {user['role_name']}")
    with st.sidebar.expander("Account"):
        _render_account_menu(user)
    if st.sidebar.button("Sign out"):
        token = st.session_state.get(SK_SESSION_TOKEN)
        if token:
            auth.logout(token)
        st.session_state.pop(SK_SESSION_TOKEN, None)
        st.session_state["_failed_login_attempts"] = 0
        st.rerun()
    st.sidebar.divider()


def _render_account_menu(user: dict[str, Any]) -> None:
    """The per-user account menu entry point for C3's personal preferences
    screen (My notification preferences)."""
    from src.ui.settings_tab import render_personal_notification_prefs

    render_personal_notification_prefs(user)


def render_security_events_tab(user: dict[str, Any]) -> None:
    """Partner-visible, read-only security events panel (retrofit item 7).

    Admin sees these inside the Identity & Access console's "Security"
    sub-tab; Partner gets this standalone tab. When C4 (Security &
    Credential Vault) is built, swap this panel's data source to C4's
    formal event sink — keep the screen as-is.
    """
    if not (auth.is_admin(user) or user.get("role_name") == "Partner"):
        st.warning("You don't have access to security events.")
        return
    st.subheader("Recent Security Events")
    st.caption(
        "Failed logins, permission escalations, and deactivations, logged locally. "
        "\U0001F512 Retrofit to the Security & Credential Vault (C4) once it exists."
    )
    _render_security_events_list()


# ---------------------------------------------------------------------------
# Admin tab
# ---------------------------------------------------------------------------


def render_admin_tab(user: dict[str, Any]) -> None:
    if not auth.is_admin(user):
        st.warning("Only Admin can access User & Access Management.")
        return

    st.subheader("User & Access Management")
    st.caption(
        "Foundational identity, role, and permission layer (F1). "
        "Every other module authenticates and authorizes against this."
    )

    sub_users, sub_roles, sub_perms, sub_teams, sub_security = st.tabs(
        ["Users", "Roles", "Permission Lookup", "Clients & Teams", "Security"]
    )

    with sub_users:
        _render_users_section(user)
    with sub_roles:
        _render_roles_section(user)
    with sub_perms:
        _render_permission_lookup_section(user)
    with sub_teams:
        _render_teams_section(user)
    with sub_security:
        _render_security_section()


def _render_users_section(actor: dict[str, Any]) -> None:
    st.markdown("#### Users")
    roles = auth.list_roles()
    role_options = {r["role_id"]: r["name"] for r in roles if r["is_active"]}

    users = auth.list_users()
    for u in users:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
            with c1:
                st.markdown(f"**{u['username']}** \u2014 {u['display_name']}")
                if u["is_end_client"]:
                    st.badge("End-Client login", color="violet")
                # F4 — reusable "View History" trigger, inline next to the
                # editable user record.
                render_view_history("user", u["username"], current_user=actor, label="View History")
            with c2:
                st.caption(f"Role: {u['role_name']}")
                st.caption(f"Last login: {u['last_login_at'] or 'never'}")
            with c3:
                new_role_id = st.selectbox(
                    "Change role",
                    list(role_options.keys()),
                    index=list(role_options.keys()).index(u["role_id"]) if u["role_id"] in role_options else 0,
                    format_func=lambda rid: role_options.get(rid, str(rid)),
                    key=f"role_sel_{u['user_id']}",
                    label_visibility="collapsed",
                )
                if new_role_id != u["role_id"]:
                    auth.update_user_role(u["user_id"], new_role_id, actor=actor["username"])
                    st.rerun()
            with c4:
                if u["is_active"]:
                    st.badge("Active", color="green")
                    if u["user_id"] != actor["user_id"] and st.button("Deactivate", key=f"deact_{u['user_id']}"):
                        auth.set_user_active(u["user_id"], False, actor=actor["username"])
                        st.rerun()
                else:
                    st.badge("Inactive", color="gray")
                    if st.button("Reactivate", key=f"react_{u['user_id']}"):
                        auth.set_user_active(u["user_id"], True, actor=actor["username"])
                        st.rerun()

            with st.expander("Permission overrides / reset password"):
                _render_user_overrides(u, actor)
                new_pw = st.text_input(
                    "Reset password", type="password", key=f"pw_{u['user_id']}"
                )
                if st.button("Set new password", key=f"pwbtn_{u['user_id']}") and new_pw:
                    try:
                        auth.reset_user_password(u["user_id"], new_pw, actor=actor["username"])
                        st.success("Password reset.")
                    except auth.AuthError as exc:
                        st.error(str(exc))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.markdown("##### Create new user")

    from src.clients import service as clients_service

    f2_clients = clients_service.list_clients(include_inactive=False)
    client_options = {c["client_id"]: c["legal_name"] for c in f2_clients}

    with st.form("create_user_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            new_username = st.text_input("Username")
            new_display = st.text_input("Display name")
            new_email = st.text_input("Email (optional)")
        with c2:
            new_role_id = st.selectbox(
                "Role", list(role_options.keys()), format_func=lambda rid: role_options.get(rid, str(rid))
            )
            new_password = st.text_input("Temporary password", type="password")
            is_end_client = st.checkbox("This is a shared End-Client login")
            linked_client_id = None
            if is_end_client and client_options:
                linked_client_id = st.selectbox(
                    "Link to client (F2 \u2014 reads contact/GSTIN data at creation time)",
                    list(client_options.keys()), format_func=lambda cid: client_options.get(cid, str(cid)),
                )
            elif is_end_client:
                st.caption("No clients exist yet in Client Profiles \u2014 create one on the **Clients** tab first.")
        create_clicked = st.form_submit_button("Create user", type="primary")

    if create_clicked:
        if not (new_username and new_display and new_password):
            st.error("Username, display name, and password are required.")
        else:
            try:
                if is_end_client and linked_client_id is not None:
                    # F1 retrofit: provisioning now reads F2's own contact
                    # record + GSTIN data instead of a bare stub.
                    contacts = clients_service.list_contacts(linked_client_id)
                    contact_id = contacts[0]["contact_id"] if contacts else None
                    clients_service.provision_end_client_login(
                        linked_client_id, username=new_username, password=new_password,
                        contact_id=contact_id, actor=actor["username"],
                    )
                else:
                    auth.create_user(
                        username=new_username, display_name=new_display,
                        email=new_email or None, password=new_password,
                        role_id=new_role_id, actor=actor["username"],
                        is_end_client=is_end_client,
                    )
                st.success(f"User '{new_username}' created.")
                st.rerun()
            except (auth.AuthError, clients_service.ClientError) as exc:
                st.error(str(exc))

    render_info_banner(
        "\U0001F512 **Client login setup** \u2014 linking to a client above now reads that "
        "client's contact record and GSTIN data from Client Profiles (F2) directly."
    )


def _render_user_overrides(u: dict[str, Any], actor: dict[str, Any]) -> None:
    perms = auth.list_permissions()
    overrides = {o["code"]: o for o in auth.user_overrides(u["user_id"])}

    st.caption("Per-user exceptions on top of the role's default permissions.")
    for p in perms:
        current = overrides.get(p["code"])
        c1, c2, c3 = st.columns([3, 2, 2])
        with c1:
            st.caption(f"{p['code']} \u2014 {p['description']}")
        with c2:
            choice = st.selectbox(
                "Override",
                ["(role default)", "grant", "revoke"],
                index={"(role default)": 0, "grant": 1, "revoke": 2}.get(
                    current["effect"] if current else "(role default)", 0
                ),
                key=f"ov_{u['user_id']}_{p['permission_id']}",
                label_visibility="collapsed",
            )
        with c3:
            if choice == "(role default)":
                if current is not None:
                    auth.clear_user_override(u["user_id"], p["permission_id"], actor=actor["username"])
                    st.rerun()
            elif current is None or current["effect"] != choice:
                auth.set_user_override(
                    u["user_id"], p["permission_id"], choice,
                    reason=None, actor=actor["username"],
                )
                st.rerun()


def _render_roles_section(actor: dict[str, Any]) -> None:
    st.markdown("#### Dynamic Role Management")
    st.caption("Admin can create new roles, edit names/permissions, and deactivate roles.")

    roles = auth.list_roles()
    perms = auth.list_permissions()

    for r in roles:
        assigned = auth.role_assigned_user_count(r["role_id"])
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                new_name = st.text_input("Name", value=r["name"], key=f"rname_{r['role_id']}")
                new_desc = st.text_input("Description", value=r["description"] or "", key=f"rdesc_{r['role_id']}")
                # F4 — reusable "View History" trigger, inline next to the
                # editable role record.
                render_view_history("role", r["name"], current_user=actor, label="View History")
                if (new_name, new_desc) != (r["name"], r["description"]):
                    if st.button("Save", key=f"rsave_{r['role_id']}"):
                        auth.update_role(r["role_id"], name=new_name, description=new_desc, actor=actor["username"])
                        st.rerun()
            with c2:
                if r["is_active"]:
                    st.badge("Active", color="green")
                    # item 3: Deactivate (neutral, always enabled, no confirm).
                    if st.button("Deactivate", key=f"rdeact_{r['role_id']}"):
                        auth.set_role_active(r["role_id"], False, actor=actor["username"])
                        st.rerun()
                else:
                    st.badge("Inactive", color="gray")
                    if st.button("Reactivate", key=f"react_role_{r['role_id']}"):
                        auth.set_role_active(r["role_id"], True, actor=actor["username"])
                        st.rerun()

                # item 3: Delete is destructive + irreversible; disabled with
                # an inline reason when users are assigned.
                blocked = assigned > 0
                delete_key = f"rdel_{r['role_id']}"
                confirm_key = f"_confirm_del_{r['role_id']}"

                if blocked:
                    st.button("Delete", key=delete_key, disabled=True)
                    # Foundation 3.4: disabled-with-inline-reason — the reason
                    # is always-visible danger-coral micro text, never a tooltip.
                    render_inline_reason(
                        f"Can't delete \u2014 {assigned} user(s) assigned. "
                        "Reassign or deactivate instead."
                    )
                elif st.session_state.get(confirm_key, False):
                    st.warning(f"Delete role '{r['name']}'? This is irreversible.")
                    cy, cn = st.columns(2)
                    with cy:
                        if st.button("Yes, delete", type="primary", key=f"rdel_yes_{r['role_id']}"):
                            try:
                                auth.delete_role(r["role_id"], actor=actor["username"])
                                st.session_state.pop(confirm_key, None)
                                st.success(f"Role '{r['name']}' deleted.")
                                st.rerun()
                            except auth.AuthError as exc:
                                st.error(str(exc))
                    with cn:
                        if st.button("Cancel", key=f"rdel_no_{r['role_id']}"):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                else:
                    if st.button("Delete", key=delete_key, type="secondary"):
                        st.session_state[confirm_key] = True
                        st.rerun()

            with st.expander(f"Permissions for {r['name']}"):
                current_codes = auth.role_permission_codes(r["role_id"])
                chosen_ids = []
                for p in perms:
                    checked = st.checkbox(
                        f"{p['code']} \u2014 {p['description']}",
                        value=p["code"] in current_codes,
                        key=f"perm_{r['role_id']}_{p['permission_id']}",
                    )
                    if checked:
                        chosen_ids.append(p["permission_id"])
                if st.button("Save permission set", key=f"rpermsave_{r['role_id']}"):
                    auth.set_role_permissions(r["role_id"], chosen_ids, actor=actor["username"])
                    st.success("Permissions updated.")
                    st.rerun()

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.markdown("##### Create new role")
    with st.form("create_role_form", clear_on_submit=True):
        role_name = st.text_input("Role name")
        role_desc = st.text_input("Description")
        create_role_clicked = st.form_submit_button("Create role", type="primary")
    if create_role_clicked:
        if not role_name:
            st.error("Role name is required.")
        else:
            try:
                auth.create_role(role_name, role_desc, actor=actor["username"])
                st.success(f"Role '{role_name}' created.")
                st.rerun()
            except auth.AuthError as exc:
                st.error(str(exc))


def _render_permission_lookup_section(actor: dict[str, Any]) -> None:
    st.markdown("#### Permission Lookup")
    st.caption("Answers 'who can currently do X' in both directions.")

    direction = st.radio(
        "Look up by",
        ["User/Role \u2192 permissions", "Action/capability \u2192 who holds it"],
        horizontal=True,
    )

    if direction == "User/Role \u2192 permissions":
        roles = auth.list_roles()
        role_id = st.selectbox(
            "Role", [r["role_id"] for r in roles],
            format_func=lambda rid: next(r["name"] for r in roles if r["role_id"] == rid),
        )
        codes = sorted(auth.role_permission_codes(role_id))
        if codes:
            for c in codes:
                st.write(f"\u2022 {c}")
        else:
            st.info("This role has no permissions assigned.")
    else:
        perms = auth.list_permissions()
        code = st.selectbox(
            "Action / capability", [p["code"] for p in perms],
            format_func=lambda c: c,
        )
        holders = auth.permission_holders(code)

        st.write("**Roles that grant this:**")
        if holders["roles"]:
            for role_name in holders["roles"]:
                # Foundation: "N users affected" banner uses the
                # informational-accent tint (not a warning) — it's informational.
                affected = _role_affected_count(role_name)
                st.markdown(
                    f"\u2022 {role_name} "
                    f'<span style="color:var(--setu-text-secondary);font-size:12px;">'
                    f"({affected} user(s) affected)</span>",
                    unsafe_allow_html=True,
                )
        else:
            st.write("\u2014 none")

        st.markdown("**Per-user override (inline):**")
        rows = auth.permission_holder_users(code)
        if not rows:
            st.info("No active users to list.")
        for row in rows:
            _render_inline_override_row(row, actor)

        render_info_banner(
            "Role-wide changes aren't editable here \u2014 go to the "
            "**Roles** tab (Dynamic Role Management) instead."
        )


def _role_affected_count(role_name: str) -> int:
    """Number of active users assigned to a role by name (for the "N users
    affected" hint on the Permission Lookup's role-wide-change routing)."""
    for r in auth.list_roles():
        if r["name"] == role_name:
            return auth.role_assigned_user_count(r["role_id"])
    return 0


def _render_inline_override_row(row: dict[str, Any], actor: dict[str, Any]) -> None:
    """A single user's inline override control (retrofit item 2)."""
    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        badge_color = "green" if row["effective"] else "gray"
        st.markdown(
            f"**{row['username']}** ({row['role_name']}) "
            f"— {'holds' if row['effective'] else 'no access'}",
            unsafe_allow_html=False,
        )
    with c2:
        cur = row["override_effect"]
        choice = st.selectbox(
            "Override",
            ["(role default)", "grant", "revoke"],
            index={"(role default)": 0, "grant": 1, "revoke": 2}.get(cur or "(role default)", 0),
            key=f"lku_{row['user_id']}_{row['permission_id']}",
            label_visibility="collapsed",
        )
    with c3:
        needs_apply = (choice == "(role default)" and cur is not None) or (
            choice != "(role default)" and choice != cur
        )
        if needs_apply:
            if st.button("Apply", key=f"lkb_{row['user_id']}_{row['permission_id']}"):
                if choice == "(role default)":
                    auth.clear_user_override(row["user_id"], row["permission_id"], actor=actor["username"])
                else:
                    auth.set_user_override(
                        row["user_id"], row["permission_id"], choice,
                        reason=None, actor=actor["username"],
                    )
                st.rerun()


def _render_teams_section(actor: dict[str, Any]) -> None:
    from src.ui import discovery

    st.markdown("#### Clients & Teams")
    st.caption(
        "Two-pane assignment: pick clients on the left, manage assigned staff on the right. "
        "Changes take effect immediately, platform-wide."
    )

    try:
        clients = discovery.list_clients()
    except Exception:  # noqa: BLE001
        clients = []

    users = [u for u in auth.list_users() if u["is_active"]]

    if not clients:
        st.info("No clients found under `data/` yet.")
        return

    left, right = st.columns([1, 1])

    with left:
        st.markdown("**End-Clients**")
        search = st.text_input("Search clients", key="team_client_search")
        filtered = [c for c in clients if (not search) or search.lower() in c.lower()]
        st.multiselect(
            "Select clients",
            filtered,
            key="team_selected_clients",
            format_func=lambda c: c,
        )

    with right:
        selected_clients = st.session_state.get("team_selected_clients", [])
        if not selected_clients:
            st.info("Select one or more clients on the left to manage their team.")
        else:
            target = selected_clients[0]
            st.markdown(f"**Team for {target}**")
            current_team = auth.team_for_client(target)

            for m in current_team:
                c1, c2 = st.columns([4, 1])
                with c1:
                    st.write(f"{m['display_name']} ({m['role_name']})")
                with c2:
                    if st.button("\u2715", key=f"rmteam_{target}_{m['user_id']}", help=f"Remove {m['username']} from {target}"):
                        auth.unassign_user_from_client(m["user_id"], target)
                        st.rerun()

            current_ids = {m["user_id"] for m in current_team}
            addable = [u for u in users if u["user_id"] not in current_ids]
            if addable:
                multi_add = st.multiselect(
                    "Add staff", [u["user_id"] for u in addable],
                    format_func=lambda uid: next((u["display_name"] for u in addable if u["user_id"] == uid), str(uid)),
                    key="team_add_users",
                )
                if st.button("Add to team", type="primary", disabled=not multi_add):
                    for uid in multi_add:
                        for client in selected_clients:
                            auth.assign_user_to_client(uid, client, actor=actor["username"])
                    st.success(f"Added {len(multi_add)} staff to {len(selected_clients)} client(s).")
                    st.rerun()
            else:
                st.caption("Every active user is already on this client's team.")


def _render_security_events_list() -> None:
    try:
        events = auth.list_security_events(limit=100)
    except Exception:  # noqa: BLE001
        events = []
    if not events:
        st.info("No security events recorded yet.")
        return

    st.markdown('<div class="setu-card" style="padding:12px 18px;">', unsafe_allow_html=True)
    for e in events:
        # Foundation 2.2/2.3: event in body size (13px), timestamp in
        # micro/meta size (11px), hairline-separated list rows in a card.
        detail = e["detail"] or ""
        st.markdown(
            f'<div style="border-bottom:1px solid var(--setu-border);padding:8px 2px;">'
            f'<span class="setu-token-pill {_EVENT_PILL_CLASS.get(e["event_type"], "placeholder")}">'
            f'{e["event_type"]}</span> '
            f'<span style="color:var(--setu-text-primary);font-size:13px;">'
            f'{e["username"] or "?"} \u2014 {detail}</span>'
            f'<span style="float:right;color:var(--setu-text-secondary);font-size:11px;">'
            f'{e["created_at"]}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)


def _render_security_section() -> None:
    st.markdown("#### Recent Security Events")
    st.caption(
        "Failed logins, permission escalations, and deactivations, logged locally. "
        "\U0001F512 Persisted here for now \u2014 retrofit to the Security & Credential Vault (C4) once it exists."
    )
    _render_security_events_list()

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.markdown("#### Role & Permission Change Log")
    st.caption(
        "Role changes and permission grants/revokes now write real Universal Edit & "
        "Version History (F4) entries \u2014 open any user's or role's **View History** "
        "for the per-record view."
    )
    try:
        changes = auth.list_change_log(limit=100)
    except Exception:  # noqa: BLE001
        changes = []
    if not changes:
        st.info("No changes recorded yet.")
    else:
        dash = "\u2014"
        for c in changes:
            old_val = c["old_value"] or dash
            new_val = c["new_value"] or dash
            st.caption(
                f"{c['created_at']} \u2014 **{c['record_type']}** `{c['record_ref']}` "
                f"{c['field'] or ''}: {old_val} \u2192 {new_val} "
                f"(by {c['changed_by']})"
            )