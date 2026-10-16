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
import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone

import reflex as rx

from src.clients import service as clients
from src.invoice_extract import checks as checks_mod
from src.invoice_extract import confidence as confidence_mod
from src.invoice_extract import crops as crops_mod
from src.invoice_extract import doc_actions
from src.invoice_extract import extract as extract_mod
from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import normalisers as nz_mod
from src.invoice_extract import pipeline as pipeline_mod
from src.invoice_extract import review_api
from src.invoice_extract import rules as rules_mod
from src.invoice_extract import service as ie
from src.invoice_extract import triage as triage_mod
from setu.foundation import tokens as t
from setu.state.review_glossary import (
    hint_for,
    label_for,
    meaning_for,
    reason_text,
)
from setu.state.shared_upload import SharedUploadState, drop_completed
from setu.state.upload_stage import (
    STAGE_ATTENTION,
    STAGE_EXTRACTING,
    STAGE_FAILED,
    STAGE_QUEUED,
    STAGE_READING,
    STAGE_READY,
    Stage,
    StageFacts,
    derive_stage,
    progress_banner,
)

PERM_UPLOAD = "invoice_extract.upload"
PERM_REVIEW = "invoice_extract.review"

#: The four versions of "a person decided this" the screen can take.
ACTION_CONFIRMED = "confirmed_as_is"
ACTION_EDITED = "edited"
ACTION_NOT_PRINTED = "marked_not_printed"
ACTION_NOT_APPLICABLE = "marked_not_applicable"

#: The reason chips a settled edit offers first; free text rides alongside.
#: The labels are plain words: the code is what the record stores, the label is
#: what the reviewer reads (see ``review_glossary.REASON_TEXT`` for the long
#: version of each).
EDIT_REASONS = (
    ("correct", "It is right as read"),
    ("ocr_error", "Setu misread it"),
    ("wrong_reader", "The wrong reader was used"),
    ("other", "Another reason"),
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

#: Friendly names for the checks, used when a check card has no single field to
#: name (checks_mod.CHECK_LABELS stays the source for the checks strip).
#: One source: the same dict the engine uses for a FAILED check's headline.
CHECK_TITLES: dict[str, str] = dict(checks_mod.REVIEW_FAIL_TITLES)

#: Plain words for a document that was skipped by design (no value extraction).
PLAIN_NOTES = {
    "supporting": "This is a supporting document — kept with the file, not read for values.",
    "ticket": "This is a ticket — kept with the file, not read for values.",
    "not_a_voucher": "This is not a voucher — kept with the file, not read for values.",
    "undetermined": "This page was not identified as an invoice — kept with the file, not read for values.",
}


def _plain_note_for(category: str, status: str, reason: str) -> str:
    """Why this document asks no value questions, in plain words ("" if none)."""
    if status != "skipped":
        return ""
    note = PLAIN_NOTES.get(category or "", "")
    if not note:
        note = (reason or "This document was not extracted for values.").rstrip(".") + "."
    return note + " Nothing was extracted from it, so there are no values to check here."


#: A printed date often carries its weekday ("Fri Apr 17 2026"); the pipeline's
#: own normaliser reads "17 Apr 2026" but not the prefix, and a guessed date is
#: a date whatever the reader wrote in front of it.
#: A printed date often carries its weekday, and a reader sometimes returns the
#: American month-first form ("Fri Apr 17 2026") the pipeline's own normaliser
#: (day-first, ``normalisers._DATE_FORMATS``) cannot read. A guess is still a
#: date, so the WIDGET reads those two shapes too — the pipeline's normaliser is
#: deliberately not touched.
_WEEKDAY_PREFIX = re.compile(
    r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+", re.IGNORECASE,
)
_EXTRA_DATE_FORMATS: tuple[str, ...] = (
    "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%b-%d-%Y", "%B-%d-%Y",
)


def _iso_from_guess(text: str) -> str:
    """The date a read guessed at, as ISO ("" when it is not a date at all)."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    stripped = _WEEKDAY_PREFIX.sub("", raw).strip()
    for candidate in (raw, stripped):
        iso = nz_mod.normalise_date(candidate)
        if iso:
            return iso
        for fmt in _EXTRA_DATE_FORMATS:
            try:
                return datetime.strptime(candidate.rstrip("."), fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def _dmy(iso: str) -> str:
    """ISO → dd-mm-yyyy — the order this screen shows a date in ("" if none)."""
    parts = str(iso or "").split("-")
    if len(parts) != 3 or not all(parts):
        return ""
    return f"{parts[2]}-{parts[1]}-{parts[0]}"


def _box_tuple(row: dict):
    """A {page,x,y,w,h} row as the (x, y, w, h) tuple the geometry helpers want.

    None when the row carries no usable box, so a caller never compares a
    degenerate rectangle against a real one.
    """
    try:
        box = (
            float(row.get("x") or 0.0), float(row.get("y") or 0.0),
            float(row.get("w") or 0.0), float(row.get("h") or 0.0),
        )
    except (TypeError, ValueError):
        return None
    return box if box[2] > 0 and box[3] > 0 else None


def _looks_like_a_field_key(value: str) -> bool:
    """Is this string one of the extraction's field keys?

    Used to keep stored check inputs from printing raw keys at a reviewer: a
    key list is shown as "3 field(s)", because the card names the fields in
    plain words beside it.
    """
    key = str(value or "")
    if not key:
        return False
    return key in fv2.INVOICE_KEYS or key.startswith("line.") or key.startswith("invoice_")


def _is_date_field(field_key: str) -> bool:
    """Is this field a DATE by its own spec (fields_v2.KIND_DATE)?

    Only fields the pipeline declares as dates get a picker. ``reference_no_date``
    is deliberately TEXT ("Reference no. and date (as printed)"): it carries a
    reference NUMBER beside the date, and a picker would throw the number away.
    """
    try:
        return fv2.kind_of(field_key) == fv2.KIND_DATE
    except Exception:  # noqa: BLE001 — an unknown field is simply not a date
        return False


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
    #: The PERSISTED chain state — what makes "triage done, extraction pending"
    #: visible instead of looking finished.
    chain_state: str = ""
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
    #: When the upload was queued — the source of the elapsed-time words.
    created_at: str = ""
    # --- the derived stage (see setu/state/upload_stage.py) ---------------
    stage_key: str = ""
    stage_label: str = ""
    stage_detail: str = ""
    stage_running: bool = False
    stage_needs_action: bool = False
    stage_done: int = 0
    stage_total: int = 0
    stage_show_progress: bool = False
    stage_pct: int = 0
    elapsed_label: str = ""


@dataclass
class JobStageRow:
    """The COMPACT, live row the Files list renders.

    Kept apart from ``JobRow`` on purpose: the 2-second poll rebuilds ONLY this
    list, so a tick never re-serializes the big state (the shared upload/viewer
    fields) or the full row model — just one small row per file.
    """

    upload_id: int = 0
    filename: str = ""
    client_label: str = ""
    stage_key: str = ""
    stage_label: str = ""
    stage_detail: str = ""
    running: bool = False
    needs_action: bool = False
    can_resume: bool = False
    can_retry: bool = False
    started: bool = False
    done: int = 0
    total: int = 0
    show_progress: bool = False
    pct: int = 0
    progress_label: str = ""
    elapsed_label: str = ""
    counts_line: str = ""
    error: str = ""


def stage_of_row(row: JobStageRow) -> Stage:
    """The Stage a rendered row carries — used for the live banner."""
    return Stage(
        key=row.stage_key, label=row.stage_label, detail=row.stage_detail,
        running=row.running, needs_attention=row.needs_action,
        done=row.done, total=row.total, show_progress=row.show_progress,
    )


def _elapsed_label(created_at: str, *, now: datetime | None = None) -> str:
    """How long this upload has been going — short and human ("2m 30s").

    Only shown while a stage is RUNNING; a finished file shows its counts
    instead.
    """
    text = str(created_at or "").strip()
    if not text:
        return ""
    try:
        started = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - started).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _stage_facts(
    *, started: bool, run_status: str, chain_state: str, pages_ok: int, pages_total: int,
    pages_failed: int, extraction: dict,
) -> StageFacts:
    """The stage's inputs, from the counts the review API already returns.

    The extraction's own denominator is the number of extraction ROWS: that is
    the set of documents the extraction actually works through. (Counting the
    upload's documents instead would leave a supporting or excluded document
    that has no row waiting for ever, so a finished file could never read as
    ready.)
    """
    counts: dict[str, int] = {}
    for key in ("queued", "running", "complete", "partial", "failed", "skipped"):
        try:
            counts[key] = int((extraction or {}).get(key) or 0)
        except (TypeError, ValueError):
            counts[key] = 0
    try:
        total = int((extraction or {}).get("total") or sum(counts.values()))
    except (TypeError, ValueError):
        total = sum(counts.values())
    terminal = counts["complete"] + counts["partial"] + counts["failed"] + counts["skipped"]
    return StageFacts(
        started=started, run_status=run_status, chain_state=chain_state,
        pages_ok=pages_ok, pages_total=pages_total, pages_failed=pages_failed,
        documents=total, documents_terminal=terminal,
        documents_complete=counts["complete"], documents_failed=counts["failed"],
        documents_running=counts["running"], documents_queued=counts["queued"],
    )


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
class HandwritingSuggestion:
    """A handwritten reading Setu can offer for one empty field.

    It is a SUGGESTION and nothing more: the reviewer presses "Use this
    reading" to put it in the box, and the value still only becomes a decision
    when Confirm is pressed. Handwriting is never a value on its own.
    """

    field_key: str = ""
    label: str = ""
    input_key: str = ""
    reading: str = ""
    is_date: bool = False


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
    implicated_labels: list[str] = dc_field(default_factory=list)
    figures: str = ""
    page: int = 0
    handwriting_text: str = ""
    has_handwriting: bool = False
    #: Plain words: the friendly meaning of the field, under the title.
    hint: str = ""
    #: The kind of problem, in plain words ("A required value is not there").
    kind_label: str = ""
    #: Handwritten readings offered for the empty fields this card is about.
    suggestions: list[HandwritingSuggestion] = dc_field(default_factory=list)

    #: The input box this card owns, keyed by DOCUMENT + field (never the
    #: field key alone — two documents can both ask about `invoice_total`).
    input_key: str = ""
    #: What each read produced, and the guess the box starts with — the card
    #: says "Best guess, please check" while the box still holds it.
    read_a: str = ""
    read_b: str = ""
    best_guess: str = ""
    reads_line: str = ""
    #: A DATE field (fields_v2.KIND_DATE): the card asks with a date picker and
    #: ``guess_iso`` is the read's best guess as ISO, for the picker to start on.
    is_date: bool = False
    guess_iso: str = ""

    #: The field's own box, cropped out of the upright page with padding —
    #: a real crop, not the whole page positioned by CSS.
    crop_src: str = ""


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
    #: The field's plain meaning, for the info icon beside the value.
    hint: str = ""


@dataclass
class CheckLine:
    check_key: str = ""
    label: str = ""
    status: str = ""
    detail: str = ""
    ok: bool = False
    #: A person accepted this check's figures as read: the strip shows it
    #: amber while the ORIGINAL status and detail stay as computed.
    accepted: bool = False
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


#: Upright page renders, keyed (upload_id, page). PIL images — process-local,
#: deliberately NOT a state field (state fields must be serialisable).
_RENDER_CACHE: dict = {}


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
    if state == "focused":
        # The box of the field the card on the right is about. A THIN OUTLINE
        # with a transparent fill: the value inside the box is the whole point
        # of the box, and a tinted fill plus a label on top made it unreadable.
        # The label rides OUTSIDE the box (see ``_region_box``).
        return {
            "position": "absolute",
            "border": f"2px solid {t.Color.ACCENT.value}",
            "background": "transparent",
            "border_radius": "4px",
            "pointer_events": "none",
            "z_index": "4",
            # The label tag rides above the box: when the viewer scrolls the box
            # to the top of its window, this keeps room for the tag.
            "scroll_margin_top": "26px",
        }
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


def _scope(document) -> bool:
    """Is this document IN the review scope at all?

    THE single definition the whole screen uses: extractable category and not
    excluded. "To review" is then one more step — see :func:`_counts`.

    Accepts either shape the screen holds documents in: the raw triage dicts the
    Files view works with, or the ``DocRow``s the review screen builds.
    """
    if isinstance(document, dict):
        excluded = bool(document.get("excluded"))
        category = document.get("category")
    else:
        excluded = bool(getattr(document, "excluded", False))
        category = getattr(document, "category", None)
    return not excluded and category in doc_actions.EXTRACTABLE_CATEGORIES


def _counts(documents, *, reviewed_of) -> tuple[int, int]:
    """(still to review, reviewed) — computed ONCE for every counter.

    "To review" = in the review scope AND not marked reviewed. The Files row,
    the top bar and the document footer all read these two numbers, so the same
    file can never report two different totals.
    """
    scope = [d for d in documents if _scope(d)]
    reviewed = sum(1 for d in scope if reviewed_of(d))
    return len(scope) - reviewed, reviewed


class InvoiceReviewState(SharedUploadState):
    """The v2 Files view + review screen."""

    v2_enabled: bool = False
    v2_reason: str = ""

    # --- Files view ---------------------------------------------------
    client_options: list[ClientPick] = []
    client_label_map: dict[str, str] = {}
    upload_client_id: int = 0
    jobs: list[JobRow] = []
    #: The live rows the list RENDERS — the poll's only write target.
    job_rows: list[JobStageRow] = []
    poll_active: bool = False
    #: A tick is executing right now (overlapping ticks are dropped).
    poll_in_flight: bool = False
    #: Bumped every time the timer mounts, so a re-entered page can tell its own
    #: loop from a leftover one.
    poll_token: int = 0
    upload_error: str = ""
    #: Amendment 5 — WHO reads the files, on the Files view itself.
    models_line: str = ""
    models_warning: str = ""

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
    #: Documents that still HAVE needs-you items — reported separately, never
    #: mixed into "to review".
    needs_you_count: int = 0
    doc_label: str = ""
    doc_category: str = ""
    doc_category_label: str = ""
    doc_direction: str = ""
    doc_direction_label: str = ""
    doc_direction_basis: str = ""
    #: The direction was inherited from another document in the same file (the
    #: person answered once for the file) — the label says so, and the card is
    #: still offered so any one document can be changed on its own.
    doc_direction_inherited: bool = False
    doc_needs_a_human: bool = False
    doc_alias_text: str = ""
    doc_alias_refusal: str = ""
    remember_alias: bool = False
    page_line: str = ""
    page_tag: str = ""
    #: Plain words for a skipped document (no values were extracted): "" for
    #: an extracted invoice.
    plain_note: str = ""
    cards: list[CardRow] = []
    settled: list[ValueRow] = []
    not_applicable: list[ValueRow] = []
    derived: list[ValueRow] = []
    checks: list[CheckLine] = []
    settled_document_order: bool = False
    settled_collapsed: bool = False
    drawer_open: bool = False
    #: Keyed by "<document_id>:<field_key>" — NEVER by the field key alone.
    #: Two documents can both ask about `invoice_total`; text typed on one
    #: must never appear on the other (it did, before this key).
    field_inputs: dict[str, str] = {}
    #: "Type the value…" under the one box it applies to, keyed the same way.
    input_warnings: dict[str, str] = {}
    #: DATE fields only: the picker's own value (ISO — what an <input type=date>
    #: holds) and the dd-mm-yyyy text shown beside it. ``field_inputs`` keeps
    #: the same ISO, so the confirm path stores the pipeline's date form.
    date_inputs: dict[str, str] = {}
    date_labels: dict[str, str] = {}
    edited_was: dict[str, str] = {}
    open_reason_for: str = ""          # the settled row whose reason chips are open
    edit_value: str = ""
    can_mark_reviewed: bool = False
    refusal: str = ""
    flash: str = ""
    #: A short line about what just happened ("Marked reviewed — now on document
    #: 3 of 22"), shown above the document and cleared on the next move.
    notice: str = ""
    error: str = ""
    #: Which card on the right the viewer's heavy box belongs to.
    focused_field_key: str = ""

    # --- viewer -------------------------------------------------------
    view_page: int = 1
    view_page_count: int = 0
    view_regions: list[RegionBox] = []
    view_page_chips: list[PageChip] = []
    page_srcs: dict[str, str] = {}
    page_info_rows: list[dict] = []
    regions_cache: dict = {}
    #: field_key -> {page, x, y, w, h} for EVERY field of the open document.
    field_boxes: dict = {}
    #: The open document's handwriting annotations (page, box, text). Private:
    #: the screen reads it, the frontend never needs it.
    _annotations: list[dict] = []
    #: The open document's raw check rows (detail + inputs), for the cards.
    check_rows_raw: list[dict] = []
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
            for c in clients.list_clients(viewer=self._viewer(), include_inactive=False)
        ]
        self.client_label_map = {
            str(c["client_id"]): str(c["legal_name"])
            for c in clients.list_clients(viewer=self._viewer(), include_inactive=True)
        }
        self._read_flag()
        self._load_models_line()
        self._load_jobs()

    def _load_models_line(self) -> None:
        """Which model reads the triage page, read A and read B — and a warning
        when read A and read B are the SAME provider: the two reads exist to be
        independent, and one provider means agreement proves less.
        """
        from src.ai_models import defaults as ai_defaults
        from src.ai_models import service as ai_service

        names = {
            "invoice_triage": "triage",
            "invoice_extraction_visual": "read A",
            "invoice_extraction_verify": "read B",
        }
        parts: list[str] = []
        providers: dict[str, str] = {}
        for key in ai_defaults.VISION_SEED_TOUCHPOINTS:
            try:
                assignment = ai_service.get_assignment(key) or {}
            except Exception:  # noqa: BLE001
                assignment = {}
            # The assignment row's column is `primary_model`; the extra
            # `primary_model_id` lookup covers callers that remap it.
            model = assignment.get("primary_model") or assignment.get("primary_model_id")
            provider = assignment.get("primary_provider")
            parts.append(f"{names.get(key, key)}: {model or 'not assigned yet'}")
            if provider:
                providers[key] = str(provider)
        self.models_line = "Reading is done by — " + " · ".join(parts)
        read_a = providers.get("invoice_extraction_visual")
        read_b = providers.get("invoice_extraction_verify")
        self.models_warning = (
            f"Read A and read B are both {read_a} — the two reads are no longer "
            "independent, so agreement between them proves less."
            if read_a and read_a == read_b else ""
        )

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
        """Is any file on the list in a RUNNING stage? Drives the poll."""
        return any(row.running for row in self.job_rows)

    @rx.var
    def live_progress_banner(self) -> str:
        """One live line for the Files list; "" when nothing is running, so the
        line disappears by itself."""
        if not self.job_rows:
            return ""
        return progress_banner(
            [stage_of_row(row) for row in self.job_rows],
            [row.filename for row in self.job_rows],
        )

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
            self.job_rows = []
            self.poll_active = False
            return
        rows: list[JobRow] = []
        compact: list[JobStageRow] = []
        for upload in ie.list_uploads(client_id=self.upload_client_id, viewer=self._viewer()):
            row, stage_row = self._job_row(upload)
            rows.append(row)
            compact.append(stage_row)
        order = sorted(
            range(len(rows)),
            key=lambda i: (-rows[i].running, not rows[i].started, -rows[i].upload_id),
        )
        self.jobs = [rows[i] for i in order]
        self.job_rows = [compact[i] for i in order]
        # ONE loop, and only while something genuinely runs: this is what the
        # rendered timer is gated on.
        self.poll_active = any(r.running for r in self.job_rows)

    def _stage(self, row: JobRow, extraction: dict) -> Stage:
        """The row's stage, derived from facts (see upload_stage.derive_stage)."""
        return derive_stage(_stage_facts(
            started=row.started, run_status=row.status, chain_state=row.chain_state,
            pages_ok=row.pages_ok, pages_total=row.pages_total, pages_failed=row.pages_failed,
            extraction=extraction,
        ))

    def _stage_row(self, job: JobRow, extraction: dict) -> JobStageRow:
        stage = self._stage(job, extraction)
        return JobStageRow(
            upload_id=job.upload_id, filename=job.filename, client_label=job.client_label,
            stage_key=stage.key, stage_label=stage.label, stage_detail=stage.detail,
            running=stage.running, needs_action=stage.needs_attention,
            can_resume=job.can_resume, can_retry=job.can_retry, started=job.started,
            done=stage.done, total=stage.total, show_progress=stage.show_progress,
            pct=stage.percent,
            progress_label=job.progress_label,
            elapsed_label=_elapsed_label(job.created_at) if stage.running else "",
            counts_line=job.summary_label, error=job.error,
        )

    def _jobs_free_stage_rows(self) -> tuple[list[JobStageRow], bool]:
        """Rebuild ONLY the compact rows, from a read-only query.

        This is the poll's whole body: it touches ``review_api`` exactly the way
        the first paint does (so it inherits the app's own connection settings —
        no new locking behaviour, safe while the pipeline writes), writes ONE
        small state variable, and never goes near ``jobs`` or the shared
        upload/viewer state. A row whose read fails keeps the stage it had and
        is tried again on the next tick (the second value says so).
        """
        previous = {row.upload_id: row for row in self.job_rows}
        out: list[JobStageRow] = []
        read_failed = False
        for job in self.jobs:
            try:
                progress = review_api.get_upload_progress(job.upload_id)
            except Exception:  # noqa: BLE001 — keep the last known stage
                read_failed = True
                if job.upload_id in previous:
                    out.append(previous[job.upload_id])
                continue
            out.append(self._stage_row(job, progress.get("extraction") or {}))
        return out, read_failed

    def _job_row(self, upload: dict) -> tuple[JobRow, JobStageRow]:
        upload_id = int(upload["upload_id"])
        row = JobRow(
            upload_id=upload_id,
            filename=str(upload.get("filename") or ""),
            client_label=self._label(upload.get("client_id")),
            created_at=str(upload.get("created_at") or ""),
        )
        try:
            progress = review_api.get_upload_progress(upload_id)
        except Exception as exc:  # noqa: BLE001 — one bad row must not hide the list
            row.error = str(exc)
            return row, self._stage_row(row, {})
        triage = progress["triage"]
        review = progress["review"]
        extraction = progress["extraction"]
        row.chain_state = str(progress.get("chain_state") or "")
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
        # The counters on this row use the SAME definition as the review
        # screen's, computed from the documents themselves rather than from
        # the progress call's own tally.
        try:
            documents = triage_mod.get_triage(upload_id).get("documents") or []
            row.to_review, row.reviewed = _counts(
                documents,
                reviewed_of=lambda d: doc_actions.is_reviewed(int(d["document_id"])),
            )
        except Exception:  # noqa: BLE001 — the progress tallies remain as fallback
            pass
        self._label_job(row, extraction)
        return row, self._stage_row(row, extraction)

    def _label_job(self, row: JobRow, extraction: dict) -> None:
        """The job's own words — one stage, derived from FACTS.

        The label used to come from the PERSISTED chain state alone, so a file
        whose triage run and extraction had both finished still said "Working —
        31 of 31 pages read" while its chain row was stale. The stage now comes
        from the triage run row and the extraction rows (see
        ``upload_stage.derive_stage``); the chain state is only one input.
        """
        chain = str(row.chain_state or "")
        if chain == "interrupted" or row.status == triage_mod.RUN_INTERRUPTED:
            row.can_resume = True
        if row.status == triage_mod.RUN_PARTIAL or row.pages_failed:
            row.can_resume = True
            row.can_retry = row.pages_failed > 0

        stage = self._stage(row, extraction)
        row.stage_key = stage.key
        row.stage_label = stage.label
        row.stage_detail = stage.detail
        row.stage_running = stage.running
        row.stage_needs_action = stage.needs_attention
        row.stage_done = stage.done
        row.stage_total = stage.total
        row.stage_show_progress = stage.show_progress
        row.stage_pct = stage.percent
        row.running = stage.running
        row.status_label = stage.label
        pages = (
            f"{row.pages_ok} of {row.pages_total} page(s) read"
            if row.pages_total else f"{row.pages_ok} page(s) read"
        )
        if stage.key in (STAGE_READY, STAGE_ATTENTION):
            row.progress_label = pages
            if row.fallback_pages:
                row.progress_label += f" — {row.fallback_pages} by the fallback model"
        else:
            row.progress_label = stage.detail
        row.elapsed_label = _elapsed_label(row.created_at) if stage.running else ""
        # The counts line — the numbers a reviewer works from — once the file is
        # past reading. It is kept for every stage so the row can show it the
        # moment the stage turns ready.
        row.summary_label = (
            f"{row.documents} document(s) · {row.excluded} excluded · "
            f"{row.to_review} to review · {row.reviewed} reviewed"
        )
        if row.with_needs_you:
            row.summary_label += f" · {row.with_needs_you} need you"

    # ------------------------------------------------------------------
    # Live progress: ONE poll, and it writes ONE small variable
    # ------------------------------------------------------------------
    @rx.event
    def start_progress_poll(self):
        """The timer mounted: this page owns one loop.

        Re-entering the page mounts a NEW timer (and unmounts the old one, which
        calls ``stop_progress_poll``), so a stale loop can never keep ticking.
        """
        self.poll_token += 1
        self.poll_active = any(row.running for row in self.job_rows)

    @rx.event
    def stop_progress_poll(self):
        """The timer unmounted (the page was left): stop ticking."""
        self.poll_active = False

    @rx.event
    def poll(self):
        """One tick: re-read ONLY the compact stage rows while something runs.

        It stops by itself when nothing is running, drops overlapping ticks, and
        shows the last known stage when a read fails (the next tick tries again).
        """
        if not self.poll_active or self.poll_in_flight:
            return
        self.poll_in_flight = True
        try:
            rows, read_failed = self._jobs_free_stage_rows()
            self.job_rows = rows
            # Tick again while something runs, or while a read failed (the next
            # tick tries again); otherwise the loop stops by itself.
            self.poll_active = read_failed or any(row.running for row in rows)
        except Exception:  # noqa: BLE001 — a failed read must not blank the list
            self.poll_active = True
        finally:
            self.poll_in_flight = False

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
            # Short: the LIVE line above the list carries the running stages now
            # (and disappears when nothing is running).
            self.flash = f"{len(queued)} file(s) queued for reading."
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
        # The index goes with the lists it points into (see ``back_to_files``):
        # a stale index over a freshly-loaded list is what turned one earlier
        # failure into an IndexError on every update that followed.
        self.current_document_id = 0
        self.current_index = 0
        self.document_count = 0
        self.documents = []
        self.cards = []
        self.settled = []
        self.check_rows_raw = []
        self.field_boxes = {}
        self._annotations = []
        self.field_inputs = {}
        self.edited_was = {}
        self.open_reason_for = ""
        self.drawer_open = False
        self.refusal = ""
        self.flash = ""
        self.notice = ""
        self._load_upload()
        self._load_review(first=True)

    @rx.event
    def back_to_files(self):
        """Close the review screen and go back to the Files view.

        The order matters: the index and the counts are cleared BEFORE the lists
        they describe, so there is never an update where ``documents`` is empty
        while ``current_index`` still points into it. This handler used to assign
        ``self._page_srcs`` — a field that does not exist (the real one is
        ``page_srcs``), so Reflex raised SetUndefinedStateVarError here and
        stopped half-way, leaving exactly that broken state behind.
        """
        self.open_upload_id = 0
        self.current_document_id = 0
        self.current_index = 0
        self.document_count = 0
        self.to_review_count = 0
        self.reviewed_count = 0
        self.needs_you_count = 0
        self.documents = []
        self.cards = []
        self.settled = []
        self.not_applicable = []
        self.derived = []
        self.checks = []
        self.check_rows_raw = []
        self.field_boxes = {}
        self._annotations = []
        self.field_inputs = {}
        self.input_warnings = {}
        self.date_inputs = {}
        self.date_labels = {}
        self.edited_was = {}
        self.open_reason_for = ""
        self.drawer_open = False
        self.refusal = ""
        self.flash = ""
        # The short line about the last action belongs to the review screen: a
        # move-on line set AFTER this call (mark reviewed) still shows.
        self.notice = ""
        self.notice = ""
        self.view_regions = []
        self.view_page_chips = []
        self.page_srcs = {}
        self.page_info_rows = []
        self.regions_cache = {}
        self._annotations = []
        self._load_jobs()

    def _load_upload(self) -> None:
        upload = ie.get_upload(self.open_upload_id, viewer=self._viewer()) or {}
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
        return 0 < self.current_index < len(self.documents)

    @rx.var
    def can_go_next(self) -> bool:
        return 0 <= self.current_index < len(self.documents) - 1

    @rx.var
    def review_counter(self) -> str:
        """The footer's counters — the same numbers as the Files row's."""
        text = f"{self.to_review_count} to review · {self.reviewed_count} reviewed"
        if self.needs_you_count:
            text += f" · {self.needs_you_count} need you"
        return text

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
        next unreviewed one, else the first that asks — wrapping once.

        Bounds-safe by construction: the LIST is the authority, never a stored
        count or index. An empty list, a shorter list, or an index left over
        from an earlier document all answer "no next issue" instead of raising
        IndexError (they used to raise on every update after a failed handler).
        """
        count = len(self.documents)
        if count == 0:
            return None
        if 0 <= self.current_index < count:
            order = list(range(self.current_index + 1, count)) + list(range(0, self.current_index))
        else:
            order = list(range(count))
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
        self.notice = ""
        self.open_reason_for = ""
        self.drawer_open = False
        # The typed text belongs to the document it was typed on: a new
        # document starts with EMPTY boxes (the old bare-key dict carried
        # one document's text onto another — seen in the review log).
        self.field_inputs = {}
        self.input_warnings = {}
        self.date_inputs = {}
        self.date_labels = {}
        self._load_document()

    def _load_review(self, *, first: bool = False) -> None:
        self._load_documents()
        # THE counters: the same two numbers the Files row shows for this file.
        self.to_review_count, self.reviewed_count = _counts(
            self.documents, reviewed_of=lambda d: d.reviewed,
        )
        self.needs_you_count = sum(1 for d in self.documents if d.needs_you and not d.excluded)
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
        self.plain_note = ""
        self.refusal = ""
        if not self.current_document_id:
            return
        order = "document" if self.settled_document_order else "confidence"
        try:
            model = rules_mod.get_review_model(self.current_document_id, order=order)
        except Exception as exc:  # noqa: BLE001
            self._annotations = []
            # A document with no extraction yet is a normal state mid-run — and
            # the header must NOT keep the previous document's words while the
            # viewer already shows this one's pages (seen in a screenshot pass).
            # A SUPPORTING page is the exception: it will never be extracted,
            # so its missing extraction row is a finished state, not a wait —
            # it gets the plain-words panel instead of a refusal.
            blank = next(
                (d for d in self.documents if d.document_id == self.current_document_id),
                DocRow(),
            )
            self.plain_note = (
                _plain_note_for(blank.category, "skipped", "")
                if blank.category in PLAIN_NOTES else ""
            )
            self.refusal = "" if self.plain_note else str(exc)
            self.doc_label = blank.label
            self.doc_category = blank.category
            self.doc_category_label = blank.category_label
            self.doc_direction = ""
            self.doc_direction_label = "No extraction yet" if not self.plain_note else "Not extracted"
            self.doc_direction_basis = ""
            self.doc_needs_a_human = False
            self.page_tag = ""
            self.page_line = (
                f"{blank.label or 'This document'} is kept with the file — no values to read."
                if self.plain_note
                else f"{blank.label or 'This document'} has no extraction to review yet."
            )
            # A kept-with-the-file page can be marked reviewed; a document
            # still waiting for its extraction cannot (it has no questions yet).
            self.can_mark_reviewed = bool(self.plain_note)
            self._load_field_boxes()
            self._load_viewer()
            return

        doc = next((d for d in self.documents if d.document_id == self.current_document_id), DocRow())
        self.doc_label = doc.label
        self.doc_category = doc.category
        self.doc_category_label = doc.category_label
        self._load_field_boxes()
        direction = model.get("direction") or {}
        self._set_direction(direction, model)
        self.page_tag = self._page_tag(doc)
        self.refusal = "" if not doc.excluded else f"Excluded: {doc.excluded_reason or 'no reason recorded'}"
        self._load_page_line(doc)
        # The document's handwriting annotations (text + box): the cards offer
        # them as a SUGGESTION for an empty field, never as a value.
        self._annotations = [dict(a) for a in (model.get("annotations") or [])]
        self.checks = [
            CheckLine(
                check_key=str(chk["check_key"]),
                label=str(chk.get("label") or chk["check_key"]),
                status=str(chk.get("status")),
                detail=str(chk.get("detail") or ""),
                ok=str(chk.get("status")) == "pass",
                accepted=bool(chk.get("accepted")),
                implicated=list(chk.get("fields_implicated") or []),
            )
            for chk in model.get("checks") or []
        ]
        # The check rows, kept for the cards: the detail AND the inputs.
        self.check_rows_raw = [dict(chk) for chk in model.get("checks") or []]
        # A SKIPPED extraction (supporting / ticket / not-a-voucher) has no
        # fields and no checks: the screen says why in plain words instead of
        # rendering empty question boxes for it.
        self.plain_note = _plain_note_for(
            doc.category,
            str(model.get("extraction_status") or ""),
            str(model.get("extraction_skipped_reason") or ""),
        )
        # GROUP BY CAUSE. A failed check that implicates fields speaks for them:
        # one card titled with the cause, listing the fields it is about, is the
        # honest shape — a reviewer acts on the cause, not on five symptoms.
        grouped: set[str] = set()
        for item in model.get("needs_you") or []:
            if str(item.get("kind")) == "check_failed":
                grouped.update(str(k) for k in (item.get("fields_implicated") or []))
        for item in model.get("needs_you") or []:
            kind = str(item.get("kind") or "")
            # The direction question has its own card (with the two answers and
            # the remember-the-name checkbox); it is not a value card.
            if kind == "direction_unsettled":
                continue
            if kind in ("missing_required", "below_threshold") and (
                str(item.get("field_key") or "") in grouped
            ):
                continue
            self.cards.append(self._card(item, doc))
        # What a read actually produced is PREFILLED into each empty box, so
        # the reviewer edits rather than types — but a box that already holds
        # something (typed text, or a value deliberately cleared) is never
        # overwritten by a reload.
        prefilled = dict(self.field_inputs)
        date_inputs = dict(self.date_inputs)
        date_labels = dict(self.date_labels)
        for card in self.cards:
            if card.is_date:
                # A date card's box is the PICKER, which holds ISO: the guess
                # is parsed into it ("Fri Apr 17 2026" -> 2026-04-17) and the
                # card's own words still show the read text beside it.
                if card.guess_iso and card.input_key not in prefilled:
                    prefilled[card.input_key] = card.guess_iso
                    date_inputs[card.input_key] = card.guess_iso
                    date_labels[card.input_key] = _dmy(card.guess_iso)
                continue
            if card.best_guess and card.input_key not in prefilled:
                prefilled[card.input_key] = card.best_guess
        self.field_inputs = prefilled
        self.date_inputs = date_inputs
        self.date_labels = date_labels
        for item in model.get("settled") or []:
            self.settled.append(self._value_row(item))
        for item in model.get("not_applicable") or []:
            reason = str(item.get("reason") or "no reason recorded")
            self.not_applicable.append(ValueRow(
                field_key=str(item["field_key"]),
                label=label_for(str(item["field_key"])),
                tag="not_applicable",
                # The reason is the RULE's own words, quoted as stored (for
                # example "inter-state supply (Delhi to Uttar Pradesh)").
                reason=reason,
            ))
        for item in model.get("derived") or []:
            self.derived.append(ValueRow(
                field_key=str(item["key"]),
                label=str(item.get("label") or item["key"]),
                value=str(item.get("value") or ""),
                tag="derived",
            ))
        self.can_mark_reviewed = not self.cards and not doc.excluded
        # The first thing that needs a person is focused: its box is the heavy
        # one in the viewer, so the card and the page agree from the start. A
        # card whose value was never extracted has no box, so the first card
        # that HAS one wins.
        self.focused_field_key = next(
            (c.field_key for c in self.cards if c.field_key in self.field_boxes),
            self.cards[0].field_key if self.cards else "",
        )
        self._load_viewer()

    def _set_direction(self, direction: dict, model: dict) -> None:
        value = str(direction.get("direction") or "undetermined")
        self.doc_direction = value
        self.doc_direction_inherited = bool(direction.get("inherited"))
        label = {
            "sales": "Sales (outward)",
            "purchase": "Purchase (inward)",
            "likely_sales": "Likely sales — needs a person",
            "likely_purchase": "Likely purchase — needs a person",
            "undetermined": "Not settled",
        }.get(value, value)
        # An inherited answer SAYS it was inherited: the person answered once
        # for the file, and this document is following it (not their own word).
        if self.doc_direction_inherited:
            label = f"{label} — same as the rest of this file"
        self.doc_direction_label = label
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
        card.label = label_for(field_key) if field_key else ""
        card.field_id = int(item.get("field_id") or 0)
        card.value = str(item.get("value") or "")
        card.page = int(item.get("page") or doc.first_page or 1)
        card.reason = str(item.get("reason") or item.get("detail") or "")
        card.detail = str(item.get("detail") or "")
        card.check_key = str(item.get("check_key") or "")
        card.implicated = [str(k) for k in (item.get("fields_implicated") or [])]
        # The chips, the title and the hint are the GLOSSARY's words now, not
        # the raw keys: "Line 2 · hsn_sac" reads as "Line 2 — HSN/SAC code".
        card.implicated_labels = [label_for(k) for k in card.implicated]
        card.kind_label = NEEDS_YOU_TITLES.get(kind, kind)
        friendly = ""
        if field_key:
            friendly = label_for(field_key)
            card.title = friendly
        elif card.check_key:
            card.title = CHECK_TITLES.get(card.check_key, card.check_key.replace("_", " ").title())
        card.hint = hint_for(field_key) if field_key else ""
        if card.kind == "check_failed":
            # A failed check's detail renders once, in its own branch; it must
            # NOT also stand in for `reason`, or the same long sentence showed
            # up three times in one card (reason + two detail renders).
            card.reason = str(item.get("reason") or "")
            self._explain_check(card)
        else:
            card.reason = reason_text(kind, field_label=card.title) or str(
                item.get("reason") or item.get("detail") or ""
            )
        # A handwriting card (or a missing date/amount) can offer the reading of
        # the figure that is written there — as a SUGGESTION the reviewer uses
        # with one click and still has to confirm.
        suggested = list(card.implicated) or ([field_key] if field_key else [])
        if card.kind in ("check_failed", "missing_required", "below_threshold"):
            card.suggestions = self._handwriting_suggestions(suggested)
        handwriting = item.get("handwriting") or None
        if handwriting:
            card.has_handwriting = True
            card.handwriting_text = str(handwriting.get("text") or "")
        # The box comes from the FIELD's own row: the review model carries one
        # only for the items that need a person, and a below-threshold item
        # carries none at all.
        box = self.field_boxes.get(field_key) if field_key else None
        if box is not None:
            card.page = int(box.get("page") or card.page or 0)
            self._crop(card, box)
        if not card.field_id and card.implicated:
            # A cause card has no field row of its own: the decision is logged
            # against the first field it names.
            first = self.field_boxes.get(card.implicated[0]) or {}
            card.field_id = int(first.get("field_id") or 0)
        # The input box is keyed by DOCUMENT + field — never by the field key
        # alone (item 4: text must not leak between documents).
        card.input_key = f"{self.current_document_id}:{field_key or card.check_key}"
        if kind in ("missing_required", "below_threshold"):
            card.read_a = str(item.get("value_a") or "")
            card.read_b = str(item.get("value_b") or "")
            # "Show what was read": the merged value if there is one, else the
            # one read that found something — and the card SAYS it is a guess.
            card.best_guess = str(item.get("value") or "") or card.read_a or card.read_b
            shown: list[str] = []
            if card.read_a:
                shown.append(f"Read A: {card.read_a}")
            if card.read_b:
                shown.append(f"Read B: {card.read_b}")
            card.reads_line = " · ".join(shown)
            # A date field asks with a picker (see the view): the guess is
            # parsed to ISO here so the widget has nothing to interpret.
            card.is_date = _is_date_field(field_key)
            if card.is_date:
                card.guess_iso = _iso_from_guess(card.best_guess)
        return card

    def _page_render(self, page: int):
        """The upright page render, cached per (upload, page).

        The renders are PIL images, so they live in a module-level cache — a
        state field must be serialisable, and a render never leaves the process.
        """
        key = (self.open_upload_id, int(page))
        if key in _RENDER_CACHE:
            return _RENDER_CACHE[key]
        rendered = None
        try:
            data = ie.get_upload_bytes(self.open_upload_id, viewer=self._viewer()) or b""
            upload = ie.get_upload(self.open_upload_id, viewer=self._viewer()) or {}
            rendered = crops_mod.render_upright_page(
                data, str(upload.get("source_format") or "pdf"), int(page),
                dpi=review_api.DEFAULT_PAGE_DPI,
            )
        except Exception:  # noqa: BLE001 — no crop is better than a broken screen
            rendered = None
        _RENDER_CACHE[key] = rendered
        return rendered

    def _crop(self, card: CardRow, box: dict) -> None:
        """The field's OWN box, cropped out of the upright page WITH padding.

        Built when the document loads (never on the render path) and only for
        the cards that carry a box. ``crop_region`` caps the long edge, so a
        full-page A4 region does not become a megabyte of data URI.
        """
        crop_box = (
            float(box.get("x") or 0.0), float(box.get("y") or 0.0),
            float(box.get("w") or 0.0), float(box.get("h") or 0.0),
        )
        if crop_box[2] <= 0 or crop_box[3] <= 0:
            return
        rendered = self._page_render(int(box.get("page") or 1))
        if rendered is None:
            return
        try:
            crop = crops_mod.crop_region(rendered, crop_box, region_index=1, max_edge_px=520)
        except Exception:  # noqa: BLE001
            return
        card.crop_src = "data:image/png;base64," + base64.b64encode(crop.png).decode("ascii")

    def _explain_check(self, card: CardRow) -> None:
        """A failed check, in plain words and with its OWN figures.

        ``invoice_doc_checks`` stores the detail line AND the inputs the check
        ran on. The detail is written by the check itself; where it does not
        already carry the figures, they are named here from the stored inputs —
        a reviewer must never have to guess which numbers disagreed.
        """
        row = next(
            (c for c in self.check_rows_raw if str(c.get("check_key")) == card.check_key),
            None,
        )
        if row is None:
            return
        # The card is titled with the FAILURE in plain words (the check's own
        # positive name is what HOLDS, and would read as nonsense over a red
        # cross); the check key is the fallback.
        card.title = CHECK_TITLES.get(
            card.check_key, str(row.get("label") or card.title or card.check_key)
        )
        inputs = row.get("inputs") or {}
        if card.check_key == "provenance_in_region":
            # A plain sentence with readable field labels — NEVER the raw
            # Python list the stored input carries. At most five names, then
            # the count of the rest.
            outside = [str(k) for k in (inputs.get("outside_regions") or [])]
            total = inputs.get("valued_fields")
            if outside and total is not None:
                shown = ", ".join(_field_label(k) for k in outside[:5])
                more = len(outside) - len(outside[:5])
                card.figures = (
                    f"{len(outside)} of {total} read value(s) were outside its region: "
                    f"{shown}" + (f" (+{more} more)" if more else "")
                )
        else:
            parts: list[str] = []
            for key, value in inputs.items():
                label = str(key).replace("_", " ")
                if isinstance(value, (list, tuple)):
                    items = [str(v) for v in value]
                    if items and all(_looks_like_a_field_key(v) for v in items):
                        # A list of FIELD KEYS is named on the card in plain
                        # words instead (see implicated_labels) — repeating them
                        # here as raw keys was what made the box unreadable.
                        parts.append(f"{label}: {len(items)} field(s)")
                    else:
                        shown = ", ".join(items[:5])
                        more = len(items) - len(items[:5])
                        parts.append(f"{label}: {shown}" + (f" (+{more} more)" if more else ""))
                else:
                    parts.append(f"{label}: {value}")
            card.figures = " · ".join(parts)
        detail = str(row.get("detail") or card.detail or "")
        if card.figures and not any(ch.isdigit() for ch in detail):
            # The detail says what is wrong but not by how much — name the
            # figures in it rather than showing a bare sentence.
            detail = f"{detail.rstrip('.')} — {card.figures}." if detail else card.figures
        card.detail = detail

    def _value_row(self, item: dict) -> ValueRow:
        field_key = str(item["field_key"])
        score = item.get("score")
        row = ValueRow(
            field_key=field_key,
            field_id=int(item.get("field_id") or 0),
            label=label_for(field_key),
            value=str(item.get("value") or ""),
            score_label="" if score is None else str(int(round(float(score)))),
            # A HUMAN decision carries its own words ("You confirmed" /
            # "Not printed (you confirmed)") — shown as a chip, never a score.
            tag=str(item.get("human_label") or ""),
            page=int(item.get("page") or 0),
            edited_was=self.edited_was.get(field_key, ""),
            # The info icon beside the value shows this, in the glossary's words.
            hint=hint_for(field_key) or meaning_for(field_key),
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
            regions = self._regions_of(doc)
            for index, region in enumerate(self._regions_of(doc), start=1):
                if int(region["page"]) != page:
                    continue
                box_x = float(region["x"])
                box_y = float(region["y"])
                box_w = float(region["w"])
                box_h = float(region["h"])
                # A document with several regions names them, so "which box is
                # this?" has an answer on the box itself.
                label = ""
                if doc.document_id == self.current_document_id:
                    label = (
                        f"Region {index} of {len(regions)} — Document #{doc.ordinal}"
                        if len(regions) > 1 else f"Document #{doc.ordinal}"
                    )
                boxes.append(RegionBox(
                    page=page, x=box_x, y=box_y, w=box_w, h=box_h,
                    label=label,
                    state=state,
                    style={
                        **_box_style(state),
                        "left": _pct(box_x),
                        "top": _pct(box_y),
                        "width": _pct(box_w),
                        "height": _pct(box_h),
                    },
                ))
        # The focused card's own field box, drawn on top of the document boxes.
        focus = self._focused_box(page)
        if focus is not None:
            box_x, box_y, box_w, box_h, label = focus
            boxes.append(RegionBox(
                page=page, x=box_x, y=box_y, w=box_w, h=box_h, label=label,
                state="focused",
                style={
                    **_box_style("focused"),
                    "left": _pct(box_x),
                    "top": _pct(box_y),
                    "width": _pct(box_w),
                    "height": _pct(box_h),
                },
            ))
        return boxes

    def _focused_box(self, page: int):
        """(x, y, w, h, label) of the focused card's field box on this page."""
        if not self.focused_field_key:
            return None
        for card in self.cards:
            if card.field_key != self.focused_field_key or card.page != page:
                continue
            box = self.field_boxes.get(card.field_key) or None
            if box is None:
                return None
            return (
                float(box["x"]), float(box["y"]), float(box["w"]), float(box["h"]),
                f"{card.label} — the card you are on",
            )
        return None

    def _load_field_boxes(self) -> None:
        """Every field's stored page box, so a card can point at the page.

        The review model carries boxes only for the items that need a person;
        the viewer has to highlight the box of ANY field a card is about, and
        of the fields a failed check implicates.
        """
        try:
            self.field_boxes = rules_mod.field_boxes(self.current_document_id)
        except Exception:  # noqa: BLE001
            self.field_boxes = {}

    @rx.event
    def focus_field(self, field_key: str):
        """Point the viewer at a field: switch to its page and box it."""
        self.focused_field_key = field_key
        box = self.field_boxes.get(field_key) or {}
        page = int(box.get("page") or 0)
        if page:
            self.view_page = page
        self._show_view_page()

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
    def set_field_input(self, value: str, input_key: str):
        """The card inputs bind as ``on_change={value}`` — Reflex supplies the
        value first, so the document+field key rides as a partial argument."""
        self.field_inputs = {**self.field_inputs, input_key: value}
        if input_key in self.input_warnings:
            self.input_warnings = {
                k: v for k, v in self.input_warnings.items() if k != input_key
            }

    def _clear_input(self, input_key: str) -> None:
        self.field_inputs = {k: v for k, v in self.field_inputs.items() if k != input_key}
        self.input_warnings = {k: v for k, v in self.input_warnings.items() if k != input_key}
        self.date_inputs = {k: v for k, v in self.date_inputs.items() if k != input_key}
        self.date_labels = {k: v for k, v in self.date_labels.items() if k != input_key}

    @rx.event
    def set_date_input(self, value: str, input_key: str):
        """The date picker's own value, normalised to the day-first ISO form.

        The CONFIRM path is untouched: this stores the ISO string that path
        already stores for a date field (``normalisers.normalise_date`` is the
        pipeline's date form, and it reads ISO back). The dd-mm-yyyy text beside
        the picker is derived here because a Var cannot be formatted in the
        template.
        """
        iso = str(value or "").strip()
        self.date_inputs = {**self.date_inputs, input_key: iso}
        self.date_labels = {**self.date_labels, input_key: _dmy(iso)}
        self.field_inputs = {**self.field_inputs, input_key: iso}
        if input_key in self.input_warnings:
            self.input_warnings = {
                k: v for k, v in self.input_warnings.items() if k != input_key
            }

    @rx.event
    def confirm_card(self, input_key: str):
        """Confirm stores the typed value THROUGH the service layer (one
        transaction: reviewed value on the field row, human-reviewed marker,
        correction log) — the screen reloads from the model afterwards."""
        card = next((c for c in self.cards if c.input_key == input_key), None)
        if card is None:
            return
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        value = (self.field_inputs.get(input_key) or "").strip()
        if not value:
            # Write NOTHING; the box says what to do instead.
            self.input_warnings = {
                **self.input_warnings,
                input_key: "Type the value, or use Not printed on the document.",
            }
            return
        try:
            doc_actions.apply_field_decision(
                self.current_document_id, card.field_key, action="confirmed",
                value=value, actor=self._actor(),
            )
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
            return
        self._clear_input(input_key)
        self.flash = f"{card.label or card.field_key}: confirmed."
        self._load_review()

    @rx.event
    def reject_card(self, input_key: str, reason: str):
        """Not printed: an ANSWER, stored through the same service call — no
        value, the human-reviewed marker, the log row."""
        card = next((c for c in self.cards if c.input_key == input_key), None)
        if card is None:
            return
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.apply_field_decision(
                self.current_document_id, card.field_key, action="not_printed",
                value=None, actor=self._actor(),
            )
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
            return
        self._clear_input(input_key)
        self.flash = f"{card.label or card.field_key}: recorded as not printed on the document."
        self._load_review()

    @rx.event
    def accept_check(self, check_key: str):
        """A person accepted a check's figures as read: recorded ON the check
        row (actor + time), the card drops, the strip shows it amber."""
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.accept_check(
                self.current_document_id, check_key, actor=self._actor(),
            )
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
            return
        self.flash = "The figures were accepted as read."
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
            # The answer is for the FILE: the document answered on is settled
            # explicitly, the rest of the upload inherits it where nothing the
            # person or the rule decided already stands (see review_api).
            result = review_api.confirm_direction_for_upload(
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
        count = int(result.get("inherited_count") or 0)
        self.flash = (
            f"Direction settled as {direction}."
            + (f" {count} other document(s) in this file now say the same." if count else "")
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
        """Mark this document reviewed and MOVE ON to the next one.

        The move follows document order: the first not-yet-reviewed, not-excluded
        document after this one; failing that the first earlier one; failing that
        the whole file is done and the screen goes back to the Files list with a
        line saying so. It never raises on a short, empty or stale list.
        """
        if self.open_read_only:
            self.refusal = "This file has been exported — its history must not move."
            return
        try:
            doc_actions.mark_reviewed(self.current_document_id, actor=self._actor())
        except Exception as exc:  # noqa: BLE001
            self.refusal = str(exc)
            self._load_review()
            return
        self._load_review()
        target = self._next_unreviewed_index()
        if target is None:
            self.back_to_files()
            self.notice = "All documents in this file are reviewed"
            return
        doc = self.documents[target]
        self._select(doc.document_id)
        self.notice = (
            f"Marked reviewed — now on document {doc.ordinal} of {len(self.documents)}"
        )

    def _next_unreviewed_index(self):
        """The next document to review after this one, wrapping once, else None.

        Document order, not "next issue": the first not-yet-reviewed and not
        excluded document after the current one, then the first earlier one.
        Bounds-safe: an empty list or an index left over from another document
        answers None rather than indexing.
        """
        count = len(self.documents)
        if count == 0:
            return None
        if 0 <= self.current_index < count:
            order = list(range(self.current_index + 1, count)) + list(range(0, self.current_index))
        else:
            order = list(range(count))
        for index in order:
            doc = self.documents[index]
            if not doc.reviewed and not doc.excluded:
                return index
        return None

    @rx.event
    def use_handwriting_reading(self, input_key: str, reading: str, is_date: bool = False):
        """Put a handwritten reading in the box — as a DRAFT, never as a value.

        This is a suggestion the reviewer accepts with one click and then still
        has to Confirm; Setu's rule that a handwritten figure is never a value
        without a human decision is unchanged (nothing is logged as a decision
        here, and Confirm is the only thing that writes one).
        """
        text = str(reading or "").strip()
        if not text or not input_key:
            return
        if is_date:
            iso = _iso_from_guess(text)
            self.date_inputs = {**self.date_inputs, input_key: iso or ""}
            self.date_labels = {**self.date_labels, input_key: _dmy(iso) if iso else text}
            self.field_inputs = {**self.field_inputs, input_key: iso or text}
        else:
            self.field_inputs = {**self.field_inputs, input_key: text}
        self.input_warnings = {
            **self.input_warnings,
            input_key: "From the handwriting — press Confirm if it is right.",
        }

    def _handwriting_suggestions(self, field_keys: list[str]) -> list[HandwritingSuggestion]:
        """The handwritten reading offered for each EMPTY field, from storage.

        An annotation stores the text it carries AND its box, so the reading is
        found by geometry: the annotation whose box sits over the field's own
        box on the same page. A value is never taken from it here.
        """
        out: list[HandwritingSuggestion] = []
        if not self._annotations:
            return out
        for key in field_keys:
            if not key or self._value_of(key):
                continue  # a field that already has a value needs no suggestion
            reading = self._annotation_text_over(key)
            if not reading:
                continue
            out.append(HandwritingSuggestion(
                field_key=key,
                label=label_for(key),
                input_key=f"{self.current_document_id}:{key}",
                reading=reading,
                is_date=_is_date_field(key),
            ))
        return out

    def _value_of(self, field_key: str) -> str:
        for card in self.cards:
            if card.field_key == field_key and (card.read_a or card.read_b or card.best_guess):
                return card.read_a or card.read_b or card.best_guess
        return ""

    def _annotation_text_over(self, field_key: str) -> str:
        """The text of the annotation written over this field's box, if any."""
        box = self.field_boxes.get(field_key)
        if not box:
            return ""
        page = int(box.get("page") or 0)
        target = _box_tuple(box)
        if target is None:
            return ""
        best, best_ratio = "", 0.0
        for ann in self._annotations:
            if int(ann.get("page") or 0) != page:
                continue
            other = _box_tuple(ann)
            if other is None:
                continue
            # Geometry, not guessing: the annotation whose box covers most of
            # the field's own box is the one written on it.
            ratio = crops_mod.overlap_ratio_of(target, other)
            if ratio > best_ratio:
                best, best_ratio = str(ann.get("text_verbatim") or ""), ratio
        return best if best_ratio > 0 else ""

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
            self.confirm_card(self.cards[0].input_key)

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
