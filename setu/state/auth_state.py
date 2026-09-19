"""Navigation + session state.

Owns the signed-in user's identity/permission view of the app: which areas and
screens they may see, the current area, and the sign-in/sign-out actions.

Business logic is NOT written here — every handler calls ``src.auth.service``
(the F1 public API), exactly as the Streamlit login gate did.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from setu.routes import AREAS, Screen

# Session token storage key. Stored in a Cookie so a browser refresh keeps the
# user signed in (Reflex reconnects the client by cookie rather than replaying
# server-side session_state, unlike Streamlit).
SESSION_COOKIE = "setu_session"


@dataclass
class NavScreen:
    """A single sidebar link, typed so it can be iterated over in a foreach."""

    label: str
    route: str
    icon: str


@dataclass
class NavArea:
    """A nav area and its visible screens."""

    area: str
    screens: list[NavScreen]


class AuthState(rx.State):
    """Authentication, session lifecycle and permission projection."""

    # --- session ---------------------------------------------------------
    session_token: str = rx.Cookie(name=SESSION_COOKIE, max_age=60 * 60 * 8)

    # --- current user (projected from the DB on load / after login) ------
    user_id: int = 0
    username: str = ""
    display_name: str = ""
    role_name: str = ""
    is_end_client: bool = False
    last_login_at: str = ""

    # --- login form ------------------------------------------------------
    login_username: str = ""
    login_password: str = ""
    login_error: str = ""
    failed_attempts: int = 0
    signed_out_inactivity: bool = False

    # --- derived ---------------------------------------------------------
    @rx.var
    def is_authenticated(self) -> bool:
        return self.user_id != 0

    @rx.var
    def is_admin(self) -> bool:
        return self.role_name == "Admin"

    @rx.var
    def is_partner(self) -> bool:
        return self.role_name == "Partner"

    @rx.var
    def is_manager_or_above(self) -> bool:
        return self.role_name in ("Admin", "Partner", "Manager")

    @rx.var
    def initials(self) -> str:
        name = self.display_name or self.username or "?"
        parts = [p for p in name.replace(".", " ").split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return name[:2].upper()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _project_user(self) -> None:
        """Refresh the cached user fields from the session token. Clears them
        if the session has expired/been revoked."""
        user = auth.current_user(self.session_token or None)
        if user is None:
            self.user_id = 0
            self.username = ""
            self.display_name = ""
            self.role_name = ""
            self.is_end_client = False
            self.last_login_at = ""
            return
        self.user_id = user["user_id"]
        self.username = user["username"]
        self.display_name = user["display_name"]
        self.role_name = user["role_name"]
        self.is_end_client = bool(user.get("is_end_client"))
        self.last_login_at = user.get("last_login_at") or ""

    def _permission_codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        if user is None:
            return set()
        return auth.effective_permissions(user)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @rx.event
    def check_session(self):
        """Runs on page load: restore the user from the cookie, if valid.

        If the session expired since the last visit, surface the explicit
        inactivity message (never a silent redirect) — same contract as
        F1's ``render_login_gate``.
        """
        had_token = bool(self.session_token)
        self._project_user()
        if had_token and self.user_id == 0:
            self.signed_out_inactivity = True

    @rx.event
    def require_auth(self):
        """Gate every protected route.

        Runs before a module's own ``load()`` so a deep link (or a stale
        session) is sent to the sign-in page — never silently deposited on
        the dashboard, which was the bug: an unauthenticated visitor to
        ``/clients`` was redirected to ``/`` (which had no gate) and saw the
        app shell without ever being asked to sign in.
        """
        self._project_user()
        if not self.is_authenticated:
            self.signed_out_inactivity = False
            return rx.redirect("/login")

    def _gate(self, permission: str | None = None) -> str | None:
        """The route to redirect to when access must be refused, else ``None``.

        Unauthenticated → ``/login`` (so the user is asked to sign in rather
        than dumped on the dashboard). Signed in but lacking the permission →
        ``/`` — the F1 contract that hiding a link is not access control, so
        the route itself refuses.
        """
        self._project_user()
        if not self.is_authenticated:
            return "/login"
        if permission and permission not in self._permission_codes():
            return "/"
        return None

    # ------------------------------------------------------------------
    # Dashboard quick access — the key screens, permission-projected so the
    # landing page always offers a visible route to each important feature.
    # ------------------------------------------------------------------
    KEY_SCREENS: list[tuple[str, str, str, str | None]] = [
        ("Clients", "/clients", "building-2", "clients.profile.view"),
        ("Reconcile", "/reconcile", "route", None),
        ("Reconciliation", "/reconciliation", "scale", "module2.view"),
        ("Action Center", "/action-center", "list-checks", "action_center.view"),
        ("Documents", "/documents", "folder-open", "documents.view"),
        ("Smart Ingestion", "/smart-ingestion", "sparkles", "ingestion_ai.upload"),
        ("Rules", "/rules", "book-open", "rules.view"),
        ("Settings", "/settings", "settings", "settings.view"),
    ]

    @rx.var
    def key_screens(self) -> list[NavScreen]:
        codes = self._permission_codes()
        visible: list[NavScreen] = []
        for label, route, icon, permission in self.KEY_SCREENS:
            if permission is None or permission in codes:
                visible.append(NavScreen(label=label, route=route, icon=icon))
        return visible

    # ------------------------------------------------------------------
    # Login / logout
    # ------------------------------------------------------------------
    def set_login_username(self, value: str):
        self.login_username = value
        self.login_error = ""

    def set_login_password(self, value: str):
        self.login_password = value
        self.login_error = ""

    # Attempt 4+ shows the "multiple failed attempts" notice; the service
    # layer separately records every failure and escalates at 5.
    FAILED_LOGIN_NOTICE_THRESHOLD = 4

    @rx.var
    def show_failed_login_notice(self) -> bool:
        return self.failed_attempts >= self.FAILED_LOGIN_NOTICE_THRESHOLD

    @rx.event
    def submit_login(self, form_data: dict):
        """Authenticate via the F1 service. Never locks out — every failed
        attempt is logged as a security event by the service layer."""
        username = str(form_data.get("username", "")).strip()
        password = str(form_data.get("password", ""))

        if not username or not password:
            self.login_error = "Enter your username and password."
            return

        try:
            token = auth.login(username, password)
        except auth.AuthError as exc:
            self.failed_attempts += 1
            self.login_error = str(exc) or "Invalid username or password."
            return

        self.session_token = token
        self.failed_attempts = 0
        self.login_error = ""
        self.signed_out_inactivity = False
        self.login_password = ""
        self._project_user()
        return rx.redirect("/")

    @rx.event
    def logout(self):
        if self.session_token:
            try:
                auth.logout(self.session_token)
            except Exception:  # noqa: BLE001 — logout must never fail loudly
                pass
        self.session_token = ""
        self.failed_attempts = 0
        self.login_password = ""
        self._project_user()
        return rx.redirect("/login")

    # ------------------------------------------------------------------
    # Session-timeout warning (retrofit item 5)
    # ------------------------------------------------------------------
    SESSION_WARN_SECONDS = 120

    @rx.var
    def session_seconds_remaining(self) -> int:
        if not self.session_token:
            return -1
        remaining = auth.session_seconds_remaining(self.session_token)
        return int(remaining) if remaining is not None else -1

    @rx.var
    def show_session_warning(self) -> bool:
        return 0 <= self.session_seconds_remaining <= self.SESSION_WARN_SECONDS

    @rx.event
    def extend_session(self):
        if self.session_token:
            auth.extend_session(self.session_token)

    # ------------------------------------------------------------------
    # Permission-gated navigation
    # ------------------------------------------------------------------
    @rx.var
    def nav_areas(self) -> list[NavArea]:
        """The areas/screens this user may see, as typed models for the Var
        system. Mirrors ``_build_nav`` in the Streamlit ``app.py``."""
        codes = self._permission_codes()
        result: list[NavArea] = []
        for area, screens in AREAS.items():
            visible: list[NavScreen] = []
            for screen in screens:
                if self._can_see(screen, codes):
                    visible.append(
                        NavScreen(
                            label=screen.label,
                            route=screen.route,
                            icon=screen.icon,
                        )
                    )
            if visible:
                result.append(NavArea(area=area, screens=visible))
        return result

    def _can_see(self, screen: Screen, codes: set[str]) -> bool:
        if screen.label == "Security":
            # F1 retrofit item 7: Admin (inside Admin console) or Partner.
            return self.role_name in ("Admin", "Partner")
        if screen.label == "Admin":
            return self.is_admin
        if screen.permission is None:
            return True
        return screen.permission in codes