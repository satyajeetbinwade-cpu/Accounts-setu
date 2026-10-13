"""Phase 5a — the v2 review screen's state.

Everything the screen shows comes from the committed service layer:

* ``review_api.get_upload_progress`` / ``get_page_image`` / ``get_page_info``
  / ``confirm_direction`` for the pipeline's progress and the page images;
* ``triage.get_triage`` for the documents, their regions and the run;
* ``rules.get_review_model`` for what needs a person and what is settled;
* ``confidence.record_correction`` for EVERY decision this screen takes;
* ``doc_actions`` for the document log (category, exclude, reviewed).

Only v2 data is touched, and every call is behind ``invoice_extract.v2.enabled``
(the services themselves refuse when it is off). With the flag off this state is
never loaded — ``invoice_extract_page`` renders the legacy screen untouched.

Two behaviours worth knowing:

* the page image is fetched ONCE per page and kept as a data URI, so paging does
  not re-render server-side;
* the region overlay styles are BAKED here (active / dimmed / excluded), because
  ``file_viewer_panel``'s props are hoisted and a style built inside the
  ``rx.foreach`` could not read the row's state.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field as dc_field

import reflex as rx

from src.auth import service as auth  # noqa: F401 — permission checks ride AuthState helpers
from src.clients import service as clients
from src.invoice_extract import confidence as confidence_mod
from src.invoice_extract import doc_actions
from src.invoice_extract import extract as extract_mod
from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import pipeline as pipeline_mod
from src.invoice_extract import review_api
from src.invoice_extract import rules as rules_mod
from src.invoice_extract import service as ie
from src.invoice_extract import triage as triage_mod
from setu.foundation import tokens as t
from setu.state.shared_upload import SharedUploadState, drop_completed

PERM_UPLOAD = "invoice_extract.upload"
PERM_REVIEW = "invoice_extract.review"

#: The four versions of "a person decided this" the screen can take.
ACTION_CONFIRMED = "confirmed_as_is"
ACTION_EDITED = "edited"
ACTION_NOT_PRINTED = "marked_not_printed"
ACTION_NOT_APPLICABLE = "marked_not_applicable"

#: The reason chips a settled edit offers first; free text rides alongside.
EDIT_REASONS = (
    ("correct", "Correct as read"),
    ("ocr_error", "OCR slipped"),
    ("wrong_reader", "Wrong reader used"),
    ("other", "Other"),
)

CATEGORY_LABELS = {
    "tax_invoice": "Tax invoice",
    "no_gst_expense": "Expense (no GST)",
    "not_a_voucher": "Not a voucher",
    "supporting": "Supporting document",
    "undetermined": "Undetermined",
}

NEEDS_YOU_TITLES = {
    "missing_required": "A required value is not there",
    "check_failed": "A check did not pass",
    "below_threshold": "This value scored below the threshold",
    "direction_unsettled": "Which way does this invoice go?",
}


@dataclass
class ClientPick:
    client_id: int = 0
    legal_name: str = ""


@dataclass
class JobRow:
    """One upload, as the Files view shows it."""

    upload_id: int = 0
    filename: str = ""
    client_label: str = ""
    started: bool = False              # has a triage run at all
    status: str = ""                   # triage run status ("running", "partial", …)
    status_label: str = ""
    running: bool = False
    can_resume: bool = False           # interrupted / partial — finish the rest
    can_retry: bool = False            # pages failed — retry just those
    pages_ok: int = 0
    pages_total: int = 0
    pages_failed: int = 0
    fallback_pages: int = 0
    documents: int = 0
    to_review: int = 0
    reviewed: int = 0
    excluded: int = 0
    unassigned_pages: int = 0
    with_needs_you: int = 0
    progress_label: str = ""
    summary_label: str = ""
    error: str = ""


@dataclass
class DocRow:
    document_id: int = 0
    ordinal: int = 0
    label: str = ""
    category: str = ""
    category_label: str = ""
    excluded: bool = False
    excluded_reason: str = ""
    reviewed: bool = False
    needs_you: int = 0
    pages: list[int] = dc_field(default_factory=list)
    first_page: int = 0


@dataclass
class CardRow:
    """One needs-you decision card."""

    kind: str = ""
    title: str = ""
    field_key: str = ""
    field_id: int = 0
    label: str = ""
    value: str = ""
    reason: str = ""
    detail: str = ""
    check_key: str = ""
    implicated: list[str] = dc_field(default_factory=list)
    page: int = 0
    handwriting_text: str = ""
    has_handwriting: bool = False
    has_box: bool = False
    # The crop of the value's own region: the page image, positioned so the
    # region fills a small fixed-height frame. Plain px maths — no CSS crop
    # arithmetic, so a wide or tall region can never come out mirrored.
    crop_src: str = ""
    crop_frame_w: str = "0px"
    crop_frame_h: str = "0px"
    crop_img_w: str = "0px"
    crop_img_h: str = "0px"
    crop_left: str = "0px"
    crop_top: str = "0px"


@dataclass
class ValueRow:
    """A settled / not-applicable / optional / derived line."""

    field_key: str = ""
    field_id: int = 0
    label: str = ""
    value: str = ""
    score_label: str = ""
    tag: str = ""
    reason: str = ""
    page: int = 0
    handwriting_text: str = ""
    why: str = ""
    edited_was: str = ""


@dataclass
class CheckLine:
    check_key: str = ""
    label: str = ""
    status: str = ""
    detail: str = ""
    ok: bool = False
    implicated: list[str] = dc_field(default_factory=list)


@dataclass
class RegionBox:
    """One overlay box: the viewer's ``regions_var`` row."""

    page: int = 1
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    label: str = ""
    state: str = "dimmed"
    #: Baked here, not in the view: these props are hoisted.
    style: dict = dc_field(default_factory=dict)


@dataclass
class PageChip:
    page: int = 1
    count: int = 0
    label: str = ""


def _pct(value: float) -> str:
    return f"{value * 100:.4f}%"


def _px(value: float) -> str:
    return f"{value:.2f}px"


def _field_label(key: str) -> str:
    parsed = fv2.parse_line_key(key)
    if parsed is not None:
        n, field = parsed
        spec = fv2.field_spec(field)
        return f"Line {n} · {spec.label if spec else field}"
    spec = fv2.field_spec(key)
    return spec.label if spec else key.replace("_", " ").title()


def _box_style(state: str) -> dict:
    """The baked style for one overlay box, by state."""
    if state == "active":
        return {
            "position": "absolute",
            "border": f"2px solid {t.Color.ACCENT.value}",
            "background": "rgba(59, 111, 219, 0.16)",
            "border_radius": "4px",
            "pointer_events": "none",
            "z_index": "3",
        }
    if state == "excluded":
        return {
            "position": "absolute",
            "border": f"2px dashed {t.Color.TEXT_MUTED.value}",
            "background": "rgba(120, 120, 120, 0.10)",
            "border_radius": "4px",
            "pointer_events": "none",
            "z_index": "2",
        }
    # dimmed — another document's region on this page
    return {
        "position": "absolute",
        "border": f"1px dashed {t.Color.TEXT_SECONDARY.value}",
        "background": "rgba(120, 120, 120, 0.05)",
        "border_radius": "4px",
        "pointer_events": "none",
        "z_index": "1",
        "opacity": "0.75",
    }


class InvoiceReviewState(SharedUploadState):
    """The v2 Files view + review screen."""

    v2_enabled: bool = False
    v2_reason: str = ""

    # --- Files view ---------------------------------------------------
    client_options: list[ClientPick] = []
    client_label_map: dict[str, str] = {}
    upload_client_id: int = 0
    jobs: list[JobRow] = []
    poll_active: bool = False
    upload_error: str = ""

    # --- review screen ------------------------------------------------
    open_upload_id: int = 0
    open_filename: str = ""
    open_client_label: str = ""
    open_read_only: bool = False
    open_read_only_note: str = ""
    documents: list[DocRow] = []
    current_document_id: int = 0
    current_index: int = 0
    document_count: int = 0
    to_review_count: int = 0
    reviewed_count: int = 0
    doc_label: str = ""
    doc_category: str = ""
    doc_category_label: str = ""
    doc_direction: str = ""
    doc_direction_label: str = ""
    doc_direction_basis: str = ""
    doc_needs_a_human: bool = False
    doc_alias_text: str = ""
    doc_alias_refusal: str = ""
    remember_alias: bool = False
    page_line: str = ""
    page_tag: str = ""
    cards: list[CardRow] = []
    settled: list[ValueRow] = []
    not_applicable: list[ValueRow] = []
    derived: list[ValueRow] = []
    checks: list[CheckLine] = []
    settled_document_order: bool = False
    settled_collapsed: bool = False
    drawer_open: bool = False
    field_inputs: dict[str, str] = {}
    edited_was: dict[str, str] = {}
    open_reason_for: str = ""          # the settled row whose reason chips are open
    edit_value: str = ""
    can_mark_reviewed: bool = False
    refusal: str = ""
    flash: str = ""
    error: str = ""

    # --- viewer -------------------------------------------------------
    view_page: int = 1
    view_page_count: int = 0
    view_regions: list[RegionBox] = []
    view_page_chips: list[PageChip] = []
    page_srcs: dict[str, str] = {}
    page_info_rows: list[dict] = []
    regions_cache: dict = {}
    batch_name: str = ""
    busy_label: str = ""
    #: Typed empties for the shared viewer's table props — the component
    #: foreaches them, and a bare Python ``[]`` is an untyped var there.
    empty_headers: list[str] = []
    empty_rows: list[list[str]] = []

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if (deny := self._gate(PERM_UPLOAD)):
            return rx.redirect(deny)
        self.client_options = [
            ClientPick(client_id=int(c["client_id"]), legal_name=str(c["legal_name"]))
            for c in clients.list_clients(include_inactive=False)
        ]
        self.client_label_map = {
            str(c["client_id"]): str(c["legal_name"])
            for c in clients.list_clients(include_inactive=True)
        }
        self._read_flag()
        self._load_jobs()

    def _read_flag(self) -> None:
        try:
            cfg = extract_mod.get_config()
            self.v2_enabled = bool(cfg.enabled)
            self.v2_reason = ""
        except Exception as exc:  # noqa: BLE001 — the flag must never break the page
            self.v2_enabled = False
            self.v2_reason = str(exc)

    def _label(self, cid) -> str:
        if cid is None:
            return "—"
        return self.client_label_map.get(str(cid), f"Client #{cid}")

    @rx.var
    def any_job_running(self) -> bool:
        return any(job.running for job in self.jobs)

    @rx.var
    def upload_client_name(self) -> str:
        for option in self.client_options:
            if option.client_id == self.upload_client_id:
                return option.legal_name
        return ""

    @rx.var
    def can_start_pipeline(self) -> bool:
        return bool(self.pending) and bool(self.upload_client_id) and self.v2_enabled

    @rx.event
    def set_upload_client(self, v: str):
        self.upload_client_id = int(v or 0)
        self._load_jobs()

    @rx.event
    def go_legacy(self):
        """The escape hatch: the flag is a setting, the screen just obeys it."""
        self.v2_enabled = False

    # ------------------------------------------------------------------
    # The Files view: jobs, progress, polling
    # ------------------------------------------------------------------
    def _load_jobs(self) -> None:
        if not self.upload_client_id:
            self.jobs = []
            self.poll_active = False
            return
        rows: list[JobRow] = []
        for upload in ie.list_uploads(client_id=self.upload_client_id):
            rows.append(self._job_row(upload))
        rows.sort(key=lambda r: (-r.running, not r.started, -r.upload_id))
        self.jobs = rows
        self.poll_active = any(r.running for r in rows)

    def _job_row(self, upload: dict) -> JobRow:
        upload_id = int(upload["upload_id"])
        row = JobRow(
            upload_id=upload_id,
            filename=str(upload.get("filename") or ""),
            client_label=self._label(upload.get("client_id")),
        )
        try:
            progress = review_api.get_upload_progress(upload_id)
        except Exception as exc:  # noqa: BLE001 — one bad row must not hide the list
            row.error = str(exc)
            return row
        triage = progress["triage"]
        review = progress["review"]
        extraction = progress["extraction"]
        run_status = str(triage.get("status") or "")
        row.started = run_status != "" or bool(triage.get("run_id"))
        row.status = run_status
        row.pages_ok = int(triage.get("pages_ok") or 0)
        row.pages_total = int(triage.get("pages_total") or 0)
        row.pages_failed = int(triage.get("pages_failed") or 0)
        row.fallback_pages = int(triage.get("fallback_pages") or 0)
        row.error = str(triage.get("error") or "")
        row.documents = int(review.get("documents") or 0)
        row.to_review = int(review.get("to_review") or 0)
        row.reviewed = int(review.get("reviewed") or 0)
        row.excluded = int(review.get("excluded") or 0)
        row.unassigned_pages = int(review.get("unassigned_pages") or 0)
        row.with_needs_you = int(review.get("with_needs_you") or 0)
        self._label_job(row, extraction)
        return row

    def _label_job(self, row: JobRow, extraction: dict) -> None:
        """The job's own words — a state a person can act on, never a colour."""
        running = extraction.get("queued", 0) + extraction.get("running", 0)
        if row.status == triage_mod.RUN_RUNNING or running:
            row.running = True
            row.status_label = "Reading the file"
            row.progress_label = (
                f"{row.pages_ok} of {row.pages_total or '?'} pages read"
                + (f" — {row.fallback_pages} by the fallback model" if row.fallback_pages else "")
            )
            return
        if row.status == triage_mod.RUN_INTERRUPTED:
            row.can_resume = True
            row.status_label = "Stopped part-way"
            row.progress_label = (
                f"the server restarted at page {row.pages_ok + 1} of {row.pages_total}"
            )
            return
        if row.status == triage_mod.RUN_PARTIAL or row.pages_failed:
            row.can_resume = True
            row.can_retry = row.pages_failed > 0
            row.status_label = f"Read {row.pages_ok} of {row.pages_total}, {row.pages_failed} page(s) failed"
            row.progress_label = "The document is ready to review; the failed pages can be retried."
            return
        if not row.started:
            row.status_label = "Not extracted yet"
            row.progress_label = "Extract (new pipeline) starts the reading."
            return
        row.status_label = "Read"
        row.progress_label = f"{row.pages_ok} page(s) read"
        if row.fallback_pages:
            row.progress_label += f" — {row.fallback_pages} by the fallback model"
        row.summary_label = (
            f"{row.documents} document(s) · {row.excluded} excluded · "
            f"{row.to_review} to review · {row.reviewed} reviewed"
        )
        if row.with_needs_you:
            row.summary_label += f" · {row.with_needs_you} still ask a question"

    @rx.event
    def poll(self):
        """Re-read the jobs while something is running."""
        self._load_jobs()

    # ------------------------------------------------------------------
    # Upload → pipeline (the v2 entry point)
    # ------------------------------------------------------------------
    @rx.event
    async def upload_and_start(self):
        """Stage → store → queue the v2 pipeline for every staged file."""
        import asyncio

        self.upload_error = ""
        self.flash = ""
        if not self.pending:
            self.upload_error = "Choose at least one invoice to upload."
            return
        if not self.upload_client_id:
            self.upload_error = "Select a client before uploading."
            return
        if not self.v2_enabled:
            self.upload_error = "The v2 pipeline is off (invoice_extract.v2.enabled)."
            return

        staged = list(self.pending)
        self.upload_progress = []
        from setu.state.shared_upload import UploadProgressRow

        for pf in staged:
            self.upload_progress = self.upload_progress + [
                UploadProgressRow(key=pf.key, filename=pf.filename, stage="queued", pct=0, label="Queued")
            ]
        self.busy_label = f"Queueing {len(staged)} invoice(s)…"
        yield

        errors: list[str] = []
        queued: list[int] = []
        for pf in staged:
            self._set_stage(pf.key, "uploading", pct=50)
            yield
            try:
                res = await asyncio.to_thread(
                    ie.upload_invoice_v2,
                    client_id=self.upload_client_id,
                    filename=pf.filename,
                    file_bytes=self._pending_bytes(pf.key),
                    batch_id=self.batch_name or None,
                    actor=self.username,
                )
                queued.append(int(res["upload_id"]))
                self._set_stage(pf.key, "done", pct=100, label="Queued for reading")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{pf.filename}: {exc}")
                self._set_stage(pf.key, "failed", label=str(exc))
            yield

        if errors:
            self.upload_error = " · ".join(errors)
        if queued:
            self.flash = (
                f"{len(queued)} file(s) queued — the triage run reads every page, then "
                "the extraction follows. Progress is saved; it is safe to leave this page."
            )
        self.upload_progress = drop_completed(self.upload_progress)
        self.clear_pending()
        self.batch_name = ""
        self.busy_label = ""
        self._load_jobs()

    @rx.event
    def start_pipeline(self, upload_id: int):
        """The 'Extract (new pipeline)' action on an existing file."""
        try:
            pipeline_mod.start_v2_pipeline(int(upload_id), actor=self.username)
            self.flash = "Queued — triage reads every page, then the extraction follows."
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        self._load_jobs()

    @rx.event
    def resume_job(self, upload_id: int):
        try:
            progress = review_api.get_upload_progress(int(upload_id))
            run_id = progress["triage"].get("run_id")
            if run_id:
                triage_mod.resume_triage(int(run_id), actor=self.username, include_disabled=True)
                self.flash = "Resumed from the first unfinished page."
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        self._load_jobs()

    @rx.event
    def retry_job(self, upload_id: int):
        try:
            triage_mod.retry_failed_pages(int(upload_id), actor=self.username, include_disabled=True)
            self.flash = "Retrying only the pages that failed."
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        self._load_jobs()

    # ------------------------------------------------------------------
    # Opening the review screen
    # ------------------------------------------------------------------
    @rx.event
    def open_review(self, upload_id: int):
        self.open_upload_id = int(upload_id)
        self.field_inputs = {}
        self.edited_was = {}
        self.open_reason_for = ""
        self.drawer_open = False
        self.refusal = ""
        self.flash = ""
        self._load_upload()
        self._load_review(first=True)

    @rx.event
    def back_to_files(self):
        self.open_upload_id = 0
        self.documents = []
        self.cards = []
        self.settled = []
        self.view_regions = []
        self._page_srcs = {}
        self._load_jobs()

    def _load_upload(self) -> None:
        upload = ie.get_upload(self.open_upload_id) or {}
        self.open_filename = str(upload.get("filename") or "")
        self.open_client_label = self._label(upload.get("client_id"))
        from src.invoice_extract.immutable import is_immutable_status

        status = str(upload.get("status") or "")
        self.open_read_only = is_immutable_status(status)
        self.open_read_only_note = (
            "This file is in an exported batch — its history is fixed. A correction "
            "needs a new batch, not an edit here."
            if self.open_read_only else ""
        )
        self.page_srcs = {}
        self.regions_cache = {}
        try:
            self.page_info_rows = review_api.get_page_info(self.open_upload_id)
        except Exception:  # noqa: BLE001
            self.page_info_rows = []
        self.view_page_count = len(self.page_info_rows)

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    def _load_documents(self) -> None:
        info = triage_mod.get_triage(self.open_upload_id)
        rows: list[DocRow] = []
        for doc in info.get("documents") or []:
            document_id = int(doc["document_id"])
            pages = sorted({int(r["page"]) for r in (doc.get("regions") or [])})
            category = str(doc.get("category") or "undetermined")
            needs = 0
            try:
                model = rules_mod.get_review_model(document_id, db_path=None)
                needs = len(model.get("needs_you") or [])
            except Exception:  # noqa: BLE001 — no extraction yet is normal mid-run
                needs = 0
            rows.append(DocRow(
                document_id=document_id,
                ordinal=int(doc.get("ordinal") or 0),
                label=f"Document #{int(doc.get('ordinal') or 0)}",
                category=category,
                category_label=CATEGORY_LABELS.get(category, category),
                excluded=bool(doc.get("excluded")),
                excluded_reason=str(doc.get("excluded_reason") or ""),
                reviewed=doc_actions.is_reviewed(document_id),
                needs_you=needs,
                pages=pages,
                first_page=pages[0] if pages else 0,
            ))
        rows.sort(key=lambda r: r.ordinal)
        self.documents = rows
        self.document_count = len(rows)

    @rx.var
    def open_ordinal(self) -> int:
        for doc in self.documents:
            if doc.document_id == self.current_document_id:
                return doc.ordinal
        return 0

    @rx.var
    def can_go_prev(self) -> bool:
        return self.current_index > 0

    @rx.var
    def can_go_next(self) -> bool:
        return self.current_index < self.document_count - 1

    @rx.var
    def review_counter(self) -> str:
        return f"{self.reviewed_count} of {self.to_review_count} to review"

    @rx.var
    def has_next_issue(self) -> bool:
        return self._next_issue_index() is not None

    @rx.event
    def open_document(self, document_id: int):
        self._select(int(document_id))

    @rx.event
    def next_document(self):
        if self.can_go_next:
            self._select(self.documents[self.current_index + 1].document_id)

    @rx.event
    def prev_document(self):
        if self.can_go_prev:
            self._select(self.documents[self.current_index - 1].document_id)

    @rx.event
    def next_issue(self):
        index = self._next_issue_index()
        if index is not None:
            self._select(self.documents[index].document_id)

    def _next_issue_index(self):
        """The next document AFTER this one that still asks something, else the
        next unreviewed one, else the first that asks — wrapping once."""
        order = list(range(self.current_index + 1, self.document_count))
        order += list(range(0, self.current_index))
        for index in order:
            doc = self.documents[index]
            if doc.needs_you and not doc.excluded and not doc.reviewed:
                return index
        for index in order:
            doc = self.documents[index]
            if not doc.reviewed and not doc.excluded:
                return index
        return None

    @rx.event
    def toggle_drawer(self):
        self.drawer_open = not self.drawer_open

    @rx.event
    def toggle_settled(self):
        self.settled_collapsed = not self.settled_collapsed

    @rx.event
    def set_settled_order(self, document_order: bool):
        self.settled_document_order = bool(document_order)
        self._load_document()

    def _select(self, document_id: int) -> None:
        self.current_document_id = int(document_id)
        for index, doc in enumerate(self.documents):
            if doc.document_id == self.current_document_id:
                self.current_index = index
                break
        self.refusal = ""
        self.flash = ""
        self.open_reason_for = ""
        self.drawer_open = False
        self._load_document()

    def _load_review(self, *, first: bool = False) -> None:
        self._load_documents()
        in_review = [d for d in self.documents if not d.excluded and d.category in doc_actions.EXTRACTABLE_CATEGORIES]
        self.to_review_count = len(in_review)
        self.reviewed_count = sum(1 for d in in_review if d.reviewed)
        if first or not any(d.document_id == self.current_document_id for d in self.documents):
            if self.documents:
                self.current_document_id = self.documents[0].document_id
                self.current_index = 0
        if not self.documents:
            self.cards = []
            self.settled = []
            self.checks = []
            self.view_regions = []
            return
        self._load_document()

    # ------------------------------------------------------------------
    # The open document
    # ------------------------------------------------------------------
    def _load_document(self) -> None:
        self.cards = []
        self.settled = []
        self.not_applicable = []
        self.derived = []
        self.checks = []
        self.refusal = ""
        if not self.current_document_id:
            return
        order = "document" if self.settled_document_order else "confidence"
        try:
            model = rules_mod.get_review_model(self.current_document_id, order=order)
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
            self._load_viewer()
            return

        doc = next((d for d in self.documents if d.document_id == self.current_document_id), DocRow())
        self.doc_label = doc.label
        self.doc_category = doc.category
        self.doc_category_label = doc.category_label
        direction = model.get("direction") or {}
        self._set_direction(direction, model)
        self.page_tag = self._page_tag(doc)
        self.refusal = "" if not doc.excluded else f"Excluded: {doc.excluded_reason or 'no reason recorded'}"
        self._load_page_line(doc)
        self.checks = [
            CheckLine(
                check_key=str(chk["check_key"]),
                label=str(chk.get("label") or chk["check_key"]),
                status=str(chk.get("status")),
                detail=str(chk.get("detail") or ""),
                ok=str(chk.get("status")) == "pass",
                implicated=list(chk.get("fields_implicated") or []),
            )
            for chk in model.get("checks") or []
        ]
        for item in model.get("needs_you") or []:
            # The direction question has its own card (with the two answers and
            # the remember-the-name checkbox); it is not a value card.
            if str(item.get("kind")) == "direction_unsettled":
                continue
            self.cards.append(self._card(item, doc))
        for item in model.get("settled") or []:
            self.settled.append(self._value_row(item))
        for item in model.get("not_applicable") or []:
            self.not_applicable.append(ValueRow(
                field_key=str(item["field_key"]),
                label=_field_label(str(item["field_key"])),
                tag="not_applicable",
                reason=str(item.get("reason") or ""),
            ))
        for item in model.get("derived") or []:
            self.derived.append(ValueRow(
                field_key=str(item["key"]),
                label=str(item.get("label") or item["key"]),
                value=str(item.get("value") or ""),
                tag="derived",
            ))
        self.can_mark_reviewed = not self.cards and not doc.excluded
        self._load_viewer()

    def _set_direction(self, direction: dict, model: dict) -> None:
        value = str(direction.get("direction") or "undetermined")
        self.doc_direction = value
        self.doc_direction_label = {
            "sales": "Sales (outward)",
            "purchase": "Purchase (inward)",
            "likely_sales": "Likely sales — needs a person",
            "likely_purchase": "Likely purchase — needs a person",
            "undetermined": "Not settled",
        }.get(value, value)
        self.doc_direction_basis = str(direction.get("basis") or "")
        self.doc_needs_a_human = bool(direction.get("needs_a_human"))
        match = direction.get("match") or {}
        alias = str(match.get("counterparty") or match.get("name") or "")
        self.doc_alias_text = alias
        self.doc_alias_refusal = ""
        if alias:
            try:
                self.doc_alias_refusal = review_api.alias_refusal_reason(
                    self.current_document_id, alias,
                ) or ""
            except Exception:  # noqa: BLE001
                self.doc_alias_refusal = ""
        self.remember_alias = False

    def _page_tag(self, doc: DocRow) -> str:
        """The page tag rides on a LATER page of a multi-page document only."""
        if not doc.pages or len(doc.pages) < 2:
            return ""
        page = doc.pages[-1]
        on_page = sum(1 for d in self.documents if page in d.pages)
        return (
            f"Page {page} — {on_page} document(s) on this page — "
            f"{sum(1 for d in self.documents if d.excluded and page in d.pages)} excluded — "
            f"{sum(1 for d in self.documents if not d.excluded and d.needs_you and page in d.pages)} still ask a question"
        )

    def _load_page_line(self, doc: DocRow) -> None:
        if not doc.pages:
            self.page_line = "No page regions recorded for this document."
            return
        if len(doc.pages) == 1:
            self.page_line = f"Page {doc.pages[0]} — 1 document — 0 excluded — {doc.needs_you} to review"
        else:
            self.page_line = (
                f"Pages {doc.pages[0]}–{doc.pages[-1]} — {doc.needs_you} field(s) still ask something"
            )

    def _card(self, item: dict, doc: DocRow) -> CardRow:
        kind = str(item.get("kind") or "")
        field_key = str(item.get("field_key") or "")
        card = CardRow(kind=kind, title=NEEDS_YOU_TITLES.get(kind, kind), field_key=field_key)
        card.label = _field_label(field_key) if field_key else ""
        card.field_id = int(item.get("field_id") or 0)
        card.value = str(item.get("value") or "")
        card.page = int(item.get("page") or doc.first_page or 1)
        card.reason = str(item.get("reason") or item.get("detail") or "")
        card.detail = str(item.get("detail") or "")
        card.check_key = str(item.get("check_key") or "")
        card.implicated = [str(k) for k in (item.get("fields_implicated") or [])]
        handwriting = item.get("handwriting") or None
        if handwriting:
            card.has_handwriting = True
            card.handwriting_text = str(handwriting.get("text") or "")
        card.has_box = item.get("w") is not None
        if card.has_box:
            self._crop(card, item)
        return card

    def _crop(self, card: CardRow, item: dict) -> None:
        """A small crop of the value's own region, from the page image."""
        src = self._page_src(card.page)
        card.crop_src = src
        info = next(
            (p for p in self.page_info_rows if int(p.get("page") or 0) == card.page), None,
        )
        width = float((info or {}).get("width") or 0.0)
        height = float((info or {}).get("height") or 0.0)
        if not src or not info or width <= 0 or height <= 0:
            card.crop_src = ""
            return
        box_x = float(item.get("x") or 0.0)
        box_y = float(item.get("y") or 0.0)
        box_w = max(float(item.get("w") or 0.0), 0.01)
        box_h = max(float(item.get("h") or 0.0), 0.01)
        # Frame: fixed height, width from the region's true aspect.
        frame_h = 76.0
        frame_w = frame_h * (box_w * width) / (box_h * height)
        displayed_w = frame_w / box_w
        displayed_h = displayed_w * height / width
        card.crop_frame_w = _px(frame_w)
        card.crop_frame_h = _px(frame_h)
        card.crop_img_w = _px(displayed_w)
        card.crop_img_h = _px(displayed_h)
        card.crop_left = _px(-box_x * displayed_w)
        card.crop_top = _px(-box_y * displayed_h)

    def _value_row(self, item: dict) -> ValueRow:
        field_key = str(item["field_key"])
        score = item.get("score")
        row = ValueRow(
            field_key=field_key,
            field_id=int(item.get("field_id") or 0),
            label=_field_label(field_key),
            value=str(item.get("value") or ""),
            score_label="" if score is None else str(int(round(float(score)))),
            page=int(item.get("page") or 0),
            edited_was=self.edited_was.get(field_key, ""),
        )
        handwriting = item.get("handwriting") or None
        if handwriting:
            row.handwriting_text = str(handwriting.get("text") or "")
        row.why = self._why(item)
        return row

    @staticmethod
    def _why(item: dict) -> str:
        """Why this score — from the STORED signals and caps, never re-derived."""
        bits: list[str] = []
        for signal in item.get("signals") or []:
            name = str(signal.get("name") or "")
            points = signal.get("points")
            reason = str(signal.get("reason") or "")
            if name:
                bits.append(f"{name} {points:+g} — {reason}" if points else f"{name} — {reason}")
        for cap in item.get("caps") or []:
            bits.append(f"capped at {cap.get('cap')} — {cap.get('reason')}")
        threshold = item.get("threshold_used")
        if threshold is not None:
            bits.append(f"threshold in force: {threshold}")
        version = item.get("formula_version")
        if version:
            bits.append(f"formula {version}")
        return "\n".join(bits)

    # ------------------------------------------------------------------
    # The viewer
    # ------------------------------------------------------------------
    def _page_src(self, page: int) -> str:
        key = str(page)
        if key in self.page_srcs:
            return self.page_srcs[key]
        try:
            data = review_api.get_page_image(self.open_upload_id, page)
        except Exception:  # noqa: BLE001
            return ""
        src = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        self.page_srcs = {**self.page_srcs, key: src}
        return src

    def _load_viewer(self) -> None:
        if not self.page_info_rows:
            self.view_page_count = 0
            self.viewer_image_src = ""
            self.view_regions = []
            return
        doc = next((d for d in self.documents if d.document_id == self.current_document_id), None)
        pages = list(doc.pages) if doc else []
        page = self.view_page if self.view_page in pages else (pages[0] if pages else self.view_page)
        self.view_page = page
        self._show_view_page()

    def _show_view_page(self) -> None:
        src = self._page_src(self.view_page)
        self.viewer_title = self.open_filename
        self.viewer_kind = "image"
        self.viewer_image_src = src
        self.viewer_page = self.view_page
        self.viewer_page_count = self.view_page_count
        self.view_regions = self._regions_for(self.view_page)
        self.view_page_chips = self._chips_for(self.view_page)

    def _regions_for(self, page: int) -> list[RegionBox]:
        boxes: list[RegionBox] = []
        for doc in self.documents:
            if page not in doc.pages:
                continue
            state = "excluded" if doc.excluded else (
                "active" if doc.document_id == self.current_document_id else "dimmed"
            )
            for region in self._regions_of(doc):
                if int(region["page"]) != page:
                    continue
                box_x = float(region["x"])
                box_y = float(region["y"])
                box_w = float(region["w"])
                box_h = float(region["h"])
                boxes.append(RegionBox(
                    page=page, x=box_x, y=box_y, w=box_w, h=box_h,
                    label=f"Document #{doc.ordinal}" if doc.document_id == self.current_document_id else "",
                    state=state,
                    style={
                        **_box_style(state),
                        "left": _pct(box_x),
                        "top": _pct(box_y),
                        "width": _pct(box_w),
                        "height": _pct(box_h),
                    },
                ))
        return boxes

    def _regions_of(self, doc: DocRow) -> list[dict]:
        key = str(doc.document_id)
        if key in self.regions_cache:
            return self.regions_cache[key]
        rows: list[dict] = []
        try:
            info = triage_mod.get_triage(self.open_upload_id)
            for raw in info.get("documents") or []:
                if int(raw["document_id"]) == doc.document_id:
                    rows = list(raw.get("regions") or [])
                    break
        except Exception:  # noqa: BLE001
            rows = []
        self.regions_cache = {**self.regions_cache, key: rows}
        return rows

    def _chips_for(self, page: int) -> list[PageChip]:
        chips: list[PageChip] = []
        for doc in self.documents:
            if page in doc.pages:
                chips.append(PageChip(
                    page=page, count=1,
                    label=f"{doc.label}{' (excluded)' if doc.excluded else ''}"
                          f"{' — you are here' if doc.document_id == self.current_document_id else ''}",
                ))
        return chips

    @rx.event
    def view_page_next(self):
        if self.view_page < self.view_page_count:
            self.view_page += 1
            self._show_view_page()
            yield

    @rx.event
    def view_page_prev(self):
        if self.view_page > 1:
            self.view_page -= 1
            self._show_view_page()
            yield

    @rx.event
    def set_view_page(self, page: int):
        page = int(page)
        if 1 <= page <= max(self.view_page_count, 1):
            self.view_page = page
            self._show_view_page()

    @rx.event
    def jump_to_field_page(self):
        """Centre the viewer on the page of the first thing that needs a person."""
        target = 0
        if self.cards:
            target = int(self.cards[0].page or 0)
        if not target:
            doc = next((d for d in self.documents if d.document_id == self.current_document_id), None)
            target = doc.first_page if doc else 0
        if target:
            self.set_view_page(target)

    # ------------------------------------------------------------------
    # Decisions — every one logged through record_correction
    # ------------------------------------------------------------------
    def _actor(self) -> str:
        return self.username or "ui:review"

    def _log(self, field_id: int, action: str, *, reason_code: str | None = None,
             value_after: str | None = None) -> bool:
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return False
        if not field_id:
            self.refusal = "This item has no field row to record a decision against."
            return False
        try:
            confidence_mod.record_correction(
                field_id, action, actor=self._actor(),
                value_after=value_after, reason_code=reason_code,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
            return False

    @rx.event
    def set_field_input(self, field_key: str, value: str):
        self.field_inputs = {**self.field_inputs, field_key: value}

    @rx.event
    def confirm_card(self, field_key: str):
        card = next((c for c in self.cards if c.field_key == field_key), None)
        if card is None:
            return
        value = (self.field_inputs.get(field_key) or card.value or "").strip()
        if self._log(card.field_id, ACTION_CONFIRMED, reason_code="correct", value_after=value or None):
            self.flash = f"{card.label or field_key}: confirmed."
            self._load_review()

    @rx.event
    def reject_card(self, field_key: str, reason: str):
        card = next((c for c in self.cards if c.field_key == field_key), None)
        if card is None:
            return
        if self._log(card.field_id, ACTION_NOT_PRINTED, reason_code=reason or "not_on_document"):
            self.flash = f"{card.label or field_key}: recorded as not printed on the document."
            self._load_review()

    @rx.event
    def not_applicable_card(self, field_key: str, reason: str):
        card = next((c for c in self.cards if c.field_key == field_key), None)
        if card is None:
            return
        if self._log(card.field_id, ACTION_NOT_APPLICABLE, reason_code=reason or "not_applicable_here"):
            try:
                rules_mod.override_applicability(
                    self.current_document_id, field_key, "not_applicable",
                    actor=self._actor(), reason="review screen: not applicable here",
                )
            except Exception as exc:  # noqa: BLE001
                self.refusal = str(exc)
            self.flash = f"{card.label or field_key}: not applicable here."
            self._load_review()

    @rx.event
    def override_to_required(self, field_key: str):
        try:
            rules_mod.override_applicability(
                self.current_document_id, field_key, "required",
                actor=self._actor(), reason="review screen: this value is printed here",
            )
            self.flash = f"{field_key}: put back to required."
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
        self._load_review()

    @rx.event
    def open_reason(self, field_key: str):
        self.open_reason_for = field_key
        row = next((r for r in self.settled if r.field_key == field_key), None)
        self.edit_value = row.value if row else ""

    @rx.event
    def set_edit_value(self, value: str):
        self.edit_value = value

    @rx.event
    def cancel_reason(self):
        self.open_reason_for = ""
        self.edit_value = ""

    @rx.event
    def confirm_settled(self, field_key: str, reason: str):
        row = next((r for r in self.settled if r.field_key == field_key), None)
        if row is None:
            return
        if self._log(row.field_id, ACTION_CONFIRMED, reason_code=reason or "correct"):
            self.flash = f"{row.label}: confirmed as read."
            self.open_reason_for = ""
            self._load_review()

    @rx.event
    def edit_settled(self, field_key: str, reason: str):
        row = next((r for r in self.settled if r.field_key == field_key), None)
        if row is None:
            return
        new_value = (self.edit_value or "").strip()
        if not new_value:
            self.refusal = "An edit must record the value it was changed to."
            return
        if self._log(row.field_id, ACTION_EDITED, reason_code=reason or "other", value_after=new_value):
            self.edited_was = {**self.edited_was, field_key: row.value}
            self.flash = (
                f"{row.label}: corrected to “{new_value}” and logged. "
                "The stored read is kept — the log is what calibration reads."
            )
            self.open_reason_for = ""
            self._load_review()

    @rx.event
    def set_remember_alias(self, value: bool):
        self.remember_alias = bool(value)

    @rx.event
    def settle_direction(self, direction: str):
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            result = review_api.confirm_direction(
                self.current_document_id, direction, actor=self._actor(),
                remember_alias=self.remember_alias,
                alias_text=self.doc_alias_text or None,
            )
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
            return
        if not result.get("ok"):
            self.refusal = str(result.get("reason") or "That direction was refused.")
            return
        alias = result.get("alias")
        self.flash = (
            f"Direction settled as {direction}."
            + (f" The name was remembered as “{alias.get('alias_text')}”." if alias else "")
        )
        self._load_review()

    @rx.event
    def recategorise(self, category: str):
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.set_category(
                self.current_document_id, category, actor=self._actor(),
                reason="review screen: recategorised by a person",
            )
            self.flash = f"Moved to {CATEGORY_LABELS.get(category, category)}."
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
        self._load_review()

    @rx.event
    def exclude_document(self, reason: str):
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.exclude_document(
                self.current_document_id, actor=self._actor(),
                reason=reason or "review screen: excluded by a person",
            )
            self.flash = "Excluded from the totals. Nothing was deleted."
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
        self._load_review()

    @rx.event
    def include_document(self):
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.include_document(
                self.current_document_id, actor=self._actor(),
                reason="review screen: put back in by a person",
            )
            self.flash = "Back in the totals."
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
        self._load_review()

    @rx.event
    def mark_reviewed(self):
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.mark_reviewed(self.current_document_id, actor=self._actor())
            self.flash = "Marked reviewed."
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
        self._load_review()

    @rx.event
    def unmark_reviewed(self):
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.unmark_reviewed(self.current_document_id, actor=self._actor())
            self.flash = "Reopened."
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
        self._load_review()

    # ------------------------------------------------------------------
    # Keyboard: Enter = confirm the focused card, E = edit, N = next issue
    # The view binds these on the page body with rx.call_script-free handlers.
    # ------------------------------------------------------------------
    @rx.event
    def key_next_issue(self):
        self.next_issue()

    @rx.event
    def key_confirm_focused(self):
        if self.cards:
            self.confirm_card(self.cards[0].field_key)

    @rx.event
    def key_edit_focused(self):
        if self.settled:
            self.open_reason(self.settled[0].field_key)

    @rx.event
    def handle_key(self, key: str):
        """Enter / E / N on the page body.

        Keys are ignored while an inline decision is open (``open_reason_for``),
        so typing in the correction box can never be mistaken for a shortcut.
        A text field elsewhere swallows its own keys — those bindings are on the
        page wrapper and a key pressed inside an input does bubble here, which
        is why nothing here acts while a decision is mid-flight.
        """
        if self.open_upload_id == 0 or self.open_reason_for:
            return
        pressed = (key or "").lower()
        if pressed in ("enter", "return"):
            self.key_confirm_focused()
        elif pressed == "e":
            self.key_edit_focused()
        elif pressed == "n":
            self.key_next_issue()
