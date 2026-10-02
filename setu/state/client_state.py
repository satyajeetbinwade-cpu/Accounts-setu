"""F2 state — Client Profile & Master Data (flat model).

Roster + profile screen (Details / Contacts / Chart of Accounts /
Historical Snapshot / Documents). Every handler calls
``src.clients.service``; no client business logic is written here.

The 27-Sep-2026 revision removed the GSTINBranch sub-entity: one GST
registration = one EndClient row. So there is no branch switcher, no
per-branch status and no "Branch details" tab — the former branch fields
(GSTIN, address, state) are columns on the client itself and are edited in
the single "Details" tab. ``NewClientState`` (below) drives the standalone
``/clients/new`` screen.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.f5 import service as f5
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history

_SYNC_TO_LIVE = {
    "succeeded": ("connected", "Connected"),
    "not_attempted": ("needs_reauth", "Not attempted"),
    "attempted_failed": ("expired", "Failed"),
}
_SOURCE_LABELS = {"tally": "Tally", "gst_portal": "GST Portal", "traces": "TRACES", "bank": "Bank"}

# The five client-level tabs. "Details" replaces the old branch/settings split.
PROFILE_TABS = ["Details", "Contacts", "Chart of Accounts", "Historical Snapshot", "Documents"]
DEFAULT_PROFILE_TAB = "Details"


@dataclass
class ClientRow:
    client_id: int
    legal_name: str
    pan: str
    tan: str
    gstin: str
    assigned_team: str
    is_active: bool


@dataclass
class ContactRow:
    contact_id: int
    name: str
    role_title: str
    email: str
    phone: str
    notes: str


@dataclass
class CoaRow:
    coa_id: int
    code: str
    name: str
    account_type: str


@dataclass
class SnapshotRow:
    snapshot_id: int
    fiscal_year: str
    line_item: str
    amount: str
    notes: str


@dataclass
class SyncChip:
    source_label: str
    status: str  # connected | needs_reauth | expired
    label: str


def _text(row: dict, key: str) -> str:
    """A DB value as display text (NULL → "")."""
    return str(row.get(key) or "")


class ClientState(AuthState):
    """Client roster + profile screen state."""

    # ---- roster ---------------------------------------------------------
    roster: list[ClientRow] = []
    roster_search: str = ""
    show_inactive: bool = False
    flash: str = ""

    # ---- selected profile ----------------------------------------------
    selected_client_id: int = 0
    profile_tab: str = DEFAULT_PROFILE_TAB
    client_name: str = ""
    client_pan: str = ""
    client_tan: str = ""
    client_gstin: str = ""
    client_team: str = ""
    client_email: str = ""
    client_phone: str = ""
    client_address: str = ""
    client_active: bool = True
    # Non-empty when the Reconciliation Engine holds open work for this client,
    # so its GSTIN must not be cleared or changed.
    gstin_gate_reason: str = ""

    sync_chips: list[SyncChip] = []

    contacts: list[ContactRow] = []
    coa: list[CoaRow] = []
    snapshots: list[SnapshotRow] = []
    snapshot_years: list[str] = []
    snapshot_year: str = ""

    # Details tab — edit buffers for the unrestricted fields (one Save).
    d_name: str = ""
    d_team: str = ""
    d_email: str = ""
    d_phone: str = ""
    d_address: str = ""

    # Details tab — identity fields, each with its own reason capture.
    d_pan: str = ""
    d_pan_reason: str = ""
    d_tan: str = ""
    d_tan_reason: str = ""
    d_gstin: str = ""
    d_gstin_reason: str = ""

    # add-contact form
    ac_name: str = ""
    ac_role: str = ""
    ac_email: str = ""
    ac_phone: str = ""
    ac_notes: str = ""

    # coa bulk + add
    coa_bulk: str = ""
    coa_code: str = ""
    coa_name: str = ""
    coa_type: str = ""

    # snapshot bulk + add
    snap_bulk: str = ""
    snap_new_year: str = ""
    snap_item: str = ""
    snap_amount: str = ""
    snap_notes: str = ""

    # F4 — per-record edit history (the reusable View History component).
    client_history: list[HistoryEntry] = []

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------
    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    def _can_manage(self) -> bool:
        return "clients.profile.manage" in self._codes()

    def _can_edit_identity(self) -> bool:
        return "clients.gstin_pan.edit" in self._codes()

    @rx.var
    def can_manage(self) -> bool:
        return self._can_manage()

    @rx.var
    def can_edit_gstin_pan(self) -> bool:
        return self._can_edit_identity()

    @rx.var
    def identity_blocked_reason(self) -> str:
        """Non-empty when PAN/TAN/GSTIN can't be saved at all (role-gated)."""
        if not self._can_edit_identity():
            return "Requires Manager or above to change PAN, TAN or GSTIN."
        return ""

    @rx.var
    def gstin_blocked_reason(self) -> str:
        """Non-empty when the GSTIN specifically can't be cleared/changed."""
        return self.gstin_gate_reason

    @rx.var
    def details_missing(self) -> str:
        """The SERVICE's own validation message for the current Details
        buffers, or "".

        Surfacing the service's message (instead of a second copy of the rules
        in the UI) is what keeps the inline hint and the save gate from ever
        drifting apart.
        """
        try:
            clients.validate_client_record(
                {
                    "legal_name": self.d_name,
                    "pan": self.d_pan,
                    "tan": self.d_tan,
                    "primary_contact_email": self.d_email,
                    "primary_contact_phone": self.d_phone,
                    "primary_contact_address": self.d_address,
                }
            )
        except clients.ClientError as exc:
            return str(exc)
        return ""

    @rx.var
    def details_ready(self) -> bool:
        return self.details_missing == ""

    @rx.var
    def pan_dirty(self) -> bool:
        return self.d_pan != self.client_pan

    @rx.var
    def tan_dirty(self) -> bool:
        return self.d_tan != self.client_tan

    @rx.var
    def gstin_dirty(self) -> bool:
        return self.d_gstin != self.client_gstin

    @rx.var
    def coa_warning(self) -> str:
        return clients.coa_edit_warning()

    @rx.var
    def identity_line(self) -> str:
        """The header's one-line identity summary. GSTIN is deliberately NOT
        here: it is a former branch-level field and must be visible exactly
        once, inside the Details tab."""
        pan = self.client_pan or "—"
        team = self.client_team or "—"
        return f"PAN: {pan} · Assigned team: {team}"

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @rx.event
    def load(self, params: dict | None = None):
        if (deny := self._gate("clients.profile.view")):
            return rx.redirect(deny)
        requested = self._requested_client_id(params)
        if requested:
            # A deep link / post-create redirect (/clients?client=N) opens that
            # client's profile directly.
            self.selected_client_id = requested
            self.profile_tab = DEFAULT_PROFILE_TAB
            self._load_profile()
            return
        # Arriving at /clients WITHOUT a client always lands on the Roster.
        # Opening a client is a state-only transition (no route change — the
        # profile renders on the same route), so a lingering selection made the
        # nav / quick-access "Clients" link a dead end.
        self.selected_client_id = 0
        self._load_roster()

    def _requested_client_id(self, params: dict | None) -> int:
        """The ``?client=N`` param, from the caller's dict when supplied, else
        from this state's own router (the reliable source when the event is
        dispatched without arguments)."""
        url_params = params if isinstance(params, dict) and params else None
        if url_params is None:
            try:
                url_params = dict(self.router.page.params or {})
            except Exception:  # noqa: BLE001
                url_params = {}
        try:
            value = int(str((url_params or {}).get("client") or ""))
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    def _load_roster(self) -> None:
        rows = clients.list_clients(include_inactive=self.show_inactive)
        if self.roster_search:
            s = self.roster_search.lower()
            rows = [
                c
                for c in rows
                if s in _text(c, "legal_name").lower()
                or s in _text(c, "pan").lower()
                or s in _text(c, "tan").lower()
                or s in _text(c, "gstin").lower()
            ]
        self.roster = [
            ClientRow(
                client_id=c["client_id"],
                legal_name=c["legal_name"],
                pan=_text(c, "pan"),
                tan=_text(c, "tan"),
                gstin=_text(c, "gstin"),
                assigned_team=_text(c, "assigned_team"),
                is_active=bool(c["is_active"]),
            )
            for c in rows
        ]

    def _load_profile(self) -> None:
        client = clients.get_client(self.selected_client_id)
        if client is None:
            self.selected_client_id = 0
            return
        self.client_name = client["legal_name"]
        self.client_pan = _text(client, "pan")
        self.client_tan = _text(client, "tan")
        self.client_gstin = _text(client, "gstin")
        self.client_team = _text(client, "assigned_team")
        self.client_email = _text(client, "primary_contact_email")
        self.client_phone = _text(client, "primary_contact_phone")
        self.client_address = _text(client, "primary_contact_address")
        self.client_active = bool(client["is_active"])

        self.d_name = self.client_name
        self.d_team = self.client_team
        self.d_email = self.client_email
        self.d_phone = self.client_phone
        self.d_address = self.client_address
        self.d_pan = self.client_pan
        self.d_tan = self.client_tan
        self.d_gstin = self.client_gstin
        self.d_pan_reason = self.d_tan_reason = self.d_gstin_reason = ""

        self._load_gstin_gate()
        self._load_sync_chips()

        self.contacts = [
            ContactRow(
                contact_id=c["contact_id"],
                name=c["name"],
                role_title=c.get("role_title") or "",
                email=c.get("email") or "",
                phone=c.get("phone") or "",
                notes=c.get("notes") or "",
            )
            for c in clients.list_contacts(self.selected_client_id)
        ]
        self.coa = [
            CoaRow(coa_id=r["coa_id"], code=r["code"], name=r["name"], account_type=r.get("account_type") or "")
            for r in clients.list_coa(self.selected_client_id)
        ]
        self.snapshot_years = clients.list_snapshot_years(self.selected_client_id)
        if not self.snapshot_year and self.snapshot_years:
            self.snapshot_year = self.snapshot_years[0]
        self.snapshots = [
            SnapshotRow(
                snapshot_id=r["snapshot_id"],
                fiscal_year=r["fiscal_year"],
                line_item=r["line_item"],
                amount=str(r.get("amount") or ""),
                notes=r.get("notes") or "",
            )
            for r in clients.list_snapshots(
                self.selected_client_id,
                self.snapshot_year if self.snapshot_year in self.snapshot_years else None,
            )
        ]
        self._load_history()

    def _load_history(self) -> None:
        """F4 — per-record edit history for the reusable View History
        component."""
        self.client_history = load_history("client", self.selected_client_id)

    def _load_gstin_gate(self) -> None:
        """Whether the client's GSTIN may be cleared/changed right now.

        Best-effort: the gate must never break the profile render.
        """
        try:
            allowed, reason = clients.can_change_gstin(self.selected_client_id)
        except Exception:  # noqa: BLE001
            allowed, reason = True, None
        self.gstin_gate_reason = "" if allowed else (reason or "This GSTIN can't be changed right now.")

    def _load_sync_chips(self) -> None:
        try:
            rows = f5.sync_health_for_client(self.selected_client_id)
        except Exception:  # noqa: BLE001 — the health strip must never break the profile
            rows = []
        chips: list[SyncChip] = []
        for r in rows:
            status, label = _SYNC_TO_LIVE.get(r["status"], ("needs_reauth", r["status"]))
            chips.append(
                SyncChip(
                    source_label=_SOURCE_LABELS.get(r["source"], r["source"]),
                    status=status,
                    label=label,
                )
            )
        self.sync_chips = chips

    # ------------------------------------------------------------------
    # Roster actions
    # ------------------------------------------------------------------
    def set_roster_search(self, v: str):
        self.roster_search = v
        self._load_roster()

    def set_show_inactive(self, v: bool):
        self.show_inactive = v
        self._load_roster()

    @rx.event
    def new_client(self):
        """Open the standalone New Client screen. Client creation lives on
        exactly ONE screen now (/clients/new) — the roster renders no form
        fields at all."""
        return rx.redirect("/clients/new")

    @rx.event
    def open_client(self, client_id: int):
        self.selected_client_id = client_id
        self.snapshot_year = ""
        self._load_profile()
        if self.profile_tab == "Documents":
            return self._load_documents_vault()

    @rx.event
    def back_to_roster(self):
        self.selected_client_id = 0
        self.profile_tab = DEFAULT_PROFILE_TAB
        self._load_roster()

    @rx.event
    def set_profile_tab(self, tab: str):
        self.profile_tab = tab
        if tab == "Documents":
            return self._load_documents_vault()

    def _load_documents_vault(self) -> None:
        """Load the embedded F3 document vault for the selected client.

        The vault is a separate state (DocumentsState); this hands it the
        client id so the profile's Documents tab shows exactly the files
        filed for THIS client — including uploads made from Smart Document
        Ingestion, which file into F3 under the same client id.
        """
        if not self.selected_client_id:
            return
        from setu.state.documents_state import DocumentsState

        return DocumentsState.load_for_client(self.selected_client_id)

    # ------------------------------------------------------------------
    # Details tab
    # ------------------------------------------------------------------
    def set_d_name(self, v: str):
        self.d_name = v

    def set_d_team(self, v: str):
        self.d_team = v

    def set_d_email(self, v: str):
        self.d_email = v

    def set_d_phone(self, v: str):
        self.d_phone = v

    def set_d_address(self, v: str):
        self.d_address = v

    def set_d_pan(self, v: str):
        self.d_pan = v

    def set_d_pan_reason(self, v: str):
        self.d_pan_reason = v

    def set_d_tan(self, v: str):
        self.d_tan = v

    def set_d_tan_reason(self, v: str):
        self.d_tan_reason = v

    def set_d_gstin(self, v: str):
        self.d_gstin = v

    def set_d_gstin_reason(self, v: str):
        self.d_gstin_reason = v

    @rx.event
    def save_details(self):
        """Save the unrestricted Details fields in one action. The service
        validates the WHOLE record, so the inline message and this gate agree."""
        try:
            clients.update_client_details(
                self.selected_client_id,
                actor=self.username,
                legal_name=self.d_name,
                assigned_team=self.d_team,
                primary_contact_email=self.d_email,
                primary_contact_phone=self.d_phone,
                primary_contact_address=self.d_address,
            )
        except clients.ClientError as exc:
            self.flash = str(exc)
        else:
            self.flash = "Details saved."
        self._load_profile()

    @rx.event
    def save_pan_change(self):
        self._save_identity_field("pan", self.d_pan, self.d_pan_reason)

    @rx.event
    def save_tan_change(self):
        self._save_identity_field("tan", self.d_tan, self.d_tan_reason)

    @rx.event
    def save_gstin_change(self):
        self._save_identity_field("gstin", self.d_gstin, self.d_gstin_reason)

    def _save_identity_field(self, field: str, value: str, reason: str) -> None:
        label = clients.SENSITIVE_FIELDS.get(field, field.upper())
        try:
            clients.update_client_field(
                self.selected_client_id, field, value,
                actor=self.username, reason=reason,
            )
        except clients.ClientError as exc:
            self.flash = str(exc)
        else:
            self.flash = f"{label} updated."
        self._load_profile()

    @rx.event
    def set_client_active(self, active: bool):
        clients.set_client_active(self.selected_client_id, active, actor=self.username)
        self._load_profile()
        self._load_roster()

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------
    def set_ac_name(self, v: str):
        self.ac_name = v

    def set_ac_role(self, v: str):
        self.ac_role = v

    def set_ac_email(self, v: str):
        self.ac_email = v

    def set_ac_phone(self, v: str):
        self.ac_phone = v

    def set_ac_notes(self, v: str):
        self.ac_notes = v

    @rx.event
    def add_contact(self):
        try:
            clients.add_contact(
                self.selected_client_id,
                name=self.ac_name,
                role_title=self.ac_role or None,
                email=self.ac_email or None,
                phone=self.ac_phone or None,
                notes=self.ac_notes or None,
                actor=self.username,
            )
        except clients.ClientError as exc:
            self.flash = str(exc)
            return
        self.flash = f"Contact '{self.ac_name}' added."
        self.ac_name = self.ac_role = self.ac_email = self.ac_phone = self.ac_notes = ""
        self._load_profile()

    @rx.event
    def remove_contact(self, contact_id: int):
        clients.remove_contact(contact_id, actor=self.username)
        self._load_profile()

    # ------------------------------------------------------------------
    # Chart of accounts
    # ------------------------------------------------------------------
    def set_coa_bulk(self, v: str):
        self.coa_bulk = v

    def set_coa_code(self, v: str):
        self.coa_code = v

    def set_coa_name(self, v: str):
        self.coa_name = v

    def set_coa_type(self, v: str):
        self.coa_type = v

    @rx.event
    def import_coa(self):
        try:
            n = clients.bulk_import_coa(self.selected_client_id, self.coa_bulk, actor=self.username)
            self.flash = f"Imported {n} row(s)."
            self.coa_bulk = ""
        except clients.ClientError as exc:
            self.flash = str(exc)
        self._load_profile()

    @rx.event
    def add_coa_row(self):
        try:
            clients.add_coa_row(
                self.selected_client_id, self.coa_code, self.coa_name, self.coa_type, actor=self.username
            )
            self.flash = "Row added."
            self.coa_code = self.coa_name = self.coa_type = ""
        except clients.ClientError as exc:
            self.flash = str(exc)
        self._load_profile()

    @rx.event
    def delete_coa_row(self, coa_id: int):
        clients.delete_coa_row(coa_id, actor=self.username)
        self._load_profile()

    # ------------------------------------------------------------------
    # Historical snapshot
    # ------------------------------------------------------------------
    def set_snapshot_year(self, v: str):
        self.snapshot_year = v
        self._load_profile()

    def set_snap_new_year(self, v: str):
        self.snap_new_year = v

    def set_snap_bulk(self, v: str):
        self.snap_bulk = v

    def set_snap_item(self, v: str):
        self.snap_item = v

    def set_snap_amount(self, v: str):
        self.snap_amount = v

    def set_snap_notes(self, v: str):
        self.snap_notes = v

    @rx.event
    def import_snapshot(self):
        year = self.snapshot_year or self.snap_new_year
        try:
            n = clients.bulk_import_snapshot(self.selected_client_id, year, self.snap_bulk, actor=self.username)
            self.flash = f"Imported {n} row(s) for {year}."
            self.snap_bulk = ""
        except clients.ClientError as exc:
            self.flash = str(exc)
        self._load_profile()

    @rx.event
    def add_snapshot_row(self):
        year = self.snapshot_year or self.snap_new_year
        try:
            clients.add_snapshot_row(
                self.selected_client_id, year, self.snap_item, self.snap_amount, self.snap_notes,
                actor=self.username,
            )
            self.flash = "Row added."
            self.snap_item = self.snap_amount = self.snap_notes = ""
        except clients.ClientError as exc:
            self.flash = str(exc)
        self._load_profile()

    @rx.event
    def delete_snapshot_row(self, snapshot_id: int):
        clients.delete_snapshot_row(snapshot_id, actor=self.username)
        self._load_profile()


class NewClientState(AuthState):
    """The standalone New Client screen (``/clients/new``).

    Its own state so the create form has no route-back into the roster's
    state: the roster page renders zero form fields, and this page renders
    zero client rows.
    """

    legal_name: str = ""
    pan: str = ""
    tan: str = ""
    gstin: str = ""
    assigned_team: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    error: str = ""

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    def _can_create(self) -> bool:
        return "clients.profile.manage" in self._codes()

    def _record(self) -> dict:
        return {
            "legal_name": self.legal_name,
            "pan": self.pan,
            "tan": self.tan,
            "primary_contact_email": self.email,
            "primary_contact_phone": self.phone,
            "primary_contact_address": self.address,
        }

    @rx.var
    def can_create(self) -> bool:
        return self._can_create()

    @rx.var
    def validation_message(self) -> str:
        """The SERVICE's own message for the current form — the same rules the
        save runs, so the inline hint can never disagree with the server.

        Uses the CREATE validator (not the shared record one): creating a
        client requires at least one of PAN / TAN, and the inline hint plus
        the disabled Save must reflect exactly that.
        """
        try:
            clients.validate_client_creation(self._record())
        except clients.ClientError as exc:
            return str(exc)
        return ""

    @rx.var
    def ready(self) -> bool:
        return self.validation_message == ""

    @rx.event
    def load(self):
        # Creating a client is a manage-level action, so the screen itself
        # refuses (the roster's "+ New client" is disabled for the same role).
        if (deny := self._gate("clients.profile.manage")):
            return rx.redirect(deny)
        self._reset()

    def _reset(self) -> None:
        self.legal_name = self.pan = self.tan = self.gstin = ""
        self.assigned_team = self.email = self.phone = self.address = ""
        self.error = ""

    def set_legal_name(self, v: str):
        self.legal_name = v

    def set_pan(self, v: str):
        self.pan = v

    def set_tan(self, v: str):
        self.tan = v

    def set_gstin(self, v: str):
        self.gstin = v

    def set_assigned_team(self, v: str):
        self.assigned_team = v

    def set_email(self, v: str):
        self.email = v

    def set_phone(self, v: str):
        self.phone = v

    def set_address(self, v: str):
        self.address = v

    @rx.event
    def cancel(self):
        """Secondary action — back to the roster, nothing saved."""
        return rx.redirect("/clients")

    @rx.event
    def submit(self):
        """Primary action. Saves, then opens the new client's profile.

        The redirect carries ``?client=N`` so the profile is reachable by URL
        (``ClientState.load`` reads it) rather than relying on state that a
        route change would reset.
        """
        self.error = ""
        try:
            client_id = clients.create_client(
                legal_name=self.legal_name,
                pan=self.pan,
                tan=self.tan,
                gstin=self.gstin,
                assigned_team=self.assigned_team,
                primary_contact_email=self.email,
                primary_contact_phone=self.phone,
                primary_contact_address=self.address,
                actor=self.username,
            )
        except clients.ClientError as exc:
            self.error = str(exc)
            return
        self._reset()
        return rx.redirect(f"/clients?client={client_id}")
