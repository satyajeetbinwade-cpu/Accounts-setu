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
from setu.state.shared_upload import SharedUploadState

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
    ai_used: bool
    ai_model: str
    ai_error: str


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
    # Review-screen grouping + click-to-highlight + math suggestions.
    section: str = "header"
    has_bbox: bool = False
    bbox_x: float = 0.0
    bbox_y: float = 0.0
    bbox_w: float = 0.0
    bbox_h: float = 0.0
    bbox_page: int = 1
    has_suggestion: bool = False
    suggestion_label: str = ""


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


class InvoiceExtractState(SharedUploadState):
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
    detail_ai_used: bool = False
    detail_ai_model: str = ""
    detail_ai_error: str = ""
    detail_ai_latency_ms: int = 0
    fields: list[FieldRow] = []
    remaining_count: int = 0
    preview_kind: str = ""          # "image" | "table" | "text" | ""
    preview_image_src: str = ""     # data URI
    preview_text: str = ""
    preview_table_rows: list[list[str]] = []
    preview_table_headers: list[str] = []
    field_inputs: dict[str, str] = {}
    can_review: bool = False
    # Inline correction of an already-accepted/resolved field (empty = none).
    editing_field: str = ""
    # Click-to-highlight target on the source document. Only one mode is
    # active at a time; highlight_field() clears the others.
    highlight_active: bool = False
    highlight_x: float = 0.0
    highlight_y: float = 0.0
    highlight_w: float = 0.0
    highlight_h: float = 0.0
    highlight_page: int = 1
    highlight_row_index: int = -1
    highlight_snippet: str = ""

    # export
    export_client_id: int = 0
    confirmed_uploads: list[UploadRow] = []
    export_selected: list[str] = []
    export_fmt: str = "xlsx"
    can_export: bool = False
    batches: list[BatchRow] = []

    # model assignment (read-only display; editing lives in Setup → AI Models)
    touchpoints: list[TouchpointRow] = []
    threshold: int = 90
    is_model_admin: bool = False
    is_threshold_admin: bool = False

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

    @rx.var
    def resolved_count(self) -> int:
        """How many fields on the open upload are resolved — drives the
        Resolved checklist's visibility + headline count."""
        return sum(1 for f in self.fields if f.resolved)

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if (deny := self._gate("invoice_extract.upload")):
            return rx.redirect(deny)
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
            ai_used=bool(u.get("ai_used")),
            ai_model=u.get("ai_model") or "",
            ai_error=u.get("ai_error") or "",
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
        self.detail_ai_used = bool(up.get("ai_used"))
        self.detail_ai_model = up.get("ai_model") or ""
        self.detail_ai_error = up.get("ai_error") or ""
        self.detail_ai_latency_ms = int(up.get("ai_latency_ms") or 0)
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
        # Math-derived suggestion: only for a flagged field whose dependencies
        # are all trustworthy. Pre-fills the input; the reviewer still clicks
        # Resolve (never auto-confirmed).
        suggestion = ie.suggested_value(self.selected_upload_id, f["field_name"]) if flagged else None
        default_input = self.field_inputs.get(f["field_name"], f.get("extracted_value") or "")
        if suggestion and not self.field_inputs.get(f["field_name"]):
            default_input = suggestion["value"]
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
            input_value=default_input,
            section=f.get("section") or "header",
            has_bbox=bool(f.get("has_bbox")),
            bbox_x=float(f.get("bbox_x") or 0.0),
            bbox_y=float(f.get("bbox_y") or 0.0),
            bbox_w=float(f.get("bbox_w") or 0.0),
            bbox_h=float(f.get("bbox_h") or 0.0),
            bbox_page=int(f.get("bbox_page") or 1),
            has_suggestion=bool(suggestion),
            suggestion_label=suggestion["formula_label"] if suggestion else "",
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
            if text.strip():
                self.preview_text = text.strip()[:4000]
                self.preview_kind = "text"
                return
            # Scanned PDF — no text layer. Render the first page to an image
            # so the reviewer sees the actual document, not a blank pane.
            from src.invoice_extract import ai_extractor

            png = ai_extractor.first_page_png(data)
            if png:
                self.preview_image_src = ai_extractor.data_uri("image/png", png)
                self.preview_kind = "image"
            return
        if fmt == "word":
            text = extractor._docx_text(data, up["filename"])
            if text.strip():
                self.preview_text = text.strip()[:4000]
                self.preview_kind = "text"
            return

    def _load_shared_viewer(self) -> None:
        """Populate the shared FileViewerPanel fields so F3-B's review screen
        renders the document through the SAME viewer F3 uses."""
        data = ie.get_upload_bytes(self.selected_upload_id)
        up = ie.get_upload(self.selected_upload_id)
        if not data:
            return
        from setu.state.shared_upload import (
            VIEWER_DOWNLOAD,
            VIEWER_IMAGE,
            VIEWER_PDF,
            VIEWER_TABLE,
            VIEWER_TEXT,
            pdf_page_pngs,
            sniff_file,
        )

        filename = up["filename"] if up else self.detail_filename
        info = sniff_file(filename, data)
        self.viewer_title = filename
        self.viewer_open = True
        self.viewer_kind = info["kind"]
        self.viewer_mime = info.get("mime", "")
        self.viewer_page = 1
        self.viewer_download_name = filename

        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "pdf": "application/pdf",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xls": "application/vnd.ms-excel", "csv": "text/csv",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "doc": "application/msword",
        }.get(_ext(filename), "application/octet-stream")
        self.viewer_download_mime = mime
        self.viewer_download_b64 = base64.b64encode(data).decode("ascii")

        if info["kind"] == VIEWER_IMAGE:
            self.viewer_image_src = f"data:{info['mime']};base64,{base64.b64encode(data).decode('ascii')}"
            self.viewer_page_count = 0
        elif info["kind"] == VIEWER_PDF:
            pages = pdf_page_pngs(data)
            self.viewer_pdf_pages = pages
            self.viewer_page_count = len(pages)
            self.viewer_image_src = pages[0] if pages else ""
            if not pages:
                self.viewer_kind = VIEWER_DOWNLOAD
        elif info["kind"] == VIEWER_TABLE:
            try:
                import io

                import pandas as pd

                if filename.lower().endswith(".csv"):
                    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
                else:
                    df = pd.read_excel(io.BytesIO(data), dtype=str)
                self.viewer_table_headers = [str(c) for c in df.columns]
                self.viewer_table_rows = [
                    ["" if v is None else str(v) for v in row]
                    for row in df.head(60).itertuples(index=False)
                ]
            except Exception:  # noqa: BLE001
                self.viewer_kind = VIEWER_DOWNLOAD
        elif info["kind"] == VIEWER_TEXT:
            from src.invoice_extract import extractor

            try:
                text = extractor._docx_text(data, filename)
                self.viewer_text = (text or "").strip()[:8000]
            except Exception:  # noqa: BLE001
                self.viewer_text = ""


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
    def _ie_set_stage(self, key: str, stage: str, *, pct: int = 0, label: str = "") -> None:
        from setu.state.shared_upload import UploadProgressRow

        rows = []
        for r in self.upload_progress:
            if r.key == key:
                rows.append(UploadProgressRow(key=key, filename=r.filename, stage=stage, pct=pct, label=label))
            else:
                rows.append(r)
        self.upload_progress = rows

    @rx.event
    async def upload_pending(self):
        """Upload staged invoices through the distinct queued/uploading/
        processing/done states (shared behaviour with F3)."""
        import asyncio

        if not self.pending:
            self.upload_error = "Choose at least one invoice to upload."
            return
        if not self.upload_client_id:
            self.upload_error = "Select a client before uploading."
            return

        staged = list(self.pending)
        self.upload_progress = []
        from setu.state.shared_upload import UploadProgressRow

        for pf in staged:
            self.upload_progress = self.upload_progress + [
                UploadProgressRow(key=pf.key, filename=pf.filename, stage="queued", pct=0, label="Queued")
            ]
        yield

        results: list[dict] = []
        errors: list[str] = []
        for pf in staged:
            self._ie_set_stage(pf.key, "uploading", pct=10)
            yield
            await asyncio.sleep(0.03)
            self._ie_set_stage(pf.key, "uploading", pct=60)
            yield
            self._ie_set_stage(pf.key, "processing", label="Extracting…")
            yield
            try:
                res = ie.upload_invoice(
                    client_id=self.upload_client_id,
                    filename=pf.filename,
                    file_bytes=self._pending_bytes(pf.key),
                    batch_id=self.batch_name or None,
                    actor=self.username,
                )
                if res["status"] == "needs_review":
                    self._ie_set_stage(pf.key, "needs_review", label="Routed to Review Queue")
                else:
                    self._ie_set_stage(pf.key, "done", pct=100, label="Extracted")
                results.append(res)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{pf.filename}: {exc}")
                self._ie_set_stage(pf.key, "failed", label=str(exc))
            yield

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
        self.clear_pending()
        self.batch_name = ""
        self._load_all()

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
        self.editing_field = ""
        self._clear_highlight()
        self._load_detail()
        self._load_shared_viewer()

    @rx.event
    def back_to_list(self):
        self.selected_upload_id = 0
        self.editing_field = ""
        self._clear_highlight()
        self._load_all()

    @rx.event
    def resolve_field(self, field_name: str):
        # Capture the human label BEFORE _load_all() rebuilds self.fields —
        # the flash must read "Resolved 'IGST Amount'", not the raw key.
        label = next((f.label for f in self.fields if f.field_name == field_name), field_name)
        val = (self.field_inputs.get(field_name) or "").strip() or None
        try:
            ie.resolve_field(
                self.selected_upload_id,
                field_name=field_name,
                resolved_value=val,
                actor=self.username,
            )
            self.flash = f"Resolved '{label}'."
            self.editing_field = ""
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def start_edit_field(self, field_name: str):
        """Open the inline correction input on an already-accepted/resolved
        field, so an AI error can be fixed without a full re-extract."""
        self.editing_field = field_name
        row = next((f for f in self.fields if f.field_name == field_name), None)
        if row is not None:
            self.field_inputs[field_name] = row.effective_value or row.extracted_value or ""

    @rx.event
    def cancel_edit_field(self):
        self.editing_field = ""

    def _clear_highlight(self) -> None:
        self.highlight_active = False
        self.highlight_row_index = -1
        self.highlight_snippet = ""

    @rx.event
    def highlight_field(self, field_name: str):
        """Locate a field on the source document.

        Visual touchpoint (image / scanned PDF): jump to the field's page and
        draw its bounding box. Structured touchpoint (Excel table / PDF-Word
        text): highlight the matching table row or show the matched snippet —
        there is no image to draw on, so no coordinates are fabricated.
        """
        self._clear_highlight()
        row = next((f for f in self.fields if f.field_name == field_name), None)
        if row is None:
            return
        if row.has_bbox:
            self.highlight_active = True
            self.highlight_x = row.bbox_x
            self.highlight_y = row.bbox_y
            self.highlight_w = row.bbox_w
            self.highlight_h = row.bbox_h
            self.highlight_page = row.bbox_page
            if self.viewer_kind == "pdf" and 1 <= row.bbox_page <= self.viewer_page_count:
                self.viewer_page = row.bbox_page
                self._show_page()
            return
        # Structured: table row match, else text snippet.
        if self.viewer_kind == "table":
            idx = self._match_table_row(row)
            if idx >= 0:
                self.highlight_row_index = idx
                return
        if self.viewer_kind == "text":
            self.highlight_snippet = self._snippet_for(row)

    def _match_table_row(self, row: FieldRow) -> int:
        """Find the preview table row whose cells contain this field's value
        (or its source column name). Returns -1 when nothing matches."""
        needle = (row.effective_value or row.extracted_value or "").strip().lower()
        col = (row.source_location or "").strip().lower()
        if not needle and not col:
            return -1
        for i, cells in enumerate(self.viewer_table_rows):
            joined = " ".join(str(c).lower() for c in cells)
            if needle and needle in joined:
                return i
            if col and col in joined:
                return i
        return -1

    def _snippet_for(self, row: FieldRow) -> str:
        """A short excerpt of the source text around this field's value, so
        the reviewer can see it in context."""
        value = (row.effective_value or row.extracted_value or "").strip()
        text = self.viewer_text or ""
        if not value or not text:
            return value
        pos = text.lower().find(value.lower())
        if pos < 0:
            return value
        start = max(0, pos - 60)
        end = min(len(text), pos + len(value) + 60)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end].strip()}{suffix}"

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
    def re_extract_upload(self):
        """Re-run the AI extraction layer on this upload and refresh fields."""
        try:
            res = ie.re_extract_upload(self.selected_upload_id, actor=self.username)
            if res["ai_used"]:
                self.flash = (
                    f"Re-extracted with AI ({res['ai_model']}) — "
                    f"{len(res['reviewable_fields'])} field(s) need review."
                )
            else:
                self.flash = (
                    f"Re-extracted with the offline parser — AI unavailable: {res['ai_error']}"
                )
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self.field_inputs = {}
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
            mime = (
                "text/csv" if res["filename"].endswith(".csv")
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            self.export_selected = []
            self._load_all()
            # rx.download() drives a REAL browser download via a client-side
            # event (creates + clicks a hidden <a download>), bypassing
            # react-router's <Link> click interception — which is why the old
            # data-URI rx.link silently did nothing when clicked.
            yield rx.download(data=res["data"], filename=res["filename"], mime_type=mime)
            return
        except ie.InvoiceExtractError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def download_batch(self, batch_id: int, filename: str, fmt: str):
        try:
            data = ie.regenerate_export_bytes(batch_id)
            mime = (
                "text/csv" if fmt == "csv"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            self.flash = f"Downloading {filename}."
            yield rx.download(data=data, filename=filename, mime_type=mime)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)

    # ------------------------------------------------------------------
    # Model assignment (read-only) + threshold (admin)
    #
    # Model primary/fallback are DISPLAY-ONLY here — the single editable copy
    # lives at Setup → AI Models (/ai-models). The confidence threshold has no
    # other home, so it stays editable on this tab.
    # ------------------------------------------------------------------
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


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


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