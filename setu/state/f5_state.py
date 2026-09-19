"""F5 state — Data Integrity & Validation Layer.

Data Integrity Queue (structural validity blocks — a SECOND gate, distinct
from F3-AI's mapping queue), Sync Health (per client/source), and
Reconciliation-of-the-Reconciliation. Every handler calls ``src.f5.service``.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.f5 import service as f5
from setu.state.auth_state import AuthState

SYNC_TO_LIVE = {
    "succeeded": ("connected", "Connected"),
    "not_attempted": ("needs_reauth", "Not attempted"),
    "attempted_failed": ("expired", "Failed"),
}
SOURCE_LABELS = {"tally": "Tally", "gst_portal": "GST Portal", "traces": "TRACES", "bank": "Bank"}
SEVERITY_LABEL = {"cosmetic": "Cosmetic", "minor": "Minor", "gstin_pan": "GSTIN/PAN-level"}


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class BlockRow:
    block_id: int
    client_label: str
    severity: str
    severity_label: str
    issue_label: str
    source_type: str
    source_file: str
    description: str


@dataclass
class SyncRow:
    source_label: str
    status: str
    label: str
    checked_at: str
    is_stub: bool
    detail: str


@dataclass
class EscalationRow:
    source_label: str
    consecutive_fails: int
    message: str
    created_at: str


@dataclass
class ReconRow:
    run_id: int
    matched_sum: str
    unmatched_sum: str
    excluded_sum: str
    ingested_total: str
    ties_out: bool


class F5State(AuthState):
    """Data Integrity & Validation Layer state."""

    section: str = "Data Integrity Queue"

    client_options: list[ClientOption] = []
    client_label_map: dict[str, str] = {}  # str(client_id) -> legal_name

    # queue
    blocks: list[BlockRow] = []
    # grouped by severity for rendering
    gstin_pan_blocks: list[BlockRow] = []
    minor_blocks: list[BlockRow] = []
    cosmetic_blocks: list[BlockRow] = []

    # block detail
    selected_block_id: int = 0
    detail_issue_label: str = ""
    detail_client: str = ""
    detail_context: str = ""
    detail_description: str = ""
    detail_severity: str = ""
    detail_severity_label: str = ""
    detail_status: str = ""
    detail_resolved_by: str = ""
    detail_resolved_at: str = ""
    detail_override_reason: str = ""
    override_reason: str = ""
    override_allowed: bool = False
    override_deny_reason: str = ""

    # sync health
    sync_client_id: int = 0
    sync_rows: list[SyncRow] = []
    escalations: list[EscalationRow] = []

    # recon of recon
    ror_client_id: int = 0
    ror_rows: list[ReconRow] = []

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def open_count(self) -> int:
        return len(self.blocks)

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    def _role(self) -> str:
        user = auth.current_user(self.session_token or None)
        return (user or {}).get("role_name", "")

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if (deny := self._gate("f5.view")):
            return rx.redirect(deny)
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self.client_label_map = {
            str(c["client_id"]): c["legal_name"]
            for c in clients.list_clients(include_inactive=True)
        }
        if not self.sync_client_id and self.client_options:
            self.sync_client_id = self.client_options[0].client_id
        if not self.ror_client_id and self.client_options:
            self.ror_client_id = self.client_options[0].client_id
        self._load_all()

    def _load_all(self) -> None:
        self._load_blocks()
        self._load_sync()
        self._load_ror()
        if self.selected_block_id:
            self._load_block_detail()

    def _client_label(self, client_id: int) -> str:
        if client_id is None:
            return "—"
        return self.client_label_map.get(str(client_id), f"Client #{client_id}")

    def _to_block_row(self, b: dict) -> BlockRow:
        return BlockRow(
            block_id=b["block_id"],
            client_label=self._client_label(b["client_id"]),
            severity=b["severity"],
            severity_label=SEVERITY_LABEL.get(b["severity"], b["severity"]),
            issue_label=b["issue_type"].replace("_", " ").title(),
            source_type=b["source_type"],
            source_file=b["source_file"],
            description=b["description"],
        )

    def _load_blocks(self) -> None:
        rows = [self._to_block_row(b) for b in f5.list_blocks(status="open")]
        self.blocks = rows
        self.gstin_pan_blocks = [r for r in rows if r.severity == "gstin_pan"]
        self.minor_blocks = [r for r in rows if r.severity == "minor"]
        self.cosmetic_blocks = [r for r in rows if r.severity == "cosmetic"]

    def _load_block_detail(self) -> None:
        block = f5.get_block(self.selected_block_id)
        if block is None:
            self.selected_block_id = 0
            return
        self.detail_issue_label = block["issue_type"].replace("_", " ").title()
        self.detail_client = self._client_label(block["client_id"])
        self.detail_context = f"{block['source_type']}/{block['source_file']} · {block['recon_type']}"
        self.detail_description = block["description"]
        self.detail_severity = block["severity"]
        self.detail_severity_label = SEVERITY_LABEL.get(block["severity"], block["severity"])
        self.detail_status = block["status"]
        self.detail_resolved_by = block.get("resolved_by") or ""
        self.detail_resolved_at = str(block.get("resolved_at") or "").replace("T", " ").split(".")[0]
        self.detail_override_reason = block.get("override_reason") or ""
        allowed, deny = f5.can_override_block(block, self._role())
        perm = "f5.override.gstin_pan" if block["severity"] == "gstin_pan" else "f5.override.minor"
        self.override_allowed = allowed and (perm in self._codes())
        self.override_deny_reason = deny or ("You don't have permission to override this block." if not allowed else "")

    def _load_sync(self) -> None:
        if not self.sync_client_id:
            self.sync_rows = []
            return
        self.sync_rows = []
        for r in f5.sync_health_for_client(self.sync_client_id):
            status, label = SYNC_TO_LIVE.get(r["status"], ("needs_reauth", r["status"]))
            self.sync_rows.append(
                SyncRow(
                    source_label=SOURCE_LABELS.get(r["source"], r["source"]),
                    status=status,
                    label=label,
                    checked_at=str(r.get("checked_at") or "").replace("T", " ").split(".")[0],
                    is_stub=bool(r.get("is_stub")),
                    detail=r.get("detail") or "",
                )
            )
        self.escalations = [
            EscalationRow(
                source_label=SOURCE_LABELS.get(e["source"], e["source"]),
                consecutive_fails=int(e["consecutive_fails"]),
                message=e["message"],
                created_at=str(e["created_at"]).replace("T", " ").split(".")[0],
            )
            for e in f5.list_escalations(client_id=self.sync_client_id)
        ]

    def _load_ror(self) -> None:
        if not self.ror_client_id:
            self.ror_rows = []
            return
        self.ror_rows = [
            ReconRow(
                run_id=r["run_id"],
                matched_sum=f"{r['matched_sum']:,.2f}",
                unmatched_sum=f"{r['unmatched_sum']:,.2f}",
                excluded_sum=f"{r['excluded_sum']:,.2f}",
                ingested_total=f"{r['ingested_total']:,.2f}",
                ties_out=bool(r["ties_out"]),
            )
            for r in f5.recon_of_recon_for_client(self.ror_client_id)
        ]

    # ------------------------------------------------------------------
    # Nav + setters
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def open_block(self, block_id: int):
        self.selected_block_id = block_id
        self.override_reason = ""
        self._load_block_detail()

    @rx.event
    def back_to_queue(self):
        self.selected_block_id = 0
        self._load_blocks()

    def set_override_reason(self, v: str):
        self.override_reason = v

    @rx.event
    def override_block(self):
        if not self.override_reason.strip():
            return
        try:
            f5.override_block(
                self.selected_block_id,
                actor=self.username,
                actor_role=self._role(),
                reason=self.override_reason,
            )
            self.flash = "Block overridden — logged as an Audit Trail Entry and an Edit History Entry."
            self.selected_block_id = 0
        except f5.F5Error as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def set_sync_client(self, client_id: int):
        self.sync_client_id = client_id
        self._load_sync()

    @rx.event
    def set_ror_client(self, client_id: int):
        self.ror_client_id = client_id
        self._load_ror()