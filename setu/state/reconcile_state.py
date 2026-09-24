"""Reconcile state — the guided five-stage flow (the LAST module).

One guided path from source files to a finished report: Context → Upload →
Reconcile → Review → Export. ORCHESTRATION ONLY — it reuses the same engine
and services every other screen uses (``ingestion_ai.normalize_source_file``,
``runner.execute_run``, ``export.export_run``, ``queries``, ``f5``, ``module2``).
No matching logic is recomputed here.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import reflex as rx

from src import queries
from src.auth import service as auth
from src.clients import service as clients
from src.config_loader import load_config
from src.data_paths import source_data_path
from src.export import export_run
from src.f5 import service as f5
from src.ingestion_ai import service as ingestion_ai
from src.ingestion_ai import periods as period_utils
from src.runner import RunExecutionError, execute_run
from src.shared import discovery
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history

STAGES: list[tuple[int, str]] = [
    (1, "Context"),
    (2, "Upload"),
    (3, "Reconcile"),
    (4, "Review"),
    (5, "Export"),
]

_SYNC_SOURCE_FOR = {
    "tally": "tally", "gstr2b": "gst_portal", "ims": "gst_portal",
    "form26as": "traces", "tds": "traces",
    "bank": "bank", "vendor_ledger": "tally", "opening_balances": "tally",
    "loan_sheet": "tally", "salary": "tally",
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
class ExceptionRow:
    result_id: int
    classification: str
    difference_type: str
    confidence_band: str
    reviewed: bool
    match_reason: str


@dataclass
class RecordField:
    label: str
    value: str
    key: str = ""
    side: str = ""
    overridden: bool = False


@dataclass
class CaveatRow:
    label: str
    detail: str
    source: str


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

    # stage 4
    review_total: int = 0
    review_matched: int = 0
    review_exceptions: int = 0
    by_classification: dict[str, int] = {}
    caveats: list[CaveatRow] = []
    exception_rows: list[ExceptionRow] = []
    exception_filter: str = "All exceptions"
    selected_result_id: int = 0
    detail_classification: str = ""
    detail_difference_type: str = ""
    detail_reason: str = ""
    detail_reviewed: bool = False
    detail_books: list[RecordField] = []
    detail_portal: list[RecordField] = []

    # stage 4 — inline field editing
    edit_side: str = ""
    edit_field: str = ""
    edit_value: str = ""
    edit_reason: str = ""
    edit_open: bool = False
    edit_error: str = ""

    # stage 4 — edit history for the open record
    detail_history: list[HistoryEntry] = []

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
    def load(self):
        self.clients = discovery.list_clients()
        if not self.ctx_client and self.clients:
            self.ctx_client = self.clients[0]
        self._load_context()

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
        portals = set(discovery.portal_source_types(self.ctx_recon_type))
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
        if not self.run_id:
            return
        summary = queries.get_run_summary(self.run_id)
        self.review_total = int(summary.get("total_results") or 0)
        self.by_classification = summary.get("by_classification") or {}
        self.review_matched = int(self.by_classification.get("Matched") or 0)
        self.review_exceptions = self.review_total - self.review_matched

        run = queries.get_run(self.run_id) or {}
        self.caveats = [
            CaveatRow(
                label=c.get("label") or c.get("code") or "Limitation",
                detail=c.get("detail") or "",
                source=c.get("filename") or c.get("source_type") or "",
            )
            for c in (run.get("caveats") or [])
        ]

        df = queries.get_results(self.run_id)
        rows: list[ExceptionRow] = []
        for _, r in df.iterrows():
            if r["classification"] == "Matched":
                continue
            if self.exception_filter != "All exceptions" and r["classification"] != self.exception_filter:
                continue
            rows.append(
                ExceptionRow(
                    result_id=int(r["result_id"]),
                    classification=r["classification"],
                    difference_type=r["difference_type"] if isinstance(r.get("difference_type"), str) else "",
                    confidence_band=r["confidence_band"],
                    reviewed=bool(r.get("reviewed")),
                    match_reason=r.get("match_reason") or "",
                )
            )
        self.exception_rows = rows
        if self.selected_result_id:
            self._load_exception_detail()

    @rx.event
    def set_exception_filter(self, v: str):
        self.exception_filter = v
        self.selected_result_id = 0
        self._load_review()

    @rx.event
    def open_exception(self, result_id: int):
        self.selected_result_id = result_id
        self._load_exception_detail()

    @rx.event
    def close_exception(self):
        self.selected_result_id = 0

    def _load_exception_detail(self) -> None:
        df = queries.get_results(self.run_id)
        match = df[df["result_id"] == self.selected_result_id]
        if match.empty:
            self.selected_result_id = 0
            return
        row = match.iloc[0]
        self.detail_classification = row["classification"]
        self.detail_difference_type = row["difference_type"] if isinstance(row.get("difference_type"), str) else ""
        self.detail_reason = row.get("match_reason") or ""
        self.detail_reviewed = bool(row.get("reviewed"))
        overridden = set(row.get("overridden_fields") or [])
        self.detail_books = _record_fields(row.get("books_record"), "books", overridden)
        self.detail_portal = _record_fields(row.get("portal_record"), "portal", overridden)
        fp = row.get("fingerprint")
        if fp:
            try:
                self.detail_history = load_history("match_result", f"{fp}:books")
                self.detail_history += load_history("match_result", f"{fp}:portal")
            except Exception:  # noqa: BLE001
                self.detail_history = []
        else:
            self.detail_history = []

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
            self._load_exception_detail()
        except Exception as exc:  # noqa: BLE001
            self.edit_error = f"The correction couldn't be saved: {exc}"

    @rx.event
    def revert_edit(self, side: str, field: str):
        """Revert a field to the engine's original value."""
        df = queries.get_results(self.run_id)
        match = df[df["result_id"] == self.selected_result_id]
        if match.empty:
            return
        fingerprint = match.iloc[0].get("fingerprint")
        if not fingerprint:
            return
        try:
            queries.clear_field_override(
                self.ctx_client, self.ctx_period, self.ctx_recon_type, fingerprint, side, field,
            )
            self.flash = f"Reverted {field} to the engine's value."
            self._load_exception_detail()
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


def _record_fields(record, side: str = "", overridden: set[str] | None = None) -> list[RecordField]:
    if not isinstance(record, dict):
        return []
    overridden = overridden or set()
    out: list[RecordField] = []
    for k, v in record.items():
        if str(k).startswith("_") or v in (None, ""):
            continue
        out.append(RecordField(
            label=str(k).replace("_", " "),
            value=str(v),
            key=str(k),
            side=side,
            overridden=f"{side}.{k}" in overridden,
        ))
    return out


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
