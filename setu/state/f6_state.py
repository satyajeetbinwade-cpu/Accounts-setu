"""F6 state — Source File Ingestion & Format Registry.

Upload → identify (Path A/B/C) → (Path B) mapping review + confirm →
Format Registry screen. Every handler calls ``src.f6.service``.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.f6 import service as f6
from setu.state.auth_state import AuthState

SLOT_LABELS = {"portal": "Portal (GSTN)", "books": "Books / Purchase Register"}


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class FormatVersionRow:
    version_id: int
    source_format_key: str
    label: str
    version_number: int
    status: str
    status_label: str
    provenance: str
    provenance_label: str
    scope: str
    scope_label: str
    confirmed_by: str
    confirmed_at: str


@dataclass
class RunRow:
    run_id: int
    filename: str
    slot: str
    path_taken: str
    path_label: str
    status: str
    status_label: str
    created_at: str


@dataclass
class ValidationRow:
    check: str
    result: str
    result_label: str
    detail: str


@dataclass
class FieldMappingRow:
    canonical_field: str
    kind: str
    source_columns: str
    confidence: int
    rationale: str


PATH_LABELS = {"A": "Known — deterministic", "B": "Unknown — AI proposal", "C": "Rejected"}
STATUS_LABELS = {
    "parsed": "Parsed", "blocked": "Blocked", "rejected": "Rejected",
    "pending_confirmation": "Needs your confirmation",
}
VERSION_STATUS_LABELS = {"active": "Active", "superseded": "Superseded", "quarantined": "Quarantined"}
PROVENANCE_LABELS = {"seeded": "Seeded", "ai_proposed_confirmed": "AI-proposed, confirmed"}


class F6State(AuthState):
    """Source File Ingestion & Format Registry state."""

    section: str = "Upload"

    client_options: list[ClientOption] = []
    client_label_map: dict[str, str] = {}

    upload_client_id: int = 0
    upload_period: str = ""
    upload_slot: str = "portal"

    # Result of the last ingest_file() call.
    last_run_id: int = 0
    last_path_taken: str = ""
    last_path_label: str = ""
    last_status: str = ""
    last_status_label: str = ""
    last_rejection_reason: str = ""
    last_validation: list[ValidationRow] = []
    last_recognised_not_parsed: list[str] = []
    last_row_count: int = 0
    last_proposal_id: int = 0
    last_ambiguities: list[str] = []
    last_field_mappings: list[FieldMappingRow] = []
    last_ai_error: str = ""

    # Runs list
    runs: list[RunRow] = []

    # Format registry
    versions: list[FormatVersionRow] = []
    quarantine_reason: str = ""
    quarantine_target_version_id: int = 0

    flash: str = ""
    error: str = ""

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    @rx.event
    def load(self):
        if (deny := self._gate("f6.view")):
            return rx.redirect(deny)
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self.client_label_map = {
            str(c["client_id"]): c["legal_name"] for c in clients.list_clients(include_inactive=True)
        }
        if not self.upload_client_id and self.client_options:
            self.upload_client_id = self.client_options[0].client_id
        self._load_runs()
        self._load_versions()

    def _client_label(self, client_id: int) -> str:
        return self.client_label_map.get(str(client_id), f"Client #{client_id}")

    def _load_runs(self) -> None:
        rows = f6.list_runs(client_id=self.upload_client_id or None)
        self.runs = [
            RunRow(
                run_id=r["run_id"], filename=r["filename"], slot=SLOT_LABELS.get(r["slot"], r["slot"]),
                path_taken=r["path_taken"], path_label=PATH_LABELS.get(r["path_taken"], r["path_taken"]),
                status=r["status"], status_label=STATUS_LABELS.get(r["status"], r["status"]),
                created_at=str(r["created_at"]).replace("T", " ").split(".")[0],
            )
            for r in rows
        ]

    def _load_versions(self) -> None:
        rows = f6.list_format_versions()
        formats = {f["source_format_id"]: f for f in f6.list_source_formats()}
        out = []
        for v in rows:
            fmt = formats.get(v["source_format_id"], {})
            out.append(FormatVersionRow(
                version_id=v["version_id"], source_format_key=fmt.get("key", "?"),
                label=fmt.get("label", "?"), version_number=v["version_number"],
                status=v["status"], status_label=VERSION_STATUS_LABELS.get(v["status"], v["status"]),
                provenance=v["provenance"], provenance_label=PROVENANCE_LABELS.get(v["provenance"], v["provenance"]),
                scope=v["scope"], scope_label="Firm-wide" if v["scope"] == "firm" else "Client-scoped",
                confirmed_by=v.get("confirmed_by") or "—",
                confirmed_at=str(v.get("confirmed_at") or "").replace("T", " ").split(".")[0] or "—",
            ))
        self.versions = out

    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def set_upload_client(self, value: str):
        try:
            self.upload_client_id = int(value)
        except (TypeError, ValueError):
            pass
        self._load_runs()

    @rx.event
    def set_upload_period(self, value: str):
        self.upload_period = value

    @rx.event
    def set_upload_slot(self, value: str):
        self.upload_slot = value

    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        if "f6.upload" not in self._codes():
            self.error = "You don't have permission to upload."
            return
        if not files:
            return
        file = files[0]
        data = await file.read()
        filename = file.filename or "upload"
        try:
            result = f6.ingest_file(
                client_id=self.upload_client_id, period=self.upload_period or "unspecified",
                slot=self.upload_slot, filename=filename, file_bytes=data,
                actor=self.username or "system",
            )
        except Exception as exc:  # noqa: BLE001
            self.error = f"Upload failed: {exc}"
            return

        self.last_run_id = result.get("run_id", 0)
        self.last_path_taken = result.get("path_taken", "")
        self.last_path_label = PATH_LABELS.get(self.last_path_taken, self.last_path_taken)
        self.last_status = result.get("status", "")
        self.last_status_label = STATUS_LABELS.get(self.last_status, self.last_status)
        self.last_rejection_reason = result.get("rejection_reason", "")
        self.last_validation = [
            ValidationRow(
                check=v["check"].replace("_", " ").title(), result=v["result"],
                result_label={"pass": "Pass", "row_flag": "Flagged", "hard_stop": "Hard stop"}.get(v["result"], v["result"]),
                detail=v["detail"],
            )
            for v in result.get("validation", [])
        ]
        self.last_recognised_not_parsed = result.get("recognised_not_parsed", [])
        self.last_row_count = len(result.get("rows", []))
        self.last_proposal_id = result.get("proposal_id") or 0
        self.last_ai_error = result.get("ai_error", "")

        proposal = result.get("proposal") or {}
        self.last_ambiguities = proposal.get("ambiguities", [])
        self.last_field_mappings = [
            FieldMappingRow(
                canonical_field=fm["canonical_field"], kind=fm["kind"],
                source_columns=", ".join(fm.get("source_columns") or []) or "—",
                confidence=fm.get("confidence") or 0, rationale=fm.get("rationale", ""),
            )
            for fm in proposal.get("field_mappings", [])
        ]

        if self.last_path_taken == "A":
            self.flash = f"{filename}: recognised format — {self.last_status_label}, {self.last_row_count} rows."
        elif self.last_path_taken == "B":
            self.flash = f"{filename}: unrecognised layout — review the proposed mapping below."
        else:
            self.flash = f"{filename}: rejected — {self.last_rejection_reason}"

        self._load_runs()

    @rx.event
    def set_quarantine_reason(self, value: str):
        self.quarantine_reason = value

    @rx.event
    def open_quarantine(self, version_id: int):
        self.quarantine_target_version_id = version_id
        self.quarantine_reason = ""

    @rx.event
    def confirm_quarantine(self):
        if "f6.format.quarantine" not in self._codes():
            self.error = "You don't have permission to quarantine a format version."
            return
        if not self.quarantine_reason.strip():
            self.error = "A reason is required to quarantine a format version."
            return
        try:
            result = f6.quarantine_format_version(
                version_id=self.quarantine_target_version_id, reason=self.quarantine_reason,
                actor=self.username or "system",
            )
            self.flash = f"Quarantined — {result['affected_run_count']} prior run(s) used this version."
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        self.quarantine_target_version_id = 0
        self._load_versions()
