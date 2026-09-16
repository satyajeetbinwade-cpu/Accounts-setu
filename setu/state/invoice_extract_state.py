"""F3-B state — Invoice Extraction & Digitalization.

Upload invoices (four formats: image / pdf / excel / word) → AI extracts the
canonical field set with per-field confidence → sub-threshold fields gate into
this module's OWN review queue → confirm → generate an immutable Tally-ready
export. Every handler calls ``src.invoice_extract.service``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.invoice_extract import service as ie
from setu.state.auth_state import AuthState

STATUS_LABELS = {
    "extracted": "Extracted — ready to confirm",
    "needs_review": "Needs review",
    "confirmed": "Confirmed",
    "exported": "Exported",
}
PATH_LABELS = {
    "visual": "Visual touchpoint (vision model)",
    "structured": "Structured touchpoint (text/table parsing)",
}
FORMAT_CHIPS = [
    "🖼 Image (JPG/PNG)",
    "📄 PDF",
    "📊 Excel/CSV",
    "📝 Word",
]


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class UploadRow:
    upload_id: int
    client_label: str
    filename: str
    batch_id: str
    extraction_path: str
    path_label: str
    status: str
    status_label: str
    duplicate_warning: bool
    flagged_count: int


@dataclass
class FieldRow:
    field_name: str
    label: str
    extracted_value: str
    effective_value: str
    source_location: str
    confidence: int
    has_confidence: bool
    is_present: bool
    resolved: bool
    flagged: bool
    auto_accepted: bool
    not_present: bool
    resolved_value: str
    badge_pct: str
    input_value: str


@dataclass
class ClientCount:
    client_id: int
    label: str
    count: int


@dataclass
class BatchRow:
    batch_id: int
    filename: str
    row_count: int
    generated_by: str
    generated_at: str
    fmt: str


@dataclass
class TouchpointRow:
    touchpoint_key: str
    label: str
    is_active: bool
    primary_model: str
    fallback_model: str
    primary_provider: str


class InvoiceExtractState(AuthState):
    """Invoice Extraction state."""

    section: str = "Upload"

    client_options: list[ClientOption] = []
    client_label_map: dict[str, str] = {}

    # upload
    upload_client_id: int = 0
    batch_name: str = ""
    upload_error: str = ""

    # uploads list
    uploads: list[UploadRow] = []

    # review queue
    queue_open_count: int = 0
    queue_by_client: list[ClientCount] = []
    queue_entries: list[UploadRow] = []

    # review screen
    selected_upload_id: int = 0
    detail_filename: str = ""
    detail_client: str = ""
    detail_path_label: str = ""
    detail_status: str = ""
    detail_status_label: str = ""
    detail_duplicate: bool = False
    detail_threshold: int = 90
    fields: list[FieldRow] = []
    remaining_count: int = 0
    preview_kind: str = ""          # "image" | "table" | "text" | ""
    preview_image_src: str = ""     # data URI
    preview_text: str = ""
    preview_table_rows: list[list[str]] = []
    preview_table_headers: list[str] = []
    field_inputs: dict[str, str] = {}
    can_review: bool = False

    # export
    export_client_id: int = 0
    confirmed_uploads: list[UploadRow] = []
    export_selected: list[str] = []
    export_fmt: str = "xlsx"
    can_export: bool = False
    batches: list[BatchRow] = []
    last_export_name: str = ""
    last_export_b64: str = ""
    last_export_mime: str = ""

    # model assignment
    touchpoints: list[TouchpointRow] = []
    threshold: int = 90
    is_model_admin: bool = False
    is_threshold_admin: bool = False
    model_edit_key: str = ""
    model_primary: str = ""
    model_fallback: str = ""

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    def _client_label(self, cid) -> str:
        if cid is None:
            return "—"
        return self.client_label_map.get(str(cid), f"Client #{cid}")

    @rx.var
    def upload_client_name(self) -> str:
        for o in self.client_options:
            if o.client_id == self.upload_client_id:
                return o.legal_name
        return ""

    @rx.var
    def export_client_name(self) -> str:
        for o in self.client_options:
            if o.client_id == self.export_client_id:
                return o.legal_name
        return ""

    @rx.var
    def client_names(self) -> list[str]:
        return [o.legal_name for o in self.client_options]

    @rx.var
    def touchpoint_keys(self) -> list[str]:
        return [tp.touchpoint_key for tp in self.touchpoints]

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "invoice_extract.upload" not in self._codes():
            return rx.redirect("/")
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self.client_label_map = {
            str(c["client_id"]): c["legal_name"]
            for c in clients.list_clients(include_inactive=True)
        }
        if not self.upload_client_id and self.client_options:
            self.upload_client_id = self.client_options[0].client_id
        if not self.export_client_id and self.client_options:
            self.export_client_id = self.client_options[0].client_id
        self.can_review = "invoice_extract.review" in self._codes()
        self.can_export = "invoice_extract.export" in self._codes()
        self.is_model_admin = "invoice_extract.models.manage" in self._codes()
        self.is_threshold_admin = "invoice_extract.threshold.manage" in self._codes()
        self.threshold = ie.get_threshold()
        self._load_all()

    def _load_all(self) -> None:
        self._load_uploads()
        self._load_queue()
        self._load_export()
        self._load_models()
        if self.selected_upload_id:
            self._load_detail()

    def _to_upload_row(self, u: dict, *, flagged: int = -1) -> UploadRow:
        fc = flagged
        if fc < 0:
            fc = len(ie.get_reviewable_fields(u["upload_id"]))
        return UploadRow(
            upload_id=u["upload_id"],
            client_label=self._client_label(u["client_id"]),
            filename=u["filename"],
            batch_id=u.get("batch_id") or "",
            extraction_path=u.get("extraction_path") or "",
            path_label=PATH_LABELS.get(u.get("extraction_path") or "", u.get("extraction_path") or ""),
            status=u["status"],
            status_label=STATUS_LABELS.get(u["status"], u["status"]),
            duplicate_warning=bool(u.get("duplicate_warning")),
            flagged_count=fc,
        )

    def _load_uploads(self) -> None:
        if not self.upload_client_id:
            self.uploads = []
            return
        self.uploads = [self._to_upload_row(u) for u in ie.list_uploads(client_id=self.upload_client_id)]

    def _load_queue(self) -> None:
        summary = ie.review_queue_summary()
        self.queue_open_count = summary["open_count"]
        self.queue_by_client = [
            ClientCount(client_id=cid, label=self._client_label(cid), count=count)
            for cid, count in sorted(summary["by_client"].items())
        ]
        self.queue_entries = [
            self._to_upload_row(e["upload"], flagged=len(e.get("flagged_fields") or []))
            for e in ie.list_review_queue(status="open")
            if e.get("upload")
        ]

    def _load_export(self) -> None:
        if self.export_client_id:
            self.confirmed_uploads = [
                self._to_upload_row(u)
                for u in ie.list_uploads(client_id=self.export_client_id, status="confirmed")
            ]
        else:
            self.confirmed_uploads = []
        self.batches = [
            BatchRow(
                batch_id=b["batch_id"],
                filename=b["filename"],
                row_count=int(b.get("row_count") or 0),
                generated_by=b.get("generated_by") or "",
                generated_at=str(b.get("generated_at") or "").replace("T", " ").split(".")[0],
                fmt="csv" if b["filename"].endswith(".csv") else "xlsx",
            )
            for b in ie.list_export_batches()
        ]

    def _load_models(self) -> None:
        self.touchpoints = [
            TouchpointRow(
                touchpoint_key=t["touchpoint_key"],
                label=t["label"],
                is_active=bool(t["is_active"]),
                primary_model=t.get("primary_model") or "",
                fallback_model=t.get("fallback_model") or "",
                primary_provider=t.get("primary_provider") or "",
            )
            for t in ie.list_touchpoints()
        ]
        if not self.model_edit_key and self.touchpoints:
            self.model_edit_key = self.touchpoints[0].touchpoint_key
            self.model_primary = self.touchpoints[0].primary_model
            self.model_fallback = self.touchpoints[0].fallback_model

    def _load_detail(self) -> None:
        up = ie.get_upload(self.selected_upload_id)
        if up is None:
            self.selected_upload_id = 0
            return
        self.detail_filename = up["filename"]
        self.detail_client = self._client_label(up["client_id"])
        self.detail_path_label = PATH_LABELS.get(up.get("extraction_path") or "", up.get("extraction_path") or "")
        self.detail_status = up["status"]
        self.detail_status_label = STATUS_LABELS.get(up["status"], up["status"])
        self.detail_duplicate = bool(up.get("duplicate_warning"))
        self.detail_threshold = ie.get_threshold()
        self.remaining_count = len(ie.get_reviewable_fields(self.selected_upload_id))
        self.fields = [self._to_field_row(f) for f in ie.get_fields(self.selected_upload_id)]
        for f in self.fields:
            self.field_inputs.setdefault(f.field_name, f.extracted_value or "")
        self._load_preview(up)

    def _to_field_row(self, f: dict) -> FieldRow:
        threshold = self.detail_threshold or ie.get_threshold()
        conf = f.get("confidence")
        is_present = bool(f.get("is_present"))
        resolved = bool(f.get("resolved"))
        flagged = (not resolved) and ((not is_present) or conf is None or conf < threshold)
        return FieldRow(
            field_name=f["field_name"],
            label=f.get("label") or f["field_name"],
            extracted_value=f.get("extracted_value") or "",
            effective_value=str(f.get("effective_value")) if f.get("effective_value") is not None else "",
            source_location=f.get("source_location") or "",
            confidence=int(conf) if conf is not None else 0,
            has_confidence=conf is not None,
            is_present=is_present,
            resolved=resolved,
            flagged=flagged,
            auto_accepted=(not resolved) and is_present and conf is not None and conf >= threshold,
            not_present=(not is_present) or conf is None,
            resolved_value=f.get("resolved_value") or "",
            badge_pct=f"AI — {int(conf)}%" if conf is not None else "not present",
            input_value=self.field_inputs.get(f["field_name"], f.get("extracted_value") or ""),
        )

    def _load_preview(self, up: dict) -> None:
        self.preview_kind = ""
        self.preview_image_src = ""
        self.preview_text = ""
        self.preview_table_rows = []
        self.preview_table_headers = []
        data = ie.get_upload_bytes(self.selected_upload_id)
        if not data:
            return
        fmt = up.get("source_format")
        if fmt == "image":
            ext = (up.get("file_ext") or "png").lower()
            mime = "image/png" if ext == "png" else "image/jpeg"
            self.preview_image_src = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            self.preview_kind = "image"
            return
        if fmt == "excel":
            try:
                import io

                import pandas as pd

                if data[:4] == b"PK\x03\x04" or data[:4] == b"\xd0\xcf\x11\xe0":
                    df = pd.read_excel(io.BytesIO(data), dtype=str)
                else:
                    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
                self.preview_table_headers = [str(c) for c in df.columns]
                self.preview_table_rows = [
                    ["" if v is None else str(v) for v in row]
                    for row in df.head(40).itertuples(index=False)
                ]
                self.preview_kind = "table"
                return
            except Exception:  # noqa: BLE001
                return
        from src.invoice_extract import extractor

        if fmt == "pdf":
            text = extractor._pdf_text(data)
        elif fmt == "word":
            text = extractor._docx_text(data, up["filename"])
        else:
            text = ""
        if text.strip():
            self.preview_text = text.strip()[:4000]
            self.preview_kind = "text"

    # ------------------------------------------------------------------
    # Nav + setters
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def set_upload_client(self, v: str):
        self.upload_client_id = _to_int(v)
        self._load_uploads()

    @rx.event
    def set_upload_client_by_name(self, name: str):
        self.upload_client_id = self._id_for_name(name)
        self._load_uploads()

    def set_batch_name(self, v: str):
        self.batch_name = v

    @rx.event
    def set_export_client_by_name(self, name: str):
        self.export_client_id = self._id_for_name(name)
        self._load_export()

    def _id_for_name(self, name: str) -> int:
        for o in self.client_options:
            if o.legal_name == name:
                return o.client_id
        return self.client_options[0].client_id if self.client_options else 0

    def set_field_input(self, field_name: str, value: str):
        self.field_inputs[field_name] = value
        self.fields = [
            _replace_input(f, field_name, value) for f in self.fields
        ]

    # ------------------------------------------------------------------
    # Upload (async — UploadFile.read() is a coroutine)
    # ------------------------------------------------------------------
    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        self.upload_error = ""
        self.flash = ""
        self.error = ""
        if not files:
            self.upload_error = "Choose at least one invoice to upload."
            return
        results = []
        errors = []
        for f in files:
            data = await f.read()
            try:
                res = ie.upload_invoice(
                    client_id=self.upload_client_id,
                    filename=f.filename or "invoice",
                    file_bytes=data,
                    batch_id=self.batch_name or None,
                    actor=self.username,
                )
                results.append(res)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{f.filename}: {exc}")
        if errors:
            self.error = " · ".join(errors)
        if results:
            n_review = sum(1 for r in results if r["status"] == "needs_review")
            n_dup = sum(1 for r in results if r["duplicate"])
            if n_review:
                self.flash = (
                    f"{len(results)} uploaded — {n_review} routed to the Review Queue "
                    "(sub-threshold or not-present fields)."
                )
            else:
                self.flash = (
                    f"{len(results)} uploaded — every field auto-accepted at or above the threshold."
                )
            if n_dup:
                self.flash += (
                    f" {n_dup} upload(s) matched an existing invoice number + vendor GSTIN "
                    "— a duplicate warning, not a block."
                )
        self.batch_name = ""
        self._load_all()

    # ------------------------------------------------------------------
    # Review screen
    # ------------------------------------------------------------------
    @rx.event
    def open_upload(self, upload_id: int):
        self.selected_upload_id = upload_id
        self.field_inputs = {}
        self._load_detail()

    @rx.event
    def back_to_list(self):
        self.selected_upload_id = 0
        self._load_all()

    @rx.event
    def resolve_field(self, field_name: str):
        val = (self.field_inputs.get(field_name) or "").strip() or None
        try:
            ie.resolve_field(
                self.selected_upload_id,
                field_name=field_name,
                resolved_value=val,
                actor=self.username,
            )
            self.flash = f"Resolved '{field_name}'."
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def confirm_upload(self):
        try:
            ie.confirm_upload(self.selected_upload_id, actor=self.username)
            self.flash = "Confirmed — now eligible for export."
            self.selected_upload_id = 0
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def discard_upload(self):
        try:
            ie.discard_upload(self.selected_upload_id, actor=self.username)
            self.flash = "Upload discarded."
            self.selected_upload_id = 0
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def goto_queue_client(self, client_id: int):
        self.section = "Review Queue"
        self._load_queue()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    @rx.event
    def toggle_export_select(self, upload_id: str, checked: bool):
        if checked and upload_id not in self.export_selected:
            self.export_selected = self.export_selected + [upload_id]
        elif not checked and upload_id in self.export_selected:
            self.export_selected = [u for u in self.export_selected if u != upload_id]

    @rx.event
    def set_export_fmt(self, v: str):
        self.export_fmt = v

    @rx.event
    def generate_export(self):
        ids = [_to_int(u) for u in self.export_selected]
        try:
            res = ie.generate_export(upload_ids=ids, actor=self.username, fmt=self.export_fmt)
            self.flash = (
                f"Batch {res['batch_id']} generated — {res['row_count']} row(s), rows {res['row_range']}."
            )
            self.last_export_name = res["filename"]
            self.last_export_b64 = base64.b64encode(res["data"]).decode()
            self.last_export_mime = (
                "text/csv" if res["filename"].endswith(".csv")
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            self.export_selected = []
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def download_batch(self, batch_id: int, filename: str, fmt: str):
        try:
            data = ie.regenerate_export_bytes(batch_id)
            self.last_export_name = filename
            self.last_export_b64 = base64.b64encode(data).decode()
            self.last_export_mime = (
                "text/csv" if fmt == "csv"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            self.flash = f"Ready to download {filename}."
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)

    # ------------------------------------------------------------------
    # Model assignment + threshold (admin)
    # ------------------------------------------------------------------
    @rx.event
    def set_model_edit_key(self, v: str):
        self.model_edit_key = v
        for t in self.touchpoints:
            if t.touchpoint_key == v:
                self.model_primary = t.primary_model
                self.model_fallback = t.fallback_model
                break

    def set_model_primary(self, v: str):
        self.model_primary = v

    def set_model_fallback(self, v: str):
        self.model_fallback = v

    @rx.event
    def save_model_assignment(self):
        try:
            ie.update_touchpoint_models(
                self.model_edit_key,
                primary_model=self.model_primary,
                fallback_model=self.model_fallback,
                actor=self.username,
            )
            self.flash = "Model assignment updated."
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_models()

    @rx.event
    def test_touchpoint(self, key: str):
        res = ie.test_touchpoint_call(key)
        if res["ok"]:
            self.flash = f"{key}: test call OK — {res['message']}"
        else:
            self.error = f"{key}: {res['message']}"

    def set_threshold(self, v: str):
        self.threshold = _to_int(v)

    @rx.event
    def save_threshold(self):
        try:
            ie.set_threshold(self.threshold, actor=self.username)
            self.flash = f"Threshold set to {self.threshold}%."
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_all()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _replace_input(f: FieldRow, field_name: str, value: str) -> FieldRow:
    if f.field_name != field_name:
        return f
    return FieldRow(
        field_name=f.field_name, label=f.label, extracted_value=f.extracted_value,
        effective_value=f.effective_value, source_location=f.source_location,
        confidence=f.confidence, has_confidence=f.has_confidence, is_present=f.is_present,
        resolved=f.resolved, flagged=f.flagged, auto_accepted=f.auto_accepted,
        not_present=f.not_present, resolved_value=f.resolved_value, badge_pct=f.badge_pct,
        input_value=value,
    )