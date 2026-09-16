"""C4 state — Security & Credential Vault.

Security Overview (DPDP count, 30d event volume, event-type breakdown, backup/DR
placeholder, activity log), Credential Vault (masked refs, rotation, add),
DPDP Deletion Requests. Every handler calls ``src.vault.service``.

Secrets are NEVER stored or held in state: the add/rotate forms pass a value
straight through to the vault service, which encrypts it, and the state only
ever reads back a masked reference.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.vault import service as vault
from setu.state.auth_state import AuthState


@dataclass
class CredentialRow:
    credential_id: int
    service: str
    label: str
    masked_ref: str
    status: str


@dataclass
class RotationRow:
    title: str
    detail: str
    timestamp: str


@dataclass
class EventRow:
    title: str
    detail: str
    timestamp: str


@dataclass
class EventTypeCount:
    event_type: str
    count: int


@dataclass
class DpdpRow:
    request_id: int
    client_label: str
    scope: str
    legal_basis: str
    deletable_items: list[str]
    exempt_items: list[str]
    status: str
    submitted_by: str
    submitted_at: str
    decided_by: str
    decided_at: str
    decision_reason: str


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


class VaultState(AuthState):
    """Security & Credential Vault state."""

    section: str = "Security Overview"
    detail_filter: str = ""

    open_dpdp_count: int = 0
    event_volume_30d: int = 0
    breakdown: list[EventTypeCount] = []
    backup_label: str = ""
    backup_rto: str = ""
    backup_rpo: str = ""

    credentials: list[CredentialRow] = []
    rotation_history: list[RotationRow] = []
    events: list[EventRow] = []

    dpdp_pending: list[DpdpRow] = []
    dpdp_decided: list[DpdpRow] = []

    client_options: list[ClientOption] = []

    # add-credential form
    add_service: str = ""
    add_label: str = ""
    add_scoped: bool = False
    add_client_id: int = 0
    add_secret: str = ""
    add_error: str = ""

    # rotate form (keyed by credential_id)
    rotate_secret: dict[str, str] = {}

    # dpdp submit form
    dpdp_client_id: int = 0
    dpdp_scope: str = ""
    dpdp_basis: str = ""
    dpdp_deletable: str = ""
    dpdp_exempt: str = ""

    # dpdp decision (keyed by request_id)
    dpdp_reason: dict[str, str] = {}

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def can_manage(self) -> bool:
        return "vault.credentials.manage" in self._codes()

    @rx.var
    def can_submit(self) -> bool:
        return "vault.dpdp.submit" in self._codes()

    @rx.var
    def can_approve(self) -> bool:
        return "vault.dpdp.approve" in self._codes()

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "vault.view" not in self._codes():
            return rx.redirect("/")
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self._load_all()

    def _load_all(self) -> None:
        self.open_dpdp_count = len(vault.list_dpdp_requests(status="pending"))
        breakdown = vault.event_type_breakdown_last_30_days()
        self.event_volume_30d = sum(breakdown.values())
        self.breakdown = [
            EventTypeCount(event_type=k, count=v)
            for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1])
        ]

        dr = vault.backup_dr_status()
        self.backup_label = dr["label"]
        self.backup_rto = dr["rto"]
        self.backup_rpo = dr["rpo"]

        self.credentials = [
            CredentialRow(
                credential_id=c["credential_id"],
                service=c["service"],
                label=c.get("label") or "",
                masked_ref=c["masked_ref"],
                status=c["status"],
            )
            for c in vault.list_credentials()
        ]
        self.rotation_history = [
            RotationRow(
                title=h["service"] + (f" ({h['label']})" if h.get("label") else ""),
                detail=f"rotated by {h['rotated_by']}",
                timestamp=str(h["rotated_at"]).replace("T", " ").split(".")[0],
            )
            for h in vault.list_rotation_history(limit=100)
        ]

        events = vault.merged_security_events(limit=200)
        if self.detail_filter:
            events = [e for e in events if e["event_type"] == self.detail_filter]
        self.events = [
            EventRow(
                title=f"{e['event_type']} — {e['actor']}",
                detail=e.get("detail") or "",
                timestamp=str(e["created_at"]).replace("T", " ").split(".")[0],
            )
            for e in events
        ]

        self.dpdp_pending = [self._dpdp_row(r) for r in vault.list_dpdp_requests(status="pending")]
        self.dpdp_decided = [
            self._dpdp_row(r) for r in vault.list_dpdp_requests() if r["status"] != "pending"
        ]

    @staticmethod
    def _dpdp_row(r: dict) -> DpdpRow:
        return DpdpRow(
            request_id=r["request_id"],
            client_label=r["client_label"],
            scope=r.get("scope") or "",
            legal_basis=r.get("legal_basis") or "",
            deletable_items=r.get("deletable_items") or [],
            exempt_items=r.get("exempt_items") or [],
            status=r["status"],
            submitted_by=r.get("submitted_by") or "",
            submitted_at=str(r.get("submitted_at") or "").split("T")[0],
            decided_by=r.get("decided_by") or "",
            decided_at=str(r.get("decided_at") or "").split("T")[0],
            decision_reason=r.get("decision_reason") or "",
        )

    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def filter_events(self, event_type: str):
        self.detail_filter = event_type
        self._load_all()

    @rx.event
    def clear_filter(self):
        self.detail_filter = ""
        self._load_all()

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def set_add_service(self, v: str):
        self.add_service = v

    def set_add_label(self, v: str):
        self.add_label = v

    def set_add_scoped(self, v: bool):
        self.add_scoped = v

    def set_add_client_id(self, v: int):
        self.add_client_id = v

    def set_add_secret(self, v: str):
        self.add_secret = v

    @rx.event
    def add_credential(self):
        self.add_error = ""
        if not self.add_service.strip() or not self.add_secret.strip():
            self.add_error = "Service and secret are both required."
            return
        try:
            vault.add_credential(
                self.add_service,
                self.add_secret,
                label=self.add_label or None,
                client_id=self.add_client_id if self.add_scoped else None,
                actor=self.username,
            )
        except vault.VaultError as exc:
            self.add_error = str(exc)
            return
        self.flash = f"'{self.add_service}' credential added — encrypted at rest."
        # Never retain the secret in state beyond the call.
        self.add_service = self.add_label = self.add_secret = ""
        self.add_scoped = False
        self.add_client_id = 0
        self._load_all()

    def set_rotate_secret(self, credential_id: int, v: str):
        self.rotate_secret[str(credential_id)] = v

    @rx.event
    def rotate_credential(self, credential_id: int):
        secret = self.rotate_secret.get(str(credential_id), "")
        try:
            vault.rotate_credential(credential_id, secret, actor=self.username)
            self.flash = "Credential rotated. The new value will not be shown again."
            self.rotate_secret[str(credential_id)] = ""
        except vault.VaultError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def deactivate_credential(self, credential_id: int):
        vault.deactivate_credential(credential_id, actor=self.username)
        self._load_all()

    # ------------------------------------------------------------------
    # DPDP
    # ------------------------------------------------------------------
    def set_dpdp_client_id(self, v: int):
        self.dpdp_client_id = v

    def set_dpdp_scope(self, v: str):
        self.dpdp_scope = v

    def set_dpdp_basis(self, v: str):
        self.dpdp_basis = v

    def set_dpdp_deletable(self, v: str):
        self.dpdp_deletable = v

    def set_dpdp_exempt(self, v: str):
        self.dpdp_exempt = v

    @rx.event
    def submit_dpdp(self):
        self.error = ""
        client_name = next(
            (c.legal_name for c in self.client_options if c.client_id == self.dpdp_client_id),
            "",
        )
        deletable = [line.strip() for line in self.dpdp_deletable.splitlines() if line.strip()]
        exempt = [line.strip() for line in self.dpdp_exempt.splitlines() if line.strip()]
        try:
            vault.submit_dpdp_request(
                client_id=self.dpdp_client_id or None,
                client_label=client_name or "(unspecified)",
                scope=self.dpdp_scope,
                legal_basis=self.dpdp_basis,
                deletable_items=deletable,
                exempt_items=exempt,
                actor=self.username,
            )
            self.flash = "DPDP deletion request submitted, awaiting Partner approval."
            self.dpdp_scope = self.dpdp_basis = self.dpdp_deletable = self.dpdp_exempt = ""
        except vault.VaultError as exc:
            self.error = str(exc)
        self._load_all()

    def set_dpdp_reason(self, request_id: int, v: str):
        self.dpdp_reason[str(request_id)] = v

    @rx.event
    def decide_dpdp(self, request_id: int, approve: bool):
        reason = self.dpdp_reason.get(str(request_id), "")
        try:
            vault.decide_dpdp_request(
                request_id, approve=approve, reason=reason, actor=self.username,
            )
            self.flash = "Request approved and executed." if approve else "Request rejected."
            self.dpdp_reason[str(request_id)] = ""
        except vault.VaultError as exc:
            self.error = str(exc)
        self._load_all()