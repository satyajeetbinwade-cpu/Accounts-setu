"""C3 — System Settings (base).

Left-nav hub with five sections + a read-only Locale row. Uses only Foundation
components. The AI Ingestion section is C3-ext (step 4) and is marked as such.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.settings_state import SettingsState
from setu.views import shell

_SECTIONS = [
    "Firm Profile",
    "Notifications",
    "Connections",
    "AI Ingestion",
    "Onboarding Defaults",
    "Data Retention",
]


def _nav_item(label: str) -> rx.Component:
    active = SettingsState.section == label
    return rx.button(
        label,
        on_click=SettingsState.set_section(label),
        variant="ghost",
        size="2",
        width="100%",
        justify="start",
        background=rx.cond(active, "#EAF0FB", "transparent"),
        color=rx.cond(active, t.Color.ACCENT.value, t.Color.TEXT_PRIMARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_left=rx.cond(active, f"3px solid {t.Color.ACCENT.value}", "3px solid transparent"),
        border_radius="8px",
        _hover={"background": "#F1F4FA"},
    )


def _field(label: str, value, on_change, *, disabled=None, placeholder: str = "", type_: str = "text") -> rx.Component:
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.input(
            value=value,
            on_change=on_change,
            placeholder=placeholder,
            type=type_,
            disabled=disabled,
            width="100%",
        ),
        spacing="1",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Firm Profile
# ---------------------------------------------------------------------------


def _firm_profile() -> rx.Component:
    return rx.vstack(
        rx.text("Firm profile", style=t.TEXT["card_title"]),
        rx.text("Branding and identity for client-facing surfaces and exported documents.", style=t.TEXT["label"]),
        rx.grid(
            _field("Firm name", SettingsState.firm_name, SettingsState.set_firm_name, disabled=~SettingsState.can_manage),
            _field("Firm PAN (optional)", SettingsState.firm_pan, SettingsState.set_firm_pan, disabled=~SettingsState.can_manage),
            columns="2",
            spacing="4",
            width="100%",
        ),
        rx.button(
            "Save firm profile",
            on_click=SettingsState.save_firm_profile,
            disabled=~SettingsState.can_manage,
            background=t.Color.ACCENT.value,
            color="#FFFFFF",
        ),
        spacing="4",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Notifications (admin)
# ---------------------------------------------------------------------------


def _notif_row(p) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(p.label, font_size="13px", font_weight="600"),
            rx.text(p.kind, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.vstack(
            rx.text("Default on", style=t.TEXT["micro"]),
            rx.switch(
                checked=p.firm_default,
                on_change=lambda v: SettingsState.set_firm_default(p.pref_key, v),
                disabled=~SettingsState.can_manage_notifications,
                color_scheme="indigo",
            ),
            spacing="1",
            align="center",
        ),
        rx.vstack(
            rx.text("Firm-mandatory", style=t.TEXT["micro"]),
            rx.switch(
                checked=p.firm_mandatory,
                on_change=lambda v: SettingsState.set_firm_mandatory(p.pref_key, v),
                disabled=~SettingsState.can_manage_notifications,
                color_scheme="indigo",
            ),
            spacing="1",
            align="center",
        ),
        width="100%",
        align="center",
        spacing="5",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _notifications() -> rx.Component:
    return rx.vstack(
        rx.text("Notification preferences", style=t.TEXT["card_title"]),
        rx.text(
            "Platform-wide defaults. Individually overridable except where the firm marks a notification compulsory.",
            style=t.TEXT["label"],
        ),
        rx.cond(
            SettingsState.notif_prefs.length() > 0,
            rx.vstack(rx.foreach(SettingsState.notif_prefs, _notif_row), spacing="0", width="100%"),
            c.empty_state("No notification preferences defined.", icon="bell"),
        ),
        spacing="4",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


def _connection_row(conn) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(conn.service, font_size="13px", font_weight="600"),
            rx.text(conn.masked_ref, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        c.live_status_chip(conn.status),
        rx.cond(
            SettingsState.can_manage_connections,
            rx.icon_button(
                rx.icon("x", size=14),
                variant="ghost",
                color_scheme="gray",
                size="1",
                on_click=SettingsState.remove_connection(conn.connection_id),
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _connections() -> rx.Component:
    return rx.vstack(
        rx.text("Credential / API connections", style=t.TEXT["card_title"]),
        rx.text(
            "Secrets are entered here and handed off to the Security & Credential Vault (C4) for real encrypted "
            "storage — only a masked reference and live status are shown here; the secret itself is never persisted by Settings.",
            style=t.TEXT["label"],
        ),
        rx.cond(
            SettingsState.connections.length() > 0,
            rx.vstack(rx.foreach(SettingsState.connections, _connection_row), spacing="0", width="100%"),
            c.empty_state("No connections configured yet.", icon="plug"),
        ),
        rx.cond(
            SettingsState.can_manage_connections,
            c.card(
                rx.vstack(
                    rx.text("+ Add connection", style=t.TEXT["card_title"]),
                    rx.cond(SettingsState.conn_error != "", c.inline_reason(SettingsState.conn_error), rx.fragment()),
                    rx.grid(
                        _field("Service", SettingsState.conn_service, SettingsState.set_conn_service),
                        _field(
                            "Secret / API key",
                            SettingsState.conn_secret,
                            SettingsState.set_conn_secret,
                            type_="password",
                        ),
                        columns="2",
                        spacing="4",
                        width="100%",
                    ),
                    rx.button(
                        "Add connection",
                        on_click=SettingsState.add_connection,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        spacing="4",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# AI Ingestion (C3-ext — not yet migrated)
# ---------------------------------------------------------------------------


def _ai_ingestion() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(c.placeholder_badge("Module C3-ext"), rx.spacer(), width="100%"),
            rx.text(
                "AI model and provider configuration (per-touchpoint routing, masked API keys, learned file shapes) "
                "moves here once C3-ext is migrated.",
                style=t.TEXT["body"],
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Onboarding defaults
# ---------------------------------------------------------------------------


def _onboarding_defaults() -> rx.Component:
    return rx.vstack(
        rx.text("Onboarding defaults", style=t.TEXT["card_title"]),
        rx.text("Applied automatically when a new End-Client is created (F2).", style=t.TEXT["label"]),
        rx.grid(
            _field(
                "Default branch status for new clients",
                SettingsState.ob_status,
                SettingsState.set_ob_status,
                disabled=~SettingsState.can_manage,
            ),
            _field(
                "Default assigned team for new clients",
                SettingsState.ob_team,
                SettingsState.set_ob_team,
                disabled=~SettingsState.can_manage,
            ),
            columns="2",
            spacing="4",
            width="100%",
        ),
        rx.button(
            "Save onboarding defaults",
            on_click=SettingsState.save_onboarding_defaults,
            disabled=~SettingsState.can_manage,
            background=t.Color.ACCENT.value,
            color="#FFFFFF",
        ),
        spacing="4",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Data retention
# ---------------------------------------------------------------------------


def _retention() -> rx.Component:
    return rx.vstack(
        rx.text("Data retention", style=t.TEXT["card_title"]),
        rx.text(
            "Platform-wide retention default, feeding the Security & Credential Vault (C4).",
            style=t.TEXT["label"],
        ),
        _field(
            "Retention period (years)",
            SettingsState.retention_years,
            SettingsState.set_retention_years,
            disabled=~SettingsState.can_manage,
            placeholder="Leave blank for no change",
        ),
        rx.button(
            "Save retention setting",
            on_click=SettingsState.save_retention,
            disabled=~SettingsState.can_manage,
            background=t.Color.ACCENT.value,
            color="#FFFFFF",
        ),
        rx.text(
            "Exception: the Document & Data Repository (F3) retains documents indefinitely, layered on top of this default.",
            style=t.TEXT["micro"],
        ),
        spacing="4",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def settings_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("System Settings", "The platform's own control panel."),
            rx.cond(SettingsState.flash != "", c.info_banner(SettingsState.flash), rx.fragment()),
            rx.grid(
                c.card(
                    rx.vstack(
                        *[_nav_item(label) for label in _SECTIONS],
                        spacing="1",
                        width="100%",
                    ),
                    width="100%",
                ),
                c.card(
                    rx.match(
                        SettingsState.section,
                        ("Firm Profile", _firm_profile()),
                        ("Notifications", _notifications()),
                        ("Connections", _connections()),
                        ("AI Ingestion", _ai_ingestion()),
                        ("Onboarding Defaults", _onboarding_defaults()),
                        ("Data Retention", _retention()),
                        _firm_profile(),
                    ),
                    width="100%",
                ),
                columns="1fr 3fr",
                spacing="4",
                width="100%",
            ),
            rx.text(SettingsState.locale_text, style=t.TEXT["micro"]),
            spacing="5",
            width="100%",
            align="start",
        )
    )