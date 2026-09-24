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
from typing import Any

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.data_paths import VALID_SOURCE_TYPES
from src.ingestion_ai import service as ingestion_ai
from src.ingestion_ai import periods as period_utils
from src.shared import discovery
from setu.state.history import HistoryEntry
from setu.state.shared_upload import SharedUploadState

# The dropdown must speak the accountant's vocabulary, not our folder keys,
# and it must be displayed in a sensible order. The books slot in particular
# is NOT "a Tally export" — Tally is merely one FORMAT the internal books /
# purchase register can arrive in (an ERP export or a hand-prepared register
# are equally valid), so it is labelled for what it IS. The raw key stays
# "tally" (the data/ folder name and every downstream module key off it).
SOURCE_TYPE_LABELS = {
    "tally": "Books / Purchase Register",
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

# Display order for the pickers above — books first (it feeds every recon),
# then the portal sources, then the 2C "Other" sources. Anything a future
# source-type adds but doesn't list here still appears (appended, A–Z).
SOURCE_TYPE_ORDER = [
    "tally",
    "gstr2b",
    "ims",
    "form26as",
    "tds",
    "bank",
    "vendor_ledger",
    "opening_balances",
    "loan_sheet",
    "salary",
]


def _ordered_source_types() -> list[str]:
    """Raw source_type keys in display order: books first (it feeds every
    recon), then the portal sources, then the 2C "Other" sources. Any key a
    future release adds but doesn't list still appears (appended, A–Z)."""
    known = [s for s in SOURCE_TYPE_ORDER if s in VALID_SOURCE_TYPES]
    extra = sorted(s for s in VALID_SOURCE_TYPES if s not in SOURCE_TYPE_ORDER)
    return known + extra


_STATUS_LABELS = {
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

# §7 — display labels. NEVER snake_case in the UI; the reviewer is an
# accountant, not a developer.
_FIELD_LABELS: dict[str, str] = {
    "gstin": "Supplier GSTIN",
    "party_name": "Supplier name",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "taxable_value": "Taxable value",
    "igst": "IGST",
    "cgst": "CGST",
    "sgst": "SGST",
    "cess": "Cess",
    "total_tax": "Total tax",
    "rounding_adjustment": "Rounding adjustment",
    "invoice_value": "Invoice value",
}

# §8 check names in plain language.
_CHECK_LABELS: dict[str, str] = {
    "row_accounting": "Row accounting",
    "required_fields": "Required fields",
    "control_total": "Control total",
    "gstin_validity": "GSTIN structure",
    "date_sanity": "Dates within the period",
    "tax_arithmetic": "Tax arithmetic",
    "mirror_columns": "CGST = SGST mirror",
    "implied_rate": "Implied tax rate",
    "duplicate_detection": "Duplicate invoices",
    "re_upload_guard": "Re-upload guard",
}

# Canonical display order — GST fields first, in reading order.
_CANONICAL_ORDER: list[str] = [
    "gstin", "party_name", "invoice_number", "invoice_date",
    "taxable_value", "igst", "cgst", "sgst", "cess", "total_tax",
    "rounding_adjustment", "invoice_value",
]

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _pretty_date(iso: str) -> str:
    """ISO yyyy-mm-dd -> '01 Aug 2026' (unambiguous, never 01/08/2026)."""
    try:
        y, m, d = str(iso).split("-")
        return f"{int(d):02d} {_MONTHS[int(m) - 1]} {y}"
    except Exception:  # noqa: BLE001
        return str(iso)


def _field_tier(canonical_field: str) -> str:
    """§5 Part 2.2 requiredness tier (C1 owns these in production)."""
    try:
        from src.f6.books_register import field_tier

        return field_tier(canonical_field)
    except Exception:  # noqa: BLE001
        return "optional"


_MONEY_FIELDS = {
    "taxable_value", "igst", "cgst", "sgst", "cess", "total_tax",
    "rounding_adjustment", "invoice_value",
}


def _format_money(value) -> str:
    """Indian digit grouping, 2 dp — ₹1,23,456.78 style (no symbol here)."""
    if value is None:
        return ""
    try:
        from decimal import Decimal

        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return str(value)
    negative = d < 0
    q = abs(d).quantize(Decimal("0.01"))
    whole, _, frac = str(q).partition(".")
    # Indian grouping: last 3 digits, then groups of 2.
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        whole = head + "," + tail
    return ("-" if negative else "") + whole + "." + (frac or "00")


def _format_cell(field: str, value) -> str:
    """One canonical cell, formatted for a reviewer: money with Indian
    grouping, dates unambiguous, nothing showing 'None' or 'nan'."""
    if value is None:
        return ""
    if field in _MONEY_FIELDS:
        return _format_money(value)
    if field == "invoice_date":
        return _pretty_date(str(value)) if str(value) else ""
    s = str(value)
    return "" if s.strip().lower() in ("none", "nan") else s


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class SourceOption:
    """One choice in the upload source-type picker: the human label shown
    and the raw source_type key it maps to (value must stay the raw key —
    the pipeline and the data/ folder both key off it)."""

    key: str
    label: str


@dataclass
class CheckRow:
    """One §8 guardrail outcome, for the "What we checked" card."""

    check: str
    label: str
    result: str          # 'pass' | 'row_flag' | 'hard_stop'
    detail: str
    failed: bool


@dataclass
class RateRow:
    """One head x rate row of the Tax-columns card."""

    head: str
    rate_label: str
    taxable_column: str
    tax_column: str
    setu_field: str
    mirror_of: str
    mirror_note: str


@dataclass
class DispositionRow:
    """One source column's disposition (§5 Part 1.2)."""

    column: str
    disposition: str     # used-in | validation-signal | ignored | needs-decision
    detail: str
    variant: str


@dataclass
class FieldEvidence:
    """A canonical field's verification evidence (§5 Part 4)."""

    canonical_field: str
    label: str
    tier: str            # required | recommended | derived | optional
    source_summary: str
    evidence_chip: str
    status: str          # verified | failed | ai_only | missing
    mapped: bool


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
    # Filed without a period → unreachable by every reconciliation. Drives
    # the "File to period" repair action on the row.
    is_unfiled: bool


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
class SampleValue:
    """A real value read from the file for one canonical field — so the
    reviewer sees actual data (a GSTIN, an amount), not just which column
    was picked. Derived from the raw preview rows; never invented."""

    canonical_field: str
    label: str
    value: str
    raw_column: str
    is_amount: bool


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
    source_type: str = "tally"
    period: str = ""
    upload_error: str = ""
    # The period is REQUIRED: the engine reads
    # data/<client>/<period>/<source_type>/, so a file filed without one is
    # unreachable by every reconciliation. These carry the best-effort
    # suggestion and the evidence behind it, so the field is pre-filled
    # rather than blank — but the human still confirms it.
    period_inferred: str = ""
    period_inferred_source: str = ""

    # "File to period" — the repair action for uploads filed before the
    # period was required (they sit in data/<client>/-/<source_type>/).
    file_period_open: bool = False
    file_period_upload_id: int = 0
    file_period_filename: str = ""
    file_period_value: str = ""
    file_period_suggestion: str = ""
    file_period_source: str = ""
    file_period_error: str = ""

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
    # §6 (D21) — leaving with unsaved edits asks first.
    discard_prompt_open: bool = False

    # --- Corrective build (§5 Parts 2-7): evidence-first review screen ---
    # The identification headline: report title, entity, period, counts.
    review_title: str = ""
    review_entity: str = ""
    review_period_label: str = ""
    # The client the upload belongs to (NOT the list filter — showing the
    # filter here read "Filed under All clients", which is meaningless).
    review_client_label: str = ""
    review_invoice_count: int = 0
    review_supplier_count: int = 0
    review_total_label: str = ""
    # Truthful provenance (§5.3) — never claims AI shaped a deterministic parse.
    review_provenance_label: str = ""
    review_provenance_detail: str = ""
    review_provenance_path: str = ""
    # Period/entity mismatch → an explicit acknowledgement is required.
    mismatch_banner: str = ""
    mismatch_ack: bool = False
    # The summary line: "N verified · M need review · K not in this file".
    summary_verified: int = 0
    summary_review: int = 0
    summary_absent: int = 0
    # §5.6 — the F4 correction/confirmation history for the open upload.
    mapping_history: list[HistoryEntry] = []

    # §8 checks, so "silence is never a pass signal".
    checks: list[CheckRow] = []
    checks_failed: list[CheckRow] = []
    checks_passed: list[CheckRow] = []
    show_passed_checks: bool = False
    # Tax-columns card.
    rate_rows: list[RateRow] = []
    rate_note: str = ""
    # Field evidence (§5 Part 4).
    verified_fields: list[FieldEvidence] = []
    review_fields: list[FieldEvidence] = []
    absent_fields: list[FieldEvidence] = []
    # Ignored / signal columns.
    ignored_columns: list[DispositionRow] = []
    # Normalised preview (what Setu will read) — §7 item 5.
    normalised_headers: list[str] = []
    normalised_rows: list[list[str]] = []
    normalised_total_row: list[str] = []
    normalised_note: str = ""
    show_all_preview: bool = False
    # The confirm gate verdict (§6) — drives the disabled-with-inline-reason.
    gate_blocked: bool = False
    gate_reasons: str = ""
    gate_summary: str = ""
    # Save-as-layout choice (§5.5) — replaces the bare "Trust" checkbox.
    layout_scope: str = "none"   # none | client | firmwide

    # Per-row editing on the mapping table: the raw_column currently being
    # edited ("" = none) and the dropdown draft. Editing one row at a time
    # keeps the table readable — 20 always-open dropdowns is what made the
    # old table overflow and feel unusable.
    editing_column: str = ""
    edit_draft_field: str = ""

    # Re-run through model (per-file, confirm-gated).
    rerun_open: bool = False
    rerun_busy: bool = False
    rerun_target_id: int = 0
    rerun_target_name: str = ""

    # A generic "AI is working" indicator: non-empty while a long model call
    # runs, so the view can show a spinner + what it's doing.
    busy_label: str = ""

    # raw-file preview (first rows of the actual sheet)
    preview_headers: list[str] = []
    preview_rows: list[list[str]] = []
    preview_note: str = ""

    # Mapped values — a real sample value per canonical field, so the
    # reviewer sees actual data (GSTIN, invoice number, amounts) rather than
    # only which column was chosen.
    sample_values: list[SampleValue] = []

    # Non-reactive: the preview frame, used only to derive sample_values on
    # the same pass. Underscore-prefixed so Reflex never serialises it.
    _preview_df: Any = None

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
        """The raw upload keys, in display order (used by any caller that
        needs the underlying value list)."""
        return _ordered_source_types()

    @rx.var
    def source_type_options_by_label(self) -> list[str]:
        """Display LABELS only — used by the source FILTER, which matches
        rows through the label map and so needs no raw keys."""
        return [SOURCE_TYPE_LABELS.get(s, s) for s in _ordered_source_types()]

    @rx.var
    def source_type_choices(self) -> list[SourceOption]:
        """The upload source-type picker's choices: human label + raw key,
        in display order (books first). The control shows the label but
        carries the raw key as its value, so the pipeline and the data/
        folder keep keying off `tally`/`gstr2b`/… unchanged."""
        return [
            SourceOption(key=s, label=SOURCE_TYPE_LABELS.get(s, s))
            for s in _ordered_source_types()
        ]

    @rx.var
    def source_type_label(self) -> str:
        """Label for the currently-selected upload slot, for the in-upload
        context readout."""
        return SOURCE_TYPE_LABELS.get(self.source_type, self.source_type)

    @rx.var
    def source_type_hint(self) -> str:
        """One-line "what does this slot want" text for the selected slot."""
        return discovery.source_type_hint(self.source_type)

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
        return ["All sources"] + self.source_type_options_by_label

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
    def mapped_checklist(self) -> list[FieldRow]:
        """Every canonical field with its mapping status — the checklist the
        review screen renders alongside the column table. One row per
        canonical field, in schema order, so nothing is silently absent."""
        seen: dict[str, FieldRow] = {}
        order: list[str] = []
        for f in list(self.needs_attention) + list(self.confident_fields):
            if f.canonical_field not in seen:
                seen[f.canonical_field] = f
                order.append(f.canonical_field)
        # Canonical fields with no report entry at all still belong on the
        # checklist — EXCEPT residual fields (rounding_adjustment defaults
        # to 0 when absent; a portal export never carries one), which are
        # not a gap and must not read as one.
        residual_fields = {"rounding_adjustment"}
        for opt in self.canonical_field_options:
            if opt not in seen:
                seen[opt] = FieldRow(
                    canonical_field=opt, raw_column="", confidence=0, reason="",
                    required=False, from_trusted_profile=False, candidates=[],
                    status="unmapped", needs_attention=opt not in residual_fields,
                )
                order.append(opt)
        return [seen[k] for k in order]

    @rx.var
    def mapped_checklist_done(self) -> int:
        return sum(1 for f in self.mapped_checklist if not f.needs_attention)

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
                    is_unfiled=period_utils.is_unfiled(u.get("period")),
                )
            )
        self.uploads = self._apply_list_filters(out)

    def _apply_list_filters(self, rows: list[UploadRow]) -> list[UploadRow]:
        out = rows
        if self.filter_client != ALL_CLIENTS:
            out = [r for r in out if r.client_label == self.filter_client]
        if self.filter_source != "All sources":
            # The filter control carries the human LABEL; the rows carry the
            # raw source_type key — match through the label map.
            out = [
                r for r in out
                if SOURCE_TYPE_LABELS.get(r.source_type, r.source_type) == self.filter_source
            ]
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

    def set_source_type(self, key: str):
        """The picker carries the raw source_type key as its value (only the
        label is human-facing), so this stores it directly."""
        if key in VALID_SOURCE_TYPES:
            self.source_type = key

    def set_period(self, v: str):
        self.period = v
        # A human edit supersedes the suggestion — stop claiming it.
        self.period_inferred_source = ""

    # ------------------------------------------------------------------
    # "File to period" — repair an upload filed without a period
    # ------------------------------------------------------------------
    @rx.event
    def open_file_period(self, upload_id: int, filename: str):
        """Open the re-file dialog, pre-filled with the best suggestion."""
        self.file_period_upload_id = upload_id
        self.file_period_filename = filename
        self.file_period_error = ""
        suggested, source = ingestion_ai.suggest_period_for_upload(upload_id)
        self.file_period_suggestion = suggested or ""
        self.file_period_source = source
        self.file_period_value = suggested or ""
        self.file_period_open = True

    @rx.event
    def close_file_period(self):
        self.file_period_open = False
        self.file_period_error = ""

    def set_file_period_value(self, v: str):
        self.file_period_value = v

    @rx.event
    def confirm_file_period(self):
        """Re-file the upload under the chosen period.

        Moves the bytes, updates the document's period, re-keys the stored
        ingestion result and writes an F4 audit entry — all four, because
        doing only some leaves the file half-visible.
        """
        self.file_period_error = ""
        if not period_utils.is_valid_period(self.file_period_value):
            self.file_period_error = "Enter a period as YYYY-MM (for example 2026-08)."
            return
        try:
            out = ingestion_ai.set_upload_period(
                self.file_period_upload_id,
                self.file_period_value.strip(),
                actor=self.username,
                client_ref=self._client_ref(),
            )
        except ingestion_ai.IngestionAIError as exc:
            self.file_period_error = str(exc)
            return
        moved = " and moved on disk" if out.get("moved") else ""
        self.flash = (
            f"'{self.file_period_filename}' is now filed under {out['period']}{moved} — "
            "it can be picked for reconciliation."
        )
        self.file_period_open = False
        self._load_all()

    def set_trust_reuse(self, v: bool):
        self.trust_reuse = v

    def set_layout_scope(self, v: str):
        """§5.5 — the Save-as-layout choice that replaces the bare Trust
        checkbox, with one sentence of consequence shown next to it."""
        self.layout_scope = v
        self.trust_reuse = v in ("client", "firmwide")

    def set_layout_scope_value(self, label: str):
        """The select emits a human label; map it to the internal scope."""
        self.layout_scope = {
            "This client only": "client",
            "Firm-wide": "firmwide",
        }.get(label, "none")
        self.trust_reuse = self.layout_scope in ("client", "firmwide")

    def set_mismatch_ack(self, v: bool):
        self.mismatch_ack = v

    def toggle_passed_checks(self):
        self.show_passed_checks = not self.show_passed_checks

    def toggle_all_preview(self):
        self.show_all_preview = not self.show_all_preview

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
    # Period (required) — suggestion + evidence
    # ------------------------------------------------------------------
    def _infer_period(self) -> None:
        """Pre-fill the required Period field from the staged file's own
        evidence (its stated period, its return period, then its filename).

        Never overwrites a value the human has already typed, and never
        invents one: when there is no signal the field stays empty and the
        upload is blocked until a period is supplied.
        """
        if self.period.strip() or not self.pending:
            return
        first = self.pending[0]
        suggested, source = period_utils.suggest_period(first.filename, None)
        if suggested:
            self.period = suggested
            self.period_inferred = suggested
            self.period_inferred_source = source

    @rx.event
    async def stage(self, files: list[rx.UploadFile]):
        """Stage dropped files, then pre-fill the required Period field from
        the first file's own evidence."""
        await super().stage(files)
        self._infer_period()

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
        # The period is REQUIRED. The engine reads
        # data/<client>/<period>/<source_type>/, so a file filed without one
        # lands in a folder no period picker can select and is permanently
        # invisible to reconciliation. Blocking here is the whole point.
        if not period_utils.is_valid_period(self.period):
            self.upload_error = (
                "A period is required — the file is filed under "
                "data/<client>/<period>/<source_type>/ and must match the period "
                "you reconcile against. Enter one as YYYY-MM (for example 2026-08)."
            )
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
                    period=self.period.strip(),
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
        self.period_inferred = ""
        self.period_inferred_source = ""
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
        self.layout_scope = "none"
        self.mismatch_ack = False
        self.show_passed_checks = False
        self.show_all_preview = False
        self.mapping_error = ""
        self.allow_empty_ack = False
        self._load_review()

    @rx.event
    @rx.event
    def back_to_uploads(self):
        """Leave the review screen. §6 (D21): if there are unsaved edits, the
        first attempt asks for confirmation rather than silently discarding
        them; the second (the dialog's own button) proceeds."""
        if self._has_unsaved_edits() and not self.discard_prompt_open:
            self.discard_prompt_open = True
            return
        self.discard_prompt_open = False
        self.selected_upload_id = 0
        self.confirm_open = False
        self._load_uploads()

    @rx.event
    def confirm_discard_edits(self):
        self.discard_prompt_open = False
        self.selected_upload_id = 0
        self.confirm_open = False
        self._load_uploads()

    @rx.event
    def cancel_discard_edits(self):
        self.discard_prompt_open = False

    def _has_unsaved_edits(self) -> bool:
        """True when the reviewer moved a field WITHOUT confirming — i.e. the
        mapping table no longer matches what was stored."""
        try:
            stored = ingestion_ai.stored_result_for_upload(self.selected_upload_id) or {}
            proposal = {
                m["canonical_field"]: (m.get("source_column") or None)
                for m in (stored.get("mapping") or [])
            }
            if not proposal:
                return False
            for field_name, value in self.field_overrides.items():
                if (proposal.get(field_name) or None) != (value or None):
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    def load_history(self, record_type: str, record_id: str):
        """F4 history rows for a record (mapping edits / confirmations)."""
        try:
            from setu.state.history import load_history as _load

            return _load(record_type, record_id)
        except Exception:  # noqa: BLE001
            return []

    @rx.var
    def mapping_edit_count(self) -> int:
        """How many field corrections / confirmations exist for the open
        upload — shown on the View History trigger."""
        return len(self.mapping_history)

    @rx.var
    def history_record_id(self) -> str:
        try:
            upload = ingestion_ai.get_upload(self.selected_upload_id)
            if upload is None:
                return ""
            return f"{upload['source_type']}:{upload['filename']}"
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # Re-run through model (per-file, confirm-gated)
    # ------------------------------------------------------------------
    @rx.event
    def open_rerun(self, upload_id: int, filename: str):
        """Open the confirm dialog for a per-file model re-run.

        A re-run is a live model call (~15-25s) and discards the cached
        mapping, so it is always confirmed first — never one-click.
        """
        self.rerun_target_id = upload_id
        self.rerun_target_name = filename
        self.rerun_open = True

    @rx.event
    def close_rerun(self):
        self.rerun_open = False
        self.rerun_busy = False

    @rx.event
    async def confirm_rerun(self):
        """Re-run the mapping through the model, yielding progress.

        A live model call takes ~15-25s and BLOCKS — a plain sync handler
        can never repaint a spinner mid-call (its `busy=True` → work →
        `busy=False` all happen inside one server round-trip). As an async
        generator, each `yield` flushes state to the browser, so the spinner
        and status text genuinely show while the model runs.
        """
        if not self.rerun_target_id:
            self.rerun_open = False
            return
        self.rerun_busy = True
        self.busy_label = f"Re-running {self.rerun_target_name} through the model…"
        self.error = ""
        yield
        try:
            out = ingestion_ai.re_run_mapping(
                self.rerun_target_id, actor=self.username, client_ref=self._client_ref(),
            )
            self.flash = (
                f"Re-ran {self.rerun_target_name} through the model — "
                f"{out['row_count_out']} row(s) mapped."
            )
        except ingestion_ai.IngestionAIError as exc:
            self.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            self.error = f"Re-run failed for {self.rerun_target_name}: {exc}"
        finally:
            self.rerun_busy = False
            self.busy_label = ""
            self.rerun_open = False
            if self.selected_upload_id == self.rerun_target_id:
                self._load_review()
            else:
                self._load_uploads()
        yield

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
        self.review_period = upload.get("period") or ""

        threshold = ingestion_ai.get_preselect_threshold()
        fields = report["field_mapping"] if report else []
        self.review_c5_used = bool(report.get("c5_context_used")) if report else False

        # --- Corrective build: evidence-first model -----------------------
        self._load_review_headline(upload, report)
        self._load_review_checks(upload)
        self._load_review_matrix(upload)
        self._load_review_evidence(upload, report)
        self._load_review_gate(upload)
        # §5.6 — the correction/confirmation audit trail (F4, append-only).
        try:
            self.mapping_history = self.load_history(
                "ingestion_mapping", f"{upload['source_type']}:{upload['filename']}"
            )
        except Exception:  # noqa: BLE001
            self.mapping_history = []
        self._load_normalised_preview(upload)

        needs: list[FieldRow] = []
        confident: list[FieldRow] = []
        for f in fields:
            # A mapped field with NO confidence is RULE-sourced (the
            # deterministic parser, or a trusted profile) — not a
            # low-confidence guess. Only an explicit numeric confidence below
            # the threshold counts as needing attention.
            conf = f.get("confidence")
            is_needs = (
                not f.get("raw_column")
                or (conf is not None and (conf or 0) / 100.0 < threshold)
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

        # Real sample values per mapped field — the reviewer sees actual data.
        self._build_sample_values(fields)

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

    # ------------------------------------------------------------------
    # Corrective build (§5 Parts 2-7): evidence-first review helpers
    # ------------------------------------------------------------------
    def _load_review_headline(self, upload: dict, report: Optional[dict]) -> None:
        """§7 item 1 — the identification headline: what this file IS."""
        stored = ingestion_ai.stored_result_for_upload(self.selected_upload_id) or {}
        meta = stored.get("metadata") or {}
        self.review_title = meta.get("report_title") or self.review_source_label
        self.review_entity = meta.get("entity_name") or ""
        # The upload's OWN client — never the list filter.
        self.review_client_label = self._client_label(upload.get("client_id"))
        period_start, period_end = meta.get("period_start"), meta.get("period_end")
        if period_start and period_end:
            self.review_period_label = f"{_pretty_date(period_start)} – {_pretty_date(period_end)}"
        else:
            self.review_period_label = self.review_period

        rows = stored.get("mapping") or []
        self.review_invoice_count = int(stored.get("row_count_out") or 0)

        prov = ingestion_ai.provenance(self.selected_upload_id)
        self.review_provenance_label = prov.get("label") or ""
        self.review_provenance_detail = prov.get("detail") or ""
        self.review_provenance_path = prov.get("path") or ""

        # Period / entity mismatch → an explicit acknowledgement is required
        # (§5 Part 3.1). Name matching alone must never hard-block: the file
        # carries no buyer GSTIN, so this can only ever be a confirm.
        msgs: list[str] = []
        filed = self.review_period or ""
        if filed and period_start and period_end and not period_start.startswith(filed):
            msgs.append(
                f"The file covers {_pretty_date(period_start)} to {_pretty_date(period_end)}, "
                f"but it is filed under {filed}."
            )
        self.mismatch_banner = " ".join(msgs)
        self.mismatch_ack = not msgs

    def _load_review_checks(self, upload: dict) -> None:
        """§7 item 3 — every §8 check, failures first. Silence is never a pass."""
        stored = ingestion_ai.stored_result_for_upload(self.selected_upload_id) or {}
        validation = stored.get("validation") or []
        rows: list[CheckRow] = []
        for c in validation:
            name = c.get("check") or ""
            rows.append(CheckRow(
                check=name,
                label=_CHECK_LABELS.get(name, name.replace("_", " ").title()),
                result=c.get("result") or "",
                detail=c.get("detail") or "",
                failed=c.get("result") in ("hard_stop", "row_flag"),
            ))
        self.checks = rows
        self.checks_failed = [r for r in rows if r.failed]
        self.checks_passed = [r for r in rows if not r.failed]

    def _load_review_matrix(self, upload: dict) -> None:
        """§7 item 4 — the head x rate matrix, so the grouping is visible."""
        stored = ingestion_ai.stored_result_for_upload(self.selected_upload_id) or {}
        matrix = stored.get("rate_matrix") or []
        self.rate_rows = [
            RateRow(
                head=m.get("head") or "",
                rate_label=f"{m.get('rate'):g}%" if m.get("rate") is not None else "",
                taxable_column=m.get("taxable_column") or "—",
                tax_column=m.get("tax_column") or "—",
                setu_field=m.get("canonical_field") or "",
                mirror_of=m.get("mirror_of") or "",
                mirror_note=(
                    f"Mirror of {m['mirror_of']} — not added again"
                    if m.get("mirror_of") else ""
                ),
            )
            for m in matrix
        ]
        self.rate_note = (
            "Each tax head is split across rate buckets, so Setu sums the buckets. "
            "CGST and SGST restate the same taxable base, so only one side is added — "
            "summing both would double-count it."
            if self.rate_rows else ""
        )
        # Column dispositions (§5 Part 1.2) — signals + ignored.
        self.ignored_columns = [
            DispositionRow(
                column=d.get("column") or "",
                disposition=d.get("disposition") or "",
                detail=d.get("detail") or "",
                variant=(
                    "ai" if d.get("disposition") == "validation-signal"
                    else "placeholder"
                ),
            )
            for d in (stored.get("dispositions") or [])
            if d.get("disposition") in ("ignored", "validation-signal")
        ]

    def _load_review_evidence(self, upload: dict, report: Optional[dict]) -> None:
        """§5 Part 4 — evidence chips, grouped Verified / Needs review / Not in this file."""
        evidence = ingestion_ai.field_evidence(self.selected_upload_id)
        stored = ingestion_ai.stored_result_for_upload(self.selected_upload_id) or {}
        mapping = {m["canonical_field"]: m for m in (stored.get("mapping") or [])}
        if report:
            for m in report.get("field_mapping") or []:
                mapping.setdefault(m["canonical_field"], m)

        verified: list[FieldEvidence] = []
        review: list[FieldEvidence] = []
        absent: list[FieldEvidence] = []
        for f, m in mapping.items():
            ev = evidence.get(f) or {}
            # The STORED mapping uses `source_column`; the report's
            # field_mapping uses `raw_column`. Accept either — a mismatch here
            # silently renders every field as "Not in this file".
            source_col = m.get("source_column") or m.get("raw_column") or ""
            mapped = bool(source_col)
            tier = _field_tier(f)
            if not mapped:
                status = "missing"
            else:
                status = ev.get("status") or "ai_only"
            row = FieldEvidence(
                canonical_field=f,
                label=_FIELD_LABELS.get(f, f.replace("_", " ").title()),
                tier=tier,
                source_summary=source_col,
                evidence_chip=ev.get("chip") or (
                    "Not present in this file" if not mapped else "AI only"
                ),
                status=status,
                mapped=mapped,
            )
            if not mapped:
                absent.append(row)
            elif status == "failed":
                review.append(row)
            elif status == "verified":
                verified.append(row)
            else:
                review.append(row)

        order = {f: i for i, f in enumerate(_CANONICAL_ORDER)}
        key = lambda r: order.get(r.canonical_field, 999)
        self.verified_fields = sorted(verified, key=key)
        self.review_fields = sorted(review, key=key)
        self.absent_fields = sorted(absent, key=key)
        self.summary_verified = len(self.verified_fields)
        self.summary_review = len(self.review_fields)
        self.summary_absent = len(self.absent_fields)

    def _load_review_gate(self, upload: dict) -> None:
        """§6 — the confirm gate verdict, so the button and the pipeline agree."""
        gate = ingestion_ai.confirm_gate(self.selected_upload_id)
        self.gate_blocked = bool(gate.get("blocked"))
        reasons = gate.get("reasons") or []
        self.gate_reasons = " · ".join(reasons)
        counts = gate.get("counts") or {}
        parts = []
        if counts.get("rows_out"):
            parts.append(f"{counts['rows_out']} invoices")
        if self.review_total_label:
            parts.append(self.review_total_label)
        parts.append(
            "0 blocking issues" if not reasons
            else f"{len(reasons)} blocking issue(s)"
        )
        self.gate_summary = " · ".join(parts)

    def _load_normalised_preview(self, upload: dict) -> None:
        """§7 item 5 — a preview of what SETU WILL READ, not the raw file.

        This is the reviewer's real check: the canonical rows the pipeline
        produced, with a totals row set against the file's own Total row.
        A mapping that looks plausible column-by-column can be judged in one
        glance here.
        """
        self.normalised_headers = []
        self.normalised_rows = []
        self.normalised_total_row = []
        self.normalised_note = ""
        try:
            stored = ingestion_ai.stored_result_for_upload(self.selected_upload_id) or {}
            mapping = stored.get("mapping") or []
            if not mapping:
                self.normalised_note = "No normalised rows available for this upload yet."
                return
            df = ingestion_ai.canonical_frame_for_upload(self.selected_upload_id)
            if df is None or df.empty:
                self.normalised_note = "No normalised rows available for this upload yet."
                return

            display = [f for f in _CANONICAL_ORDER if f in df.columns]
            self.normalised_headers = [_FIELD_LABELS.get(f, f) for f in display]
            limit = len(df) if self.show_all_preview else 20
            rows: list[list[str]] = []
            for _, r in df.head(limit).iterrows():
                rows.append([_format_cell(f, r.get(f)) for f in display])
            self.normalised_rows = rows

            # Totals row — sums the money columns, so it can be set against
            # the file's own Total row (already verified by control_total).
            totals: list[str] = []
            for f in display:
                if f in _MONEY_FIELDS:
                    try:
                        from decimal import Decimal

                        vals = [v for v in df[f].tolist() if v is not None]
                        s = sum(vals, Decimal("0.00"))
                        totals.append(_format_money(s))
                    except Exception:  # noqa: BLE001
                        totals.append("")
                elif f == "invoice_number":
                    totals.append(f"Total ({len(df)} invoices)")
                else:
                    totals.append("")
            self.normalised_total_row = totals

            # Supplier count from the normalised frame.
            if "gstin" in df.columns:
                self.review_supplier_count = int(df["gstin"].nunique(dropna=True))
            if "invoice_value" in df.columns:
                try:
                    from decimal import Decimal

                    vals = [v for v in df["invoice_value"].tolist() if v is not None]
                    total = sum(vals, Decimal("0.00"))
                    self.review_total_label = "₹" + _format_money(total)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            self.normalised_note = f"Normalised preview unavailable ({exc})."

    def _load_preview(self, upload: dict) -> None:
        """First several rows of the ACTUAL sheet, so the mapping can be
        sanity-checked against real values rather than column headers alone.
        Read once per open upload — never on every rerun."""
        self.preview_headers = []
        self.preview_rows = []
        self.preview_note = ""
        self._preview_df = None
        try:
            from src.documents import service as documents
            from src.ingestion_ai import normalizer

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
                # Use the SAME reader the ingestion pipeline used, so the
                # preview shows the sheet that was actually ingested and its
                # columns carry the identical flattened 'parent :: child'
                # labels the mapping refers to. Reading with the plain
                # single-row detector picked the wrong sheet on GSTR-2B/IMS
                # exports and its headers could never match the mapped
                # raw_column labels — which left the Mapped-values card and
                # the raw preview blank.
                raw = normalizer.read_raw_with_header_detection(path)
                df = raw.df
            finally:
                path.unlink(missing_ok=True)
            self._preview_df = df
            self.preview_headers = [str(c) for c in df.columns]
            rows: list[list[str]] = []
            for _, r in df.head(8).iterrows():
                rows.append(["" if _is_blank(v) else str(v) for v in r])
            self.preview_rows = rows
            if df.empty:
                self.preview_note = "The sheet has no data rows."
        except Exception as exc:  # noqa: BLE001
            self.preview_note = f"Source preview unavailable ({exc})."

    def _build_sample_values(self, fields: list[dict]) -> None:
        """Pull a REAL first non-blank value for each mapped canonical field
        from the preview frame — so the reviewer sees actual data (a GSTIN,
        an invoice number, rupee amounts), not just the column name. Values
        are read, never invented; a field with no value shows as blank."""
        self.sample_values = []
        df = self._preview_df
        if df is None or df.empty:
            return
        amount_fields = {
            "taxable_value", "cgst", "sgst", "igst", "cess", "total_tax",
            "invoice_value", "amount_paid_credited", "tax_deducted",
            "tax_deposited", "amount", "rounding_adjustment",
        }
        priority = [
            "gstin", "pan", "party_name", "deductee_name", "invoice_number",
            "invoice_date", "taxable_value", "invoice_value", "total_tax",
            "cgst", "sgst", "igst", "cess", "section", "reference", "date", "amount",
        ]
        by_field = {f["canonical_field"]: f for f in fields}
        ordered = [f for f in priority if f in by_field] + [
            f["canonical_field"] for f in fields if f["canonical_field"] not in priority
        ]
        seen: set[str] = set()
        out: list[SampleValue] = []
        for cf in ordered:
            if cf in seen:
                continue
            seen.add(cf)
            f = by_field[cf]
            col = f.get("raw_column")
            if not col or col not in df.columns:
                continue
            value = ""
            for v in df[col].tolist():
                if not _is_blank(v):
                    value = str(v).strip()
                    break
            out.append(SampleValue(
                canonical_field=cf,
                label=cf.replace("_", " ").title(),
                value=value,
                raw_column=str(col),
                is_amount=cf in amount_fields,
            ))
        self.sample_values = out

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

    @rx.event
    def start_edit_column(self, raw_column: str):
        """Open the inline editor for one row of the mapping table."""
        self.editing_column = raw_column
        current = self.column_overrides.get(raw_column, "")
        self.edit_draft_field = current if current else LEAVE_UNMAPPED

    @rx.event
    def cancel_edit_column(self):
        self.editing_column = ""
        self.edit_draft_field = ""

    @rx.event
    def set_edit_draft_field(self, v: str):
        self.edit_draft_field = v

    @rx.event
    def save_edit_column(self):
        """Commit the row edit and close the inline editor."""
        if not self.editing_column:
            self.editing_column = ""
            return
        self.set_column_mapping(self.editing_column, self.edit_draft_field)
        self.editing_column = ""
        self.edit_draft_field = ""

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
    async def confirm_mapping(self):
        self.error = ""
        self.mapping_error = ""
        self._recompute_field_overrides()
        if self.mapping_error:
            self.confirm_open = False
            return
        # §6 — refuse at the UI layer too, with the SAME reasons the service
        # will use, so the button and the pipeline can never disagree.
        if self.gate_blocked:
            self.mapping_error = "Confirm is blocked — " + self.gate_reasons
            self.confirm_open = False
            return
        # A period/entity mismatch must be explicitly acknowledged (§5.3.1).
        if self.mismatch_banner and not self.mismatch_ack:
            self.mapping_error = (
                "Acknowledge the period/client mismatch before confirming."
            )
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
        self.confirm_open = False
        self.busy_label = "Confirming the mapping and filing the source…"
        yield
        try:
            ingestion_ai.confirm_mapping(
                self.selected_upload_id,
                field_overrides=override_map,
                trust_for_reuse=self.trust_reuse,
                layout_scope=self.layout_scope,
                actor=self.username,
                client_ref=self._client_ref(),
                allow_empty=self.allow_empty_ack,
            )
            self.flash = (
                "Mapping confirmed — the file is filed to the client's data folder and "
                "can now be picked for reconciliation."
            )
            self.selected_upload_id = 0
        except ingestion_ai.IngestionAIError as exc:
            self.error = str(exc)
        finally:
            self.busy_label = ""
        self._load_all()
        yield

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