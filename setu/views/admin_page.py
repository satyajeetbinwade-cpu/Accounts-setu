"""F1 admin console — Identity & Access.

Faithful to ``render_admin_tab`` in the Streamlit build: five sections (Users,
Roles, Permission Lookup, Clients & Teams, Security), Admin-only except the
read-only Security view (Admin + Partner). Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.admin_state import AdminState
from setu.state.auth_state import AuthState
from setu.views import shell

_TABS = ["Users", "Roles", "Permission Lookup", "Clients & Teams", "Security"]


def _tab_button(label: str) -> rx.Component:
    active = AdminState.tab == label
    return rx.button(
        label,
        on_click=AdminState.set_tab(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={
            "background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8"),
            "color": rx.cond(active, "#FFFFFF", t.Color.TEXT_PRIMARY.value),
        },
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def _user_role_chip(u_username, u_role_name, r) -> rx.Component:
    active = u_role_name == r.name
    return rx.button(
        r.name,
        size="1",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="8px",
        on_click=AdminState.change_user_role_by_name(u_username, r.name),
    )


def _user_row(u) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.hstack(
                        rx.text(u.username, style=t.TEXT["card_title"]),
                        rx.text(f"— {u.display_name}", style=t.TEXT["label"]),
                        rx.cond(
                            u.is_end_client,
                            c.pill("End-Client login", variant="accent"),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="center",
                    ),
                    rx.text(
                        f"Role: {u.role_name} · Last login: {u.last_login_at}",
                        style=t.TEXT["micro"],
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.spacer(),
                # F4 — the reusable View History trigger, inline next to the user.
                c.view_history(
                    "View History",
                    AdminState.user_history[u.username],
                    count=AdminState.user_history[u.username].length(),
                ),
                rx.cond(
                    u.is_active,
                    c.pill("Active", variant="rule"),
                    c.pill("Inactive", variant="placeholder"),
                ),
                width="100%",
                align="center",
                spacing="3",
            ),
            # Role chips: the current role is always visible, no dropdown.
            rx.hstack(
                rx.text("Role:", style=t.TEXT["micro"]),
                rx.foreach(
                    AdminState.role_options,
                    lambda r: _user_role_chip(u.username, u.role_name, r),
                ),
                spacing="1",
                wrap="wrap",
                flex="1",
                align="center",
            ),
            rx.hstack(
                rx.spacer(),
                rx.cond(
                    u.is_active,
                    c.deactivate_button(
                        "Deactivate",
                        on_click=AdminState.set_user_active(u.user_id, False),
                        size="1",
                    ),
                    c.deactivate_button(
                        "Reactivate",
                        on_click=AdminState.set_user_active(u.user_id, True),
                        size="1",
                    ),
                ),
                width="100%",
                align="center",
            ),
            spacing="3",
            width="100%",
        ),
    )


def _users_section() -> rx.Component:
    return rx.vstack(
        rx.text("Users", style=t.TEXT["section_title"]),
        rx.text(
            "Only Admin creates, deactivates, or reassigns users. Deactivation is immediate; historic actions stay visible.",
            style=t.TEXT["label"],
        ),
        rx.cond(
            AdminState.user_flash != "",
            c.info_banner(AdminState.user_flash),
            rx.fragment(),
        ),
        rx.vstack(rx.foreach(AdminState.users, _user_row), spacing="3", width="100%"),
        c.divider(),
        rx.text("Create new user", style=t.TEXT["card_title"]),
        rx.cond(
            AdminState.user_error != "",
            c.inline_reason(AdminState.user_error),
            rx.fragment(),
        ),
        c.card(
            rx.grid(
                rx.vstack(
                    rx.text("Username", style=t.TEXT["label"]),
                    rx.input(value=AdminState.new_username, on_change=AdminState.set_new_username, width="100%"),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Display name", style=t.TEXT["label"]),
                    rx.input(value=AdminState.new_display, on_change=AdminState.set_new_display, width="100%"),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Email (optional)", style=t.TEXT["label"]),
                    rx.input(value=AdminState.new_email, on_change=AdminState.set_new_email, width="100%"),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Role", style=t.TEXT["label"]),
                    rx.hstack(
                        rx.foreach(
                            AdminState.role_options,
                            lambda r: rx.button(
                                r.name,
                                size="1",
                                variant="soft",
                                background=rx.cond(
                                    AdminState.new_role_id == r.role_id,
                                    t.Color.ACCENT.value,
                                    "transparent",
                                ),
                                color=rx.cond(
                                    AdminState.new_role_id == r.role_id,
                                    "#FFFFFF",
                                    t.Color.TEXT_SECONDARY.value,
                                ),
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                                on_click=AdminState.set_new_role_id_int(r.role_id),
                            ),
                        ),
                        spacing="1",
                        wrap="wrap",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Temporary password", style=t.TEXT["label"]),
                    rx.input(
                        value=AdminState.new_password,
                        on_change=AdminState.set_new_password,
                        type="password",
                        width="100%",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("End-Client login?", style=t.TEXT["label"]),
                    rx.switch(
                        checked=AdminState.new_is_end_client,
                        on_change=AdminState.set_new_is_end_client,
                        color_scheme="indigo",
                    ),
                    spacing="1",
                    align="start",
                ),
                columns="3",
                spacing="4",
                width="100%",
            ),
            rx.box(height="12px"),
            rx.button(
                "Create user",
                on_click=AdminState.create_user,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
            ),
            width="100%",
        ),
        width="100%",
        spacing="3",
        align="start",
    )


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def _role_row(r) -> rx.Component:
    blocked = r.assigned_users > 0
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text(r.name, style=t.TEXT["card_title"]),
                    rx.text(
                        rx.cond(r.description != "", r.description, "(no description)"),
                        style=t.TEXT["micro"],
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.spacer(),
                # F4 — the reusable View History trigger, inline next to the role.
                c.view_history(
                    "View History",
                    AdminState.role_history[r.name],
                    count=AdminState.role_history[r.name].length(),
                ),
                rx.cond(r.is_active, c.pill("Active", variant="rule"), c.pill("Inactive", variant="placeholder")),
                width="100%",
                align="center",
                spacing="3",
            ),
            rx.hstack(
                rx.button(
                    "Edit permissions",
                    variant="soft",
                    color_scheme="gray",
                    size="1",
                    on_click=AdminState.open_role_perms(r.role_id),
                ),
                rx.spacer(),
                # 3.2 — Deactivate (always enabled) vs Delete (guarded).
                rx.cond(
                    r.is_active,
                    c.deactivate_button("Deactivate", on_click=AdminState.set_role_active(r.role_id, False), size="1"),
                    c.deactivate_button("Reactivate", on_click=AdminState.set_role_active(r.role_id, True), size="1"),
                ),
                c.delete_button(
                    "Delete",
                    on_click=AdminState.ask_delete_role(r.role_id),
                    disabled=blocked,
                    size="1",
                ),
                width="100%",
                align="center",
                spacing="2",
            ),
            # 3.4 — disabled-with-inline-reason for a blocked delete.
            rx.cond(
                blocked,
                c.inline_reason(
                    f"Can't delete — {r.assigned_users} user(s) assigned. Reassign or deactivate instead."
                ),
                rx.fragment(),
            ),
            # Delete confirmation (irreversible).
            rx.cond(
                AdminState.confirm_delete_role_id == r.role_id,
                c.warning_banner(f"Delete role '{r.name}'? This is irreversible."),
                rx.fragment(),
            ),
            rx.cond(
                AdminState.confirm_delete_role_id == r.role_id,
                rx.hstack(
                    rx.button("Yes, delete", color_scheme="red", size="1", on_click=AdminState.confirm_delete_role(r.role_id)),
                    rx.button("Cancel", variant="soft", size="1", on_click=AdminState.cancel_delete_role),
                    spacing="2",
                ),
                rx.fragment(),
            ),
            rx.cond(
                AdminState.perms_role_id == r.role_id,
                rx.vstack(
                    c.divider(),
                    rx.text("Permission set", style=t.TEXT["label"]),
                    rx.vstack(
                        rx.foreach(
                            AdminState.role_perm_options,
                            lambda p: rx.hstack(
                                rx.checkbox(
                                    checked=p.override_effect == "grant",
                                    on_change=lambda _: AdminState.toggle_role_perm(p.permission_id),
                                ),
                                rx.text(p.code, font_size="12px", font_weight="600"),
                                rx.text(f"— {p.description}", style=t.TEXT["micro"]),
                                spacing="2",
                                align="center",
                                width="100%",
                            ),
                        ),
                        spacing="1",
                        width="100%",
                        max_height="260px",
                        overflow_y="auto",
                    ),
                    rx.button("Save permission set", size="1", on_click=AdminState.save_role_perms),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                rx.fragment(),
            ),
            spacing="3",
            width="100%",
        ),
        border=rx.cond(blocked, f"1px solid {t.Color.BORDER.value}", f"1px solid {t.Color.BORDER.value}"),
    )


def _roles_section() -> rx.Component:
    return rx.vstack(
        rx.text("Dynamic Role Management", style=t.TEXT["section_title"]),
        rx.text(
            "Create roles, edit names and permission sets, deactivate. Role deletion is blocked while users are assigned.",
            style=t.TEXT["label"],
        ),
        rx.cond(AdminState.role_flash != "", c.info_banner(AdminState.role_flash), rx.fragment()),
        rx.vstack(rx.foreach(AdminState.roles, _role_row), spacing="3", width="100%"),
        c.divider(),
        rx.text("Create new role", style=t.TEXT["card_title"]),
        rx.cond(AdminState.role_error != "", c.inline_reason(AdminState.role_error), rx.fragment()),
        c.card(
            rx.hstack(
                rx.input(value=AdminState.new_role_name, on_change=AdminState.set_new_role_name, placeholder="Role name", width="220px"),
                rx.input(value=AdminState.new_role_desc, on_change=AdminState.set_new_role_desc, placeholder="Description", flex="1"),
                rx.button("Create role", on_click=AdminState.create_role, background=t.Color.ACCENT.value, color="#FFFFFF"),
                spacing="3",
                width="100%",
                align="center",
            ),
            width="100%",
        ),
        width="100%",
        spacing="3",
        align="start",
    )


# ---------------------------------------------------------------------------
# Permission Lookup
# ---------------------------------------------------------------------------


def _holder_row(row) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.hstack(
                rx.text(row.username, font_size="12px", font_weight="600"),
                rx.text(f"({row.role_name})", style=t.TEXT["micro"]),
                spacing="1",
                align="baseline",
            ),
            rx.text(
                rx.cond(row.effective, "holds (effective)", "no access"),
                style=t.TEXT["micro"],
            ),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.select(
            ["(role default)", "grant", "revoke"],
            value=rx.cond(row.override_effect != "", row.override_effect, "(role default)"),
            on_change=lambda v: AdminState.apply_holder_override(row.permission_id, row.user_id, v),
            size="1",
            width="150px",
        ),
        width="100%",
        align="center",
        padding="6px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _lookup_section() -> rx.Component:
    return rx.vstack(
        rx.text("Permission Lookup", style=t.TEXT["section_title"]),
        rx.text("Answers 'who can currently do X' in both directions.", style=t.TEXT["label"]),
        rx.radio_group(
            ["User/Role → permissions", "Action/capability → who holds it"],
            value=rx.match(
                AdminState.lookup_direction,
                ("user_to_perms", "User/Role → permissions"),
                ("perm_to_users", "Action/capability → who holds it"),
                "User/Role → permissions",
            ),
            on_change=AdminState.set_lookup_direction,
            direction="row",
        ),
        rx.cond(
            AdminState.lookup_direction == "user_to_perms",
            c.card(
                rx.vstack(
                    rx.hstack(
                        rx.text("Role:", style=t.TEXT["micro"]),
                        rx.foreach(
                            AdminState.role_options,
                            lambda r: rx.button(
                                r.name,
                                size="1",
                                variant="soft",
                                background=rx.cond(
                                    AdminState.lookup_role_id == r.role_id,
                                    t.Color.ACCENT.value,
                                    "transparent",
                                ),
                                color=rx.cond(
                                    AdminState.lookup_role_id == r.role_id,
                                    "#FFFFFF",
                                    t.Color.TEXT_SECONDARY.value,
                                ),
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                                on_click=AdminState.set_lookup_role_int(r.role_id),
                            ),
                        ),
                        spacing="2",
                        wrap="wrap",
                        align="center",
                    ),
                    rx.vstack(
                        rx.foreach(
                            AdminState.lookup_role_codes,
                            lambda code: rx.text(f"• {code}", style=t.TEXT["body"]),
                        ),
                        spacing="1",
                        width="100%",
                        max_height="360px",
                        overflow_y="auto",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            c.card(
                rx.vstack(
                    rx.select(
                        AdminState.all_permission_codes,
                        value=AdminState.lookup_perm_code,
                        on_change=AdminState.set_lookup_perm,
                        width="320px",
                    ),
                    rx.text("Roles that grant this:", style=t.TEXT["label"]),
                    rx.hstack(
                        rx.foreach(
                            AdminState.lookup_roles_granting,
                            lambda rn: c.accent_pill(rn),
                        ),
                        spacing="2",
                        wrap="wrap",
                    ),
                    c.divider(),
                    rx.text("Per-user override (inline):", style=t.TEXT["label"]),
                    rx.vstack(
                        rx.foreach(AdminState.lookup_holder_users, _holder_row),
                        spacing="0",
                        width="100%",
                        max_height="320px",
                        overflow_y="auto",
                    ),
                    c.info_banner(
                        "Role-wide changes aren't editable here — go to the Roles section instead."
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
        ),
        width="100%",
        spacing="3",
        align="start",
    )


# ---------------------------------------------------------------------------
# Clients & Teams
# ---------------------------------------------------------------------------


def _team_member(m) -> rx.Component:
    return rx.hstack(
        rx.text(m.display_name, font_size="13px", font_weight="600"),
        rx.text(m.role_name, style=t.TEXT["micro"]),
        rx.spacer(),
        rx.icon_button(
            rx.icon("x", size=14),
            variant="ghost",
            color_scheme="gray",
            size="1",
            on_click=AdminState.remove_team_member(m.user_id),
        ),
        width="100%",
        align="center",
        padding="6px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _team_addable(u) -> rx.Component:
    return rx.button(
        f"+ {u.display_name} ({u.role_name})",
        size="1",
        variant="soft",
        background="transparent",
        color=t.Color.TEXT_PRIMARY.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="8px",
        _hover={"background": "#EEF2F8", "border_color": t.Color.ACCENT.value},
        on_click=AdminState.add_team_member(u.user_id),
    )


def _teams_section() -> rx.Component:
    return rx.vstack(
        rx.text("Clients & Teams", style=t.TEXT["section_title"]),
        rx.text(
            "Assign staff to clients. Changes scope visibility platform-wide and take effect immediately.",
            style=t.TEXT["label"],
        ),
        rx.grid(
            c.card(
                rx.vstack(
                    rx.text("Clients", style=t.TEXT["card_title"]),
                    rx.vstack(
                        rx.foreach(
                            AdminState.team_clients,
                            lambda cl: rx.button(
                                cl,
                                size="1",
                                variant="soft",
                                width="100%",
                                justify="start",
                                background=rx.cond(
                                    AdminState.team_selected_client == cl,
                                    t.Color.ACCENT.value,
                                    "transparent",
                                ),
                                color=rx.cond(
                                    AdminState.team_selected_client == cl,
                                    "#FFFFFF",
                                    t.Color.TEXT_PRIMARY.value,
                                ),
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                                _hover={"background": "#EEF2F8"},
                                on_click=AdminState.select_team_client(cl),
                            ),
                        ),
                        spacing="1",
                        width="100%",
                        max_height="320px",
                        overflow_y="auto",
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            c.card(
                rx.cond(
                    AdminState.team_selected_client != "",
                    rx.vstack(
                        rx.text(f"Team for {AdminState.team_selected_client}", style=t.TEXT["card_title"]),
                        rx.cond(
                            AdminState.team_members.length() > 0,
                            rx.vstack(
                                rx.foreach(AdminState.team_members, _team_member),
                                spacing="0",
                                width="100%",
                            ),
                            c.empty_state("No staff assigned to this client yet.", icon="users"),
                        ),
                        c.divider(),
                        rx.text("Add staff", style=t.TEXT["label"]),
                        rx.hstack(
                            rx.foreach(AdminState.team_addable, _team_addable),
                            spacing="2",
                            wrap="wrap",
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    c.empty_state("Select a client to manage its team.", icon="building-2"),
                ),
                width="100%",
            ),
            columns="2",
            spacing="4",
            width="100%",
        ),
        width="100%",
        spacing="3",
        align="start",
    )


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

_EVENT_PILL = {
    "login_failed": "danger",
    "login_success": "rule",
    "permission_denied": "danger",
    "escalation": "danger",
    "deactivation": "danger",
}


def _event_pill(event_type) -> rx.Component:
    """Security-event pill. The variant must be resolved to a literal string
    per branch (pills take a Python variant), so map the event type with
    rx.match on the *label* and construct each pill explicitly."""
    return rx.match(
        event_type,
        ("login_failed", c.pill("login_failed", variant="danger")),
        ("login_success", c.pill("login_success", variant="rule")),
        ("permission_denied", c.pill("permission_denied", variant="danger")),
        ("escalation", c.pill("escalation", variant="danger")),
        ("deactivation", c.pill("deactivation", variant="danger")),
        c.pill(event_type, variant="placeholder"),
    )


def _event_row(e) -> rx.Component:
    return rx.hstack(
        _event_pill(e.event_type),
        rx.text(
            rx.cond(e.detail != "", f"{e.username} — {e.detail}", e.username),
            font_size="13px",
            color=t.Color.TEXT_PRIMARY.value,
        ),
        rx.spacer(),
        rx.text(e.created_at, style=t.TEXT["micro"], white_space="nowrap"),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 2px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _security_section() -> rx.Component:
    return rx.vstack(
        rx.text("Recent Security Events", style=t.TEXT["section_title"]),
        rx.text(
            "Failed logins, permission escalations, and deactivations — logged locally, persisted to the Credential "
            "Vault (C4) event sink when it is wired.",
            style=t.TEXT["label"],
        ),
        c.card(
            rx.cond(
                AdminState.security_events.length() > 0,
                rx.vstack(
                    rx.foreach(AdminState.security_events, _event_row),
                    spacing="0",
                    width="100%",
                ),
                c.empty_state("No security events recorded yet.", icon="shield"),
            ),
            padding="12px 18px",
            width="100%",
        ),
        width="100%",
        spacing="3",
        align="start",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def admin_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "User & Access Management",
                "Foundational identity, role, and permission layer (F1). Every other module authenticates and authorizes against this.",
            ),
            rx.hstack(
                *[_tab_button(label) for label in _TABS],
                spacing="2",
                wrap="wrap",
                padding="4px",
                background=t.Color.SURFACE.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
            ),
            rx.match(
                AdminState.tab,
                ("Users", _users_section()),
                ("Roles", _roles_section()),
                ("Permission Lookup", _lookup_section()),
                ("Clients & Teams", _teams_section()),
                ("Security", _security_section()),
                _users_section(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )