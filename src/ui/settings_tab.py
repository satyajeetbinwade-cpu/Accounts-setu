"""C3 UI — System Settings hub.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt + C3's
design table §3a):
- Settings hub uses a left-nav structure (Streamlit radio inside the tab)
  with five sections plus a sixth read-only Locale row rendered in muted
  text (not a disabled control — it isn't blocked, just not configurable).
- Credential connection add/edit: standard form; on save the secret field
  disappears (handed to C4) and the row re-renders with a masked reference
  + a live status chip (component 3.7).
- My notification preferences (personal screen): same row list as the
  admin default editor; firm-mandatory rows render disabled with the
  inline reason "Firm-mandatory — set by Admin" (component 3.4).
- Notification default editor (admin): one list inside a single card, no
  card-per-row.
- Retention: standard field + a muted note explaining the F3
  infinite-retention exception, always visible (not a tooltip).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.auth import service as auth
from src.settings import service as settings
from src.ui.ai_settings_tab import render_ai_ingestion_settings
from src.ui.theme import render_inline_reason, render_live_status_chip

_SECTIONS = [
    "Firm Profile",
    "Notifications",
    "Connections",
    "AI Ingestion",
    "Onboarding Defaults",
    "Data Retention",
]


def render_settings_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "settings.view"):
        st.warning("You don't have access to System Settings.")
        return

    can_manage = auth.has_permission(current_user, "settings.manage")

    st.subheader("System Settings")
    st.caption("The platform's own control panel.")

    # Left-nav hub (radio) + content, both inside a card.
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)

    section = st.radio("Settings section", _SECTIONS, horizontal=True, label_visibility="collapsed")

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    if section == "Firm Profile":
        _render_firm_profile(current_user, can_manage)
    elif section == "Notifications":
        _render_notifications_admin(current_user, can_manage)
    elif section == "Connections":
        _render_connections(current_user, can_manage)
    elif section == "AI Ingestion":
        render_ai_ingestion_settings(current_user)
    elif section == "Onboarding Defaults":
        _render_onboarding_defaults(current_user, can_manage)
    elif section == "Data Retention":
        _render_retention(current_user, can_manage)

    st.markdown('</div>', unsafe_allow_html=True)

    # Read-only Locale row — muted text, not a disabled control.
    st.markdown(
        f'<div style="margin-top:12px;color:var(--setu-text-muted);font-size:12px;">'
        f'Locale: {settings.LOCALE["country"]} '
        f'({settings.LOCALE["currency"]}, {settings.LOCALE["date_format"]}) '
        f'\u2014 not configurable in v1</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Firm Profile
# ---------------------------------------------------------------------------


def _render_firm_profile(current_user: dict[str, Any], can_manage: bool) -> None:
    profile = settings.get_firm_profile()
    firm_name = st.text_input("Firm name", value=profile.get("firm_name") or "", disabled=not can_manage)
    firm_pan = st.text_input("Firm PAN (optional)", value=profile.get("firm_pan") or "", disabled=not can_manage)
    if can_manage and st.button("Save firm profile", type="primary"):
        try:
            settings.update_firm_profile(firm_name, firm_pan or None, actor=current_user["username"])
            st.success("Firm profile saved.")
        except settings.SettingsError as exc:
            st.error(str(exc))

    if st.button("Refresh"):
        st.rerun()


# ---------------------------------------------------------------------------
# Notification default editor (admin) — one list inside a single card
# ---------------------------------------------------------------------------


def _render_notifications_admin(current_user: dict[str, Any], can_manage: bool) -> None:
    can_manage_notif = auth.has_permission(current_user, "settings.notifications.manage")
    st.caption(
        "Platform-wide defaults. Individually overridable except where the "
        "firm marks a notification compulsory."
    )

    prefs = settings.list_notification_prefs()
    if not prefs:
        st.info("No notification preferences defined.")
        return

    for p in prefs:
        c1, c2, c3 = st.columns([3, 2, 2])
        with c1:
            st.markdown(f"**{p['label']}**")
            st.caption(p["kind"])
        with c2:
            new_default = st.toggle(
                "Default on", value=bool(p["firm_default"]), key=f"ndef_{p['pref_key']}",
                disabled=not can_manage_notif,
            )
            if can_manage_notif and new_default != bool(p["firm_default"]):
                settings.set_firm_default(p["pref_key"], new_default, actor=current_user["username"])
        with c3:
            new_mandatory = st.toggle(
                "Firm-mandatory", value=bool(p["firm_mandatory"]), key=f"nman_{p['pref_key']}",
                disabled=not can_manage_notif,
            )
            if can_manage_notif and new_mandatory != bool(p["firm_mandatory"]):
                settings.set_firm_mandatory(p["pref_key"], new_mandatory, actor=current_user["username"])


# ---------------------------------------------------------------------------
# Connections — masked reference + live status chip (3.7)
# ---------------------------------------------------------------------------


def _render_connections(current_user: dict[str, Any], can_manage: bool) -> None:
    can_manage_conn = auth.has_permission(current_user, "settings.connections.manage")
    st.caption(
        "Credential / API connections. Secrets are entered here and handed "
        "off to the Security & Credential Vault (C4) for real encrypted "
        "storage \u2014 only a masked reference and live status are shown "
        "here; the secret itself is never persisted by Settings."
    )

    conns = settings.list_connections()
    for c in conns:
        c1, c2, c3 = st.columns([3, 2, 1])
        with c1:
            st.markdown(f"**{c['service']}**")
            st.caption(c["masked_ref"])
        with c2:
            render_live_status_chip(c["status"])
        with c3:
            if can_manage_conn and st.button("\u2715", key=f"conn_rm_{c['connection_id']}"):
                settings.remove_connection(c["connection_id"], actor=current_user["username"])
                st.rerun()

    if can_manage_conn:
        with st.expander("+ Add connection"):
            service = st.text_input("Service", key="conn_service")
            secret = st.text_input("Secret / API key", type="password", key="conn_secret")
            if st.button("Add connection", type="primary", key="conn_add"):
                try:
                    settings.add_connection(service, secret, actor=current_user["username"])
                    st.success(f"Connection '{service}' added \u2014 secret handed to C4 (stub).")
                    st.rerun()
                except settings.SettingsError as exc:
                    st.error(str(exc))


# ---------------------------------------------------------------------------
# Onboarding defaults
# ---------------------------------------------------------------------------


def _render_onboarding_defaults(current_user: dict[str, Any], can_manage: bool) -> None:
    st.caption("Applied automatically when a new End-Client is created (F2).")
    defaults = settings.get_onboarding_defaults()

    status = st.text_input(
        "Default branch status for new clients",
        value=defaults.get("onboarding_default_status") or "",
        disabled=not can_manage,
    )
    assigned_team = st.text_input(
        "Default assigned team for new clients",
        value=defaults.get("onboarding_default_assigned_team") or "",
        disabled=not can_manage,
    )

    if can_manage and st.button("Save onboarding defaults", type="primary"):
        settings.update_onboarding_defaults(
            {
                "onboarding_default_status": status,
                "onboarding_default_assigned_team": assigned_team,
            },
            actor=current_user["username"],
        )
        st.success("Onboarding defaults saved.")


# ---------------------------------------------------------------------------
# Data retention — muted note about the F3 infinite-retention exception
# ---------------------------------------------------------------------------


def _render_retention(current_user: dict[str, Any], can_manage: bool) -> None:
    st.caption("Platform-wide retention default, feeding the Security & Credential Vault (C4).")

    current = settings.get_retention_years()
    value = st.text_input(
        "Retention period (years)", value=current or "", disabled=not can_manage,
        help="Leave blank for no change.",
    )

    if can_manage and st.button("Save retention setting", type="primary"):
        try:
            settings.set_retention_years(value, actor=current_user["username"])
            st.success("Retention setting saved.")
        except settings.SettingsError as exc:
            st.error(str(exc))

    # Muted, always-visible note (not a tooltip) explaining the exception.
    st.markdown(
        '<div style="margin-top:8px;color:var(--setu-text-muted);font-size:12px;">'
        "Exception: the Document &amp; Data Repository (F3) retains documents "
        "indefinitely, layered on top of this default.</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Personal "My notification preferences" — reached from the account menu.
# Reuses render_personal_notification_prefs below.
# ---------------------------------------------------------------------------


def render_personal_notification_prefs(user: dict[str, Any]) -> None:
    """The personal preferences screen. Same row list as the admin editor;
    firm-mandatory rows render locked with an inline reason (3.4)."""
    st.markdown("**My notification preferences**")
    state = settings.user_notification_state(user["user_id"])

    if not state:
        st.info("No notification preferences to configure.")
        return

    for pref_key, s in state.items():
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"**{s['label']}**")
            if s["mandatory"]:
                render_inline_reason("Firm-mandatory \u2014 set by Admin")
        with c2:
            if s["mandatory"]:
                st.toggle(
                    "Enabled", value=s["effective"], key=f"mynotif_{pref_key}", disabled=True,
                )
            else:
                has_override = s["override"] is not None
                col_t, col_r = st.columns([2, 1])
                with col_t:
                    new_val = st.toggle(
                        "Enabled",
                        value=s["effective"],
                        key=f"mynotif_{pref_key}",
                        help="Toggle on/off (sets a personal override).",
                    )
                with col_r:
                    if has_override:
                        if st.button("Reset", key=f"mynotif_reset_{pref_key}", help="Follow the platform default"):
                            settings.clear_user_notification_override(
                                user["user_id"], pref_key,
                            )
                            st.rerun()
                    else:
                        st.caption("Default")
                if new_val != s["effective"]:
                    settings.set_user_notification_override(
                        user["user_id"], pref_key, new_val,
                    )
                    st.rerun()