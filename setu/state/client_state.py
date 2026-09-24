"""F2 state — Client Profile & Master Data.

Roster + profile screen (branches/GSTIN, contacts, chart of accounts,
historical snapshot). Every handler calls ``src.clients.service``; no client
business logic is written here.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.f5 import service as f5
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history

BRANCH_STATUSES = ["Not Started", "In Progress", "Under Review", "Filed", "Blocked"]

_SYNC_TO_LIVE = {
    "succeeded": ("connected", "Connected"),
    "not_attempted": ("needs_reauth", "Not attempted"),
    "attempted_failed": ("expired", "Failed"),
}
_SOURCE_LABELS = {"tally": "Tally", "gst_portal": "GST Portal", "traces": "TRACES", "bank": "Bank"}


@dataclass
class ClientRow:
    client_id: int
    legal_name: str
    pan: str
    primary_gstin: str
    assigned_team: str
    branch_count: int
    is_active: bool


@dataclass
class BranchRow:
    branch_id: int
    gstin: str
    branch_name: str
    address: str
    state: str
    status: str
    is_primary: bool
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


class ClientState(AuthState):
    """Client roster + profile screen state."""

    # ---- roster ---------------------------------------------------------
    roster: list[ClientRow] = []
    roster_search: str = ""
    show_inactive: bool = False
    show_new_client_form: bool = False

    # new-client form
    nc_legal_name: str = ""
    nc_pan: str = ""
    nc_team: str = ""
    nc_gstin: str = ""
    nc_state: str = ""
    nc_error: str = ""
    flash: str = ""

    # ---- selected profile ----------------------------------------------
    selected_client_id: int = 0
    profile_tab: str = "Branch details"
    client_name: str = ""
    client_pan: str = ""
    client_team: str = ""
    client_active: bool = True
    client_primary_gstin: str = ""

    branches: list[BranchRow] = []
    selected_branch_id: int = 0
    sync_chips: list[SyncChip] = []

    contacts: list[ContactRow] = []
    coa: list[CoaRow] = []
    snapshots: list[SnapshotRow] = []
    snapshot_years: list[str] = []
    snapshot_year: str = ""

    # branch edit buffers
    b_name: str = ""
    b_address: str = ""
    b_state: str = ""
    b_status: str = "Not Started"
    b_gstin: str = ""
    b_gstin_reason: str = ""

    # add-branch form
    ab_gstin: str = ""
    ab_name: str = ""
    ab_state: str = ""
    ab_address: str = ""

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

    # client settings
    cs_name: str = ""
    cs_team: str = ""
    cs_pan: str = ""
    cs_pan_reason: str = ""

    # F4 — per-record edit history (the reusable View History component).
    client_history: list[HistoryEntry] = []
    branch_history: list[HistoryEntry] = []

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------
    @rx.var
    def can_manage(self) -> bool:
        return "clients.profile.manage" in self._codes()

    @rx.var
    def can_edit_gstin_pan(self) -> bool:
        return "clients.gstin_pan.edit" in self._codes()

    @rx.var
    def gstin_dirty(self) -> bool:
        return self.b_gstin != self._current_branch_gstin()

    @rx.var
    def pan_dirty(self) -> bool:
        return self.cs_pan != self.client_pan

    @rx.var
    def coa_warning(self) -> str:
        return clients.coa_edit_warning()

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    def _current_branch_gstin(self) -> str:
        for b in self.branches:
            if b.branch_id == self.selected_branch_id:
                return b.gstin
        return ""

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if (deny := self._gate("clients.profile.view")):
            return rx.redirect(deny)
        self._load_roster()
        if self.selected_client_id:
            self._load_profile()

    def _load_roster(self) -> None:
        rows = clients.list_clients(include_inactive=self.show_inactive)
        if self.roster_search:
            s = self.roster_search.lower()
            rows = [
                c
                for c in rows
                if s in (c["legal_name"] or "").lower()
                or s in (c["pan"] or "").lower()
                or s in (c.get("primary_gstin") or "").lower()
            ]
        self.roster = [
            ClientRow(
                client_id=c["client_id"],
                legal_name=c["legal_name"],
                pan=c.get("pan") or "",
                primary_gstin=c.get("primary_gstin") or "",
                assigned_team=c.get("assigned_team") or "",
                branch_count=int(c.get("branch_count") or 0),
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
        self.client_pan = client.get("pan") or ""
        self.client_team = client.get("assigned_team") or ""
        self.client_active = bool(client["is_active"])
        self.client_primary_gstin = client.get("primary_gstin") or ""
        self.cs_name = self.client_name
        self.cs_team = self.client_team
        self.cs_pan = self.client_pan

        self.branches = [
            BranchRow(
                branch_id=b["branch_id"],
                gstin=b["gstin"],
                branch_name=b.get("branch_name") or "",
                address=b.get("address") or "",
                state=b.get("state") or "",
                status=b["status"],
                is_primary=bool(b["is_primary"]),
                is_active=bool(b["is_active"]),
            )
            for b in clients.list_branches(self.selected_client_id, include_inactive=True)
        ]
        if self.branches and self.selected_branch_id not in {b.branch_id for b in self.branches}:
            self.selected_branch_id = self.branches[0].branch_id
        self._load_branch_buffers()

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
        """F4 — load the per-record edit history for the reusable View
        History component (client + the selected branch)."""
        self.client_history = load_history("client", self.selected_client_id)
        if self.selected_branch_id:
            self.branch_history = load_history(
                "branch", self.selected_branch_id, client_id=self.selected_client_id
            )
        else:
            self.branch_history = []

    def _load_branch_buffers(self) -> None:
        for b in self.branches:
            if b.branch_id == self.selected_branch_id:
                self.b_name = b.branch_name
                self.b_address = b.address
                self.b_state = b.state
                self.b_status = b.status
                self.b_gstin = b.gstin
                self.b_gstin_reason = ""
                return

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
    def toggle_new_client_form(self):
        self.show_new_client_form = not self.show_new_client_form
        self.nc_error = ""

    def set_nc_legal_name(self, v: str):
        self.nc_legal_name = v

    def set_nc_pan(self, v: str):
        self.nc_pan = v

    def set_nc_team(self, v: str):
        self.nc_team = v

    def set_nc_gstin(self, v: str):
        self.nc_gstin = v

    def set_nc_state(self, v: str):
        self.nc_state = v

    @rx.event
    def create_client(self):
        self.nc_error = ""
        self.flash = ""
        if not self.nc_legal_name.strip():
            self.nc_error = "Legal name is required."
            return
        try:
            client_id = clients.create_client(
                legal_name=self.nc_legal_name,
                pan=self.nc_pan or None,
                assigned_team=self.nc_team or None,
                initial_gstin=self.nc_gstin or None,
                initial_state=self.nc_state or None,
                actor=self.username,
            )
        except clients.ClientError as exc:
            self.nc_error = str(exc)
            return
        self.flash = f"Client '{self.nc_legal_name}' created."
        self.nc_legal_name = self.nc_pan = self.nc_team = self.nc_gstin = self.nc_state = ""
        self.show_new_client_form = False
        self._load_roster()
        self.open_client(client_id)

    @rx.event
    def open_client(self, client_id: int):
        self.selected_client_id = client_id
        self.selected_branch_id = 0
        self.snapshot_year = ""
        self._load_profile()
        if self.profile_tab == "Documents":
            return self._load_documents_vault()

    @rx.event
    def back_to_roster(self):
        self.selected_client_id = 0
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
    # Branch actions
    # ------------------------------------------------------------------
    @rx.event
    def select_branch(self, branch_id: int):
        self.selected_branch_id = branch_id
        self._load_branch_buffers()
        self._load_history()

    def set_b_name(self, v: str):
        self.b_name = v

    def set_b_address(self, v: str):
        self.b_address = v

    def set_b_state(self, v: str):
        self.b_state = v

    def set_b_status(self, v: str):
        self.b_status = v

    def set_b_gstin(self, v: str):
        self.b_gstin = v

    def set_b_gstin_reason(self, v: str):
        self.b_gstin_reason = v

    @rx.event
    def save_branch_details(self):
        bid = self.selected_branch_id
        clients.update_branch_field(bid, "branch_name", self.b_name, actor=self.username)
        clients.update_branch_field(bid, "address", self.b_address, actor=self.username)
        clients.update_branch_field(bid, "state", self.b_state, actor=self.username)
        clients.update_branch_field(bid, "status", self.b_status, actor=self.username)
        self.flash = "Branch details saved."
        self._load_profile()

    @rx.event
    def save_gstin_change(self):
        if not self.b_gstin_reason.strip():
            return
        try:
            clients.update_branch_field(
                self.selected_branch_id, "gstin", self.b_gstin,
                actor=self.username, reason=self.b_gstin_reason,
            )
            self.flash = "GSTIN updated."
        except clients.ClientError as exc:
            self.flash = str(exc)
        self._load_profile()

    @rx.event
    def set_branch_active(self, branch_id: int, active: bool):
        clients.set_branch_active(branch_id, active, actor=self.username)
        self._load_profile()

    @rx.event
    def delete_branch(self, branch_id: int):
        try:
            clients.delete_branch(branch_id, actor=self.username)
            self.flash = "Branch deleted."
        except clients.ClientError as exc:
            self.flash = str(exc)
        self._load_profile()

    def set_ab_gstin(self, v: str):
        self.ab_gstin = v

    def set_ab_name(self, v: str):
        self.ab_name = v

    def set_ab_state(self, v: str):
        self.ab_state = v

    def set_ab_address(self, v: str):
        self.ab_address = v

    @rx.event
    def add_branch(self):
        try:
            new_id = clients.add_branch(
                self.selected_client_id,
                gstin=self.ab_gstin,
                branch_name=self.ab_name or None,
                address=self.ab_address or None,
                state=self.ab_state or None,
                actor=self.username,
            )
        except clients.ClientError as exc:
            self.flash = str(exc)
            return
        self.flash = f"Branch '{self.ab_gstin}' added — extends this profile, no re-onboarding needed."
        self.ab_gstin = self.ab_name = self.ab_state = self.ab_address = ""
        self.selected_branch_id = new_id
        self._load_profile()

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
            clients.add_coa_row(self.selected_client_id, self.coa_code, self.coa_name, self.coa_type, actor=self.username)
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

    # ------------------------------------------------------------------
    # Client settings
    # ------------------------------------------------------------------
    def set_cs_name(self, v: str):
        self.cs_name = v

    def set_cs_team(self, v: str):
        self.cs_team = v

    def set_cs_pan(self, v: str):
        self.cs_pan = v

    def set_cs_pan_reason(self, v: str):
        self.cs_pan_reason = v

    @rx.event
    def save_client_name_team(self):
        clients.update_client_field(self.selected_client_id, "legal_name", self.cs_name, actor=self.username)
        clients.update_client_field(self.selected_client_id, "assigned_team", self.cs_team, actor=self.username)
        self.flash = "Saved."
        self._load_profile()
        self._load_roster()

    @rx.event
    def save_pan_change(self):
        if not self.cs_pan_reason.strip():
            return
        try:
            clients.update_client_field(
                self.selected_client_id, "pan", self.cs_pan,
                actor=self.username, reason=self.cs_pan_reason,
            )
            self.flash = "PAN updated."
        except clients.ClientError as exc:
            self.flash = str(exc)
        self._load_profile()

    @rx.event
    def set_client_active(self, active: bool):
        clients.set_client_active(self.selected_client_id, active, actor=self.username)
        self._load_profile()
        self._load_roster()