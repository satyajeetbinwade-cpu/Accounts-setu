"""F3-AI state — Smart Document Ingestion.

Upload → AI-infer mapping → human confirm (hard gate). Upload list, mapping
review screen (raw-file context + per-field edge/confidence), mapping profiles,
confidence thresholds. Every handler calls ``src.ingestion_ai.service``.

The upload handler is async because Reflex ``UploadFile.read()`` is a coroutine.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.data_paths import VALID_SOURCE_TYPES
from src.ingestion_ai import service as ingestion_ai
from setu.state.auth_state import AuthState

SOURCE_TYPE_LABELS = {
    "tally": "Tally export (books)",
    "gstr2b": "GSTR-2B (portal)",
    "ims": "IMS export (portal)",
    "form26as": "Form 26AS (portal)",
    "tds": "TDS return/challan (portal)",
    "bank": "Bank statement",
    "vendor_ledger": "Vendor ledger",
    "opening_balances": "Opening balances",
    "loan_sheet": "Loan sheet",
    "salary": "Salary register",
}

_STATUS_LABELS = {
    "pending": "Pending",
    "blocked": "Blocked",
    "needs_confirm": "Ready for review",
    "confirmed": "Confirmed",
    "unrecognized": "Unrecognized shape",
}


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class UploadRow:
    upload_id: int
    filename: str
    source_type: str
    source_label: str
    status: str
    status_label: str
    blocked_count: int


@dataclass
class FieldRow:
    canonical_field: str
    raw_column: str
    confidence: int
    reason: str
    required: bool
    from_trusted_profile: bool
    candidates: list[str]
    status: str
    needs_attention: bool


@dataclass
class ProfileRow:
    profile_id: int
    client_label: str
    source_label: str
    trusted: bool
    last_used_at: str


class IngestionAiState(AuthState):
    """Smart Document Ingestion state."""

    section: str = "Upload & Uploads List"

    client_options: list[ClientOption] = []
    client_id: int = 0

    # upload form
    source_type: str = "gstr2b"
    period: str = ""
    upload_error: str = ""

    uploads: list[UploadRow] = []

    # mapping review
    selected_upload_id: int = 0
    review_filename: str = ""
    review_source_label: str = ""
    review_status: str = ""
    blocked_banner: str = ""
    warning_banner: str = ""
    classification_error: str = ""
    review_c5_used: bool = False
    needs_attention: list[FieldRow] = []
    confident_fields: list[FieldRow] = []
    show_confident: bool = False
    overrides: dict[str, str] = {}
    trust_reuse: bool = False

    # raw-file preview context
    header_row_note: str = ""
    row_count_note: str = ""
    notes: list[str] = []
    warnings: list[str] = []

    # profiles + thresholds
    profiles: list[ProfileRow] = []
    auto_apply: int = 90
    manual: int = 70
    preselect_pct: int = 75

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def can_review(self) -> bool:
        return "ingestion_ai.review" in self._codes()

    @rx.var
    def can_manage_profiles(self) -> bool:
        return "ingestion_ai.profiles.manage" in self._codes()

    @rx.var
    def can_manage_thresholds(self) -> bool:
        return "ingestion_ai.thresholds.manage" in self._codes()

    @rx.var
    def source_type_options(self) -> list[str]:
        return sorted(VALID_SOURCE_TYPES)

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "ingestion_ai.upload" not in self._codes():
            return rx.redirect("/")
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        if not self.client_id and self.client_options:
            self.client_id = self.client_options[0].client_id
        self._load_all()

    def _load_all(self) -> None:
        self._load_uploads()
        self._load_profiles()
        self.auto_apply = ingestion_ai.get_thresholds()["auto_apply"]
        self.manual = ingestion_ai.get_thresholds()["manual"]
        self.preselect_pct = int(round(ingestion_ai.get_preselect_threshold() * 100))

    def _load_uploads(self) -> None:
        if not self.client_id:
            self.uploads = []
            return
        rows = ingestion_ai.list_uploads(client_id=self.client_id)
        threshold = ingestion_ai.get_preselect_threshold()
        out: list[UploadRow] = []
        for u in rows:
            blocked = 0
            if u["status"] == "blocked":
                report = ingestion_ai.get_report(u["upload_id"])
                if report:
                    blocked = sum(
                        1
                        for f in report["field_mapping"]
                        if not f.get("raw_column")
                        or f.get("confidence") is None
                        or (f.get("confidence") or 0) / 100.0 < threshold
                    )
            out.append(
                UploadRow(
                    upload_id=u["upload_id"],
                    filename=u["filename"],
                    source_type=u["source_type"],
                    source_label=SOURCE_TYPE_LABELS.get(u["source_type"], u["source_type"]),
                    status=u["status"],
                    status_label=_STATUS_LABELS.get(u["status"], u["status"]),
                    blocked_count=blocked,
                )
            )
        self.uploads = out

    def _load_profiles(self) -> None:
        client_map = {
            c["client_id"]: c["legal_name"] for c in clients.list_clients(include_inactive=True)
        }
        self.profiles = [
            ProfileRow(
                profile_id=p["profile_id"],
                client_label=client_map.get(p["client_id"], f"Client #{p['client_id']}"),
                source_label=SOURCE_TYPE_LABELS.get(p["source_type"], p["source_type"]),
                trusted=bool(p["trusted"]),
                last_used_at=str(p.get("last_used_at") or "").replace("T", " ").split(".")[0],
            )
            for p in ingestion_ai.list_profiles()
        ]

    # ------------------------------------------------------------------
    # Nav + upload form
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def set_client(self, client_id: int):
        self.client_id = client_id
        self.selected_upload_id = 0
        self._load_all()

    def set_source_type(self, v: str):
        self.source_type = v

    def set_period(self, v: str):
        self.period = v

    def set_trust_reuse(self, v: bool):
        self.trust_reuse = v

    def set_show_confident(self, v: bool):
        self.show_confident = v

    def _client_ref(self) -> str:
        for c in self.client_options:
            if c.client_id == self.client_id:
                return c.legal_name
        return str(self.client_id)

    # ------------------------------------------------------------------
    # Upload (async)
    # ------------------------------------------------------------------
    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        self.upload_error = ""
        self.flash = ""
        if not files:
            return
        f = files[0]
        data = await f.read()
        try:
            result = ingestion_ai.upload_and_infer(
                client_id=self.client_id,
                source_type=self.source_type,
                filename=f.filename or "upload",
                file_bytes=data,
                period=self.period or None,
                actor=self.username,
                client_ref=self._client_ref(),
            )
        except Exception as exc:  # noqa: BLE001
            self.upload_error = str(exc)
            return
        if result["status"] == "unrecognized":
            self.flash = "This file's shape doesn't match any known canonical schema — flagged as unrecognized, not force-mapped."
        elif result["status"] == "blocked":
            n = len(result["ingestion"].unmapped_required_fields)
            self.flash = f"Blocked — {n} required field(s) need mapping."
        else:
            self.flash = "Mapping inferred — ready for review."
        self.period = ""
        self.selected_upload_id = result["upload_id"]
        self._load_all()
        self._load_review()

    # ------------------------------------------------------------------
    # Mapping review
    # ------------------------------------------------------------------
    @rx.event
    def open_upload(self, upload_id: int):
        self.selected_upload_id = upload_id
        self.overrides = {}
        self.trust_reuse = False
        self._load_review()

    @rx.event
    def back_to_uploads(self):
        self.selected_upload_id = 0
        self._load_uploads()

    def _load_review(self) -> None:
        if not self.selected_upload_id:
            return
        upload = ingestion_ai.get_upload(self.selected_upload_id)
        if upload is None:
            self.selected_upload_id = 0
            return
        report = ingestion_ai.get_report(self.selected_upload_id)
        self.review_filename = upload["filename"]
        self.review_source_label = SOURCE_TYPE_LABELS.get(upload["source_type"], upload["source_type"])
        self.review_status = upload["status"]

        threshold = ingestion_ai.get_preselect_threshold()
        fields = report["field_mapping"] if report else []
        self.review_c5_used = bool(report.get("c5_context_used")) if report else False

        needs: list[FieldRow] = []
        confident: list[FieldRow] = []
        for f in fields:
            is_needs = (
                not f.get("raw_column")
                or f.get("confidence") is None
                or (f.get("confidence") or 0) / 100.0 < threshold
            )
            row = FieldRow(
                canonical_field=f["canonical_field"],
                raw_column=f.get("raw_column") or "",
                confidence=int(f.get("confidence") or 0),
                reason=f.get("reason") or "",
                required=bool(f.get("required")),
                from_trusted_profile=bool(f.get("from_trusted_profile")),
                candidates=[c.get("raw_column") for c in (f.get("candidates") or []) if c.get("raw_column")],
                status=f.get("status") or "",
                needs_attention=is_needs,
            )
            (needs if is_needs else confident).append(row)
            self.overrides.setdefault(row.canonical_field, row.raw_column)
        self.needs_attention = needs
        self.confident_fields = confident

        # raw-file context
        client_ref = str(upload.get("client_ref") or upload.get("client_id") or "")
        stored = ingestion_ai.get_stored_result(
            client_ref, upload.get("period"), upload["source_type"], upload["filename"]
        ) or {}
        self.header_row_note = (
            f"Header detected on row {stored['header_row'] + 1}." if stored.get("header_row") is not None else ""
        )
        self.row_count_note = (
            f"{stored.get('row_count_in', 0)} row(s) read · {stored.get('row_count_out', 0)} row(s) extracted."
        )
        self.notes = list(stored.get("notes") or [])[:3]
        self.warnings = list(stored.get("warnings") or [])[:5]

        self.blocked_banner = (
            f"Blocked — {len(needs)} field(s) need mapping" if upload["status"] == "blocked" else ""
        )
        self.classification_error = ""
        self.warning_banner = ""
        classification = stored.get("classification")
        if classification and not classification.get("is_financial_data", True):
            self.classification_error = (
                "This file doesn't look like reconciliation data — it doesn't contain "
                "recognizable invoice, GSTIN, or amount columns."
            )
        elif stored.get("status") == "wrong_slot":
            self.warning_banner = stored.get("message") or "This file looks like it belongs in a different upload slot."

    def set_override(self, field: str, v: str):
        self.overrides[field] = "" if v.startswith("(leave unmapped") else v

    @rx.event
    def confirm_mapping(self):
        self.error = ""
        override_map = {
            k: (None if v == "" else v) for k, v in self.overrides.items()
        }
        try:
            ingestion_ai.confirm_mapping(
                self.selected_upload_id,
                field_overrides=override_map,
                trust_for_reuse=self.trust_reuse,
                actor=self.username,
                client_ref=self._client_ref(),
            )
            self.flash = "Mapping confirmed — hard gate cleared, ready for reconciliation."
            self.selected_upload_id = 0
        except ingestion_ai.IngestionAIError as exc:
            self.error = str(exc)
        self._load_all()

    # ------------------------------------------------------------------
    # Profiles + thresholds
    # ------------------------------------------------------------------
    @rx.event
    def revoke_profile(self, profile_id: int):
        ingestion_ai.revoke_profile_trust(profile_id)
        self.flash = "Profile trust revoked — the next file of this shape will be re-mapped."
        self._load_profiles()

    def set_auto_apply(self, v: int):
        self.auto_apply = v

    def set_manual(self, v: int):
        self.manual = v

    def set_auto_apply_str(self, v: str):
        try:
            self.auto_apply = int(v)
        except (TypeError, ValueError):
            pass

    def set_manual_str(self, v: str):
        try:
            self.manual = int(v)
        except (TypeError, ValueError):
            pass

    @rx.event
    def save_thresholds(self):
        try:
            ingestion_ai.set_thresholds(auto_apply=self.auto_apply, manual=self.manual, actor=self.username)
            self.flash = "Confidence thresholds saved."
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        self._load_all()