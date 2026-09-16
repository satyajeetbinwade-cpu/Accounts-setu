"""C3 state — System Settings (base).

Firm profile, notification preferences (firm defaults + per-user overrides),
credential connection status view, onboarding defaults, data retention, locale.
Every handler calls ``src.settings.service``; secrets are never stored here.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.settings import service as settings
from setu.state.auth_state import AuthState


@dataclass
class NotifPrefRow:
    pref_key: str
    label: str
    kind: str
    firm_default: bool
    firm_mandatory: bool


@dataclass
class ConnectionRow:
    connection_id: int
    service: str
    masked_ref: str
    status: str


@dataclass
class PersonalNotifRow:
    pref_key: str
    label: str
    kind: str
    effective: bool
    mandatory: bool
    has_override: bool


class SettingsState(AuthState):
    """System Settings hub state."""

    section: str = "Firm Profile"

    # ---- firm profile ---------------------------------------------------
    firm_name: str = ""
    firm_pan: str = ""
    flash: str = ""

    # ---- notifications (admin) -----------------------------------------
    notif_prefs: list[NotifPrefRow] = []

    # ---- connections ----------------------------------------------------
    connections: list[ConnectionRow] = []
    conn_service: str = ""
    conn_secret: str = ""
    conn_error: str = ""

    # ---- onboarding defaults -------------------------------------------
    ob_status: str = ""
    ob_team: str = ""

    # ---- retention ------------------------------------------------------
    retention_years: str = ""

    # ---- personal notification prefs -----------------------------------
    personal_notifs: list[PersonalNotifRow] = []

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------
    @rx.var
    def can_manage(self) -> bool:
        return "settings.manage" in self._codes()

    @rx.var
    def can_manage_notifications(self) -> bool:
        return "settings.notifications.manage" in self._codes()

    @rx.var
    def can_manage_connections(self) -> bool:
        return "settings.connections.manage" in self._codes()

    @rx.var
    def locale_text(self) -> str:
        loc = settings.LOCALE
        return f"Locale: {loc['country']} ({loc['currency']}, {loc['date_format']}) — not configurable in v1"

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "settings.view" not in self._codes():
            return rx.redirect("/")
        self._load_all()

    def _load_all(self) -> None:
        profile = settings.get_firm_profile()
        self.firm_name = profile.get("firm_name") or ""
        self.firm_pan = profile.get("firm_pan") or ""

        self.notif_prefs = [
            NotifPrefRow(
                pref_key=p["pref_key"],
                label=p["label"],
                kind=p["kind"],
                firm_default=bool(p["firm_default"]),
                firm_mandatory=bool(p["firm_mandatory"]),
            )
            for p in settings.list_notification_prefs()
        ]

        self.connections = [
            ConnectionRow(
                connection_id=c["connection_id"],
                service=c["service"],
                masked_ref=c["masked_ref"],
                status=c["status"],
            )
            for c in settings.list_connections()
        ]

        defaults = settings.get_onboarding_defaults()
        self.ob_status = defaults.get("onboarding_default_status") or ""
        self.ob_team = defaults.get("onboarding_default_assigned_team") or ""
        self.retention_years = settings.get_retention_years() or ""

        self._load_personal_notifs()

    def _load_personal_notifs(self) -> None:
        if not self.user_id:
            self.personal_notifs = []
            return
        state = settings.user_notification_state(self.user_id)
        self.personal_notifs = [
            PersonalNotifRow(
                pref_key=key,
                label=s["label"],
                kind=s["kind"],
                effective=bool(s["effective"]),
                mandatory=bool(s["mandatory"]),
                has_override=s["override"] is not None,
            )
            for key, s in state.items()
        ]

    # ------------------------------------------------------------------
    # Section nav
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""

    # ------------------------------------------------------------------
    # Firm profile
    # ------------------------------------------------------------------
    def set_firm_name(self, v: str):
        self.firm_name = v

    def set_firm_pan(self, v: str):
        self.firm_pan = v

    @rx.event
    def save_firm_profile(self):
        try:
            settings.update_firm_profile(self.firm_name, self.firm_pan or None, actor=self.username)
            self.flash = "Firm profile saved."
        except settings.SettingsError as exc:
            self.flash = str(exc)

    # ------------------------------------------------------------------
    # Notifications (admin)
    # ------------------------------------------------------------------
    @rx.event
    def set_firm_default(self, pref_key: str, enabled: bool):
        settings.set_firm_default(pref_key, enabled, actor=self.username)
        self._load_all()

    @rx.event
    def set_firm_mandatory(self, pref_key: str, mandatory: bool):
        settings.set_firm_mandatory(pref_key, mandatory, actor=self.username)
        self._load_all()

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def set_conn_service(self, v: str):
        self.conn_service = v
        self.conn_error = ""

    def set_conn_secret(self, v: str):
        self.conn_secret = v
        self.conn_error = ""

    @rx.event
    def add_connection(self):
        self.conn_error = ""
        if not self.conn_service.strip() or not self.conn_secret.strip():
            self.conn_error = "Service and secret are both required."
            return
        try:
            settings.add_connection(self.conn_service, self.conn_secret, actor=self.username)
        except settings.SettingsError as exc:
            self.conn_error = str(exc)
            return
        self.flash = f"Connection '{self.conn_service}' added — secret handed to the vault."
        self.conn_service = ""
        self.conn_secret = ""
        self._load_all()

    @rx.event
    def remove_connection(self, connection_id: int):
        settings.remove_connection(connection_id, actor=self.username)
        self._load_all()

    # ------------------------------------------------------------------
    # Onboarding defaults
    # ------------------------------------------------------------------
    def set_ob_status(self, v: str):
        self.ob_status = v

    def set_ob_team(self, v: str):
        self.ob_team = v

    @rx.event
    def save_onboarding_defaults(self):
        settings.update_onboarding_defaults(
            {
                "onboarding_default_status": self.ob_status,
                "onboarding_default_assigned_team": self.ob_team,
            },
            actor=self.username,
        )
        self.flash = "Onboarding defaults saved."

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------
    def set_retention_years(self, v: str):
        self.retention_years = v

    @rx.event
    def save_retention(self):
        try:
            settings.set_retention_years(self.retention_years, actor=self.username)
            self.flash = "Retention setting saved."
        except settings.SettingsError as exc:
            self.flash = str(exc)

    # ------------------------------------------------------------------
    # Personal notification preferences
    # ------------------------------------------------------------------
    @rx.event
    def set_personal_notif(self, pref_key: str, enabled: bool):
        settings.set_user_notification_override(self.user_id, pref_key, enabled)
        self._load_personal_notifs()

    @rx.event
    def reset_personal_notif(self, pref_key: str):
        settings.clear_user_notification_override(self.user_id, pref_key)
        self._load_personal_notifs()