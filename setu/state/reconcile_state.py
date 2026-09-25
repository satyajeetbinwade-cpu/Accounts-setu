"""Reconcile state — the guided five-stage flow (the LAST module).

One guided path from source files to a finished report: Context → Upload →
Reconcile → Review → Export. ORCHESTRATION ONLY — it reuses the same engine
and services every other screen uses (``ingestion_ai.normalize_source_file``,
``runner.execute_run``, ``export.export_run``, ``queries``, ``f5``, ``module2``).
No matching logic is recomputed here.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from urllib.parse import quote

import reflex as rx

from src import queries
from src.clients import service as clients
from src.config_loader import load_config
from src.data_paths import source_data_path
from src.export import export_run
from src.f5 import service as f5
from src.ingestion_ai import service as ingestion_ai
from src.ingestion_ai import periods as period_utils
from src.reconciliation import narrative
from src.reconciliation import review
from src.runner import RunExecutionError, execute_run
from src.shared import discovery
from setu.foundation import tokens as _t
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history

STAGES: list[tuple[int, str]] = [
    (1, "Context"),
    (2, "Upload"),
    (3, "Reconcile"),
    (4, "Review"),
    (5, "Export"),
]

_ACCEPT = {
    "text/csv": [".csv"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
}

_SYNC_SOURCE_FOR = {
    "tally": "tally", "gstr2b": "gst_portal", "ims": "gst_portal",
    "form26as": "traces", "tds": "traces",
    "bank": "bank", "vendor_ledger": "tally", "opening_balances": "tally",
    "loan_sheet": "tally", "salary": "tally",
}


def _url_quote(v) -> str:
    return quote(str(v), safe="")


# Foundation semantic roles → token hex. Charts NEVER introduce a new hex.
_ROLE_COLOR = {
    "rule": _t.Color.RULE.value,
    "ai": _t.Color.AI.value,
    "danger": _t.Color.DANGER.value,
    "accent": _t.Color.ACCENT.value,
    "neutral": _t.Color.NEUTRAL.value,
}


@dataclass
class StageChip:
    number: int
    label: str
    reachable: bool
    active: bool


@dataclass
class ContextOption:
    """A select option whose label is human-facing and whose value is the
    raw period key (so the unfiled sentinel can read as a real sentence)."""

    value: str
    label: str


@dataclass
class SlotRow:
    source_type: str
    label: str
    hint: str
    is_books: bool
    files: list[str]
    selected: str
    code: str          # empty | not_ingested | ok | review | partial | wrong
    chip_label: str
    chip_variant: str
    rows: int
    needs_review: int
    required_missing: int
    message: str
    caveats: list[str]
    resolved: bool


@dataclass
class CaveatRow:
    label: str
    detail: str
    source: str


# ---------------------------------------------------------------------------
# Stage 4 — Review (upgraded presentation model)
# ---------------------------------------------------------------------------


@dataclass
class ReviewKpi:
    key: str
    label: str
    value_display: str
    count: int
    count_display: str
    note: str
    muted: bool
    active: bool
    available: bool = True


@dataclass
class CauseSeg:
    index: int
    cause: str
    label: str
    count_display: str
    value_display: str
    percent: float
    width: str
    color_role: str
    group: str
    is_gap: bool
    active: bool


@dataclass
class GroupSeg:
    group: str
    label: str
    hint: str
    count_display: str
    value_display: str
    percent: float
    width: str
    is_gap: bool
    active: bool


@dataclass
class FilterChip:
    facet: str
    value: str
    label: str
    count: int
    count_display: str
    active: bool
    muted: bool


@dataclass
class ActiveFilter:
    facet: str
    value: str
    label: str


@dataclass
class ReviewItem:
    result_id: int
    fingerprint: str
    supplier: str
    gstin: str
    reference: str
    date: str
    books_display: str
    portal_display: str
    difference_display: str
    difference_value: float
    difference_danger: bool
    issue: str
    cause: str
    cause_label: str
    cause_group: str
    confidence_band: str
    confidence_label: str
    match_key: str
    reviewed: bool
    status_label: str
    classification: str
    supplier_group: str
    group_key: str
    bulk_reason: str
    selected: bool
    bulk_blocked: bool


@dataclass
class GroupHeader:
    group: str
    label: str
    count_display: str
    subtotal: str


@dataclass
class ComparisonRow:
    key: str
    side: str
    section: str
    label: str
    books: str
    portal: str
    difference: str
    differs: bool
    material: bool
    overridden: bool
    books_raw: str = ""
    portal_raw: str = ""
    books_editable: bool = False
    portal_editable: bool = False


@dataclass
class KeyChip:
    label: str
    ok: bool


@dataclass
class CorrectedField:
    key: str
    side: str
    label: str


@dataclass
class SummaryBullet:
    text: str


@dataclass
class QualityNoteRow:
    key: str
    title: str
    what: str
    why: str
    how: str
    link: str
    link_label: str


@dataclass
class IntegrityRow:
    label: str
    result: str
    detail: str


class ReconcileState(AuthState):
    """The guided five-stage reconciliation flow."""

    stage: int = 1

    # context
    clients: list[str] = []
    periods: list[str] = []
    recon_types: list[str] = []
    ctx_client: str = ""
    ctx_period: str = ""
    ctx_recon_type: str = ""

    # flow state
    selections: dict[str, str] = {}
    run_id: int = 0
    auto_ran: bool = False
    pause_before_run: bool = False
    rail_return: bool = False
    started_fresh: bool = False
    has_existing_run: bool = False
    existing_run_id: int = 0
    existing_run_when: str = ""

    # stage 2
    slots: list[SlotRow] = []
    upload_slot_label: str = ""
    books_ok: bool = False
    portal_ok: bool = False
    ready: bool = False
    blockers: list[str] = []

    @rx.var
    def period_options(self) -> list[ContextOption]:
        """Periods with human labels — the unfiled sentinel reads as
        "Unfiled (not reconcilable)" rather than a bare "-"."""
        return [ContextOption(value=p, label=period_utils.period_label(p)) for p in self.periods]
    upload_error: str = ""

    # stage 2 — re-run through model (per-file, confirm-gated)
    rerun_source_type: str = ""
    rerun_filename: str = ""
    rerun_open: bool = False
    rerun_busy: bool = False
    rerun_message: str = ""

    # stage 3
    selected_files: list[str] = []

    # stage 4 — raw review data (all rows, unfiltered)
    review_total: int = 0
    review_matched: int = 0
    review_exceptions: int = 0
    caveats: list[CaveatRow] = []
    _review_rows: list[dict] = []

    # stage 4 — headline / cause split / KPIs
    headline_value: str = "₹0.00"
    headline_sub: str = ""
    headline_scope: str = ""
    progress_percent: float = 0.0
    progress_percent_int: int = 0
    progress_label: str = "0 of 0 reviewed"
    cause_segments: list[CauseSeg] = []
    group_segments: list[GroupSeg] = []
    kpis: list[ReviewKpi] = []

    # stage 4 — charts
    classification_slices: list[dict] = []
    classification_colors: list[str] = []
    classification_mode_value: bool = False
    supplier_bars: list[dict] = []
    itc_available: bool = False
    itc_slices: list[dict] = []
    itc_colors: list[str] = []
    itc_display: str = ""
    itc_claimed_display: str = ""

    # stage 4 — filters (URL-addressable)
    filter_view: str = ""            # "" | amount_difference | not_in_books | not_in_portal | matched | reviewed
    search_query: str = ""
    filter_classification: str = ""
    filter_cause: str = ""
    filter_cause_group: str = ""
    filter_confidence: str = ""
    filter_review_state: str = ""
    filter_supplier: str = ""
    sort_key: str = "difference"
    group_by: str = "none"
    active_only: bool = False
    confidence_chips: list[FilterChip] = []
    classification_chips: list[FilterChip] = []
    cause_chips: list[FilterChip] = []
    review_chips: list[FilterChip] = []
    active_filters: list[ActiveFilter] = []
    has_filters: bool = False
    shown_count: int = 0
    total_exception_count: int = 0
    page: int = 1
    page_count: int = 1
    page_label: str = ""

    # stage 4 — items table (paged) + grouping
    exception_rows: list[ReviewItem] = []
    group_headers: list[GroupHeader] = []
    selection: list[int] = []
    bulk_note: str = ""
    bulk_error: str = ""
    bulk_reason: str = ""
    bulk_disabled: bool = False

    # stage 4 — Accountant's Read
    read_text: str = ""
    read_bullets: list[SummaryBullet] = []
    read_source: str = ""
    read_bullets_source: str = ""
    read_ai_available: bool = False
    read_busy: bool = False
    read_note: str = ""
    read_guard_ok: bool = True

    # stage 4 — data quality + integrity
    quality_notes: list[QualityNoteRow] = []
    integrity_rows: list[IntegrityRow] = []
    integrity_verdict: str = ""

    # stage 4 — the evidence drawer
    selected_result_id: int = 0
    drawer_open: bool = False
    drawer_index: int = 0
    detail_supplier: str = ""
    detail_gstin: str = ""
    detail_reference: str = ""
    detail_date: str = ""
    detail_status: str = ""
    detail_classification: str = ""
    detail_cause_label: str = ""
    detail_confidence_band: str = ""
    detail_confidence_label: str = ""
    detail_verdict: str = ""
    detail_reason: str = ""
    detail_key_chips: list[KeyChip] = []
    detail_comparison: list[ComparisonRow] = []
    detail_comparison_collapsed: bool = True
    detail_extra_count: int = 0
    detail_has_material: bool = False
    detail_next_step: str = ""
    detail_step_link: str = ""
    detail_step_link_label: str = ""
    detail_step_badge: str = ""
    detail_step_badge_text: str = ""
    detail_reviewed: bool = False
    detail_note: str = ""
    detail_sources: list[str] = []
    detail_corrected_fields: list[CorrectedField] = []
    detail_position: str = ""
    detail_prev_disabled: bool = False
    detail_next_disabled: bool = False
    detail_next_unreviewed_disabled: bool = False
    detail_has_prev_unreviewed: bool = False

    # stage 4 — inline field editing (preserved; no semantic change)
    edit_side: str = ""
    edit_field: str = ""
    edit_value: str = ""
    edit_reason: str = ""
    edit_open: bool = False
    edit_side_label: str = ""
    edit_field_label: str = ""
    edit_error: str = ""

    # stage 4 — edit history for the open record
    detail_history: list[HistoryEntry] = []

    # stage 4 — deferred URL sync (applied after the review rebuild)
    _pending_url: str = ""
    _url_written: str = ""   # signature of the last URL query applied

    # stage 4 — derived, non-serialised working state
    _model: dict = {}
    _materiality: float = 10000.0
    _read_fingerprint: str = ""
    bulk_cap: int = review.BULK_SELECTION_CAP

    # ------------------------------------------------------------------
    # Derived display vars
    # ------------------------------------------------------------------
    @rx.var
    def period_label(self) -> str:
        return review.format_period(self.ctx_period)

    @rx.var
    def run_context_label(self) -> str:
        return f"Run {self.run_id}" if self.run_id else "No run yet"

    @rx.var
    def run_context_status(self) -> str:
        run = queries.get_run(self.run_id) if self.run_id else None
        if not run:
            return ""
        when = str(run.get("run_timestamp") or "").replace("T", " ").split(".")[0]
        who = str(run.get("recon_type") or "")
        return f"{who} · {when}" if when else who

    # stage 5
    export_b64: str = ""
    export_name: str = ""
    export_ready: bool = False

    flash: str = ""
    error: str = ""

    # ==================================================================
    # Load + context
    # ==================================================================
    @rx.event
    def load(self, params: dict | None = None):
        self.clients = discovery.list_clients()
        if not self.ctx_client and self.clients:
            self.ctx_client = self.clients[0]
        self._load_context()
        # Deep-link support: a filtered Review view is linkable
        # (/reconcile?stage=4&cls=…&cause=…). Params are read from the
        # caller's dict when supplied, else from this state's own router (the
        # reliable source when the event is dispatched without arguments).
        url_params = params if isinstance(params, dict) and params else None
        if url_params is None:
            try:
                url_params = dict(self.router.page.params or {})
            except Exception:  # noqa: BLE001
                url_params = {}
        if url_params:
            self.read_url_params(url_params)
            # A URL that names a record opens its drawer; one that does not
            # closes any drawer left over from the previous page.
            if not self.selected_result_id:
                self.drawer_open = False
            if str(url_params.get("stage", "")) == "4" and self.existing_run_id:
                self.run_id = self.existing_run_id
                self._seed_selections_from_run()
                self.stage = 4
                self._load_review()

    def _load_context(self) -> None:
        if not self.ctx_client:
            return
        self.periods = discovery.list_periods(self.ctx_client)
        if self.ctx_period not in self.periods:
            self.ctx_period = self.periods[0] if self.periods else ""
        if self.ctx_client and self.ctx_period:
            self.recon_types = discovery.list_recon_types(self.ctx_client, self.ctx_period)
        else:
            self.recon_types = []
        if self.ctx_recon_type not in self.recon_types:
            self.ctx_recon_type = self.recon_types[0] if self.recon_types else ""
        self._load_existing_run()
        self._load_slots()

    def _load_existing_run(self) -> None:
        self.has_existing_run = False
        self.existing_run_id = 0
        if not (self.ctx_client and self.ctx_period and self.ctx_recon_type):
            return
        df = queries.list_runs(
            client=self.ctx_client, period=self.ctx_period, recon_type=self.ctx_recon_type
        )
        if not df.empty:
            latest = df.iloc[0]
            self.has_existing_run = True
            self.existing_run_id = int(latest["run_id"])
            self.existing_run_when = str(latest.get("run_timestamp") or "").replace("T", " ").split(".")[0]

    @rx.event
    def set_ctx_client(self, v: str):
        self.ctx_client = v
        self.rail_return = False
        self._load_context()

    @rx.event
    def set_ctx_period(self, v: str):
        self.ctx_period = v
        self.rail_return = False
        self._load_context()

    @rx.event
    def set_ctx_recon_type(self, v: str):
        self.ctx_recon_type = v
        self.rail_return = False
        self._load_context()

    @rx.var
    def stage_chips(self) -> list[StageChip]:
        max_reachable = self._max_reachable()
        return [
            StageChip(
                number=n,
                label=label,
                reachable=n <= max_reachable,
                active=n == self.stage,
            )
            for n, label in STAGES
        ]

    def _max_reachable(self) -> int:
        if not (self.ctx_client and self.ctx_period and self.ctx_recon_type):
            return 1
        if not self.run_id:
            return 3 if self.ready else 2
        return 5

    @rx.event
    def goto_stage(self, stage: int):
        if stage > self._max_reachable():
            return
        self.rail_return = True
        self.stage = stage
        self.flash = ""
        self.error = ""
        if stage == 4:
            self._load_review()
        if stage == 5:
            self._load_export()

    @rx.event
    def continue_forward(self):
        self.rail_return = False
        self.stage = min(self.stage + 1, 5)
        if self.stage == 4:
            self._load_review()
        if self.stage == 5:
            self._load_export()

    # ==================================================================
    # Stage 1 — Context
    # ==================================================================
    @rx.event
    def continue_existing_run(self):
        self.run_id = self.existing_run_id
        self._seed_selections_from_run()
        self.stage = 4
        self._load_review()

    @rx.event
    def start_fresh(self):
        self.started_fresh = True
        self.run_id = 0
        self.auto_ran = False
        self.selections = {}
        self.export_ready = False
        self.export_b64 = ""
        self._load_slots()

    def _seed_selections_from_run(self) -> None:
        if self.selections or not self.run_id:
            return
        run = queries.get_run(self.run_id)
        if not run:
            return
        names = set(run.get("source_file_names") or [])
        if not names:
            return
        for source_type in discovery.RECON_SOURCE_TYPES.get(self.ctx_recon_type, []):
            for filename in _list_files(self.ctx_client, self.ctx_period, source_type):
                if filename in names:
                    self.selections[source_type] = filename
                    break

    # ==================================================================
    # Stage 2 — Upload
    # ==================================================================
    def _load_slots(self) -> None:
        if not (self.ctx_client and self.ctx_period and self.ctx_recon_type):
            self.slots = []
            return
        books_types = set(discovery.BOOKS_SOURCE_TYPES)
        out: list[SlotRow] = []
        for source_type in discovery.RECON_SOURCE_TYPES[self.ctx_recon_type]:
            filename = self.selections.get(source_type, "")
            info = self._slot_info(source_type, filename)
            out.append(
                SlotRow(
                    source_type=source_type,
                    label=discovery.source_type_label(source_type),
                    hint=discovery.source_type_hint(source_type),
                    is_books=source_type in books_types,
                    files=_list_files(self.ctx_client, self.ctx_period, source_type),
                    selected=filename,
                    code=info["code"],
                    chip_label=info["chip_label"],
                    chip_variant=info["chip_variant"],
                    rows=info["rows"],
                    needs_review=info["needs_review"],
                    required_missing=info["required_missing"],
                    message=info["message"],
                    caveats=info["caveats"],
                    resolved=info["resolved"],
                )
            )
        self.slots = out
        self._compute_readiness()

    def _slot_info(self, source_type: str, filename: str) -> dict:
        empty = {
            "code": "empty", "chip_label": "— no file yet", "chip_variant": "placeholder",
            "rows": 0, "needs_review": 0, "required_missing": 0, "message": "", "caveats": [],
            "resolved": False,
        }
        if not filename:
            return empty
        stored = ingestion_ai.get_stored_result(
            self.ctx_client, self.ctx_period, source_type, filename
        )
        if stored is None:
            return {
                **empty, "code": "not_ingested",
                "chip_label": "⚠ not ingested yet", "chip_variant": "ai",
            }
        status = stored.get("status")
        rows = int(stored.get("row_count_out") or 0)
        mapping = stored.get("mapping") or []
        unmapped = [m for m in mapping if not m.get("source_column")]
        try:
            threshold = ingestion_ai.get_preselect_threshold()
        except Exception:  # noqa: BLE001
            threshold = 0.75
        # A mapped field with NO confidence is a RULE-sourced mapping (the
        # deterministic GSTR-2B/IMS parser, or a trusted profile) — not a
        # low-confidence guess. Only an explicit numeric confidence below the
        # threshold counts as "needs review".
        low_conf = [
            m for m in mapping
            if m.get("source_column")
            and m.get("confidence") is not None
            and m["confidence"] < threshold
        ]
        needs_review = len(unmapped) + len(low_conf)
        required_missing = len(stored.get("unmapped_required") or [])
        caveats = [
            f"{c.get('label', c.get('code'))} — {c.get('detail', '')}"
            for c in (stored.get("caveats") or [])
        ]
        if status in ("wrong_slot", "unrecognized"):
            return {
                **empty, "code": "wrong", "chip_label": "✕ looks like the wrong file type",
                "chip_variant": "danger", "message": stored.get("message") or "",
            }
        if required_missing:
            return {
                **empty, "code": "partial", "chip_variant": "ai", "rows": rows,
                "required_missing": required_missing, "caveats": caveats,
                "chip_label": f"⚠ {required_missing} required field(s) unmapped — partial report",
                "resolved": True,
            }
        if status == "partial" or needs_review:
            if needs_review:
                label = f"⚠ {needs_review} field(s) need review"
            else:
                label = f"⚠ {rows} rows — partial report"
            return {
                **empty, "code": "review", "chip_variant": "ai", "rows": rows,
                "needs_review": needs_review, "caveats": caveats,
                "chip_label": label, "resolved": True,
            }
        return {
            **empty, "code": "ok", "chip_variant": "rule", "rows": rows,
            "caveats": caveats, "chip_label": f"✓ {rows} rows, fully mapped", "resolved": True,
        }

    def _compute_readiness(self) -> None:
        books = discovery.BOOKS_SOURCE_TYPES
        portals = discovery.portal_source_types(self.ctx_recon_type)
        by_type = {s.source_type: s for s in self.slots}
        self.books_ok = all(by_type.get(b) and by_type[b].resolved for b in books)
        self.portal_ok = any(by_type.get(p) and by_type[p].resolved for p in portals)
        blockers: list[str] = []
        if period_utils.is_unfiled(self.ctx_period):
            blockers.append(
                "This period holds files filed without a period — file them to a period "
                "from Smart Ingestion before reconciling."
            )
        for src in books + portals:
            s = by_type.get(src)
            if s and s.code in ("wrong", "not_ingested"):
                blockers.append(f"{s.label} — {s.message or 'needs attention'}")
        self.blockers = blockers
        self.ready = self.books_ok and self.portal_ok and not blockers

    @rx.event
    def select_slot_file(self, source_type: str, filename: str):
        self.selections[source_type] = filename
        self.rail_return = False
        # Normalize a file that's on disk but never ingested.
        if filename and ingestion_ai.get_stored_result(
            self.ctx_client, self.ctx_period, source_type, filename
        ) is None:
            self._normalize(source_type, filename)
        self._load_slots()

    def _normalize(self, source_type: str, filename: str) -> None:
        path = source_data_path(self.ctx_client, self.ctx_period, source_type) / filename
        try:
            ingestion_ai.normalize_source_file(
                path, source_type, self.ctx_client, self.ctx_period,
                client_id=_client_id_for_folder(self.ctx_client),
                recon_type=self.ctx_recon_type,
                actor=self.username,
            )
        except Exception as exc:  # noqa: BLE001
            self.error = f"AI ingestion couldn't read {filename}: {exc}"

    # ------------------------------------------------------------------
    # Re-run through model (per-file, confirm-gated)
    # ------------------------------------------------------------------
    @rx.event
    def open_rerun(self, source_type: str, filename: str):
        """Open the confirm dialog for a per-file model re-run.

        A re-run is a live model call (~15-25s) and discards the cached
        mapping, so it is always confirmed first — never a one-click action.
        """
        if not filename:
            return
        self.rerun_source_type = source_type
        self.rerun_filename = filename
        self.rerun_message = ""
        self.rerun_open = True

    @rx.event
    def close_rerun(self):
        self.rerun_open = False
        self.rerun_busy = False

    @rx.event
    def confirm_rerun(self):
        if not (self.rerun_source_type and self.rerun_filename):
            self.rerun_open = False
            return
        self.rerun_busy = True
        self.error = ""
        try:
            out = ingestion_ai.re_run_file(
                self.ctx_client, self.ctx_period, self.rerun_source_type, self.rerun_filename,
                actor=self.username,
                client_id=_client_id_for_folder(self.ctx_client),
                recon_type=self.ctx_recon_type,
            )
            how = "the deterministic layout parser" if not out.get("model_used") else "the model"
            self.rerun_message = (
                f"Re-ran {self.rerun_filename} through {how} — "
                f"{out['row_count_out']} row(s) mapped."
            )
            self.flash = self.rerun_message
        except Exception as exc:  # noqa: BLE001
            self.error = f"Re-run failed for {self.rerun_filename}: {exc}"
        finally:
            self.rerun_busy = False
            self.rerun_open = False
            self._load_slots()

    @rx.var
    def slot_labels(self) -> list[str]:
        return [s.label for s in self.slots]

    @rx.event
    def set_upload_slot_label(self, v: str):
        self.upload_slot_label = v

    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        self.upload_error = ""
        self.error = ""
        if not files:
            self.upload_error = "Choose a file to upload."
            return
        source_type = ""
        for s in self.slots:
            if s.label == self.upload_slot_label:
                source_type = s.source_type
                break
        if not source_type:
            self.upload_error = "Pick a slot for this file first."
            return
        # The period is the folder the engine reads. Stage 1 requires it, so
        # this is a defensive backstop against writing an unreachable file.
        if not period_utils.is_valid_period(self.ctx_period):
            self.upload_error = (
                "Choose a real period (YYYY-MM) in Stage 1 before uploading — "
                "files filed without one cannot be reconciled."
            )
            return
        f = files[0]
        data = await f.read()
        directory = source_data_path(self.ctx_client, self.ctx_period, source_type)
        dest = directory / (f.filename or "upload")
        try:
            dest.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            self.upload_error = f"Couldn't save {f.filename}: {exc}"
            return
        self.selections[source_type] = dest.name
        self.rail_return = False
        self._normalize(source_type, dest.name)
        self.flash = f"Uploaded {dest.name}."
        self._load_slots()

    # ==================================================================
    # Stage 3 — Reconcile
    # ==================================================================
    @rx.event
    def set_pause_before_run(self, v: bool):
        self.pause_before_run = v

    @rx.event
    def run_reconciliation(self):
        self.error = ""
        self.flash = ""
        selected = {k: v for k, v in self.selections.items() if v}
        if not selected:
            self.error = "No source files are selected."
            return
        client_id = _client_id_for_folder(self.ctx_client)
        try:
            config = load_config()
            run_id = execute_run(
                self.ctx_client, self.ctx_period, self.ctx_recon_type, None, config,
                selected_files=selected, client_id=client_id, actor=self.username,
            )
        except RunExecutionError as exc:
            self.error = str(exc)
            _record_sync_health(client_id, selected, succeeded=False)
            return
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            _record_sync_health(client_id, selected, succeeded=False)
            return

        _record_sync_health(client_id, selected, succeeded=True)
        if client_id is not None:
            try:
                from src.module2 import service as m2

                m2.generate_exceptions_for_run(run_id, client_id=client_id, actor=self.username)
            except Exception:  # noqa: BLE001
                pass

        self.run_id = run_id
        self.auto_ran = True
        self.export_ready = False
        self.export_b64 = ""
        self.flash = f"Run {run_id} complete."
        self.stage = 4
        self._load_review()

    @rx.event
    def run_again(self):
        self.run_id = 0
        self.auto_ran = False
        self.pause_before_run = True
        self.export_ready = False
        self.export_b64 = ""
        self.stage = 3

    # ==================================================================
    # Stage 4 — Review
    # ==================================================================
    def _load_review(self) -> None:
        """Rebuild the whole Review screen from the run's records.

        Everything here is DERIVED (``src.reconciliation.review``) from the
        engine's existing output — review state, classification and confidence
        are read, never written.
        """
        if not self.run_id:
            return
        record_rows = queries.get_results(self.run_id).to_dict("records")
        self._review_rows = [dict(r) for r in record_rows]

        run = queries.get_run(self.run_id) or {}
        self.caveats = [
            CaveatRow(
                label=c.get("label") or c.get("code") or "Limitation",
                detail=c.get("detail") or "",
                source=c.get("filename") or c.get("source_type") or "",
            )
            for c in (run.get("caveats") or [])
        ]

        # Materiality is SEPARATE per recon type (C1), exactly as Module 2 reads it.
        materiality_key = {
            "GST": "materiality.gst", "TDS": "materiality.tds", "OTHER": "materiality.other",
        }.get(self.ctx_recon_type, "materiality.gst")
        materiality = self._effective_number(materiality_key, review.DEFAULT_MATERIALITY)
        tolerance = self._effective_number("gst.amount_tolerance.absolute", review.DEFAULT_TOLERANCE)
        rounding = self._effective_number("gst.rounding_tolerance", review.DEFAULT_ROUNDING_TOLERANCE)

        itc_eligible = itc_claimed = None
        if (run.get("recon_type") or "GST") == "GST":
            client_id = _client_id_for_folder(self.ctx_client)
            if client_id:
                try:
                    from src.module2 import service as m2

                    credit = m2.eligible_credit_for_client(client_id, period=self.ctx_period)
                    if credit:
                        itc_eligible = float(credit.get("eligible_credit") or 0.0)
                        itc_claimed = float(credit.get("total_itc_claimed") or 0.0)
                except Exception:  # noqa: BLE001
                    itc_eligible = itc_claimed = None

        self._materiality = materiality
        self._model = review.build_review_model(
            run, self._review_rows,
            materiality=materiality, tolerance=tolerance, rounding_tolerance=rounding,
            itc_eligible=itc_eligible, itc_claimed=itc_claimed,
        )
        m = self._model
        self.review_total = m["kpi"]["total_count"]
        self.review_matched = m["kpi"]["matched_count"]
        self.review_exceptions = m["kpi"]["exception_count"]
        self.total_exception_count = m["kpi"]["exception_count"]

        self._build_headline()
        self._build_kpis()
        self._build_charts()
        self._build_quality_notes(run)
        self._build_integrity()
        self._apply_filters()

        # Re-open the drawer on the same record if it is still in view; close it
        # when the record is gone (a filter change, or navigating in from a URL
        # that carries no `open`), so a stale drawer never blocks the page.
        if self.selected_result_id:
            self._load_drawer()
        else:
            self.drawer_open = False

    def _effective_number(self, key: str, default: float) -> float:
        try:
            from src.rules import service as rules

            v = rules.get_effective_number(
                key, client_id=_client_id_for_folder(self.ctx_client), default=default
            )
            return float(v) if v is not None else default
        except Exception:  # noqa: BLE001
            return default

    # ------------------------------------------------------------------
    # Headline + cause bar
    # ------------------------------------------------------------------
    def _build_headline(self) -> None:
        k = self._model["kpi"]
        period_label = review.format_period(self.ctx_period)
        self.headline_value = k["attention_display"]
        self.headline_sub = (
            f"{k['exception_count']} item(s) · Run {self.run_id} · {period_label} "
            f"{self.ctx_recon_type}"
        )
        self.headline_scope = review.CAUSE_GROUP_LABELS["gap"] + " vs " + review.CAUSE_GROUP_LABELS["judgement"]
        total = k["total_count"] or 1
        self.progress_percent = round(k["reviewed_count"] / total * 100, 1)
        self.progress_percent_int = int(round(self.progress_percent))
        self.progress_label = f"{k['reviewed_count']} of {k['total_count']} reviewed"

        segments = self._model["cause_segments"]
        denom = sum(s["value"] for s in segments) or 1.0
        out: list[CauseSeg] = []
        for idx, s in enumerate(segments):
            width = max(s["value"] / denom * 100, 1.0)
            out.append(CauseSeg(
                index=idx,
                cause=s["cause"],
                label=s["label"],
                count_display=str(s["count"]),
                value_display=review.format_money(s["value"]),
                percent=round(s["value"] / denom * 100, 1),
                width=f"{width:.3f}%",
                color_role=s["color_role"],
                group=s["group"],
                is_gap=s["group"] == "gap",
                active=self.filter_cause == s["cause"],
            ))
        self.cause_segments = out

        groups = self._model["group_segments"]
        gout: list[GroupSeg] = []
        for g in groups:
            width = max(g["value"] / denom * 100, 1.0)
            gout.append(GroupSeg(
                group=g["group"],
                label=g["label"],
                hint=review.CAUSE_GROUP_HINTS.get(g["group"], ""),
                count_display=str(g["count"]),
                value_display=review.format_money(g["value"]),
                percent=round(g["value"] / denom * 100, 1),
                width=f"{width:.3f}%",
                is_gap=g["group"] == "gap",
                active=self.filter_cause_group == g["group"],
            ))
        self.group_segments = gout

    def _build_kpis(self) -> None:
        k = self._model["kpi"]

        def card(key, label, value_display, count, count_display, note="", available=True):
            return ReviewKpi(
                key=key, label=label, value_display=value_display,
                count=count, count_display=count_display, note=note,
                muted=(count == 0) or not available, active=(self.filter_view == key),
                available=available,
            )

        self.kpis = [
            card("amount_difference", "Amount difference",
                 review.format_money(k["amount_difference_value"]),
                 k["amount_difference_count"], f"{k['amount_difference_count']} item(s)"),
            card("not_in_books", "Not in books", review.format_money(k["not_in_books_value"]),
                 k["not_in_books_count"], f"{k['not_in_books_count']} item(s)"),
            card("not_in_portal", "Not in portal", review.format_money(k["not_in_portal_value"]),
                 k["not_in_portal_count"], f"{k['not_in_portal_count']} item(s)"),
            card("matched", "Matched", review._UNAVAILABLE, k["matched_count"],
                 f"{k['matched_count']} of {k['portal_invoice_count']} portal invoices"),
            card("reviewed", "Reviewed", review._UNAVAILABLE, k["reviewed_count"],
                 f"{k['reviewed_count']}/{k['total_count']} reviewed"),
        ]

    def _build_charts(self) -> None:
        m = self._model
        # Charts take plain dicts: recharts binds the datum directly, and a
        # dataclass instance does not serialise into a JS data object.
        slices: list[dict] = []
        colors: list[str] = []
        for s in m["classification_slices"]:
            if s["count"] == 0 and s["value"] == 0:
                continue  # never render an empty chart card
            slices.append({
                "key": s["key"],
                "name": s["name"],
                "count": int(s["count"]),
                "value": float(s["value"]),
            })
            colors.append(_ROLE_COLOR.get(s["color_role"], _t.Color.NEUTRAL.value))
        self.classification_slices = slices
        self.classification_colors = colors

        bars: list[dict] = []
        for s in (m["suppliers"] or [])[:8]:
            bars.append({
                "party": s["party"],
                "name": s["party"],
                "value": float(s["value"]),
                "count": int(s["count"]),
            })
        self.supplier_bars = bars

        itc = m.get("itc")
        self.itc_available = bool(itc)
        if itc:
            self.itc_slices = [
                {"name": s["name"], "value": float(s["value"])} for s in itc["slices"]
            ]
            self.itc_colors = [_ROLE_COLOR.get(s["color_role"], _t.Color.NEUTRAL.value)
                               for s in itc["slices"]]
            self.itc_display = review.format_money(itc["eligible"])
            self.itc_claimed_display = review.format_money(itc["claimed"])
        else:
            self.itc_slices = []
            self.itc_colors = []
            self.itc_display = review._UNAVAILABLE
            self.itc_claimed_display = review._UNAVAILABLE

    def _build_quality_notes(self, run: dict) -> None:
        notes = review.data_quality_notes(
            self._model["items"],
            recon_type=self._model["recon_type"],
            source_files=run.get("source_file_names") or [],
            caveats=run.get("caveats") or [],
            run_notes=run.get("run_notes") or [],
        )
        self.quality_notes = [
            QualityNoteRow(key=n.key, title=n.title, what=n.what, why=n.why, how=n.how,
                           link=n.link, link_label=n.link_label)
            for n in notes
        ]

    def _build_integrity(self) -> None:
        """Compact PASS/FAIL strip: the F5 gate + an independent recompute.
        Rendered only where the data exists."""
        rows: list[IntegrityRow] = []
        client_id = _client_id_for_folder(self.ctx_client)
        if client_id:
            try:
                from src.f5 import service as f5

                blocks = f5.list_blocks(client_id=client_id, status="open")
                rows.append(IntegrityRow(
                    label="F5 structural gate",
                    result="PASS" if not blocks else "FAIL",
                    detail="No open validation blocks" if not blocks else f"{len(blocks)} open block(s)",
                ))
                health = f5.sync_health_for_client(client_id)
                failed = [h for h in health if h.get("status") == "attempted_failed"]
                rows.append(IntegrityRow(
                    label="Source sync health",
                    result="FAIL" if failed else "PASS",
                    detail=f"{len(failed)} source(s) failed" if failed else "All sources in sync",
                ))
            except Exception:  # noqa: BLE001
                pass

        # Independent recompute — a genuinely separate code path.
        items = self._model["items"]
        exceptions = [i for i in items if i["bucket"] != "Matched"]
        row_sum = round(sum(i["difference"] for i in exceptions), 2)
        cause_sum = round(sum(s["value"] for s in self._model["cause_segments"]), 2)
        rows.append(IntegrityRow(
            label="Headline ties to rows",
            result="PASS" if abs(row_sum - cause_sum) < 0.05 else "FAIL",
            detail=f"{len(exceptions)} row(s) recounted → {review.format_money(row_sum)}",
        ))
        tagged = sum(1 for i in exceptions if i["cause"])
        rows.append(IntegrityRow(
            label="Every exception has a cause",
            result="PASS" if tagged == len(exceptions) else "FAIL",
            detail=f"{tagged}/{len(exceptions)} classified",
        ))
        self.integrity_rows = rows
        self.integrity_verdict = "PASS" if all(r.result != "FAIL" for r in rows) else "FAIL"

    # ------------------------------------------------------------------
    # Filters + table
    # ------------------------------------------------------------------
    def _matches(self, item: dict) -> bool:
        if self.filter_view == "matched":
            if item["bucket"] != "Matched":
                return False
        elif self.filter_view and self.filter_view != "reviewed":
            want = {"amount_difference": "Amount Difference",
                    "not_in_books": "Not in Books",
                    "not_in_portal": "Not in Portal"}.get(self.filter_view)
            if want and item["bucket"] != want:
                return False
            if item["bucket"] == "Matched":
                return False
        elif self.filter_view != "reviewed" and item["bucket"] == "Matched":
            # The default view is the exception list.
            return False
        if self.filter_view == "reviewed" and not item["reviewed"]:
            return False

        if self.filter_classification and item["bucket"] != self.filter_classification:
            return False
        if self.filter_cause and item["cause"] != self.filter_cause:
            return False
        if self.filter_cause_group and item["cause_group"] != self.filter_cause_group:
            return False
        if self.filter_confidence and item["confidence_band"] != self.filter_confidence:
            return False
        if self.filter_review_state == "reviewed" and not item["reviewed"]:
            return False
        if self.filter_review_state == "unreviewed" and item["reviewed"]:
            return False
        if self.active_only and item["reviewed"]:
            return False
        if self.filter_supplier and item["supplier_key"] != self.filter_supplier:
            return False
        if self.search_query:
            q = self.search_query.strip().lower()
            hay = f"{item['party']} {item['gstin']} {item['reference']}".lower()
            if q not in hay:
                return False
        return True

    def _apply_filters(self) -> None:
        items = self._model["items"]
        matched = [i for i in items if self._matches(i)]
        if self.sort_key == "difference":
            matched.sort(key=lambda i: (-i["difference"], i["party"]))
        elif self.sort_key == "supplier":
            matched.sort(key=lambda i: (i["supplier_key"].lower(), -i["difference"]))
        elif self.sort_key == "date":
            matched.sort(key=lambda i: (i["date"] or "", -i["difference"]))
        elif self.sort_key == "confidence":
            rank = {"High": 0, "Medium": 1, "Low": 2}
            matched.sort(key=lambda i: (rank.get(i["confidence_band"], 3), -i["difference"]))
        elif self.sort_key == "review_state":
            matched.sort(key=lambda i: (i["reviewed"], -i["difference"]))

        self.shown_count = len(matched)
        pages = max((len(matched) + 24) // 25, 1)
        self.page_count = pages
        self.page = min(max(self.page, 1), pages)
        if self.group_by != "none":
            self._build_grouped(matched)
        else:
            self.group_headers = []
            start = (self.page - 1) * 25
            self._render_rows(matched[start:start + 25], "")
        self.page_label = (
            f"Showing {len(self.exception_rows)} of {self.shown_count}"
            + (f" · page {self.page} of {self.page_count}" if self.page_count > 1 else "")
        )

        self._build_active_filters()
        self._build_chip_groups()
        self._build_selected_flags()
        self._build_read_if_needed()
        self._sync_url()

    def _build_grouped(self, items: list[dict]) -> None:
        """Collapsible group headers + every row (grouping replaces paging)."""
        key_fn = (lambda i: i["supplier_key"]) if self.group_by == "supplier" else (lambda i: i["cause_label"])
        groups: dict[str, list[dict]] = {}
        for i in items:
            groups.setdefault(key_fn(i), []).append(i)
        ordered = sorted(groups.items(), key=lambda kv: -sum(x["difference"] for x in kv[1]))
        headers: list[GroupHeader] = []
        rows: list[ReviewItem] = []
        for name, members in ordered:
            headers.append(GroupHeader(
                group=name, label=name,
                count_display=f"{len(members)} item(s)",
                subtotal=review.format_money(sum(x["difference"] for x in members)),
            ))
            rows.extend(self._item_rows(members, name))
        self.group_headers = headers
        self.exception_rows = rows

    def _item_rows(self, items: list[dict], group_name: str) -> list[ReviewItem]:
        out: list[ReviewItem] = []
        for i in items:
            out.append(ReviewItem(
                result_id=i["result_id"],
                fingerprint=i["fingerprint"],
                supplier=i["party"] or "Unknown supplier",
                gstin=i["gstin"],
                reference=i["reference"] or review._UNAVAILABLE,
                date=i["date_label"],
                books_display=i["books_display"],
                portal_display=i["portal_display"],
                difference_display=i["difference_display"],
                difference_value=i["difference"],
                difference_danger=bool(i["material"]),
                issue=i["issue"],
                cause=i["cause"],
                cause_label=i["cause_label"],
                cause_group=i["cause_group"],
                confidence_band=i["confidence_band"],
                confidence_label=i["confidence_label"],
                match_key=i["match_key_label"],
                reviewed=i["reviewed"],
                status_label="Reviewed" if i["reviewed"] else "Not reviewed",
                classification=i["bucket"],
                supplier_group=i["supplier_key"],
                group_key=group_name,
                bulk_reason=(
                    "Materiality-gated — individual review required"
                    if (review.BULK_REVIEW_MATERIALITY_GATED and i["material"]) else ""
                ),
                selected=(i["result_id"] in self.selection),
                bulk_blocked=bool(review.BULK_REVIEW_MATERIALITY_GATED and i["material"]),
            ))
        return out

    def _render_rows(self, items: list[dict], group_name: str) -> None:
        self.exception_rows = self._item_rows(items, group_name)

    def _build_selected_flags(self) -> None:
        sel = set(self.selection)
        self.exception_rows = [
            replace(r, selected=(r.result_id in sel)) for r in self.exception_rows
        ]

    def _build_active_filters(self) -> None:
        out: list[ActiveFilter] = []
        if self.search_query:
            out.append(ActiveFilter("search", self.search_query, f'Search "{self.search_query}"'))
        if self.filter_classification:
            out.append(ActiveFilter("classification", self.filter_classification, self.filter_classification))
        if self.filter_cause:
            out.append(ActiveFilter("cause", self.filter_cause,
                                    review.CAUSE_LABELS.get(self.filter_cause, self.filter_cause)))
        if self.filter_cause_group:
            out.append(ActiveFilter("cause_group", self.filter_cause_group,
                                    review.CAUSE_GROUP_LABELS.get(self.filter_cause_group, self.filter_cause_group)))
        if self.filter_confidence:
            out.append(ActiveFilter("confidence", self.filter_confidence, f"{self.filter_confidence} confidence"))
        if self.filter_review_state:
            out.append(ActiveFilter("review_state", self.filter_review_state,
                                    self.filter_review_state.capitalize()))
        if self.filter_supplier:
            out.append(ActiveFilter("supplier", self.filter_supplier, self.filter_supplier))
        if self.filter_view:
            out.append(ActiveFilter("view", self.filter_view, self.filter_view.replace("_", " ").capitalize()))
        self.active_filters = out
        self.has_filters = bool(out)

    def _build_chip_groups(self) -> None:
        items = self._model["items"]
        exceptions = [i for i in items if i["bucket"] != "Matched"]

        def chips(facet, pairs, active):
            out: list[FilterChip] = []
            for value, label, count in pairs:
                out.append(FilterChip(
                    facet=facet, value=value, label=label, count=count,
                    count_display=str(count), active=(active == value), muted=(count == 0),
                ))
            return out

        cls_pairs = []
        for name in ["Amount Difference", "Not in Books", "Not in Portal", "Matched"]:
            cls_pairs.append((name, name, sum(1 for i in items if i["bucket"] == name)))
        self.classification_chips = chips("classification", cls_pairs, self.filter_classification)

        cause_pairs = [(s["cause"], s["label"], s["count"]) for s in self._model["cause_segments"]]
        self.cause_chips = chips("cause", cause_pairs, self.filter_cause)

        conf = self._model["confidence_counts"]
        self.confidence_chips = chips("confidence", [
            ("High", "High", conf.get("High", 0)),
            ("Medium", "Medium", conf.get("Medium", 0)),
            ("Low", "Low", conf.get("Low", 0)),
        ], self.filter_confidence)

        reviewed = sum(1 for i in exceptions if i["reviewed"])
        self.review_chips = chips("review_state", [
            ("unreviewed", "Not reviewed", len(exceptions) - reviewed),
            ("reviewed", "Reviewed", reviewed),
        ], self.filter_review_state)

    # ------------------------------------------------------------------
    # Filter events
    # ------------------------------------------------------------------
    @rx.event
    def set_filter_view(self, key: str):
        self.filter_view = "" if self.filter_view == key else key
        self.page = 1
        self._load_review()

    @rx.event
    def set_search(self, v: str):
        self.search_query = v
        self.page = 1
        self._apply_filters()
        self._sync_url()

    @rx.event
    def toggle_chip(self, facet: str, value: str):
        attr = {
            "classification": "filter_classification",
            "cause": "filter_cause",
            "confidence": "filter_confidence",
            "review_state": "filter_review_state",
        }.get(facet)
        if attr is None:
            return
        if getattr(self, attr) == value:
            setattr(self, attr, "")
        else:
            setattr(self, attr, value)
        self.page = 1
        self._load_review()

    @rx.event
    def set_cause_group(self, group: str):
        self.filter_cause_group = "" if self.filter_cause_group == group else group
        self.page = 1
        self._load_review()

    @rx.event
    def set_supplier(self, party: str):
        self.filter_supplier = "" if self.filter_supplier == party else party
        self.page = 1
        self._load_review()

    @rx.event
    def set_sort(self, v: str):
        self.sort_key = v
        self.page = 1
        self._apply_filters()
        self._sync_url()

    @rx.event
    def set_group_by(self, v: str):
        self.group_by = v
        self.page = 1
        self._apply_filters()
        self._sync_url()

    @rx.event
    def set_active_only(self, v: bool):
        self.active_only = bool(v)
        self.page = 1
        self._apply_filters()
        self._sync_url()

    @rx.event
    def toggle_classification_mode(self):
        self.classification_mode_value = not self.classification_mode_value
        self._build_charts()

    @rx.event
    def clear_filter(self, facet: str):
        attr = {
            "search": "search_query",
            "classification": "filter_classification",
            "cause": "filter_cause",
            "cause_group": "filter_cause_group",
            "confidence": "filter_confidence",
            "review_state": "filter_review_state",
            "supplier": "filter_supplier",
            "view": "filter_view",
        }.get(facet)
        if attr:
            setattr(self, attr, "" if attr != "group_by" else "none")
        self.page = 1
        self._load_review()

    @rx.event
    def clear_all_filters(self):
        self.search_query = ""
        self.filter_classification = ""
        self.filter_cause = ""
        self.filter_cause_group = ""
        self.filter_confidence = ""
        self.filter_review_state = ""
        self.filter_supplier = ""
        self.filter_view = ""
        self.active_only = False
        self.page = 1
        self._load_review()

    @rx.event
    def set_page(self, p: int):
        self.page = int(p)
        self._apply_filters()
        self._sync_url()

    @rx.event
    def next_page(self):
        self.page = min(self.page + 1, self.page_count)
        self._apply_filters()
        self._sync_url()

    @rx.event
    def prev_page(self):
        self.page = max(self.page - 1, 1)
        self._apply_filters()
        self._sync_url()

    # ------------------------------------------------------------------
    # URL sync (a filtered view is linkable)
    # ------------------------------------------------------------------
    def _build_url(self) -> str:
        pairs = [
            ("view", self.filter_view),
            ("cls", self.filter_classification),
            ("cause", self.filter_cause),
            ("cgroup", self.filter_cause_group),
            ("conf", self.filter_confidence),
            ("state", self.filter_review_state),
            ("supplier", self.filter_supplier),
            ("q", self.search_query),
            ("sort", self.sort_key if self.sort_key != "difference" else ""),
            ("group", self.group_by if self.group_by != "none" else ""),
            ("page", str(self.page) if self.page > 1 else ""),
            ("open", str(self.selected_result_id) if self.selected_result_id else ""),
        ]
        query = "&".join(f"{k}={_url_quote(v)}" for k, v in pairs if v)
        return "/reconcile?stage=4" + (f"&{query}" if query else "")
    def _sync_url(self) -> None:
        self._pending_url = self._build_url()
        # Keep the applied-signature in step with what we are about to write, so
        # our OWN replaceState does not look like a new URL to hydrate from.
        query = self._pending_url.split("?", 1)[1] if "?" in self._pending_url else ""
        pairs = [p.split("=", 1) for p in query.split("&") if "=" in p]
        self._url_written = "&".join(f"{k}={v}" for k, v in sorted(pairs))

    @rx.event
    def apply_url(self):
        """After a review rebuild, push the filter state into the address bar
        (replaceState, so browser back is not polluted by every chip click)."""
        if self._pending_url:
            url = self._pending_url
            self._pending_url = ""
            return rx.call_script(
                f"window.history.replaceState({{}}, '', {json.dumps(url)})"
            )

    @rx.event
    def read_url_params(self, params: dict):
        """Hydrate filters from the URL.

        Applied whenever the URL's own query differs from the last one applied
        (not just once per session), so a link to a different filtered view —
        or a browser back/forward — is honoured. A plain re-render with the
        same query is a no-op, so local chip clicks are never clobbered.
        """
        if not isinstance(params, dict):
            return
        signature = "&".join(f"{k}={params[k]}" for k in sorted(params))
        if signature == self._url_written:
            return
        self._url_written = signature
        self.filter_view = params.get("view", "") or ""
        self.filter_classification = params.get("cls", "") or ""
        self.filter_cause = params.get("cause", "") or ""
        self.filter_cause_group = params.get("cgroup", "") or ""
        self.filter_confidence = params.get("conf", "") or ""
        self.filter_review_state = params.get("state", "") or ""
        self.filter_supplier = params.get("supplier", "") or ""
        self.search_query = params.get("q", "") or ""
        self.sort_key = params.get("sort", "") or "difference"
        self.group_by = params.get("group", "") or "none"
        try:
            self.page = int(params.get("page", "1") or 1)
        except (TypeError, ValueError):
            self.page = 1
        try:
            self.selected_result_id = int(params.get("open", "0") or 0)
        except (TypeError, ValueError):
            self.selected_result_id = 0

    # ------------------------------------------------------------------
    # Accountant's Read
    # ------------------------------------------------------------------
    def _build_read(self, *, allow_ai: bool) -> None:
        if not getattr(self, "_model", None):
            return
        result = narrative.read_summary(
            self._model,
            period_label=review.format_period(self.ctx_period),
            allow_ai=allow_ai,
        )
        self.read_text = result["text"]
        self.read_bullets = [SummaryBullet(text=b) for b in (result.get("bullets") or [])]
        self.read_source = result["source"]
        self.read_guard_ok = bool(result.get("guard", {}).get("passed"))
        self.read_ai_available = not bool(result.get("ai_error"))
        self._read_fingerprint = narrative.fingerprint(self._model)
        if result.get("ai_error"):
            self.read_note = (
                "AI unavailable — summary generated from rules. " + str(result["ai_error"])[:180]
            )
        elif allow_ai:
            self.read_note = ""
        else:
            self.read_note = ""

    def _build_read_if_needed(self) -> None:
        """The deterministic read is always available. An AI draft the reviewer
        already asked for is kept while the aggregates are unchanged."""
        if not getattr(self, "_model", None):
            return
        fp = narrative.fingerprint(self._model)
        keep_ai = self._read_fingerprint == fp and not self.read_source.startswith("rules")
        if keep_ai or (self.read_text and self._read_fingerprint == fp):
            return
        self._build_read(allow_ai=False)

    @rx.event
    def draft_read(self):
        """Explicitly request the AI-drafted summary (guard-verified)."""
        if not getattr(self, "_model", None):
            return
        self.read_busy = True
        try:
            self._build_read(allow_ai=True)
        finally:
            self.read_busy = False

    # ------------------------------------------------------------------
    # Bulk review (Module 8's materiality gate, applied consistently)
    # ------------------------------------------------------------------
    @rx.event
    def toggle_select(self, result_id: int, checked: bool):
        if checked:
            if result_id not in self.selection:
                self.selection = self.selection + [result_id]
        else:
            self.selection = [s for s in self.selection if s != result_id]
        self._build_selected_flags()
        self._evaluate_bulk()

    @rx.event
    def toggle_select_one(self, result_id: int):
        """Checkbox on_change receives only the new checked state — the row id
        is supplied by the caller's closure, so invert against the current
        membership."""
        will_check = result_id not in self.selection
        self.toggle_select(result_id, will_check)

    @rx.event
    def set_selection_note(self, v: str):
        self.bulk_note = v

    @rx.event
    def select_all_in_view(self):
        selectable = [r.result_id for r in self.exception_rows if not r.bulk_blocked]
        self.selection = sorted(set(self.selection) | set(selectable))
        self._build_selected_flags()
        self._evaluate_bulk()

    @rx.event
    def clear_selection(self):
        self.selection = []
        self.bulk_error = ""
        self.bulk_reason = ""
        self.bulk_note = ""
        self._build_selected_flags()
        self._evaluate_bulk()

    def _evaluate_bulk(self) -> None:
        self.bulk_disabled = False
        self.bulk_reason = ""
        if len(self.selection) > self.bulk_cap:
            self.bulk_disabled = True
            self.bulk_reason = f"Select at most {self.bulk_cap} items at a time."

    @rx.var
    def bulk_blocked_count(self) -> int:
        """How many rows in the current view cannot be bulk-acted on."""
        return sum(1 for r in self.exception_rows if r.bulk_blocked)

    @rx.var
    def all_selectable_selected(self) -> bool:
        selectable = [r.result_id for r in self.exception_rows if not r.bulk_blocked]
        return bool(selectable) and set(selectable) <= set(self.selection)

    @rx.event
    def toggle_select_all(self, checked: bool):
        if checked:
            return ReconcileState.select_all_in_view
        return ReconcileState.clear_selection

    def _review_verdict(self, item: dict) -> dict:
        """The verdict to store alongside a review, in the DB's own
        representation.

        ``get_results`` compares the stored verdict against the raw row to
        decide whether a review has gone stale, so an empty ``difference_type``
        MUST be stored as ``None`` (not ``""``) or every Not-in-Books item
        would immediately read back as unreviewed.
        """
        return {
            "run_id": self.run_id,
            "classification": item["classification"],
            "difference_type": item["difference_type"] or None,
            "confidence_band": item["confidence_band"] or None,
        }

    @rx.event
    def bulk_mark_reviewed(self):
        if not self.selection:
            return
        by_id = {i["result_id"]: i for i in self._model["items"]}
        n = 0
        for rid in self.selection:
            item = by_id.get(rid)
            if item is None:
                continue
            try:
                queries.mark_reviewed(
                    self.ctx_client, self.ctx_period, self.ctx_recon_type,
                    item["fingerprint"], note=self.bulk_note or None,
                    result=self._review_verdict(item),
                )
                n += 1
            except Exception as exc:  # noqa: BLE001
                self.bulk_error = f"Some items could not be marked: {exc}"
                break
        self.flash = f"Marked {n} item(s) reviewed."
        self.selection = []
        self.bulk_note = ""
        self._load_review()

    @rx.event
    def bulk_clear_review(self):
        if not self.selection:
            return
        by_id = {i["result_id"]: i for i in self._model["items"]}
        n = 0
        for rid in self.selection:
            item = by_id.get(rid)
            if item is None:
                continue
            try:
                queries.clear_review(
                    self.ctx_client, self.ctx_period, self.ctx_recon_type, item["fingerprint"]
                )
                n += 1
            except Exception as exc:  # noqa: BLE001
                self.bulk_error = f"Some items could not be cleared: {exc}"
                break
        self.flash = f"Cleared review on {n} item(s)."
        self.selection = []
        self._load_review()

    # ------------------------------------------------------------------
    # Evidence drawer
    # ------------------------------------------------------------------
    @rx.event
    def toggle_row_extra(self, result_id: int):
        self.selected_result_id = 0 if self.selected_result_id == result_id else result_id
        if self.selected_result_id:
            self._load_drawer()
        self._sync_url()
        return ReconcileState.apply_url

    # ------------------------------------------------------------------
    # Chart click-to-filter
    # ------------------------------------------------------------------
    @rx.event
    def on_chart_classification(self, payload: dict):
        """A classification slice was clicked. The payload is the sector's own
        datum, which carries the bucket `key`."""
        if not isinstance(payload, dict):
            return
        key = str(payload.get("key") or payload.get("name") or "")
        if not key:
            return
        if key == "Matched":
            self.filter_view = "" if self.filter_view == "matched" else "matched"
        else:
            self.filter_classification = "" if self.filter_classification == key else key
        self.page = 1
        self._load_review()

    @rx.event
    def on_chart_supplier(self, payload: dict):
        if not isinstance(payload, dict):
            return
        party = str(payload.get("party") or payload.get("name") or "")
        if not party:
            return
        self.set_supplier(party)

    @rx.event
    def on_drawer_open_change(self, is_open: bool):
        if not is_open:
            self.selected_result_id = 0
            self.drawer_open = False
            self._sync_url()
            return ReconcileState.apply_url

    @rx.event
    def open_drawer(self, result_id: int):
        self.selected_result_id = result_id
        self._load_drawer()
        self._sync_url()
        return ReconcileState.apply_url

    @rx.event
    def open_exception(self, result_id: int):
        """Alias kept so other screens delegating here keep working."""
        self.selected_result_id = result_id
        self._load_drawer()
        self._sync_url()
        return ReconcileState.apply_url

    @rx.event
    def close_drawer(self):
        self.selected_result_id = 0
        self.drawer_open = False
        self._sync_url()
        return ReconcileState.apply_url

    def close_exception(self):
        self.selected_result_id = 0
        self.drawer_open = False

    @rx.event
    def drawer_next(self):
        self._navigate_drawer(1)

    @rx.event
    def drawer_prev(self):
        self._navigate_drawer(-1)

    @rx.event
    def drawer_next_unreviewed(self):
        self._navigate_drawer(1, unreviewed_only=True)

    def _navigate_drawer(self, step: int, *, unreviewed_only: bool = False) -> None:
        view = self.exception_rows
        if not view:
            return
        idx = self.drawer_index
        if unreviewed_only:
            start = idx + step
            rng = range(start % len(view), start % len(view) + len(view)) if step > 0 else \
                  range(start, start - len(view), -1)
            for i in rng:
                j = i % len(view)
                if not view[j].reviewed:
                    self.open_drawer(view[j].result_id)
                    return
            self.flash = "Every item in this view is already reviewed."
            return
        j = (idx + step) % len(view)
        self.open_drawer(view[j].result_id)

    def _load_drawer(self) -> None:
        view = self.exception_rows
        match = next((r for r in view if r.result_id == self.selected_result_id), None)
        if match is None:
            # Fall back to the full item list (e.g. after a filter change).
            by_id = {i["result_id"]: i for i in self._model["items"]}
            if self.selected_result_id not in by_id:
                self.selected_result_id = 0
                self.drawer_open = False
                return
            view = self._item_rows([by_id[self.selected_result_id]], "")
        self.drawer_open = True
        idx = next((n for n, r in enumerate(view) if r.result_id == self.selected_result_id), 0)
        self.drawer_index = idx
        by_id = {i["result_id"]: i for i in self._model["items"]}
        item = by_id[self.selected_result_id]

        self.detail_supplier = item["party"] or "Unknown supplier"
        self.detail_gstin = item["gstin"] or review._UNAVAILABLE
        self.detail_reference = item["reference"] or review._UNAVAILABLE
        self.detail_date = item["date_label"]
        self.detail_classification = item["classification"]
        self.detail_cause_label = item["cause_label"]
        self.detail_confidence_band = item["confidence_band"]
        self.detail_confidence_label = item["confidence_label"]
        self.detail_status = "Reviewed" if item["reviewed"] else "Not reviewed"
        self.detail_reviewed = item["reviewed"]
        self.detail_verdict = review.verdict_text(item)
        self.detail_reason = item["match_reason"]

        # Comparison table — by default every relevant non-empty field, so the
        # reviewer sees the whole picture; "Show all fields" adds the ones empty
        # on both sides (e.g. the TDS-only columns on a GST item).
        all_rows = review.compare_fields(
            item["books"], item["portal"], self._model["recon_type"],
            materiality=self._materiality, overridden=item["_overridden"],
            include_all=self.detail_comparison_collapsed is False,
        )
        differing = [r for r in all_rows if r.differs]
        shown = all_rows
        self.detail_extra_count = 0
        self.detail_has_material = any(r.is_material for r in differing)
        self.detail_comparison = []
        for r in shown:
            self.detail_comparison.append(ComparisonRow(
                key=r.key, side="", section=r.section, label=r.label,
                books=r.books, portal=r.portal, difference=r.difference,
                differs=r.differs, material=r.is_material, overridden=r.overridden,
                books_raw=review.clean(item["books"].get(r.key)),
                portal_raw=review.clean(item["portal"].get(r.key)),
                books_editable=bool(item["books"]) and not str(r.key).startswith("_"),
                portal_editable=bool(item["portal"]) and not str(r.key).startswith("_"),
            ))

        # Compare the DISPLAYED rows (which may be filtered), so the count
        # matches what the reviewer is looking at.
        self.detail_corrected_fields = [
            CorrectedField(key=r.key, side=("books" if "books." + r.key in item["_overridden"]
                                            else "portal"),
                           label=r.label)
            for r in all_rows if r.overridden
        ]

        self.detail_key_chips = self._key_chips(item)
        self.detail_next_step, self.detail_step_link, self.detail_step_link_label, \
            self.detail_step_badge, self.detail_step_badge_text = self._next_step(item)

        sources = []
        for record in (item["books"], item["portal"]):
            f = review.clean(record.get("source_file"))
            s = review.clean(record.get("source_type"))
            if f:
                sources.append(f"{f} · {s}" if s else f)
        self.detail_sources = sources

        fp = item["fingerprint"]
        if fp:
            try:
                self.detail_history = (
                    load_history("match_result", f"{fp}:books")
                    + load_history("match_result", f"{fp}:portal")
                )
            except Exception:  # noqa: BLE001
                self.detail_history = []
        else:
            self.detail_history = []

        self.detail_note = item.get("reviewer_note") or ""
        self.detail_position = f"{idx + 1} of {len(view)}"
        self.detail_prev_disabled = len(view) < 2
        self.detail_next_disabled = len(view) < 2
        self.detail_next_unreviewed_disabled = all(r.reviewed for r in view)
        self.detail_has_prev_unreviewed = any(not r.reviewed for r in view)

    def _key_chips(self, item: dict) -> list[KeyChip]:
        """The 'how this was matched' chips — text labels, never colour alone."""
        books = item["books"]
        portal = item["portal"]
        chips: list[KeyChip] = []
        if books and portal:
            for label, a, b in (
                ("GSTIN", review.gstin_of(books), review.gstin_of(portal)),
                ("Invoice no.", review.reference_of(books), review.reference_of(portal)),
                ("Date", review.date_of(books), review.date_of(portal)),
            ):
                if not a and not b:
                    continue
                same = review._normalize_compare(a) == review._normalize_compare(b)
                chips.append(KeyChip(label=f"{label} {'✓' if same else '✗'}", ok=same))
            bv = review.basis_value(books, "invoice_value")
            pv = review.basis_value(portal, "invoice_value")
            if bv or pv:
                same = abs(bv - pv) <= review.DEFAULT_TOLERANCE
                chips.append(KeyChip(label=f"Amount {'✓' if same else '✗'}", ok=same))
        else:
            chips.append(KeyChip(label="Books side missing", ok=False))
        return chips

    def _next_step(self, item: dict):
        step = review.next_step(item["cause"])
        return (
            step.get("text", ""), step.get("link", ""), step.get("link_label", ""),
            step.get("badge", ""), step.get("badge_text", ""),
        )

    @rx.event
    def toggle_comparison(self):
        self.detail_comparison_collapsed = not self.detail_comparison_collapsed
        self._load_drawer()

    @rx.event
    def set_detail_note(self, v: str):
        self.detail_note = v

    @rx.event
    def mark_reviewed(self):
        """Mark the open record reviewed (the ONE blue primary)."""
        by_id = {i["result_id"]: i for i in self._model["items"]}
        item = by_id.get(self.selected_result_id)
        if item is None:
            return
        queries.mark_reviewed(
            self.ctx_client, self.ctx_period, self.ctx_recon_type, item["fingerprint"],
            note=self.detail_note or None,
            result=self._review_verdict(item),
        )
        self.flash = "Marked reviewed."
        self._load_review()
        self._load_drawer()

    @rx.event
    def clear_review(self):
        by_id = {i["result_id"]: i for i in self._model["items"]}
        item = by_id.get(self.selected_result_id)
        if item is None:
            return
        queries.clear_review(self.ctx_client, self.ctx_period, self.ctx_recon_type, item["fingerprint"])
        self.flash = "Review cleared."
        self._load_review()
        self._load_drawer()

    @rx.event
    def handle_hotkey(self, key: str):
        """J / K / R / Esc inside the drawer (wired from the shell's keydown)."""
        if not self.drawer_open:
            return
        k = (key or "").lower()
        if k in ("j", "arrowdown"):
            return ReconcileState.drawer_next
        if k in ("k", "arrowup"):
            return ReconcileState.drawer_prev
        if k == "r":
            return ReconcileState.mark_reviewed
        if k == "escape":
            return ReconcileState.close_drawer

    def _load_exception_detail(self) -> None:
        """Back-compat shim for callers that still use the old name."""
        self._load_drawer()

    # ------------------------------------------------------------------
    # Stage 4 — inline field editing
    # ------------------------------------------------------------------
    @rx.event
    def open_edit(self, side: str, field: str, value: str):
        self.edit_side = side
        self.edit_field = field
        self.edit_value = value
        self.edit_reason = ""
        self.edit_error = ""
        self.edit_side_label = "Books side" if side == "books" else "Portal side"
        self.edit_field_label = review.FIELD_LABELS.get(field) or str(field).replace("_", " ").capitalize()
        self.edit_open = True

    @rx.event
    def close_edit(self):
        self.edit_open = False
        self.edit_error = ""

    @rx.event
    def set_edit_value(self, v: str):
        self.edit_value = v

    @rx.event
    def set_edit_reason(self, v: str):
        self.edit_reason = v

    @rx.event
    def save_edit(self):
        """Save a reviewer's correction to one field of the matched record.

        The correction is stored against the record's fingerprint (so it
        survives a re-run) and fed back into Module 2 / the Action Center.
        A reason is required — a correction to a matched record is a
        sensitive change and must be explainable.
        """
        if not (self.edit_side and self.edit_field):
            self.edit_open = False
            return
        if not self.edit_reason.strip():
            self.edit_error = "A reason is required before saving a correction."
            return
        df = queries.get_results(self.run_id)
        match = df[df["result_id"] == self.selected_result_id]
        if match.empty:
            self.edit_open = False
            return
        fingerprint = match.iloc[0].get("fingerprint")
        if not fingerprint:
            self.edit_error = "This record has no stable identity, so it can't be corrected."
            return
        try:
            queries.set_field_override(
                self.ctx_client, self.ctx_period, self.ctx_recon_type, fingerprint,
                self.edit_side, self.edit_field, self.edit_value,
                reason=self.edit_reason.strip(), actor=self.username,
            )
            self.flash = f"Corrected {self.edit_field} — the change is logged and applied to the exceptions."
            self.edit_open = False
            self._load_review()
            self._load_drawer()
        except Exception as exc:  # noqa: BLE001
            self.edit_error = f"The correction couldn't be saved: {exc}"

    @rx.event
    def revert_edit(self, side: str, field: str):
        """Revert a field to the engine's original value."""
        by_id = {i["result_id"]: i for i in self._model["items"]}
        item = by_id.get(self.selected_result_id)
        if item is None or not item["fingerprint"]:
            return
        try:
            queries.clear_field_override(
                self.ctx_client, self.ctx_period, self.ctx_recon_type,
                item["fingerprint"], side, field,
            )
            self.flash = f"Reverted {field.replace('_', ' ')} to the engine's value."
            self._load_review()
            self._load_drawer()
        except Exception as exc:  # noqa: BLE001
            self.error = f"The revert couldn't be saved: {exc}"

    # ==================================================================
    # Stage 5 — Export
    # ==================================================================
    def _load_export(self) -> None:
        self.export_ready = False
        self.export_b64 = ""
        self.export_name = ""

    @rx.event
    def generate_export(self):
        self.error = ""
        if not self.run_id:
            self.error = "No run to export."
            return
        try:
            path = export_run(self.run_id)
            data = path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            self.error = f"The export couldn't be written: {exc}"
            return
        run = queries.get_run(self.run_id) or {}
        ts = str(run.get("run_timestamp") or "").replace("-", "").replace(":", "").split(".")[0]
        self.export_name = (
            f"{run.get('client', 'client')}_{run.get('period', 'period')}_"
            f"{run.get('recon_type', 'RECON')}_run{self.run_id}_{ts}.xlsx"
        )
        self.export_b64 = base64.b64encode(data).decode()
        self.export_ready = True
        self.flash = "Report ready."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_files(client: str, period: str, source_type: str) -> list[str]:
    directory = source_data_path(client, period, source_type)
    if not directory.exists():
        return []
    files: list[str] = []
    for pattern in ("*.csv", "*.xlsx", "*.xls"):
        files.extend(sorted(m.name for m in directory.glob(pattern)))
    return sorted(set(files))


def _client_id_for_folder(folder: str) -> int | None:
    def norm(name: str) -> str:
        return "".join(ch for ch in str(name).lower() if ch.isalnum())

    try:
        candidates = clients.list_clients(include_inactive=True)
    except Exception:  # noqa: BLE001
        return None
    target = norm(folder)
    for c in candidates:
        if norm(c["legal_name"]) == target:
            return c["client_id"]
    for c in candidates:
        n = norm(c["legal_name"])
        if target and (n.startswith(target) or target.startswith(n)):
            return c["client_id"]
    return None


def _record_sync_health(client_id, selected_files: dict, *, succeeded: bool) -> None:
    if client_id is None:
        return
    try:
        for source_type in selected_files:
            src = _SYNC_SOURCE_FOR.get(source_type)
            if src is None:
                continue
            f5.record_sync_health(
                client_id=client_id, source=src,
                status="succeeded" if succeeded else "attempted_failed",
            )
    except Exception:  # noqa: BLE001
        pass
