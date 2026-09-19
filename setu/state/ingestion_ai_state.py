"""F3-AI state — Smart Document Ingestion.

Upload → AI-infer mapping → human confirm (hard gate). Upload list, mapping
review screen (raw-file context + per-field edge/confidence), mapping profiles,
confidence thresholds. Every handler calls ``src.ingestion_ai.service``.

Upload staging, per-file progress and the in-app viewer are inherited from
``SharedUploadState`` — the SAME implementation F3 (Document Vault) and F3-B
(Invoice Extraction) use, so the three screens cannot drift apart on those
behaviours. This module is spreadsheet/CSV only (OCR is explicitly out of
scope per its own copy), so no image/PDF preview rendering is added here.

The upload handler is async because Reflex ``UploadFile.read()`` is a coroutine.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.data_paths import VALID_SOURCE_TYPES
from src.ingestion_ai import service as ingestion_ai
from setu.state.shared_upload import SharedUploadState

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
    "empty": "Empty — 0 row(s) read",
}

# The three real failure modes are DIFFERENT problems and must look
# different, not merely read differently. Each maps to a distinct Foundation
# pill variant (neutral / amber / coral) — see the view's status cell.
STATUS_NEEDS_CONFIRM = "needs_confirm"
STATUS_UNRECOGNIZED = "unrecognized"
STATUS_BLOCKED = "blocked"
STATUS_EMPTY = "empty"
STATUS_CONFIRMED = "confirmed"

ALL_CLIENTS = "All clients"
LEAVE_UNMAPPED = "(leave unmapped)"


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class UploadRow:
    upload_id: int
    filename: str
    client_label: str
    source_type: str
    source_label: str
    period: str
    status: str
    status_label: str
    blocked_count: int
    row_count_in: int
    row_count_out: int
    counts_known: bool
    # Every field at/above the auto-apply threshold AND at least one row
    # actually read — the only uploads eligible for bulk confirm.
    bulk_eligible: bool


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
class ColumnRow:
    """One row of the mapping review table — a DETECTED SOURCE COLUMN and
    the canonical field it was mapped to.

    The review table is built by inverting the AI's canonical→column
    mapping, so every column in the file appears exactly once and the user
    can correct the canonical field a column was assigned to. Canonical
    fields the AI could not map are appended as their own rows with an
    empty ``raw_column`` — shown explicitly as needs-input rather than
    omitted, because silence here is the trust problem this page exists to
    solve.
    """

    raw_column: str
    mapped_field: str
    confidence: int
    reason: str
    status: str
    required: bool
    from_trusted_profile: bool
    is_unmapped_field: bool


@dataclass
class ProfileRow:
    profile_id: int
    client_label: str
    source_label: str
    trusted: bool
    last_used_at: str
    usage_count: int
    shape_count: int
    rules: list[str]


@dataclass
class ShapeRow:
    """A learned mapping keyed by the file's exact header signature — the
    store the runtime actually reuses from."""

    shape_id: int
    client_label: str
    source_label: str
    header_signature: str
    trusted: bool
    times_used: int
    last_used_at: str
    rules: list[str]


class IngestionAiState(SharedUploadState):
    """Smart Document Ingestion state."""

    section: str = "Upload & Uploads List"

    client_options: list[ClientOption] = []
    client_label_map: dict[str, str] = {}
    # 0 = "All clients" — the top-level selector genuinely filters the list
    # below (the same fix applied to F3's Document Vault).
    client_id: int = 0

    # upload form
    source_type: str = "gstr2b"
    period: str = ""
    upload_error: str = ""

    uploads: list[UploadRow] = []

    # list filters / sort / search
    filter_client: str = ALL_CLIENTS
    filter_source: str = "All sources"
    filter_status: str = "All statuses"
    search: str = ""
    sort_key: str = "Newest first"

    # row selection + bulk confirm
    selected_upload_ids: list[int] = []
    bulk_trust: bool = False
    bulk_confirm_open: bool = False

    # mapping review
    selected_upload_id: int = 0
    review_filename: str = ""
    review_source_label: str = ""
    review_status: str = ""
    review_period: str = ""
    blocked_banner: str = ""
    warning_banner: str = ""
    classification_error: str = ""
    review_c5_used: bool = False
    needs_attention: list[FieldRow] = []
    confident_fields: list[FieldRow] = []
    show_confident: bool = False
    overrides: dict[str, str] = {}
    trust_reuse: bool = False
    # The mapping review TABLE: one row per detected source column, plus a
    # row per canonical field the AI left unmapped.
    columns: list[ColumnRow] = []
    # raw_column -> canonical_field, the user's edits.
    column_overrides: dict[str, str] = {}
    # canonical_field -> raw_column, the inverse, sent to the service.
    field_overrides: dict[str, str] = {}
    source_column_options: list[str] = []
    canonical_field_options: list[str] = []
    mapping_error: str = ""
    # Confirmation dialog state.
    confirm_open: bool = False
    allow_empty_ack: bool = False

    # raw-file preview (first rows of the actual sheet)
    preview_headers: list[str] = []
    preview_rows: list[list[str]] = []
    preview_note: str = ""

    # raw-file context
    header_row_note: str = ""
    row_count_note: str = ""
    counts_known: bool = False
    notes: list[str] = []
    warnings: list[str] = []

    # profiles + thresholds
    profiles: list[ProfileRow] = []
    shapes: list[ShapeRow] = []
    profile_search: str = ""
    profile_source_filter: str = "All sources"
    revoke_target_id: int = 0
    revoke_open: bool = False
    view_mapping_open: bool = False
    view_mapping_title: str = ""
    view_mapping_rules: list[str] = []

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

    @rx.var
    def client_names(self) -> list[str]:
        return [ALL_CLIENTS] + [o.legal_name for o in self.client_options]

    @rx.var
    def client_name(self) -> str:
        if self.client_id == 0:
            return ALL_CLIENTS
        for o in self.client_options:
            if o.client_id == self.client_id:
                return o.legal_name
        return ALL_CLIENTS

    @rx.var
    def source_filter_options(self) -> list[str]:
        return ["All sources"] + sorted(VALID_SOURCE_TYPES)

    @rx.var
    def status_filter_options(self) -> list[str]:
        return ["All statuses", "Ready for review", "Blocked", "Unrecognized shape", "Empty", "Confirmed"]

    @rx.var
    def sort_options(self) -> list[str]:
        return ["Newest first", "Oldest first", "Filename A–Z", "Status"]

    @rx.var
    def selected_count(self) -> int:
        return len(self.selected_upload_ids)

    @rx.var
    def bulk_eligible_count(self) -> int:
        return sum(1 for u in self.uploads if u.bulk_eligible)

    @rx.var
    def review_is_empty(self) -> bool:
        return self.review_status == STATUS_EMPTY

    @rx.var
    def unmapped_column_count(self) -> int:
        return sum(1 for c in self.columns if c.is_unmapped_field)

    @rx.var
    def mapped_column_count(self) -> int:
        return sum(1 for c in self.columns if not c.is_unmapped_field)

    @rx.var
    def canonical_field_select_options(self) -> list[str]:
        """The dropdown options for a column's canonical field: every valid
        canonical field for this slot, plus a leave-unmapped sentinel. Built
        here (not in the view) because a Var list cannot be concatenated
        with a Python list at compile time."""
        return [LEAVE_UNMAPPED] + list(self.canonical_field_options)

    @rx.var
    def filtered_profiles(self) -> list[ProfileRow]:
        return self._filter_profiles(self.profiles)

    @rx.var
    def filtered_shapes(self) -> list[ShapeRow]:
        return self._filter_profiles(self.shapes)

    def _filter_profiles(self, rows):
        out = rows
        if self.profile_source_filter != "All sources":
            out = [r for r in out if r.source_label == SOURCE_TYPE_LABELS.get(
                self.profile_source_filter, self.profile_source_filter
            )]
        if self.profile_search.strip():
            needle = self.profile_search.strip().lower()
            out = [
                r for r in out
                if needle in r.client_label.lower() or needle in r.source_label.lower()
            ]
        return out

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    def _client_label(self, client_id) -> str:
        if client_id is None:
            return "—"
        return self.client_label_map.get(str(client_id), f"Client #{client_id}")

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if (deny := self._gate("ingestion_ai.upload")):
            return rx.redirect(deny)
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self.client_label_map = {
            str(c["client_id"]): c["legal_name"]
            for c in clients.list_clients(include_inactive=True)
        }
        self._load_all()

    def _load_all(self) -> None:
        self._load_uploads()
        self._load_profiles()
        self.auto_apply = ingestion_ai.get_thresholds()["auto_apply"]
        self.manual = ingestion_ai.get_thresholds()["manual"]
        self.preselect_pct = int(round(ingestion_ai.get_preselect_threshold() * 100))

    def _load_uploads(self) -> None:
        rows = ingestion_ai.list_uploads_with_context(
            client_id=self.client_id or None,
            status=None,
        )
        threshold = ingestion_ai.get_preselect_threshold()
        auto = ingestion_ai.get_thresholds()["auto_apply"]
        out: list[UploadRow] = []
        for u in rows:
            blocked = 0
            all_confident = True
            if u["status"] in ("blocked", "needs_confirm", "empty"):
                report = ingestion_ai.get_report(u["upload_id"])
                if report:
                    for f in report["field_mapping"]:
                        conf = f.get("confidence")
                        if not f.get("raw_column") or conf is None or (conf or 0) < auto:
                            all_confident = False
                        if (
                            not f.get("raw_column")
                            or conf is None
                            or (conf or 0) / 100.0 < threshold
                        ):
                            blocked += 1
            out.append(
                UploadRow(
                    upload_id=u["upload_id"],
                    filename=u["filename"],
                    client_label=self._client_label(u["client_id"]),
                    source_type=u["source_type"],
                    source_label=SOURCE_TYPE_LABELS.get(u["source_type"], u["source_type"]),
                    period=u.get("period") or "",
                    status=u["status"],
                    status_label=_STATUS_LABELS.get(u["status"], u["status"]),
                    blocked_count=blocked,
                    row_count_in=int(u.get("row_count_in") or 0),
                    row_count_out=int(u.get("row_count_out") or 0),
                    counts_known=bool(u.get("has_stored_result")),
                    bulk_eligible=(
                        u["status"] == STATUS_NEEDS_CONFIRM
                        and all_confident
                        and int(u.get("row_count_out") or 0) > 0
                    ),
                )
            )
        self.uploads = self._apply_list_filters(out)

    def _apply_list_filters(self, rows: list[UploadRow]) -> list[UploadRow]:
        out = rows
        if self.filter_client != ALL_CLIENTS:
            out = [r for r in out if r.client_label == self.filter_client]
        if self.filter_source != "All sources":
            out = [r for r in out if r.source_type == self.filter_source]
        if self.filter_status != "All statuses":
            out = [r for r in out if r.status_label == self.filter_status]
        if self.search.strip():
            needle = self.search.strip().lower()
            out = [
                r for r in out
                if needle in r.filename.lower()
                or needle in r.client_label.lower()
                or needle in r.source_label.lower()
                or needle in r.period.lower()
            ]
        if self.sort_key == "Oldest first":
            out = sorted(out, key=lambda r: r.upload_id)
        elif self.sort_key == "Filename A–Z":
            out = sorted(out, key=lambda r: r.filename.lower())
        elif self.sort_key == "Status":
            out = sorted(out, key=lambda r: (r.status, r.filename.lower()))
        else:
            out = sorted(out, key=lambda r: r.upload_id, reverse=True)
        return out

    def _load_profiles(self) -> None:
        self.profiles = [
            ProfileRow(
                profile_id=p["profile_id"],
                client_label=self._client_label(p["client_id"]),
                source_label=SOURCE_TYPE_LABELS.get(p["source_type"], p["source_type"]),
                trusted=bool(p["trusted"]),
                last_used_at=str(p.get("last_used_at") or "").replace("T", " ").split(".")[0],
                usage_count=int(p.get("usage_count") or 0),
                shape_count=int(p.get("shape_count") or 0),
                rules=_rules_from_column_map(p.get("column_map") or {}),
            )
            for p in ingestion_ai.list_profiles_with_usage()
        ]
        self.shapes = [
            ShapeRow(
                shape_id=s["shape_id"],
                client_label=self._client_label(s.get("client_id")) if s.get("client_id") else (s.get("client_ref") or "—"),
                source_label=SOURCE_TYPE_LABELS.get(s["source_type"], s["source_type"]),
                header_signature=s.get("header_signature") or "",
                trusted=bool(s.get("trusted")),
                times_used=int(s.get("times_used") or 0),
                last_used_at=str(s.get("last_used_at") or "").replace("T", " ").split(".")[0],
                rules=_rules_from_column_map(s.get("mapping") or {}),
            )
            for s in ingestion_ai.list_shapes()
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
    def set_client_by_name(self, name: str):
        if name == ALL_CLIENTS:
            self.client_id = 0
        else:
            for o in self.client_options:
                if o.legal_name == name:
                    self.client_id = o.client_id
                    break
        self.selected_upload_id = 0
        self.selected_upload_ids = []
        self._load_all()

    def set_source_type(self, v: str):
        self.source_type = v

    def set_period(self, v: str):
        self.period = v

    def set_trust_reuse(self, v: bool):
        self.trust_reuse = v

    def set_show_confident(self, v: bool):
        self.show_confident = v

    def set_filter_client(self, v: str):
        self.filter_client = v
        self._load_uploads()

    def set_filter_source(self, v: str):
        self.filter_source = v
        self._load_uploads()

    def set_filter_status(self, v: str):
        self.filter_status = v
        self._load_uploads()

    def set_search(self, v: str):
        self.search = v
        self._load_uploads()

    def set_sort_key(self, v: str):
        self.sort_key = v
        self._load_uploads()

    def _client_ref(self) -> str:
        for c in self.client_options:
            if c.client_id == self.client_id:
                return c.legal_name
        return str(self.client_id)

    # ------------------------------------------------------------------
    # Upload — staged (FilePreviewChip) then per-file progress
    # (UploadProgressState), shared with F3/F3-B.
    # ------------------------------------------------------------------
    def _set_stage(self, key: str, stage: str, *, pct: int = 0, label: str = "") -> None:
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
        """Upload staged files through the distinct Queued → Uploading (%)
        → Inferring mapping → Done / Needs review / Failed states."""
        import asyncio

        from setu.state.shared_upload import UploadProgressRow

        self.upload_error = ""
        self.flash = ""
        if not self.pending:
            self.upload_error = "Choose at least one spreadsheet/CSV to upload."
            return
        if not self.client_id:
            self.upload_error = "Select a client before uploading."
            return

        staged = list(self.pending)
        self.upload_progress = [
            UploadProgressRow(key=pf.key, filename=pf.filename, stage="queued", pct=0, label="Queued")
            for pf in staged
        ]
        yield

        results: list[dict] = []
        errors: list[str] = []
        for pf in staged:
            self._set_stage(pf.key, "uploading", pct=10)
            yield
            await asyncio.sleep(0.03)
            self._set_stage(pf.key, "uploading", pct=60)
            yield
            self._set_stage(pf.key, "processing", label="Inferring mapping…")
            yield
            try:
                res = ingestion_ai.upload_and_infer(
                    client_id=self.client_id,
                    source_type=self.source_type,
                    filename=pf.filename,
                    file_bytes=self._pending_bytes(pf.key),
                    period=self.period or None,
                    actor=self.username,
                    client_ref=self._client_ref(),
                )
                status = res["status"]
                if status == STATUS_EMPTY:
                    self._set_stage(pf.key, "failed", label="Empty — 0 row(s) read")
                elif status == STATUS_UNRECOGNIZED:
                    self._set_stage(pf.key, "needs_review", label="Unrecognized shape — needs manual review")
                elif status == STATUS_BLOCKED:
                    n = len(res["ingestion"].unmapped_required_fields)
                    self._set_stage(pf.key, "needs_review", label=f"Blocked — {n} field(s) need mapping")
                else:
                    self._set_stage(pf.key, "done", pct=100, label="Mapping inferred — ready for review")
                results.append(res)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{pf.filename}: {exc}")
                self._set_stage(pf.key, "failed", label=str(exc))
            yield

        if errors:
            self.upload_error = " · ".join(errors)
        if results:
            n_review = sum(1 for r in results if r["status"] == STATUS_NEEDS_CONFIRM)
            n_empty = sum(1 for r in results if r["status"] == STATUS_EMPTY)
            n_blocked = sum(1 for r in results if r["status"] == STATUS_BLOCKED)
            n_unrec = sum(1 for r in results if r["status"] == STATUS_UNRECOGNIZED)
            parts = [f"{len(results)} uploaded"]
            if n_review:
                parts.append(f"{n_review} ready for review")
            if n_blocked:
                parts.append(f"{n_blocked} blocked — field(s) need mapping")
            if n_unrec:
                parts.append(f"{n_unrec} unrecognized shape")
            if n_empty:
                parts.append(f"{n_empty} empty — 0 row(s) read")
            self.flash = " · ".join(parts) + "."
            # Open the first upload that needs a human.
            first = next(
                (r for r in results if r["status"] in (STATUS_NEEDS_CONFIRM, STATUS_BLOCKED, STATUS_EMPTY)),
                None,
            )
            if first:
                self.selected_upload_id = first["upload_id"]
                self._load_review()
        self.period = ""
        self.clear_pending()
        self._load_all()

    # ------------------------------------------------------------------
    # Mapping review
    # ------------------------------------------------------------------
    @rx.event
    def open_upload(self, upload_id: int):
        self.selected_upload_id = upload_id
        self.overrides = {}
        self.column_overrides = {}
        self.trust_reuse = False
        self.mapping_error = ""
        self.allow_empty_ack = False
        self._load_review()

    @rx.event
    def back_to_uploads(self):
        self.selected_upload_id = 0
        self.confirm_open = False
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
                candidates=_candidate_columns(f.get("candidates")),
                status=f.get("status") or "",
                needs_attention=is_needs,
            )
            (needs if is_needs else confident).append(row)
            self.overrides.setdefault(row.canonical_field, row.raw_column)
        self.needs_attention = needs
        self.confident_fields = confident

        # --- The review TABLE: one row per detected source column ---
        stored = ingestion_ai.stored_result_for_upload(self.selected_upload_id) or {}

        # --- Raw-file data preview (first rows of the actual sheet) ---
        # Loaded BEFORE the table so the table can fall back to the preview's
        # real headers when an upload predates the stored ingestion result.
        self._load_preview(upload)

        headers = [str(h) for h in (stored.get("headers") or [])]
        if not headers:
            headers = list(self.preview_headers)
        self.source_column_options = headers
        self.canonical_field_options = _canonical_fields_for(upload["source_type"])

        by_column: dict[str, dict] = {}
        for f in fields:
            col = f.get("raw_column")
            if col:
                by_column[str(col)] = f
        columns: list[ColumnRow] = []
        for h in headers:
            f = by_column.get(h)
            if f is None:
                columns.append(ColumnRow(
                    raw_column=h, mapped_field="", confidence=0, reason="",
                    status="unmapped", required=False, from_trusted_profile=False,
                    is_unmapped_field=False,
                ))
            else:
                columns.append(ColumnRow(
                    raw_column=h,
                    mapped_field=f["canonical_field"],
                    confidence=int(f.get("confidence") or 0),
                    reason=f.get("reason") or "",
                    status=f.get("status") or "",
                    required=bool(f.get("required")),
                    from_trusted_profile=bool(f.get("from_trusted_profile")),
                    is_unmapped_field=False,
                ))
        # Canonical fields the AI could not map — shown explicitly, never omitted.
        for f in fields:
            if not f.get("raw_column"):
                columns.append(ColumnRow(
                    raw_column="", mapped_field=f["canonical_field"],
                    confidence=0, reason=f.get("reason") or "",
                    status="unavailable", required=bool(f.get("required")),
                    from_trusted_profile=False, is_unmapped_field=True,
                ))
        self.columns = columns
        self.column_overrides = {c.raw_column: c.mapped_field for c in columns if c.raw_column}
        self._recompute_field_overrides()

        # --- Raw-file context ---
        self.header_row_note = (
            f"Header detected on row {stored['header_row'] + 1}." if stored.get("header_row") is not None else ""
        )
        self.counts_known = bool(stored)
        if stored:
            self.row_count_note = (
                f"{stored.get('row_count_in', 0)} row(s) read · "
                f"{stored.get('row_count_out', 0)} row(s) extracted."
            )
        else:
            self.row_count_note = "Row counts unavailable for this upload."
        self.notes = list(stored.get("notes") or [])[:3]
        self.warnings = list(stored.get("warnings") or [])[:5]

        self.blocked_banner = (
            f"Blocked — {len(needs)} field(s) need mapping" if upload["status"] == STATUS_BLOCKED else ""
        )
        self.classification_error = ""
        self.warning_banner = ""
        classification = stored.get("classification")
        if upload["status"] == STATUS_EMPTY:
            self.classification_error = (
                "This file read 0 rows — there is no data for a mapping to apply to. "
                "Confirm it as a deliberately empty file if that is expected, or re-upload the correct file."
            )
        elif classification and not classification.get("is_financial_data", True):
            self.classification_error = (
                "This file doesn't look like reconciliation data — it doesn't contain "
                "recognizable invoice, GSTIN, or amount columns."
            )
        elif stored.get("status") == "wrong_slot":
            self.warning_banner = stored.get("message") or "This file looks like it belongs in a different upload slot."

    def _load_preview(self, upload: dict) -> None:
        """First several rows of the ACTUAL sheet, so the mapping can be
        sanity-checked against real values rather than column headers alone.
        Read once per open upload — never on every rerun."""
        self.preview_headers = []
        self.preview_rows = []
        self.preview_note = ""
        try:
            from src.documents import service as documents
            from src.ingestion_ai import mapper

            doc = documents.get_document(upload["document_id"])
            if not doc or not doc.get("current_version_id"):
                self.preview_note = "Source preview unavailable for this upload."
                return
            data = documents.get_version_bytes(doc["current_version_id"])
            if not data:
                self.preview_note = "Source preview unavailable for this upload."
                return
            import tempfile
            from pathlib import Path

            suffix = "." + upload["filename"].rsplit(".", 1)[-1].lower() if "." in upload["filename"] else ".csv"
            fd, path_str = tempfile.mkstemp(suffix=suffix)
            import os

            os.close(fd)
            path = Path(path_str)
            path.write_bytes(data)
            try:
                df = mapper.read_with_detected_header(path)
            finally:
                path.unlink(missing_ok=True)
            self.preview_headers = [str(c) for c in df.columns]
            rows: list[list[str]] = []
            for _, r in df.head(8).iterrows():
                rows.append(["" if _is_blank(v) else str(v) for v in r])
            self.preview_rows = rows
            if df.empty:
                self.preview_note = "The sheet has no data rows."
        except Exception as exc:  # noqa: BLE001
            self.preview_note = f"Source preview unavailable ({exc})."

    def set_column_mapping(self, raw_column: str, canonical_field: str):
        """The user overrides which canonical field a source column maps to.

        The ColumnRow list is the single source of truth for what the table
        displays, so the row is updated in place — binding the dropdown to a
        separate override dict would leave the control showing the AI's
        original value after an edit.
        """
        field = "" if canonical_field.startswith("(leave unmapped") else canonical_field
        self.columns = [
            _replace_column_field(c, raw_column, field) for c in self.columns
        ]
        self.column_overrides[raw_column] = field
        self.mapping_error = ""
        self._recompute_field_overrides()

    def _recompute_field_overrides(self) -> None:
        """Invert column→field into the service's field→column shape, and
        reject a mapping that assigns one canonical field to two columns
        (the same one-column-per-field rule the AI mapping enforces)."""
        inverse: dict[str, str] = {}
        dupes: list[str] = []
        for col, field in self.column_overrides.items():
            if not field:
                continue
            if field in inverse:
                dupes.append(field)
                continue
            inverse[field] = col
        self.field_overrides = inverse
        if dupes:
            self.mapping_error = (
                f"{', '.join(sorted(set(dupes)))} is assigned to more than one column — "
                "each canonical field can come from only one source column."
            )

    def set_override(self, field: str, v: str):
        self.overrides[field] = "" if v.startswith("(leave unmapped") else v

    @rx.event
    def open_confirm(self):
        self.mapping_error = ""
        self._recompute_field_overrides()
        if self.mapping_error:
            return
        self.allow_empty_ack = False
        self.confirm_open = True

    @rx.event
    def close_confirm(self):
        self.confirm_open = False

    @rx.event
    def set_allow_empty_ack(self, v: bool):
        self.allow_empty_ack = v

    @rx.event
    def confirm_mapping(self):
        self.error = ""
        self.mapping_error = ""
        self._recompute_field_overrides()
        if self.mapping_error:
            self.confirm_open = False
            return
        if self.review_is_empty and not self.allow_empty_ack:
            self.mapping_error = (
                "This file read 0 rows. Tick the acknowledgement to confirm it as a "
                "deliberately empty file."
            )
            self.confirm_open = False
            return
        override_map = {k: (None if v == "" else v) for k, v in self.field_overrides.items()}
        try:
            ingestion_ai.confirm_mapping(
                self.selected_upload_id,
                field_overrides=override_map,
                trust_for_reuse=self.trust_reuse,
                actor=self.username,
                client_ref=self._client_ref(),
                allow_empty=self.allow_empty_ack,
            )
            self.flash = "Mapping confirmed — hard gate cleared, ready for reconciliation."
            self.selected_upload_id = 0
            self.confirm_open = False
        except ingestion_ai.IngestionAIError as exc:
            self.error = str(exc)
            self.confirm_open = False
        self._load_all()

    # ------------------------------------------------------------------
    # Row selection + bulk confirm
    # ------------------------------------------------------------------
    @rx.event
    def toggle_upload_select(self, upload_id: int):
        if upload_id in self.selected_upload_ids:
            self.selected_upload_ids = [i for i in self.selected_upload_ids if i != upload_id]
        else:
            self.selected_upload_ids = self.selected_upload_ids + [upload_id]

    @rx.event
    def select_all_eligible(self):
        self.selected_upload_ids = [u.upload_id for u in self.uploads if u.bulk_eligible]

    @rx.event
    def clear_selection(self):
        self.selected_upload_ids = []

    @rx.event
    def set_bulk_trust(self, v: bool):
        self.bulk_trust = v

    @rx.event
    def open_bulk_confirm(self):
        if not self.selected_upload_ids:
            return
        self.bulk_confirm_open = True

    @rx.event
    def close_bulk_confirm(self):
        self.bulk_confirm_open = False

    @rx.event
    def bulk_confirm(self):
        """Confirm every selected upload whose mapping is already fully
        confident. Trust is opt-in via the bulk bar's toggle, and the
        confirmation dialog states the consequence before it is applied."""
        self.error = ""
        done = 0
        skipped: list[str] = []
        for uid in list(self.selected_upload_ids):
            row = next((u for u in self.uploads if u.upload_id == uid), None)
            if row is None or not row.bulk_eligible:
                skipped.append(row.filename if row else f"#{uid}")
                continue
            report = ingestion_ai.get_report(uid)
            mapping = {
                f["canonical_field"]: (f.get("raw_column") or None)
                for f in (report["field_mapping"] if report else [])
            }
            try:
                ingestion_ai.confirm_mapping(
                    uid, field_overrides=mapping, trust_for_reuse=self.bulk_trust,
                    actor=self.username, client_ref=self._client_ref(),
                )
                done += 1
            except ingestion_ai.IngestionAIError as exc:
                skipped.append(f"{row.filename} ({exc})")
        parts = [f"{done} upload(s) confirmed"]
        if self.bulk_trust and done:
            parts.append("mapping trusted for future reuse")
        if skipped:
            parts.append(f"{len(skipped)} skipped — not fully confident or not eligible")
        self.flash = " · ".join(parts) + "."
        self.selected_upload_ids = []
        self.bulk_confirm_open = False
        self.bulk_trust = False
        self._load_all()

    # ------------------------------------------------------------------
    # Profiles + thresholds
    # ------------------------------------------------------------------
    def set_profile_search(self, v: str):
        self.profile_search = v

    def set_profile_source_filter(self, v: str):
        self.profile_source_filter = v

    @rx.event
    def open_view_mapping(self, title: str, rules: list[str]):
        self.view_mapping_title = title
        self.view_mapping_rules = rules
        self.view_mapping_open = True

    @rx.event
    def close_view_mapping(self):
        self.view_mapping_open = False

    @rx.event
    def open_revoke(self, profile_id: int):
        self.revoke_target_id = profile_id
        self.revoke_open = True

    @rx.event
    def close_revoke(self):
        self.revoke_open = False
        self.revoke_target_id = 0

    @rx.event
    def confirm_revoke(self):
        if not self.revoke_target_id:
            return
        try:
            n = ingestion_ai.revoke_profile_trust(self.revoke_target_id)
            self.flash = (
                f"Profile trust revoked — {n} learned shape(s) untrusted. "
                "The next file of this shape will be re-mapped."
            )
        except ingestion_ai.IngestionAIError as exc:
            self.error = str(exc)
        self.revoke_open = False
        self.revoke_target_id = 0
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replace_column_field(row: ColumnRow, raw_column: str, field: str) -> ColumnRow:
    """Return a copy of a ColumnRow with its canonical field replaced.

    Reflex dataclasses are immutable-ish state values, so the row is rebuilt
    rather than mutated — the list assignment is what triggers the re-render.
    """
    if row.raw_column != raw_column:
        return row
    return ColumnRow(
        raw_column=row.raw_column,
        mapped_field=field,
        confidence=row.confidence,
        reason=row.reason,
        status=row.status,
        required=row.required,
        from_trusted_profile=row.from_trusted_profile,
        is_unmapped_field=row.is_unmapped_field,
    )


def _candidate_columns(candidates) -> list[str]:
    """Normalize a field's candidate columns to plain strings.

    The stored shape varies: the unified layer writes a list of column-name
    strings, while an older report shape wrote ``{"raw_column": ...}`` dicts.
    Both must render without raising.
    """
    out: list[str] = []
    for c in candidates or []:
        if isinstance(c, dict):
            col = c.get("raw_column")
        else:
            col = c
        if col:
            out.append(str(col))
    return out


def _is_blank(v) -> bool:
    """pandas NaN is truthy, so a bare ``or`` leaks "nan" into the preview."""
    try:
        import pandas as pd

        if pd.isna(v):
            return True
    except Exception:  # noqa: BLE001
        pass
    return str(v).strip().lower() in ("", "nan", "none", "nat")


def _canonical_fields_for(source_type: str) -> list[str]:
    """The valid canonical fields for a slot — the dropdown the user picks
    from when overriding an AI-mapped column."""
    try:
        from src.ingestion_ai import normalizer

        return list(normalizer.canonical_fields_for(source_type))
    except Exception:  # noqa: BLE001
        return []


def _rules_from_column_map(column_map: dict) -> list[str]:
    """Render a stored mapping as human-readable rules for the admin's
    "View mapping" dialog — canonical field → source column."""
    out: list[str] = []
    for field, entry in sorted(column_map.items()):
        col = entry.get("source_column") if isinstance(entry, dict) else entry
        out.append(f"{field} ← {col}" if col else f"{field} ← (unmapped)")
    return out