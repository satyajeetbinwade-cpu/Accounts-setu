"""F1 admin console state — Users / Roles / Permission Lookup / Clients & Teams / Security.

The Identity & Access console. Every handler calls the F1 public API
(``src.auth.service``); no auth business logic is written here.

``clients.profile.view``-style permissions are read through the same service
so the console is authoritative about what it shows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import reflex as rx

from src.auth import service as auth
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history


@dataclass
class RoleRow:
    role_id: int
    name: str
    description: str
    is_active: bool
    assigned_users: int


@dataclass
class UserRow:
    user_id: int
    username: str
    display_name: str
    email: str
    role_id: int
    role_name: str
    is_active: bool
    is_end_client: bool
    last_login_at: str


@dataclass
class PermissionOption:
    permission_id: int
    code: str
    description: str


@dataclass
class RoleOption:
    """A role as a selectable option (id + display name + description)."""

    role_id: int
    name: str
    description: str


@dataclass
class OverrideRow:
    """A permission row plus this user's override state, for the inline editor."""

    permission_id: int
    code: str
    description: str
    override_effect: str  # "" (role default) | "grant" | "revoke"


@dataclass
class HolderUserRow:
    user_id: int
    username: str
    display_name: str
    role_name: str
    effective: bool
    override_effect: str
    permission_id: int


@dataclass
class SecurityEventRow:
    event_type: str
    username: str
    detail: str
    created_at: str


def _fmt(ts) -> str:
    if not ts:
        return ""
    return str(ts).replace("T", " ").split(".")[0]


class AdminState(AuthState):
    """Identity & Access console state.

    A substate of ``AuthState`` so it inherits the session token and can name
    the acting user on every write (F4 records who changed what).
    """

    tab: str = "Users"

    # ---- Users ----------------------------------------------------------
    users: list[UserRow] = []
    role_options: list[RoleOption] = []
    all_permission_codes: list[str] = []
    new_username: str = ""
    new_display: str = ""
    new_email: str = ""
    new_password: str = ""
    new_role_id: int = 0
    new_is_end_client: bool = False
    user_error: str = ""
    user_flash: str = ""

    # Per-user expander: the permission override editor for one user.
    overrides_user_id: int = 0
    override_rows: list[OverrideRow] = []

    # ---- Roles ----------------------------------------------------------
    roles: list[RoleRow] = []
    new_role_name: str = ""
    new_role_desc: str = ""
    role_error: str = ""
    role_flash: str = ""
    # Role whose delete confirmation is open (0 = none).
    confirm_delete_role_id: int = 0
    # Role whose permission editor is open (0 = none).
    perms_role_id: int = 0
    role_perm_options: list[OverrideRow] = []

    # ---- Permission Lookup ---------------------------------------------
    lookup_direction: str = "user_to_perms"  # | "perm_to_users"
    lookup_role_id: int = 0
    lookup_role_codes: list[str] = []
    lookup_perm_code: str = ""
    lookup_roles_granting: list[str] = []
    lookup_holder_users: list[HolderUserRow] = []

    # ---- Clients & Teams ------------------------------------------------
    team_clients: list[str] = []
    team_selected_client: str = ""
    team_members: list[HolderUserRow] = []  # reuse: user_id/display_name/role_name
    team_addable: list[HolderUserRow] = []

    # ---- Security -------------------------------------------------------
    security_events: list[SecurityEventRow] = []

    # ---- F4 edit history (the reusable View History component) ----------
    user_history: dict[str, list[HistoryEntry]] = {}
    role_history: dict[str, list[HistoryEntry]] = {}

    # ---- flash/error that should clear on next interaction --------------
    _loaded: bool = False

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        """Populate the console. Called from the page's on_load.

        Gated server-side: a non-Admin who navigates here directly by URL is
        redirected away. The sidebar already hides the link, but per F1's rule
        ("Only Admin creates/deactivates users or changes roles") the route
        itself must refuse — hiding a link is not access control.
        """
        if not self._require_admin():
            return rx.redirect(self._gate("auth.users.manage") or "/")
        self._refresh_all()

    def _require_admin(self) -> bool:
        return self.role_name == "Admin"

    def _refresh_all(self) -> None:
        self._load_users()
        self._load_roles()
        self._load_lookup()
        self._load_teams()
        self._load_security()

    def _load_users(self) -> None:
        self.users = [
            UserRow(
                user_id=u["user_id"],
                username=u["username"],
                display_name=u["display_name"],
                email=u.get("email") or "",
                role_id=u["role_id"],
                role_name=u["role_name"],
                is_active=bool(u["is_active"]),
                is_end_client=bool(u["is_end_client"]),
                last_login_at=_fmt(u.get("last_login_at")) or "never",
            )
            for u in auth.list_users()
        ]
        # F4 — per-user edit history (role changes, permission overrides).
        self.user_history = {
            u.username: load_history("user", u.username) for u in self.users
        }
        self.role_options = [
            RoleOption(role_id=r["role_id"], name=r["name"], description=r.get("description") or "")
            for r in auth.list_roles()
            if r["is_active"]
        ]
        self.all_permission_codes = [p["code"] for p in auth.list_permissions()]
        if self.new_role_id == 0 and self.role_options:
            self.new_role_id = self.role_options[0].role_id

    def _load_roles(self) -> None:
        self.roles = [
            RoleRow(
                role_id=r["role_id"],
                name=r["name"],
                description=r.get("description") or "",
                is_active=bool(r["is_active"]),
                assigned_users=auth.role_assigned_user_count(r["role_id"]),
            )
            for r in auth.list_roles()
        ]
        # F4 — per-role edit history (permission grants/revokes).
        self.role_history = {
            r.name: load_history("role", r.name) for r in self.roles
        }

    def _load_lookup(self) -> None:
        roles = auth.list_roles()
        if self.lookup_role_id == 0 and roles:
            self.lookup_role_id = roles[0]["role_id"]
        if self.lookup_role_id:
            self.lookup_role_codes = sorted(auth.role_permission_codes(self.lookup_role_id))

        perms = auth.list_permissions()
        if not self.lookup_perm_code and perms:
            self.lookup_perm_code = perms[0]["code"]
        if self.lookup_perm_code:
            holders = auth.permission_holders(self.lookup_perm_code)
            self.lookup_roles_granting = holders["roles"]
            self.lookup_holder_users = [
                HolderUserRow(
                    user_id=row["user_id"],
                    username=row["username"],
                    display_name=row["display_name"],
                    role_name=row["role_name"],
                    effective=bool(row["effective"]),
                    override_effect=row.get("override_effect") or "",
                    permission_id=row["permission_id"],
                )
                for row in auth.permission_holder_users(self.lookup_perm_code)
            ]

    def _load_teams(self) -> None:
        from src.ui import discovery

        try:
            self.team_clients = discovery.list_clients()
        except Exception:  # noqa: BLE001
            self.team_clients = []
        if self.team_selected_client:
            self._load_team_members()

    def _load_team_members(self) -> None:
        client = self.team_selected_client
        if not client:
            self.team_members = []
            self.team_addable = []
            return
        team = auth.team_for_client(client)
        self.team_members = [
            HolderUserRow(
                user_id=m["user_id"],
                username=m["username"],
                display_name=m["display_name"],
                role_name=m["role_name"],
                effective=True,
                override_effect="",
                permission_id=0,
            )
            for m in team
        ]
        current_ids = {m.user_id for m in self.team_members}
        self.team_addable = [
            HolderUserRow(
                user_id=u["user_id"],
                username=u["username"],
                display_name=u["display_name"],
                role_name=u["role_name"],
                effective=True,
                override_effect="",
                permission_id=0,
            )
            for u in auth.list_users()
            if u["is_active"] and u["user_id"] not in current_ids
        ]

    def _load_security(self) -> None:
        try:
            events = auth.list_security_events(limit=100)
        except Exception:  # noqa: BLE001
            events = []
        self.security_events = [
            SecurityEventRow(
                event_type=e["event_type"],
                username=e.get("username") or "?",
                detail=e.get("detail") or "",
                created_at=_fmt(e.get("created_at")),
            )
            for e in events
        ]

    # ------------------------------------------------------------------
    # Tab + simple setters
    # ------------------------------------------------------------------
    @rx.event
    def set_tab(self, tab: str):
        self.tab = tab

    def set_new_username(self, v: str):
        self.new_username = v
        self.user_error = ""

    def set_new_display(self, v: str):
        self.new_display = v
        self.user_error = ""

    def set_new_email(self, v: str):
        self.new_email = v

    def set_new_password(self, v: str):
        self.new_password = v
        self.user_error = ""

    def set_new_role_id(self, v: str):
        self.new_role_id = int(v) if v else 0

    def set_new_role_id_int(self, role_id: int):
        self.new_role_id = role_id

    def set_new_is_end_client(self, v: bool):
        self.new_is_end_client = v

    def set_new_role_name(self, v: str):
        self.new_role_name = v
        self.role_error = ""

    def set_new_role_desc(self, v: str):
        self.new_role_desc = v

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    @rx.event
    def create_user(self):
        self.user_error = ""
        self.user_flash = ""
        if not (self.new_username and self.new_display and self.new_password):
            self.user_error = "Username, display name, and password are required."
            return
        actor = self._actor()
        try:
            auth.create_user(
                username=self.new_username,
                display_name=self.new_display,
                email=self.new_email or None,
                password=self.new_password,
                role_id=self.new_role_id,
                actor=actor,
                is_end_client=self.new_is_end_client,
            )
        except auth.AuthError as exc:
            self.user_error = str(exc)
            return
        self.user_flash = f"User '{self.new_username}' created."
        self.new_username = ""
        self.new_display = ""
        self.new_email = ""
        self.new_password = ""
        self.new_is_end_client = False
        self._load_users()
        self._load_lookup()

    @rx.event
    def change_user_role(self, user_id: int, role_id: str):
        rid = int(role_id)
        auth.update_user_role(user_id, rid, actor=self._actor())
        self._load_users()
        self._load_lookup()

    @rx.event
    def change_user_role_by_name(self, username: str, role_name: str):
        """Set a user's role from a role chip (name → id resolved here)."""
        target = next((u for u in auth.list_users() if u["username"] == username), None)
        role = next((r for r in auth.list_roles() if r["name"] == role_name), None)
        if target is None or role is None or target["role_id"] == role["role_id"]:
            return
        auth.update_user_role(target["user_id"], role["role_id"], actor=self._actor())
        self._load_users()
        self._load_lookup()

    @rx.event
    def set_user_active(self, user_id: int, active: bool):
        auth.set_user_active(user_id, active, actor=self._actor())
        self._load_users()
        self._load_lookup()
        self._load_security()

    @rx.event
    def reset_password(self, user_id: int, new_password: str):
        if not new_password:
            return
        try:
            auth.reset_user_password(user_id, new_password, actor=self._actor())
            self.user_flash = "Password reset."
        except auth.AuthError as exc:
            self.user_error = str(exc)

    @rx.event
    def open_overrides(self, user_id: int):
        self.overrides_user_id = 0 if self.overrides_user_id == user_id else user_id
        self._load_overrides()

    def _load_overrides(self) -> None:
        if not self.overrides_user_id:
            self.override_rows = []
            return
        current = {o["code"]: o for o in auth.user_overrides(self.overrides_user_id)}
        self.override_rows = [
            OverrideRow(
                permission_id=p["permission_id"],
                code=p["code"],
                description=p.get("description") or "",
                override_effect=(current[p["code"]]["effect"] if p["code"] in current else ""),
            )
            for p in auth.list_permissions()
        ]

    @rx.event
    def set_override(self, permission_id: int, choice: str):
        effect = "" if choice == "(role default)" else choice
        if effect == "":
            auth.clear_user_override(self.overrides_user_id, permission_id, actor=self._actor())
        else:
            auth.set_user_override(
                self.overrides_user_id, permission_id, effect,
                reason=None, actor=self._actor(),
            )
        self._load_overrides()
        self._load_lookup()

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------
    @rx.event
    def create_role(self):
        self.role_error = ""
        self.role_flash = ""
        if not self.new_role_name:
            self.role_error = "Role name is required."
            return
        try:
            auth.create_role(self.new_role_name, self.new_role_desc, actor=self._actor())
        except auth.AuthError as exc:
            self.role_error = str(exc)
            return
        self.role_flash = f"Role '{self.new_role_name}' created."
        self.new_role_name = self.new_role_desc = ""
        self._load_roles()
        self._load_users()

    @rx.event
    def update_role(self, role_id: int, name: str, description: str):
        try:
            auth.update_role(role_id, name=name, description=description, actor=self._actor())
            self._load_roles()
        except auth.AuthError as exc:
            self.role_error = str(exc)

    @rx.event
    def set_role_active(self, role_id: int, active: bool):
        auth.set_role_active(role_id, active, actor=self._actor())
        self._load_roles()
        self._load_users()

    @rx.event
    def ask_delete_role(self, role_id: int):
        self.confirm_delete_role_id = role_id

    @rx.event
    def cancel_delete_role(self):
        self.confirm_delete_role_id = 0

    @rx.event
    def confirm_delete_role(self, role_id: int):
        try:
            auth.delete_role(role_id, actor=self._actor())
            self.role_flash = "Role deleted."
        except auth.AuthError as exc:
            self.role_error = str(exc)
        self.confirm_delete_role_id = 0
        self._load_roles()
        self._load_users()

    @rx.event
    def open_role_perms(self, role_id: int):
        self.perms_role_id = 0 if self.perms_role_id == role_id else role_id
        self._load_role_perm_options()

    def _load_role_perm_options(self) -> None:
        if not self.perms_role_id:
            self.role_perm_options = []
            return
        current = auth.role_permission_codes(self.perms_role_id)
        self.role_perm_options = [
            OverrideRow(
                permission_id=p["permission_id"],
                code=p["code"],
                description=p.get("description") or "",
                override_effect="grant" if p["code"] in current else "",
            )
            for p in auth.list_permissions()
        ]

    @rx.event
    def toggle_role_perm(self, permission_id: int):
        for row in self.role_perm_options:
            if row.permission_id == permission_id:
                row.override_effect = "" if row.override_effect == "grant" else "grant"

    @rx.event
    def save_role_perms(self):
        chosen = [r.permission_id for r in self.role_perm_options if r.override_effect == "grant"]
        auth.set_role_permissions(self.perms_role_id, chosen, actor=self._actor())
        self.role_flash = "Permissions updated."
        self._load_lookup()

    # ------------------------------------------------------------------
    # Permission Lookup
    # ------------------------------------------------------------------
    def set_lookup_direction(self, v: str):
        # The radio reports the display label; map it back to the internal key.
        self.lookup_direction = (
            "perm_to_users" if "Action" in v else "user_to_perms"
        )
        self._load_lookup()

    @rx.event
    def set_lookup_role_int(self, role_id: int):
        self.lookup_role_id = role_id
        self.lookup_role_codes = sorted(auth.role_permission_codes(self.lookup_role_id))

    @rx.event
    def set_lookup_role(self, role_id: str):
        self.lookup_role_id = int(role_id)
        self.lookup_role_codes = sorted(auth.role_permission_codes(self.lookup_role_id))

    @rx.event
    def set_lookup_perm(self, code: str):
        self.lookup_perm_code = code
        self._load_lookup()

    @rx.event
    def apply_holder_override(self, permission_id: int, user_id: int, choice: str):
        effect = "" if choice == "(role default)" else choice
        if effect == "":
            auth.clear_user_override(user_id, permission_id, actor=self._actor())
        else:
            auth.set_user_override(user_id, permission_id, effect, reason=None, actor=self._actor())
        self._load_lookup()

    # ------------------------------------------------------------------
    # Clients & Teams
    # ------------------------------------------------------------------
    @rx.event
    def select_team_client(self, client: str):
        self.team_selected_client = client
        self._load_team_members()

    @rx.event
    def add_team_member(self, user_id: int):
        if not self.team_selected_client:
            return
        auth.assign_user_to_client(user_id, self.team_selected_client, actor=self._actor())
        self._load_team_members()

    @rx.event
    def remove_team_member(self, user_id: int):
        if not self.team_selected_client:
            return
        auth.unassign_user_from_client(user_id, self.team_selected_client)
        self._load_team_members()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _actor(self) -> str:
        """The signed-in username, for F4's "who changed this" attribution."""
        return self.username or "system"