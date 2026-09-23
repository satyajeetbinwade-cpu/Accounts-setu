"""Universal AI ingestion layer — the ONE normalization path every upload
in the app goes through.

`normalize_source_file()` sits between every upload and the reconciliation
engine. There is deliberately no code path where a raw uploaded file
reaches the engine without passing through it first (see
src/runner.py's `_canonical_frames`).

What it does, in order:

  1. STRUCTURAL READ — detect the real header row (or its absence) by
     sampling the first ~20 rows, handle title/metadata rows above the
     header, and pick the tabular sheet out of a multi-sheet workbook
     (reporting when that choice was ambiguous).

  2. CLASSIFICATION PRE-CHECK (Prompt 3) — ask the model what kind of
     document this looks like, independent of the slot the user uploaded
     it to, and compare against the declared source_type. A structurally
     valid file in the WRONG slot is stopped here, before any field
     mapping is attempted. Wrong-slot data producing a "successful"
     reconciliation is worse than a blocked upload.

  3. FIELD MAPPING (Prompt 1) — send the detected headers plus sample
     rows to the model and ask it to map each source column onto our
     canonical schema for that source_type, returning per field: the
     source column (or null), a confidence, and a one-line reason. Never
     maps silently — every mapping carries its explanation.

  4. APPLY + COERCE — apply the mapping, coerce dates/currency, and
     produce a canonical DataFrame.

  5. RETURN an IngestionResult. This function NEVER throws on missing or
     unmatched columns: missing fields land in `unmapped_required_fields`
     and the caller decides whether to block or proceed. Its job is to
     extract everything it confidently can, never to fail all-or-nothing.

Caching: the model call is cached by a fingerprint of
(client, source_type, header-set) — see `_header_signature()` and the
`ingestion_shape_cache` table. A previously-seen file shape does not
re-call the model. A shape a human confirmed AND trusted skips both the
model call and the manual step entirely.

Model tier: this layer feeds the reconciliation engine directly, so it
runs at the engine's own tier (Opus-tier) rather than a cheaper model — a
wrong column mapping here silently corrupts every downstream match. See
src/ingestion_ai/llm.py.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.ingestion import (
    GST_CANONICAL_FIELDS,
    GST_DATE_FIELDS,
    GST_NUMERIC_FIELDS,
    GST_UPPER_FIELDS,
    OTHER_CANONICAL_FIELDS,
    TDS_CANONICAL_FIELDS,
    TDS_DATE_FIELDS,
    TDS_NUMERIC_FIELDS,
    TDS_UPPER_FIELDS,
    _coerce_numeric,
    _normalize_dates_flexible,
)
from src.ingestion_ai import llm
from src.ingestion_ai import f6_bridge

# ---------------------------------------------------------------------------
# Canonical schemas per source_type
# ---------------------------------------------------------------------------

# Bookkeeping fields ingestion adds itself — never "mapped from a raw column".
_STRUCTURAL_FIELDS = {"source_type", "source_file", "original_row", "total_tax"}

# rounding_adjustment defaults to 0.0 (not None) when unmapped — it's a
# residual, not a required identity/amount field, so an absent column just
# means "this source doesn't carry one", never a caveat.
_ZERO_DEFAULT_FIELDS = {"rounding_adjustment"}

# The canonical field set the model is asked to map onto, per source_type.
# tally_purchase_register / gstr2b / ims all share the GST canonical schema
# (IMS exports observed in practice use the GSTR-2B layout plus IMS-specific
# action columns, which aren't part of the canonical schema).
_GST_SOURCES = {"tally", "tally_purchase_register", "gstr2b", "ims"}
_TDS_SOURCES = {"form26as", "tds"}
_OTHER_SOURCES = {"bank", "vendor_ledger", "opening_balances", "loan_sheet", "salary"}

# Fields the matcher genuinely cannot work without. Anything here that ends
# up unmapped puts the result in `unmapped_required_fields` and blocks the
# run; everything else is a warning at most.
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "gst": ["gstin", "party_name", "invoice_number", "invoice_date", "taxable_value", "invoice_value"],
    "tds": ["pan", "deductee_name", "section", "amount_paid_credited", "tax_deducted"],
    "other": ["reference", "date", "amount"],
}

# Human-readable labels for the classification candidates (Prompt 3).
# These describe what the FILE looks like, not which slot it belongs in —
# several of them can legitimately arrive in the same slot. "Books / purchase
# register" in particular covers an internal books export from any accounting
# system, with Tally being just one of the formats it can come in.
CLASSIFICATION_CANDIDATES = [
    "Books / purchase register",
    "GSTR-2B",
    "GSTR-1",
    "IMS export",
    "Form 26AS",
    "TDS return/challan",
    "bank statement",
    "trial balance",
    "vendor ledger",
    "opening balances",
    "loan sheet",
    "salary register",
    "something else / not a financial ledger at all",
]

# Which classification(s) are acceptable for each declared upload slot.
# A file classified as anything outside its slot's accepted set is a
# wrong-slot upload and is stopped before field mapping.
#
# The books slot accepts anything that is genuinely an internal record of
# transactions: a purchase register, a trial balance, or a vendor ledger. A
# sales register is accepted too — the matcher reconciles on GSTIN + invoice
# number + amount, which is direction-agnostic, and the books file is often a
# combined purchase-and-sales export.
_ACCEPTED_CLASSIFICATIONS: dict[str, set[str]] = {
    "tally": {"Books / purchase register", "trial balance", "vendor ledger"},
    "tally_purchase_register": {"Books / purchase register", "trial balance", "vendor ledger"},
    "gstr2b": {"GSTR-2B"},
    "ims": {"IMS export"},
    "form26as": {"Form 26AS"},
    "tds": {"TDS return/challan"},
    "bank": {"bank statement"},
    "vendor_ledger": {"vendor ledger", "Books / purchase register"},
    "opening_balances": {"opening balances", "trial balance"},
    "loan_sheet": {"loan sheet"},
    "salary": {"salary register"},
}

# Plain-language slot names for the wrong-slot message.
_SLOT_LABELS = {
    "tally": "Books / Purchase Register",
    "tally_purchase_register": "Books / Purchase Register",
    "gstr2b": "GSTR-2B",
    "ims": "IMS",
    "form26as": "Form 26AS",
    "tds": "TDS Return / Challan",
    "bank": "Bank Statement",
    "vendor_ledger": "Vendor Ledger",
    "opening_balances": "Opening Balances",
    "loan_sheet": "Loan Sheet",
    "salary": "Salary Register",
}

# Where a misclassified file most likely belongs, for the "did you mean"
# half of the wrong-slot message.
_SUGGESTED_SLOT = {
    "GSTR-2B": "gstr2b",
    "GSTR-1": None,  # no GSTR-1 slot exists in this app
    "IMS export": "ims",
    "Form 26AS": "form26as",
    "TDS return/challan": "tds",
    "Books / purchase register": "tally",
    "trial balance": "tally",
    "bank statement": "bank",
    "vendor ledger": "vendor_ledger",
    "opening balances": "opening_balances",
    "loan sheet": "loan_sheet",
    "salary register": "salary",
}

# Statuses an IngestionResult can carry.
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_BLOCKED = "blocked"
STATUS_WRONG_SLOT = "wrong_slot"
STATUS_UNRECOGNIZED = "unrecognized"
# A file that is structurally readable but carries no data rows (a
# header-only sheet, or a sheet whose every row was blank/subtotal). This is
# NOT a failure and NOT a confident mapping — it is its own outcome, so the
# review screen can never present an empty read as "every field mapped
# confidently", and confirm_mapping() can refuse it unless the reviewer
# explicitly acknowledges the file is deliberately empty.
STATUS_EMPTY = "empty"

# Default confidence above which a mapped field is pre-selected for the user
# (Prompt 2). Overridable from the frontend — see get_preselect_threshold().
DEFAULT_PRESELECT_THRESHOLD = 0.75
KEY_PRESELECT_THRESHOLD = "ingestion_ai.preselect_threshold"


class IngestionError(RuntimeError):
    """Raised only for genuinely unusable input (unreadable file, unknown
    source_type). Schema mismatches are NEVER raised — they are reported
    through IngestionResult."""


# ---------------------------------------------------------------------------
# Capabilities — what an unmapped field costs the report
# ---------------------------------------------------------------------------
#
# A missing column isn't interesting to an accountant; not being able to run a
# particular CHECK is. These map canonical fields onto the reconciliation
# capabilities they enable, so a caveat can be stated as "we couldn't check
# X" instead of "column gstin is unmapped".
_CAPABILITY_FIELDS: dict[str, tuple[str, ...]] = {
    "match_by_gstin": ("gstin",),
    "match_by_pan": ("pan",),
    "match_by_invoice": ("invoice_number", "voucher_number", "challan_number"),
    "match_by_date": ("invoice_date", "deposit_date"),
    "check_taxable_value": ("taxable_value",),
    "check_tax_amounts": ("cgst", "sgst", "igst", "cess", "total_tax"),
    "check_rounding": ("rounding_adjustment",),
    "check_invoice_total": ("invoice_value",),
    "check_tds_amounts": ("amount_paid_credited", "tax_deducted", "tax_deposited"),
    "check_section": ("section",),
    "check_party_identity": ("party_name", "deductee_name"),
    "check_reference": ("reference",),
    "check_amount": ("amount",),
}

_CAPABILITY_LABELS: dict[str, str] = {
    "match_by_gstin": "Matching by GSTIN",
    "match_by_pan": "Matching by PAN",
    "match_by_invoice": "Matching by invoice / voucher number",
    "match_by_date": "Date-based matching and timing checks",
    "check_taxable_value": "Taxable-value comparison",
    "check_tax_amounts": "Tax-component comparison (CGST/SGST/IGST/Cess)",
    "check_invoice_total": "Invoice-total comparison",
    "check_tds_amounts": "TDS amount comparison",
    "check_section": "TDS section validation",
    "check_party_identity": "Party-name comparison",
    "check_reference": "Reference matching",
    "check_amount": "Amount comparison",
    "check_rounding": "Rounding-residual tracking",
}

# Which canonical fields a recon type actually works on. The books slot
# carries the UNION of the GST and TDS schemas (one file can feed either
# run), so without this filter a GST run would report "TDS section
# validation not available" — a check it was never going to perform, which
# would bury the gaps that matter. Derived from the real schemas rather than
# a hand-maintained list, so it can't drift from them.
def _fields_for_recon(recon_type: Optional[str]) -> Optional[set[str]]:
    if not recon_type:
        return None
    try:
        from src.ingestion import (
            GST_CANONICAL_FIELDS,
            OTHER_CANONICAL_FIELDS,
            TDS_CANONICAL_FIELDS,
        )
    except Exception:  # noqa: BLE001
        return None
    mapping = {
        "GST": GST_CANONICAL_FIELDS,
        "TDS": TDS_CANONICAL_FIELDS,
        "OTHER": OTHER_CANONICAL_FIELDS,
    }
    fields = mapping.get(str(recon_type).upper())
    return set(fields) if fields else None

# Which matching key each capability affects, for the plain-language reason.
_CAPABILITY_REASON: dict[str, str] = {
    "match_by_gstin": "records are matched on invoice number and amount instead, "
                      "so a GSTIN-only mismatch will not be detected",
    "match_by_pan": "deductee records are matched on name and amount instead, "
                    "so a PAN-only mismatch will not be detected",
    "match_by_invoice": "records are matched on GSTIN/PAN and amount instead, so "
                        "a wrong invoice number will not be detected on its own",
    "match_by_date": "timing differences cannot be separated from genuine gaps",
}


def _capability_detail(capability: str, missing: list[str]) -> str:
    label = _CAPABILITY_LABELS.get(capability, capability)
    reason = _CAPABILITY_REASON.get(capability)
    fields = ", ".join(missing)
    if reason:
        return f"{label} — not available ({fields} not mapped); {reason}."
    return f"{label} — not available ({fields} not mapped), so this check is skipped."


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------


@dataclass
class FieldMapping:
    """One canonical field's mapping decision, with its explanation."""

    canonical_field: str
    source_column: Optional[str]
    confidence: Optional[float]  # 0.0-1.0; None = no candidate at all
    reason: str
    required: bool = False
    from_cache: bool = False
    from_trusted_profile: bool = False

    @property
    def mapped(self) -> bool:
        return bool(self.source_column)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_field": self.canonical_field,
            "source_column": self.source_column,
            "confidence": self.confidence,
            "reason": self.reason,
            "required": self.required,
            "from_cache": self.from_cache,
            "from_trusted_profile": self.from_trusted_profile,
        }


@dataclass
class IngestionResult:
    """What `normalize_source_file()` returns.

    `canonical_df` may be partial — callers must consult `status` and
    `unmapped_required_fields` before handing it to the engine.
    """

    source_type: str
    filename: str
    client: str
    period: Optional[str]
    status: str
    canonical_df: pd.DataFrame
    field_mappings: list[FieldMapping] = field(default_factory=list)
    unmapped_required_fields: list[str] = field(default_factory=list)
    row_count_in: int = 0
    row_count_out: int = 0
    warnings: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    header_row: Optional[int] = None
    sheet_name: Optional[str] = None
    sheet_ambiguous: bool = False
    classification: Optional[dict[str, Any]] = None
    model_used: Optional[str] = None
    llm_cached: bool = False
    header_signature: str = ""
    message: Optional[str] = None
    # What this file's mapping could NOT support. A caveat does NOT block the
    # run: it produces a partial report, and the report states plainly what
    # couldn't be checked. Distinct from `warnings` (data-quality problems in
    # rows that WERE mapped). Shape: {code, label, detail, fields}.
    caveats: list[dict[str, Any]] = field(default_factory=list)
    # Informational notes (cache reuse, multi-sheet choice) that explain HOW
    # the result was produced but are not data-quality problems. Kept
    # separate from `warnings` so "a mapping was reused from cache" doesn't
    # make an otherwise-perfect file look partial.
    notes: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """True when the canonical frame may be handed to the engine."""
        return self.status in (STATUS_OK, STATUS_PARTIAL) and not self.unmapped_required_fields

    @property
    def mapped_fields(self) -> list[FieldMapping]:
        return [m for m in self.field_mappings if m.mapped]

    @property
    def unmapped_fields(self) -> list[FieldMapping]:
        return [m for m in self.field_mappings if not m.mapped]

    def summary_line(self) -> str:
        """One-line confirmation for the Run screen (Prompt 4)."""
        label = _SLOT_LABELS.get(self.source_type, self.source_type)
        if self.status == STATUS_OK:
            return f"{label} — {self.row_count_out} rows extracted, all fields mapped"
        if self.status == STATUS_PARTIAL:
            n = len(self.unmapped_fields)
            if n:
                return f"{label} — {self.row_count_out} rows extracted, {n} field(s) unmapped"
            return f"{label} — {self.row_count_out} rows extracted"
        if self.status == STATUS_BLOCKED:
            n = len(self.unmapped_required_fields)
            return f"{label} — blocked, {n} required field(s) need mapping"
        return f"{label} — {self.message or self.status}"

    def build_caveats(self, recon_type: Optional[str] = None) -> list[dict[str, Any]]:
        """What this file's mapping cannot support for THIS recon type.

        A caveat is NOT a failure. It is a stated limitation: the run proceeds
        and produces a partial report, and the report says exactly which check
        it could not perform and why. Built from the fields that ended up
        unmapped, grouped by the capability they enable so the report reads in
        plain language rather than listing raw column names.

        `recon_type` scopes the result to the checks that recon actually runs.
        The books slot carries the union of the GST and TDS schemas, so without
        it a GST run would report TDS-only gaps (and vice versa).
        """
        unmapped = {m.canonical_field for m in self.field_mappings if not m.mapped}
        # A field the caller never asked about (not part of this source's
        # canonical set) shouldn't be reported as a gap.
        requested = {m.canonical_field for m in self.field_mappings}
        unmapped &= requested

        applicable = _fields_for_recon(recon_type)

        caveats: list[dict[str, Any]] = []
        for capability, fields in _CAPABILITY_FIELDS.items():
            # Only report gaps in fields this recon type actually consumes.
            if applicable is not None and not (set(fields) & applicable):
                continue
            missing = sorted(f for f in fields if f in unmapped and (applicable is None or f in applicable))
            if not missing:
                continue
            caveats.append({
                "code": capability,
                "label": _CAPABILITY_LABELS[capability],
                "detail": _capability_detail(capability, missing),
                "fields": missing,
                "source_type": self.source_type,
                "filename": self.filename,
            })
        return caveats

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "filename": self.filename,
            "client": self.client,
            "period": self.period,
            "status": self.status,
            "field_mappings": [m.to_dict() for m in self.field_mappings],
            "unmapped_required_fields": self.unmapped_required_fields,
            "row_count_in": self.row_count_in,
            "row_count_out": self.row_count_out,
            "warnings": self.warnings,
            "headers": self.headers,
            "header_row": self.header_row,
            "sheet_name": self.sheet_name,
            "sheet_ambiguous": self.sheet_ambiguous,
            "classification": self.classification,
            "model_used": self.model_used,
            "llm_cached": self.llm_cached,
            "header_signature": self.header_signature,
            "message": self.message,
            "notes": self.notes,
            "caveats": self.caveats,
        }


# ---------------------------------------------------------------------------
# Canonical schema helpers
# ---------------------------------------------------------------------------


def _schema_family(source_type: str, recon_type: Optional[str] = None) -> str:
    """Which canonical schema family a slot uses.

    'tally' is special: the same books export feeds BOTH GST and TDS recon
    (see src/ingestion.py's `_resolve_recon_type`), so it maps the UNION of
    both schemas. When the caller knows which recon the file is for, pass
    `recon_type` to narrow the required-field set.
    """
    if source_type in ("tally", "tally_purchase_register"):
        if recon_type in ("GST", "TDS"):
            return recon_type.lower()
        return "tally"
    if source_type in _GST_SOURCES:
        return "gst"
    if source_type in _TDS_SOURCES:
        return "tds"
    if source_type in _OTHER_SOURCES:
        return "other"
    raise IngestionError(f"Unknown source_type {source_type!r} — no canonical schema defined.")


def canonical_fields_for(source_type: str) -> list[str]:
    """The canonical fields the model is asked to map onto for this slot."""
    family = _schema_family(source_type)
    if family == "gst":
        return [f for f in GST_CANONICAL_FIELDS if f not in _STRUCTURAL_FIELDS]
    if family == "tds":
        return [f for f in TDS_CANONICAL_FIELDS if f not in _STRUCTURAL_FIELDS]
    if family == "tally":
        # Union, GST fields first — a Tally books export may carry either or
        # both sets of columns.
        gst = [f for f in GST_CANONICAL_FIELDS if f not in _STRUCTURAL_FIELDS]
        tds = [f for f in TDS_CANONICAL_FIELDS if f not in _STRUCTURAL_FIELDS]
        return gst + [f for f in tds if f not in gst]
    return [f for f in OTHER_CANONICAL_FIELDS if f not in _STRUCTURAL_FIELDS]


def required_fields_for(source_type: str, recon_type: Optional[str] = None) -> list[str]:
    """Fields the matcher genuinely cannot work without.

    For 'tally' the answer depends on which recon the file will feed, so
    when `recon_type` is unknown nothing is treated as required at upload
    time — the Run screen, which knows the recon type, applies the gate.
    """
    family = _schema_family(source_type, recon_type)
    if family == "tally":
        return []
    return list(_REQUIRED_FIELDS[family])


def _numeric_fields(source_type: str) -> list[str]:
    family = _schema_family(source_type)
    if family == "gst":
        return list(GST_NUMERIC_FIELDS)
    if family == "tds":
        return list(TDS_NUMERIC_FIELDS)
    if family == "tally":
        return list(GST_NUMERIC_FIELDS) + [f for f in TDS_NUMERIC_FIELDS if f not in GST_NUMERIC_FIELDS]
    return ["amount"]


def _date_fields(source_type: str) -> list[str]:
    family = _schema_family(source_type)
    if family == "gst":
        return list(GST_DATE_FIELDS)
    if family == "tds":
        return list(TDS_DATE_FIELDS)
    if family == "tally":
        return list(GST_DATE_FIELDS) + [f for f in TDS_DATE_FIELDS if f not in GST_DATE_FIELDS]
    return ["date"]


def _upper_fields(source_type: str) -> list[str]:
    family = _schema_family(source_type)
    if family == "gst":
        return list(GST_UPPER_FIELDS)
    if family == "tds":
        return list(TDS_UPPER_FIELDS)
    if family == "tally":
        return list(GST_UPPER_FIELDS) + [f for f in TDS_UPPER_FIELDS if f not in GST_UPPER_FIELDS]
    return []


# ---------------------------------------------------------------------------
# 1. Structural read
# ---------------------------------------------------------------------------


@dataclass
class RawRead:
    df: pd.DataFrame
    header_row: Optional[int]
    sheet_name: Optional[str]
    sheet_ambiguous: bool
    warnings: list[str]
    # Set when the file carried a TWO-ROW merged header (parent + child).
    # `header_row` is then None and the columns are already flattened to
    # "parent :: child" labels — see `detect_header_pair()`.
    header_row_pair: Optional[tuple[int, int]] = None


def _looks_like_data_row(values: list[str]) -> bool:
    """A row is 'data' if most of its non-empty cells are numeric, dates,
    or long alphanumeric codes — as opposed to short text labels."""
    non_empty = [v for v in values if v not in ("", "nan", "None")]
    if len(non_empty) < 2:
        return False
    data_like = 0
    for v in non_empty:
        if re.match(r"^-?[\d,]+(\.\d+)?$", v):
            data_like += 1
        elif re.match(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$", v):
            data_like += 1
        elif re.match(r"^[A-Z0-9]{8,}$", v, re.IGNORECASE) and any(c.isdigit() for c in v):
            data_like += 1
    return data_like >= max(2, len(non_empty) // 2)


def _score_header_row(values: list[str]) -> int:
    """Score how much a row looks like column labels rather than data."""
    non_empty = [str(v).strip() for v in values if str(v).strip() not in ("", "nan", "None")]
    if len(non_empty) < 2:
        return -1
    if any(_normalize_label(v) in ("total", "grand total", "subtotal") for v in non_empty):
        return -1
    if _looks_like_data_row(non_empty):
        return -1
    text_cells = sum(1 for v in non_empty if not re.match(r"^-?[\d,]+(\.\d+)?$", v))
    # A real header row has many short text labels and no long free text.
    long_text = sum(1 for v in non_empty if len(v) > 60)
    return text_cells * 2 + len(non_empty) - long_text * 5


def _normalize_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).strip().lower()).strip()


def detect_header_row(preview: pd.DataFrame, *, max_scan_rows: int = 20) -> Optional[int]:
    """Detect the real header row among the first ~20 rows.

    Returns the row index, or None when NO row looks like column labels —
    i.e. the file has no header at all and every row is data. Callers
    synthesize column names in that case rather than silently treating the
    first data row as a header (which would drop a record).
    """
    best_row: Optional[int] = None
    best_score = 0
    for i in range(min(len(preview), max_scan_rows)):
        score = _score_header_row(list(preview.iloc[i]))
        if score > best_score:
            best_row, best_score = i, score
    return best_row


def _is_blank_cell(v: Any) -> bool:
    return str(v).strip() in ("", "nan", "None")


def detect_header_pair(preview: pd.DataFrame, *, max_scan_rows: int = 20) -> Optional[tuple[int, int]]:
    """Detect a TWO-ROW merged header (parent row + child row).

    Real GSTN exports (GSTR-2B, IMS) ship a merged header: a parent row with
    merged spans ("Invoice Details", "Tax Amount") immediately above a child
    row carrying the finer sub-labels ("Invoice number", "Integrated Tax",
    "Central Tax", ...). The single-row detector picks the PARENT row and
    loses every child label, so those files map poorly — the columns come out
    as "Invoice Details" / "Tax Amount" / "Unnamed: N" and the required GST
    fields can't be resolved.

    The signature of a merged header is COMPLEMENTARY BLANKS: a merged parent
    cell is non-null only in its first column, so the child row fills columns
    the parent left blank. (The parent row usually has MORE non-empty cells
    overall, because it also carries single-column labels like "GSTIN of
    supplier" — so "child has more cells" is NOT the test.)

    Returns (parent_idx, child_idx) when a genuine pair is found, else None.
    A pair is: two consecutive header-shaped rows where the child fills at
    least 2 columns the parent left blank, the parent carries at least 3
    labels (a title row has 1-2), and the row after the child is NOT itself a
    header (three stacked label rows are not a merged header).
    """
    n = min(len(preview), max_scan_rows)
    scores = [_score_header_row(list(preview.iloc[i])) for i in range(n)]

    for i in range(n - 1):
        if scores[i] <= 0 or scores[i + 1] <= 0:
            continue
        parent = list(preview.iloc[i])
        child = list(preview.iloc[i + 1])
        parent_n = sum(1 for v in parent if not _is_blank_cell(v))
        if parent_n < 3:
            continue
        # Columns the parent left blank but the child fills — the merged-cell
        # signature. Require at least 2 so a single stray blank can't trigger.
        filled_by_child = sum(
            1 for p, c in zip(parent, child) if _is_blank_cell(p) and not _is_blank_cell(c)
        )
        if filled_by_child < 2:
            continue
        if i + 2 < n and scores[i + 2] > 0:
            continue
        return (i, i + 1)
    return None


def _apply_header_pair(raw: pd.DataFrame, header_pair: tuple[int, int]) -> pd.DataFrame:
    """Flatten a two-row merged header into parent::child column labels and
    return the frame with those labels and the data rows below the child row.

    Reuses F6's `flatten_two_row_header` — the SAME disambiguation mechanism
    the format registry uses for GSTR-2B/IMS — so the two paths can never
    disagree about what a merged header means.
    """
    from src.f6.fingerprint import flatten_two_row_header

    parent_idx, child_idx = header_pair
    parent_row = list(raw.iloc[parent_idx]) if parent_idx < len(raw) else []
    child_row = list(raw.iloc[child_idx]) if child_idx < len(raw) else []
    labels = flatten_two_row_header(parent_row, child_row)
    df = raw.iloc[child_idx + 1 :].reset_index(drop=True)
    if len(labels) < len(df.columns):
        labels = labels + [f"col_{i}" for i in range(len(labels), len(df.columns))]
    labels = labels[: len(df.columns)]
    # Flattened labels can collide (e.g. B2B-CDNR carries two "tax amount ::
    # integrated tax(₹)" blocks). Duplicate column names make pandas return a
    # DataFrame for df[col], which breaks every downstream per-column op — so
    # disambiguate with a numeric suffix.
    seen: dict[str, int] = {}
    unique: list[str] = []
    for label in labels:
        if label in seen:
            seen[label] += 1
            unique.append(f"{label} #{seen[label]}")
        else:
            seen[label] = 0
            unique.append(label)
    df.columns = unique
    return df


def _read_csv_tolerant(
    path: Path, header_row: Optional[int], header_pair: Optional[tuple[int, int]] = None,
) -> pd.DataFrame:
    """Read a CSV that may be RAGGED — real Tally/GSTN exports routinely
    carry title rows with fewer fields than the data rows, which makes
    pandas' parsers abort with "Expected N fields, saw M" (both the C and
    the Python engine infer the column count from the first row).

    The stdlib `csv` module handles ragged rows natively, so we read
    through it, pad every row to the widest row's width, and build the
    frame ourselves. This is the difference between "the file is
    unreadable" and "the file has a title row" — and the latter is the
    common case.

    Returns a frame with integer column labels and NO header applied; the
    caller decides which row is the header (see `_finalize_read`).
    """
    import csv

    rows: list[list[str]] = []
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [list(r) for r in csv.reader(f, dialect)]

    if not rows:
        return pd.DataFrame()

    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    df = pd.DataFrame(padded, dtype=str)

    if header_pair is not None:
        return _apply_header_pair(df, header_pair)
    if header_row is not None and 0 <= header_row < len(df):
        df.columns = [str(c).strip() for c in df.iloc[header_row]]
        df = df.iloc[header_row + 1 :].reset_index(drop=True)
    return df


def _read_sheet(
    path: Path, sheet: Any, header_row: Optional[int],
    header_pair: Optional[tuple[int, int]] = None,
) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext == ".csv":
        return _read_csv_tolerant(path, header_row, header_pair)
    if header_pair is not None:
        raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
        return _apply_header_pair(raw, header_pair)
    if header_row is None:
        return pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
    return pd.read_excel(path, sheet_name=sheet, header=header_row, dtype=str)


def _sheet_tabularity(df: pd.DataFrame) -> int:
    """Rough measure of how tabular a sheet is: rows x non-empty columns.

    Returns 0 for sheets too trivial to be the data sheet (a cover/notes
    tab with one row and one or two cells), so those don't get reported as
    ambiguous candidates alongside the real data sheet.
    """
    if df.empty or not len(df.columns):
        return 0
    non_empty_cols = sum(
        1 for c in df.columns
        if df[c].astype(str).str.strip().replace({"nan": ""}).ne("").any()
    )
    if non_empty_cols < 2:
        return 0
    if len(df) < 2 and non_empty_cols < 3:
        return 0
    return len(df) * non_empty_cols


def read_raw_with_header_detection(path: Path) -> RawRead:
    """Read a CSV/XLSX/XLS file, detecting the header row and the tabular
    sheet. Never assumes a header at row 0."""
    warnings: list[str] = []
    ext = path.suffix.lower()

    if ext == ".csv":
        preview = _read_csv_tolerant(path, None).head(20)
        header_pair = detect_header_pair(preview)
        if header_pair is not None:
            df = _read_sheet(path, None, None, header_pair)
            return RawRead(df=_finalize_read(df, None, header_pair), header_row=None,
                           sheet_name=None, sheet_ambiguous=False, warnings=warnings,
                           header_row_pair=header_pair)
        header_row = detect_header_row(preview)
        df = _read_sheet(path, None, header_row)
        if header_row is None:
            warnings.append(
                "No header row detected — every row looked like data. Columns were "
                "auto-named and the first row was kept as a record."
            )
        return RawRead(df=_finalize_read(df, header_row), header_row=header_row,
                       sheet_name=None, sheet_ambiguous=False, warnings=warnings)

    if ext not in (".xlsx", ".xls"):
        raise IngestionError(f"Unsupported file extension: {ext} ({path.name})")

    xls = pd.ExcelFile(path)
    sheet_names = list(xls.sheet_names)
    if not sheet_names:
        raise IngestionError(f"Workbook {path.name} contains no sheets.")

    candidates: list[tuple[str, Optional[int], Optional[tuple[int, int]], pd.DataFrame, int]] = []
    for name in sheet_names:
        preview = pd.read_excel(path, sheet_name=name, header=None, nrows=20, dtype=str)
        header_pair = detect_header_pair(preview)
        header_row = None if header_pair is not None else detect_header_row(preview)
        df = _read_sheet(path, name, header_row, header_pair)
        candidates.append((name, header_row, header_pair, df, _sheet_tabularity(df)))

    # Pick the sheet with tabular data. If more than one sheet is genuinely
    # tabular, the choice is ambiguous and we say so rather than pretending
    # it wasn't a judgement call.
    tabular = [c for c in candidates if c[4] > 0]
    if not tabular:
        raise IngestionError(f"Workbook {path.name} has no sheet containing tabular data.")

    # A sheet with a genuine two-row merged header is a real GSTN data sheet
    # (B2B, B2B-CDNR, ...). Prefer those over instruction/summary sheets —
    # a GSTR-2B workbook's "Read me" tab is 400+ rows of documentation and
    # would otherwise win on raw row count.
    pair_sheets = [c for c in tabular if c[2] is not None]
    pool = pair_sheets or tabular
    pool.sort(key=lambda c: c[4], reverse=True)
    chosen = pool[0]
    sheet_ambiguous = len(pool) > 1
    if sheet_ambiguous:
        others = ", ".join(c[0] for c in pool[1:])
        warnings.append(
            f"Workbook has {len(pool)} sheets with tabular data — used {chosen[0]!r} "
            f"(largest). Other candidate sheet(s): {others}."
        )
    if len(sheet_names) > 1 and not sheet_ambiguous:
        warnings.append(f"Workbook has {len(sheet_names)} sheets — used {chosen[0]!r}.")

    name, header_row, header_pair, df, _ = chosen
    if header_pair is not None:
        warnings.append(
            f"Sheet {name!r} has a two-row merged header — columns were flattened to "
            "'parent :: child' labels so the sub-fields (invoice number, tax amounts) "
            "are mapped correctly."
        )
    elif header_row is None:
        warnings.append(
            f"No header row detected in sheet {name!r} — every row looked like data. "
            "Columns were auto-named and the first row was kept as a record."
        )
    return RawRead(df=_finalize_read(df, header_row, header_pair), header_row=header_row,
                   sheet_name=name, sheet_ambiguous=sheet_ambiguous, warnings=warnings,
                   header_row_pair=header_pair)


def _finalize_read(
    df: pd.DataFrame, header_row: Optional[int],
    header_pair: Optional[tuple[int, int]] = None,
) -> pd.DataFrame:
    """Normalize the read frame: stringify headers, drop fully-empty rows
    and subtotal/grand-total rows, and reset the index."""
    if header_pair is not None:
        # Columns are already the flattened parent::child labels.
        df.columns = [str(c).strip() for c in df.columns]
    elif header_row is None:
        df.columns = [f"Column {i + 1}" for i in range(len(df.columns))]
    else:
        df.columns = [str(c).strip() for c in df.columns]

    # Drop rows that are entirely empty.
    if len(df.columns):
        non_empty = df.apply(
            lambda r: any(str(v).strip() not in ("", "nan", "None") for v in r), axis=1
        )
        df = df.loc[non_empty]

    # Drop subtotal/grand-total rows (first cell reads as one).
    if len(df.columns):
        first_col = df.columns[0]
        mask = df[first_col].astype(str).str.strip().str.lower().isin(
            ["total", "grand total", "subtotal", "totals"]
        )
        df = df.loc[~mask]

    return df.reset_index(drop=True)


def _header_signature(headers: list[str]) -> str:
    """Stable fingerprint of a file's header set — the cache key component
    that makes a learned mapping shape-specific rather than client-wide."""
    canonical = "|".join(_normalize_label(h) for h in headers)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _sample_rows(df: pd.DataFrame, n: int = 5) -> list[dict[str, str]]:
    """Up to `n` sample data rows as {header: value} — the value signal the
    model uses to infer a mapping when header text alone is ambiguous."""
    out: list[dict[str, str]] = []
    for _, row in df.head(n).iterrows():
        out.append({str(k): ("" if pd.isna(v) else str(v)) for k, v in row.items()})
    return out


# ---------------------------------------------------------------------------
# 2. Classification pre-check (Prompt 3)
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You classify raw spreadsheet exports for an Indian chartered accountancy "
    "firm's GST/TDS reconciliation tool. You are given the column headers and a "
    "few sample rows from a file. Identify what kind of document it is, based "
    "ONLY on the evidence in the file — not on what the user claims it is.\n\n"
    "Respond with a single valid JSON object and nothing else. No preamble, no "
    "markdown fences. Schema:\n"
    '{"document_type": "<one of the candidate labels>", '
    '"confidence": <integer 0-100>, '
    '"is_financial_data": <true|false>, '
    '"reason": "<one short sentence citing the specific evidence>"}\n\n'
    "Set is_financial_data to false when the file has no recognizable invoice, "
    "GSTIN, PAN, ledger, or amount columns — e.g. a photo list, a letter, or "
    "unrelated tabular data."
)


def _build_classify_prompt(headers: list[str], samples: list[dict[str, str]]) -> str:
    lines = ["Candidate document types:"]
    lines += [f"- {c}" for c in CLASSIFICATION_CANDIDATES]
    lines.append("\nDetected column headers:")
    lines.append(json.dumps(headers, ensure_ascii=False))
    lines.append("\nSample rows (first few):")
    lines.append(json.dumps(samples, ensure_ascii=False, indent=1))
    lines.append("\nClassify this file.")
    return "\n".join(lines)


def classify_document(
    headers: list[str], samples: list[dict[str, str]], *, db_path=None,
) -> tuple[dict[str, Any], str]:
    """Ask the model what this document is. Returns (classification, model_id).

    Raises LLMError when the model is unavailable — per the locked decision
    for this build there is no silent heuristic fallback.
    """
    parsed, _latency, model_id, _raw = llm.call_llm_json(
        _CLASSIFY_SYSTEM, _build_classify_prompt(headers, samples), db_path=db_path
    )
    doc_type = str(parsed.get("document_type") or "").strip()
    if not doc_type:
        raise llm.LLMError("parse_error|classification response had no document_type")
    # Snap the model's answer onto our vocabulary (and onto the pre-rename
    # wording) so a near-miss like "Tally purchase register" or a casing
    # difference still compares correctly against the slot's accepted set.
    doc_type = _canonical_doc_type(doc_type)
    try:
        confidence = int(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    return {
        "document_type": doc_type,
        "confidence": max(0, min(100, confidence)),
        "is_financial_data": bool(parsed.get("is_financial_data", True)),
        "reason": str(parsed.get("reason") or "").strip(),
    }, model_id


def _normalize_doc_type(doc_type: str) -> str:
    """Lowercase and strip punctuation from a document_type so comparisons
    aren't defeated by casing or trailing punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", str(doc_type or "").lower()).strip()


# The label the model used for the books slot before it was renamed. Files
# classified (and cached) under the old wording must keep resolving, or
# reopening a previously-mapped books file would suddenly read as a
# wrong-slot upload.
_LEGACY_DOC_TYPE_ALIASES: dict[str, str] = {
    "tally purchase register": "Books / purchase register",
}


def _canonical_doc_type(doc_type: str) -> str:
    norm = _normalize_doc_type(doc_type)
    for candidate in CLASSIFICATION_CANDIDATES:
        if _normalize_doc_type(candidate) == norm:
            return candidate
    return _LEGACY_DOC_TYPE_ALIASES.get(norm, str(doc_type or "").strip())


def _classification_verdict(
    classification: dict[str, Any], source_type: str,
) -> tuple[bool, Optional[str]]:
    """Compare the model's classification against the declared upload slot.

    Returns (ok, message). `ok=False` means STOP before field mapping.
    """
    doc_type = _canonical_doc_type(classification.get("document_type", ""))
    slot_label = _SLOT_LABELS.get(source_type, source_type)

    if not classification.get("is_financial_data", True):
        return False, (
            "This file doesn't look like reconciliation data — it doesn't contain "
            "recognizable invoice, GSTIN, or amount columns. Please check the file."
        )

    accepted = _ACCEPTED_CLASSIFICATIONS.get(source_type, set())
    if doc_type in accepted:
        return True, None

    # Wrong slot. Name the likely correct slot when one exists.
    article = "an" if doc_type[:1].lower() in "aeiou" else "a"
    suggested = _SUGGESTED_SLOT.get(doc_type)
    if suggested and suggested != source_type:
        suggested_label = _SLOT_LABELS.get(suggested, suggested)
        return False, (
            f"This file looks like {article} {doc_type} export, but you uploaded it as "
            f"the {slot_label}. Did you mean to upload it under {suggested_label} instead?"
        )
    return False, (
        f"This file looks like {article} {doc_type}, but you uploaded it as "
        f"the {slot_label}. Please check you're uploading it to the right slot."
    )


# ---------------------------------------------------------------------------
# 3. Field mapping (Prompt 1)
# ---------------------------------------------------------------------------

_MAP_SYSTEM = (
    "You map the columns of a raw Indian accounting export onto a fixed "
    "canonical schema for a GST/TDS reconciliation engine. You are given the "
    "canonical fields, the file's detected column headers, and a few sample "
    "rows.\n\n"
    "For EVERY canonical field, decide which source column (if any) it maps to. "
    "Use both the header text and the sample VALUES as evidence — e.g. a column "
    "of 15-character alphanumeric codes is a GSTIN even if its header is "
    "unhelpful, and a column of dates is a date field even if it's labelled "
    "'Col3'.\n\n"
    "Rules:\n"
    "- Map a source column to AT MOST one canonical field.\n"
    "- If no source column plausibly carries a field, return null for it. Never "
    "invent a mapping to fill a gap.\n"
    "- Confidence is 0.0-1.0. Use >=0.9 only when the header text matches "
    "unambiguously; use 0.5-0.8 when you are inferring from values; use <0.5 "
    "when you are guessing.\n"
    "- The reason must be one short sentence citing the specific evidence "
    "('header text matches' vs 'inferred from values — column contains "
    "15-character alphanumeric codes matching the GSTIN pattern').\n\n"
    "Respond with a single valid JSON object and nothing else. No preamble, no "
    "markdown fences. Schema:\n"
    '{"mappings": [{"canonical_field": "<field>", "source_column": "<header>"|null, '
    '"confidence": <0.0-1.0>, "reason": "<one sentence>"}]}'
)


def _build_map_prompt(
    source_type: str, headers: list[str], samples: list[dict[str, str]], c5_context: str = "",
) -> str:
    fields = canonical_fields_for(source_type)
    required = set(required_fields_for(source_type))
    lines = [f"Upload slot: {_SLOT_LABELS.get(source_type, source_type)}"]
    lines.append("\nCanonical fields to map (required fields marked *):")
    for f in fields:
        lines.append(f"- {f}{' *' if f in required else ''}")
    lines.append("\nDetected column headers:")
    lines.append(json.dumps(headers, ensure_ascii=False))
    lines.append("\nSample rows (first few):")
    lines.append(json.dumps(samples, ensure_ascii=False, indent=1))
    if c5_context:
        lines.append("\n" + c5_context)
    lines.append("\nReturn the mapping for every canonical field listed above.")
    return "\n".join(lines)


def infer_mapping(
    source_type: str, headers: list[str], samples: list[dict[str, str]], *, db_path=None,
    c5_context: str = "",
) -> tuple[list[FieldMapping], str]:
    """Ask the model to map this file's columns onto the canonical schema.

    Returns (field_mappings, model_id). Every canonical field gets a
    FieldMapping — unmapped ones carry source_column=None and a reason, so
    nothing is ever silently dropped.
    """
    parsed, _latency, model_id, _raw = llm.call_llm_json(
        _MAP_SYSTEM, _build_map_prompt(source_type, headers, samples, c5_context), db_path=db_path
    )
    raw_mappings = parsed.get("mappings")
    if not isinstance(raw_mappings, list):
        raise llm.LLMError("parse_error|mapping response had no 'mappings' list")

    by_field: dict[str, dict[str, Any]] = {}
    for m in raw_mappings:
        if isinstance(m, dict) and m.get("canonical_field"):
            by_field[str(m["canonical_field"])] = m

    header_set = {str(h) for h in headers}
    required = set(required_fields_for(source_type))
    used_columns: set[str] = set()
    out: list[FieldMapping] = []

    for f in canonical_fields_for(source_type):
        m = by_field.get(f)
        if m is None:
            out.append(FieldMapping(
                canonical_field=f, source_column=None, confidence=None,
                reason="The model returned no mapping for this field.", required=f in required,
            ))
            continue

        col = m.get("source_column")
        col = str(col).strip() if col not in (None, "", "null") else None
        # Reject a column the model hallucinated — it must exist in the file.
        if col is not None and col not in header_set:
            out.append(FieldMapping(
                canonical_field=f, source_column=None, confidence=None,
                reason=f"The model proposed {col!r}, which is not a column in this file.",
                required=f in required,
            ))
            continue
        # Enforce one-source-column-per-field: first (highest-confidence)
        # claim wins, later duplicates are dropped with an explanation.
        if col is not None and col in used_columns:
            out.append(FieldMapping(
                canonical_field=f, source_column=None, confidence=None,
                reason=f"{col!r} was already mapped to another field.",
                required=f in required,
            ))
            continue
        if col is not None:
            used_columns.add(col)

        try:
            conf = float(m.get("confidence")) if m.get("confidence") is not None else None
        except (TypeError, ValueError):
            conf = None
        if conf is not None:
            conf = max(0.0, min(1.0, conf))

        out.append(FieldMapping(
            canonical_field=f, source_column=col, confidence=conf,
            reason=str(m.get("reason") or "").strip() or "No reason given.",
            required=f in required,
        ))
    return out, model_id


# A GSTIN is 15 chars: 2-digit state + 5 letters + 4 digits + letter + digit
# + letter + alphanumeric. Used to RESCUE a gstin the model left unmapped but
# whose values are unmistakably GSTINs — the "fetched but not mapped" case.
_GSTIN_VALUE_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][A-Z][0-9A-Z]$")
_PAN_VALUE_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _rescue_identity_field(
    field: str, pattern: re.Pattern[str], headers: list[str], samples: list[dict[str, str]],
    mappings: list[FieldMapping], used_columns: set[str], *, required: bool,
) -> Optional[FieldMapping]:
    """Deterministically map an identity field (gstin/pan) whose VALUES are
    unmistakable, when the model left it unmapped.

    This is the fix for "the file clearly contains a GSTIN but it wasn't
    mapped": the model's one-to-one rule can let another field claim the
    column first, or the model can simply return null. We scan the sample
    values for a column that is overwhelmingly the target pattern and map it
    — but ONLY when the field is currently unmapped and the column is not
    already claimed, so a genuine model mapping is never overridden.
    """
    existing = next((m for m in mappings if m.canonical_field == field), None)
    if existing is not None and existing.mapped:
        return None
    if not samples:
        return None

    best_col: Optional[str] = None
    best_hits = 0
    for col in headers:
        if col in used_columns:
            continue
        values = [str(r.get(col, "")).strip().upper() for r in samples]
        values = [v for v in values if v]
        if not values:
            continue
        hits = sum(1 for v in values if pattern.match(v))
        # Require a strong majority of non-empty sample values to match, so a
        # stray code in a free-text column can't win.
        if hits >= max(2, int(len(values) * 0.6)) and hits > best_hits:
            best_col, best_hits = col, hits
    if best_col is None:
        return None
    return FieldMapping(
        canonical_field=field, source_column=best_col, confidence=0.9,
        reason=(
            f"Inferred from values — column {best_col!r} contains "
            f"{best_hits} value(s) matching the {field.upper()} pattern."
        ),
        required=required,
    )


def _rescue_identity_fields(
    source_type: str, headers: list[str], samples: list[dict[str, str]],
    mappings: list[FieldMapping],
) -> list[FieldMapping]:
    """Apply the deterministic identity rescue to a mapping list. Only runs
    for GST-family slots (gstin) and TDS-family slots (pan)."""
    family = _schema_family(source_type)
    targets: list[tuple[str, re.Pattern[str]]] = []
    if family in ("gst", "tally"):
        targets.append(("gstin", _GSTIN_VALUE_RE))
    if family in ("tds", "tally"):
        targets.append(("pan", _PAN_VALUE_RE))
    if not targets:
        return mappings

    used = {m.source_column for m in mappings if m.mapped and m.source_column}
    required_set = set(required_fields_for(source_type))
    out = list(mappings)
    for field, pattern in targets:
        rescue = _rescue_identity_field(
            field, pattern, headers, samples, out, used, required=field in required_set,
        )
        if rescue is None:
            continue
        used.add(rescue.source_column)
        out = [rescue if m.canonical_field == field else m for m in out]
    return out


# ---------------------------------------------------------------------------
# 4. Apply mapping + coerce
# ---------------------------------------------------------------------------


def apply_mapping(
    raw_df: pd.DataFrame, mappings: list[FieldMapping], source_type: str, filename: str,
) -> tuple[pd.DataFrame, list[str]]:
    """Build the canonical DataFrame from the raw frame + mapping, coercing
    dates and currency. Returns (canonical_df, warnings)."""
    warnings: list[str] = []
    family = _schema_family(source_type)
    canonical_fields = canonical_fields_for(source_type)

    out = pd.DataFrame(index=raw_df.index)
    for m in mappings:
        if m.mapped and m.source_column in raw_df.columns:
            out[m.canonical_field] = raw_df[m.source_column]
        else:
            out[m.canonical_field] = None

    # Bookkeeping columns the engine expects.
    out["original_row"] = raw_df.apply(
        lambda row: json.dumps({str(k): (None if pd.isna(v) else str(v)) for k, v in row.items()}),
        axis=1,
    )
    out["source_type"] = source_type
    out["source_file"] = filename

    # --- Strings ---
    numeric = set(_numeric_fields(source_type))
    dates = set(_date_fields(source_type))
    for f in canonical_fields:
        if f in numeric or f in dates:
            continue
        out[f] = out[f].astype(str).str.strip().replace({"nan": "", "None": ""})

    # --- Uppercase identity fields (GSTIN/PAN) ---
    for f in _upper_fields(source_type):
        if f in out.columns:
            out[f] = out[f].astype(str).str.upper().str.strip().replace({"NAN": "", "NONE": ""})

    # --- Dates: try the common formats rather than assuming one ---
    for f in _date_fields(source_type):
        if f in out.columns and out[f].notna().any():
            non_blank = out[f].astype(str).str.strip().replace({"": None, "nan": None, "None": None})
            if non_blank.notna().any():
                out[f] = _normalize_dates_flexible(
                    out[f], field=f, context=f"{source_type}/{filename}"
                )
                failed = int(
                    (out[f].isna() & non_blank.notna()).sum()
                )
                if failed:
                    warnings.append(
                        f"{failed} row(s) had an unparseable {f} and were left blank."
                    )

    # --- Numeric coercion ---
    for f in _numeric_fields(source_type):
        if f in out.columns:
            coerced, affected = _coerce_numeric(out[f])
            out[f] = coerced
            # rounding_adjustment defaulting to 0 is expected, not a warning
            # — it's a residual field most sources never carry.
            if affected and f not in _ZERO_DEFAULT_FIELDS:
                warnings.append(
                    f"{len(affected)} row(s) had a blank or unparseable {f} and were set to 0."
                )

    # --- Derived total_tax (GST-shaped frames only) ---
    if family in ("gst", "tally"):
        out["total_tax"] = out["cgst"] + out["sgst"] + out["igst"] + out["cess"]

    # --- Duplicate identity rows (flagged, never dropped) ---
    dedupe_key = _dedupe_key(source_type)
    if dedupe_key and all(k in out.columns for k in dedupe_key):
        dup_mask = out.duplicated(subset=dedupe_key, keep=False)
        dup_count = int(dup_mask.sum())
        if dup_count:
            warnings.append(
                f"{dup_count} row(s) share a duplicate {'+'.join(dedupe_key)} — flagged, not dropped."
            )

    # --- Rows dropped for having no usable identity at all ---
    row_count_in = len(out)
    if dedupe_key:
        identity_cols = [c for c in dedupe_key if c in out.columns]
        if identity_cols:
            has_identity = out[identity_cols].apply(
                lambda r: any(str(v).strip() not in ("", "nan", "None", "0", "0.0") for v in r), axis=1
            )
            dropped = int((~has_identity).sum())
            if dropped:
                out = out.loc[has_identity]
                warnings.append(
                    f"{dropped} row(s) dropped — no usable {'/'.join(identity_cols)} value."
                )

    final_fields = [f for f in canonical_fields] + ["source_type", "source_file", "original_row"]
    if family in ("gst", "tally") and "invoice_value" in final_fields:
        final_fields.insert(final_fields.index("invoice_value") + 1, "total_tax")
    out = out[[f for f in final_fields if f in out.columns]]

    if row_count_in != len(out):
        warnings.append(f"Rows in: {row_count_in}, rows out: {len(out)}.")
    return out.reset_index(drop=True), warnings


def _dedupe_key(source_type: str) -> list[str]:
    family = _schema_family(source_type)
    if family == "gst":
        return ["gstin", "invoice_number"]
    if family == "tds":
        return ["pan", "challan_number"]
    if family == "tally":
        # A Tally books export may be either a purchase register or a TDS
        # ledger; the caller's recon_type decides which identity applies, so
        # use the GST identity (the more common case) and let the TDS run
        # rely on the TDS columns being present.
        return ["gstin", "invoice_number"]
    return ["reference"]


# ---------------------------------------------------------------------------
# Preselect threshold (Prompt 2) — frontend-configurable
# ---------------------------------------------------------------------------


def get_preselect_threshold(*, db_path=None) -> float:
    """Confidence at/above which a mapped field is pre-selected for the
    user. Default 0.75; editable from Settings → AI Ingestion."""
    try:
        from src.settings import service as settings

        raw = settings.get_setting_str(KEY_PRESELECT_THRESHOLD, db_path=db_path)
        if raw is None:
            return DEFAULT_PRESELECT_THRESHOLD
        return max(0.0, min(1.0, float(raw)))
    except Exception:  # noqa: BLE001
        return DEFAULT_PRESELECT_THRESHOLD


def set_preselect_threshold(value: float, *, actor: str, db_path=None) -> None:
    if not (0.0 <= value <= 1.0):
        raise IngestionError("The pre-select threshold must be between 0.0 and 1.0.")
    from src.settings import service as settings

    settings.set_setting_str(KEY_PRESELECT_THRESHOLD, str(float(value)), actor=actor, db_path=db_path)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def normalize_source_file(
    file: Any,
    source_type: str,
    client: str,
    period: Optional[str],
    *,
    client_id: Optional[int] = None,
    recon_type: Optional[str] = None,
    actor: str = "system",
    filename: Optional[str] = None,
    db_path=None,
    use_cache: bool = True,
) -> IngestionResult:
    """Normalize one raw uploaded file into the canonical schema.

    Args:
        file: A path (str/Path) to the file, or raw bytes, or a file-like
            object with `.read()`.
        source_type: The declared upload slot — "tally_purchase_register",
            "gstr2b", "ims", "form26as", "tds", or a 2C source.
        client: The client folder/name the upload belongs to.
        period: The period string, or None.
        client_id: Optional F2 client_id, for the shape cache.
        recon_type: 'GST'/'TDS'/'OTHER' when known. Only affects the
            required-field gate for the 'tally' slot, which feeds both GST
            and TDS recon from one file.
        actor: Who triggered this, for the audit trail.
        filename: The caller's own name for the file. REQUIRED when `file`
            is bytes or a file-like object — without it the temp file is
            named "upload.csv" and the real extension is lost, so an .xlsx
            upload is parsed as CSV and yields no rows. Callers passing a
            path can omit it (the path carries the name).
        use_cache: When False, bypass the shape cache and always call the
            model (used by the "re-run mapping" action).

    Returns:
        IngestionResult. Never raises on a schema mismatch — missing fields
        are reported in `unmapped_required_fields` and `status`.
    """
    from src.ingestion_ai import db as idb

    required = required_fields_for(source_type, recon_type)
    filename, path, cleanup = _materialize(file, filename)
    try:
        try:
            read = read_raw_with_header_detection(path)
        except IngestionError as exc:
            # A workbook with no tabular sheet is a readable-but-empty file,
            # not a crash. Report it as its own outcome so the caller can
            # present it honestly instead of surfacing a raw exception.
            if "no sheet containing tabular data" not in str(exc):
                raise
            result = IngestionResult(
                source_type=source_type, filename=filename, client=client, period=period,
                status=STATUS_EMPTY, canonical_df=pd.DataFrame(), field_mappings=[],
                unmapped_required_fields=[], row_count_in=0, row_count_out=0,
                warnings=[], headers=[], header_row=None, sheet_name=None,
                sheet_ambiguous=False, classification=None, model_used=None,
                llm_cached=False, header_signature="",
                message="The file contains no data rows — every sheet is empty or header-only.",
            )
            _persist_result(result, client_id=client_id, actor=actor, db_path=db_path)
            return result

        headers = [str(c) for c in read.df.columns]
        samples = _sample_rows(read.df)
        signature = _header_signature(headers)

        warnings = list(read.warnings)
        notes: list[str] = []
        classification: Optional[dict[str, Any]] = None
        model_used: Optional[str] = None
        llm_cached = False
        mappings: list[FieldMapping] = []

        # --- Deterministic portal-export fast path (F6 bridge) ---
        # GSTR-2B and IMS have a KNOWN, fixed GSTN layout. F6 models it
        # precisely and parses it with zero model calls, so use that instead
        # of asking a model to guess the mapping. This runs BEFORE the shape
        # cache so a stale cached model mapping can never shadow the
        # deterministic parse. Falls through to the generic path when the
        # bridge doesn't cover the source type or yields no rows.
        if f6_bridge.supports(source_type):
            bridged = f6_bridge.parse_portal_export(path, source_type, filename)
            if bridged is not None:
                bridge_df, bridge_maps, bridge_warnings = bridged
                warnings.extend(bridge_warnings)
                notes.append(
                    "Parsed with the deterministic GSTR-2B/IMS layout (F6 format registry) — "
                    "no model call was needed."
                )
                bridge_mappings = [
                    FieldMapping(
                        canonical_field=m["canonical_field"],
                        source_column=m.get("source_column"),
                        confidence=m.get("confidence"),
                        reason=m.get("reason") or "",
                        required=bool(m.get("required")),
                    )
                    for m in bridge_maps
                ]
                bridge_unmapped_required = [
                    f for f in required
                    if not any(m.canonical_field == f and m.mapped for m in bridge_mappings)
                ]
                bridge_unmapped_optional = [
                    m.canonical_field for m in bridge_mappings if not m.required and not m.mapped
                ]
                bridge_status = (
                    STATUS_PARTIAL
                    if (bridge_unmapped_required or bridge_unmapped_optional or warnings)
                    else STATUS_OK
                )
                result = IngestionResult(
                    source_type=source_type, filename=filename, client=client, period=period,
                    status=bridge_status, canonical_df=bridge_df, field_mappings=bridge_mappings,
                    unmapped_required_fields=bridge_unmapped_required,
                    row_count_in=len(read.df), row_count_out=len(bridge_df), warnings=warnings,
                    headers=headers, header_row=read.header_row, sheet_name=read.sheet_name,
                    sheet_ambiguous=read.sheet_ambiguous, classification=None,
                    model_used=None, llm_cached=False, header_signature=signature,
                    notes=notes,
                )
                result.caveats = result.build_caveats(recon_type)
                _persist_result(result, client_id=client_id, actor=actor, db_path=db_path)
                return result

        conn = idb.connect(db_path)
        cached_shape = None
        if use_cache and conn is not None:
            try:
                cached_shape = idb.get_shape(
                    conn, client_ref=client, source_type=source_type, header_signature=signature
                )
            except Exception:  # noqa: BLE001
                cached_shape = None

        if cached_shape is not None:
            classification = cached_shape.get("classification")
            model_used = cached_shape.get("model_used")
            llm_cached = True
            trusted = bool(cached_shape.get("trusted"))
            mappings = _mappings_from_cache(
                cached_shape.get("mapping") or {}, source_type, trusted=trusted
            )
            if trusted:
                notes.append(
                    "Mapping reused from a trusted profile for this exact file shape — "
                    "no model call and no manual step needed."
                )
            else:
                notes.append("Mapping reused from cache for this exact file shape — no model call made.")
            try:
                idb.touch_shape(conn, cached_shape["shape_id"])
            except Exception:  # noqa: BLE001
                pass
        else:
            # --- Prompt 3: classification pre-check BEFORE any field mapping ---
            classification, model_used = classify_document(headers, samples, db_path=db_path)
            ok, message = _classification_verdict(classification, source_type)
            if not ok:
                status = (
                    STATUS_UNRECOGNIZED
                    if not classification.get("is_financial_data", True)
                    else STATUS_WRONG_SLOT
                )
                result = IngestionResult(
                    source_type=source_type, filename=filename, client=client, period=period,
                    status=status, canonical_df=pd.DataFrame(), field_mappings=[],
                    unmapped_required_fields=required,
                    row_count_in=len(read.df), row_count_out=0, warnings=warnings,
                    headers=headers, header_row=read.header_row, sheet_name=read.sheet_name,
                    sheet_ambiguous=read.sheet_ambiguous, classification=classification,
                    model_used=model_used, llm_cached=False, header_signature=signature,
                    message=message, notes=notes,
                )
                _persist_result(result, client_id=client_id, actor=actor, db_path=db_path)
                return result

            # --- Prompt 1: field mapping ---
            c5_context = _c5_context(client_id)
            mappings, model_used = infer_mapping(
                source_type, headers, samples, db_path=db_path, c5_context=c5_context
            )
            _store_shape(
                client, source_type, signature, headers, mappings, classification,
                model_used, client_id=client_id, actor=actor, db_path=db_path,
            )

        # --- Deterministic identity rescue ---
        # The model's one-to-one rule can leave gstin/pan unmapped even when
        # the file plainly contains them (another field claimed the column
        # first, or the model returned null). Rescue them from the VALUES —
        # never overriding a genuine mapping. Runs on both the cache and the
        # model path, so a cached gstin=None is corrected too.
        rescued = _rescue_identity_fields(source_type, headers, samples, mappings)
        if rescued != mappings:
            for m in rescued:
                if m.mapped and not any(
                    old.canonical_field == m.canonical_field and old.mapped for old in mappings
                ):
                    notes.append(f"{m.canonical_field.upper()} recovered from column values: {m.reason}")
            mappings = rescued

        # --- Apply + coerce ---
        canonical_df, apply_warnings = apply_mapping(read.df, mappings, source_type, filename)
        warnings.extend(apply_warnings)

        unmapped_required = [
            m.canonical_field for m in mappings if m.required and not m.mapped
        ]
        # For the 'tally' slot the required set depends on the recon type,
        # which the upload screen may not know yet — apply it here so the
        # gate is driven by the caller's declared recon, not by the schema.
        if required:
            unmapped_required = [
                f for f in required
                if not any(m.canonical_field == f and m.mapped for m in mappings)
            ]
        unmapped_optional = [m.canonical_field for m in mappings if not m.required and not m.mapped]

        if unmapped_required:
            # Missing required fields no longer BLOCK the run. The engine works
            # on whatever IS mapped, and the run carries a caveat saying which
            # check it could not perform — a partial report that states its own
            # limits is more useful to an accountant than a dead end. Only
            # genuinely wrong data (wrong slot / not financial data) still stops.
            status = STATUS_PARTIAL
        elif unmapped_optional or warnings:
            # Only genuine data-quality problems (dropped rows, unparseable
            # values, duplicates) downgrade a file to 'partial'. Informational
            # notes — cache reuse, multi-sheet choice — do not.
            status = STATUS_PARTIAL
        else:
            status = STATUS_OK

        result = IngestionResult(
            source_type=source_type, filename=filename, client=client, period=period,
            status=status, canonical_df=canonical_df, field_mappings=mappings,
            unmapped_required_fields=unmapped_required,
            row_count_in=len(read.df), row_count_out=len(canonical_df), warnings=warnings,
            headers=headers, header_row=read.header_row, sheet_name=read.sheet_name,
            sheet_ambiguous=read.sheet_ambiguous, classification=classification,
            model_used=model_used, llm_cached=llm_cached, header_signature=signature,
            notes=notes,
        )
        result.caveats = result.build_caveats(recon_type)
        _persist_result(result, client_id=client_id, actor=actor, db_path=db_path)
        return result
    finally:
        if cleanup:
            path.unlink(missing_ok=True)


def _materialize(file: Any, filename: Optional[str] = None) -> tuple[str, Path, bool]:
    """Turn the caller's `file` into (filename, path, needs_cleanup).

    ``filename`` is the caller's own name for the file and MUST be supplied
    whenever ``file`` is raw bytes or a file-like object. Without it the
    temp file is named ``upload.csv``, which loses the real extension — an
    .xlsx upload then gets written to a .csv path and parsed by the CSV
    reader, producing binary junk instead of rows (and a storage key that
    can never be looked up again). Callers that pass a path already carry
    the name, so this is optional for them.
    """
    import os
    import tempfile

    if isinstance(file, (str, Path)):
        p = Path(file)
        if not p.exists():
            raise IngestionError(f"File not found: {p}")
        return p.name, p, False

    if isinstance(file, (bytes, bytearray)):
        data = bytes(file)
        resolved = filename or "upload.csv"
    elif hasattr(file, "read"):
        resolved = filename or getattr(file, "name", "upload.csv")
        data = file.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
    else:
        raise IngestionError(f"Unsupported file input: {type(file)!r}")

    suffix = "." + resolved.rsplit(".", 1)[-1].lower() if "." in resolved else ".csv"
    fd, path_str = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    path = Path(path_str)
    path.write_bytes(data)
    return resolved, path, True


def _c5_context(client_id: Optional[int]) -> str:
    """C5's ingestion_mapping touchpoint context. Best-effort — C5 is a
    non-blocking input, its absence must never break ingestion."""
    try:
        from src.c5 import service as c5

        return c5.runtime_context("ingestion_mapping", client_id=client_id)
    except Exception:  # noqa: BLE001
        return ""


def _mappings_from_cache(
    stored: dict[str, Any], source_type: str, *, trusted: bool,
) -> list[FieldMapping]:
    """Rebuild FieldMapping objects from a cached shape row, keeping the
    canonical field order and filling any field the cache doesn't cover."""
    required = set(required_fields_for(source_type))
    out: list[FieldMapping] = []
    for f in canonical_fields_for(source_type):
        entry = stored.get(f)
        if isinstance(entry, dict):
            col = entry.get("source_column")
            conf = entry.get("confidence")
            out.append(FieldMapping(
                canonical_field=f, source_column=col,
                confidence=float(conf) if conf is not None else None,
                reason=str(entry.get("reason") or "Reused from a previously confirmed mapping."),
                required=f in required, from_cache=True, from_trusted_profile=trusted,
            ))
        else:
            out.append(FieldMapping(
                canonical_field=f, source_column=None, confidence=None,
                reason="Not covered by the cached mapping for this file shape.",
                required=f in required, from_cache=True, from_trusted_profile=trusted,
            ))
    return out


def _store_shape(
    client: str, source_type: str, signature: str, headers: list[str],
    mappings: list[FieldMapping], classification: Optional[dict[str, Any]],
    model_used: Optional[str], *, client_id: Optional[int], actor: str, db_path=None,
) -> None:
    """Cache the model's mapping for this exact file shape. Best-effort —
    a cache write failure must never break an otherwise-good ingestion."""
    try:
        from src.ingestion_ai import db as idb

        conn = idb.connect(db_path)
        try:
            mapping_json = {
                m.canonical_field: {
                    "source_column": m.source_column,
                    "confidence": m.confidence,
                    "reason": m.reason,
                }
                for m in mappings
            }
            confidences = [m.confidence for m in mappings if m.confidence is not None]
            idb.upsert_shape(
                conn, client_ref=client, source_type=source_type, header_signature=signature,
                headers=headers, mapping=mapping_json, classification=classification,
                model_used=model_used,
                confidence_floor=min(confidences) if confidences else None,
                trusted=False, source="ai", client_id=client_id, actor=actor,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _persist_result(
    result: IngestionResult, *, client_id: Optional[int], actor: str, db_path=None,
) -> None:
    """Record the result so the Run screen and Smart Ingestion read the
    SAME object rather than each re-deriving it. Best-effort."""
    try:
        from src.ingestion_ai import db as idb

        conn = idb.connect(db_path)
        try:
            idb.upsert_ingestion_result(
                conn,
                upload_key=upload_key(result.client, result.period, result.source_type, result.filename),
                client_ref=result.client, client_id=client_id, period=result.period,
                source_type=result.source_type, filename=result.filename, status=result.status,
                header_row=result.header_row, sheet_name=result.sheet_name,
                sheet_ambiguous=result.sheet_ambiguous, headers=result.headers,
                mapping=[m.to_dict() for m in result.field_mappings],
                classification=result.classification,
                unmapped_required=result.unmapped_required_fields, warnings=result.warnings,
                row_count_in=result.row_count_in, row_count_out=result.row_count_out,
                model_used=result.model_used, llm_cached=result.llm_cached, actor=actor,
                notes=result.notes, message=result.message,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def upload_key(client: str, period: Optional[str], source_type: str, filename: str) -> str:
    return f"{client}/{period or '-'}/{source_type}/{filename}"


# ---------------------------------------------------------------------------
# Reads for the UI / runner
# ---------------------------------------------------------------------------


def get_stored_result(
    client: str, period: Optional[str], source_type: str, filename: str, *, db_path=None,
) -> Optional[dict[str, Any]]:
    """The persisted IngestionResult for an upload, or None."""
    try:
        from src.ingestion_ai import db as idb

        conn = idb.connect(db_path)
        try:
            return idb.get_ingestion_result(
                conn, upload_key(client, period, source_type, filename)
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def confirm_shape_mapping(
    client: str, source_type: str, header_signature: str,
    *, field_overrides: dict[str, Optional[str]], trust_for_reuse: bool, actor: str, db_path=None,
) -> None:
    """Persist a human-confirmed mapping for this exact file shape.

    Called on confirm from the mapping UI. When `trust_for_reuse` is set,
    the shape is marked trusted so the next file of this shape skips both
    the model call and the manual step entirely (Prompt 2). The FULL
    mapping is persisted, not just the corrected fields.
    """
    from src.ingestion_ai import db as idb

    conn = idb.connect(db_path)
    try:
        existing = idb.get_shape(
            conn, client_ref=client, source_type=source_type, header_signature=header_signature
        )
        prior = (existing or {}).get("mapping") or {}
        mapping_json: dict[str, Any] = {}
        for field, col in field_overrides.items():
            prior_entry = prior.get(field) or {}
            mapping_json[field] = {
                "source_column": col,
                "confidence": 1.0 if col else None,
                "reason": (
                    "Confirmed by a reviewer."
                    if col != prior_entry.get("source_column")
                    else prior_entry.get("reason") or "Confirmed by a reviewer."
                ),
            }
        idb.upsert_shape(
            conn, client_ref=client, source_type=source_type, header_signature=header_signature,
            headers=(existing or {}).get("headers") or [],
            mapping=mapping_json,
            classification=(existing or {}).get("classification"),
            model_used=(existing or {}).get("model_used"),
            confidence_floor=1.0 if all(field_overrides.values()) else None,
            trusted=trust_for_reuse, source="trusted_profile" if trust_for_reuse else "ai",
            client_id=(existing or {}).get("client_id"), actor=actor,
        )
    finally:
        conn.close()


def list_shapes(*, client_ref: Optional[str] = None, source_type: Optional[str] = None, db_path=None):
    from src.ingestion_ai import db as idb

    conn = idb.connect(db_path)
    try:
        return idb.list_shapes(conn, client_ref=client_ref, source_type=source_type)
    finally:
        conn.close()


def revoke_shape_trust(shape_id: int, *, db_path=None) -> None:
    """Revoke trust for a learned shape.

    Trust is what lets a shape skip the model call AND the manual step, so
    revoking it must genuinely re-map the next file of that shape — not
    merely flip a flag while the cached mapping keeps being reused. The
    cached mapping is therefore deleted outright: the next upload of this
    shape goes back through classification + field mapping.
    """
    from src.ingestion_ai import db as idb

    conn = idb.connect(db_path)
    try:
        conn.execute("DELETE FROM ingestion_shape_cache WHERE shape_id = ?", (shape_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Canonical pair loading — what the reconciliation engine actually consumes
# ---------------------------------------------------------------------------


class IngestionBlockedError(RuntimeError):
    """Raised when a run is attempted with a file that has not been
    normalized into a usable canonical frame. This is the structural
    guarantee that the engine never sees a raw uploaded file: the runner
    calls `load_canonical_pair()`, which refuses to hand over anything that
    isn't a confirmed, fully-mapped canonical DataFrame."""


def _canonical_frame_for(
    client: str, period: Optional[str], source_type: str, filename: str,
    *, recon_type: Optional[str], client_id: Optional[int], actor: str, db_path=None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Return (canonical frame, caveats) for one selected file, normalizing
    it on demand if it hasn't been normalized yet.

    Raises IngestionBlockedError ONLY for genuinely unusable data — a file that
    is the wrong type or isn't financial data at all. A file that is missing
    optional or even required columns is NOT blocked: the engine gets whatever
    was mapped, and the caveats explain which checks the report will not be
    able to make. A partial report that states its own limits beats a dead end.
    """
    stored = get_stored_result(client, period, source_type, filename, db_path=db_path)
    if stored is not None and stored.get("status") in (STATUS_OK, STATUS_PARTIAL, STATUS_BLOCKED):
        # Already normalized — re-derive the frame from the file using the
        # stored mapping, so the engine gets exactly what the user confirmed.
        result = _renormalize_from_stored(
            client, period, source_type, filename, stored,
            recon_type=recon_type, client_id=client_id, actor=actor, db_path=db_path,
        )
    else:
        from src.data_paths import source_data_path

        path = source_data_path(client, period, source_type) / filename
        if not path.exists():
            raise IngestionBlockedError(f"Source file not found: {path}")
        result = normalize_source_file(
            path, source_type, client, period, client_id=client_id,
            recon_type=recon_type, actor=actor, db_path=db_path,
        )

    # ONLY genuinely wrong data stops the run. Everything else becomes a
    # stated caveat on a partial report.
    if result.status in (STATUS_WRONG_SLOT, STATUS_UNRECOGNIZED):
        raise IngestionBlockedError(
            result.message
            or f"{_SLOT_LABELS.get(source_type, source_type)} doesn't look like reconciliation data."
        )
    if result.canonical_df is None or result.canonical_df.empty:
        raise IngestionBlockedError(
            f"{_SLOT_LABELS.get(source_type, source_type)} ({filename}) produced no usable rows."
        )
    return result.canonical_df, list(result.caveats or [])


def _renormalize_from_stored(
    client: str, period: Optional[str], source_type: str, filename: str, stored: dict[str, Any],
    *, recon_type: Optional[str], client_id: Optional[int], actor: str, db_path=None,
) -> IngestionResult:
    """Rebuild the canonical frame from a stored result's mapping, without
    re-calling the model. Used on every run so the engine consumes exactly
    the mapping the user saw and confirmed."""
    from src.data_paths import source_data_path

    path = source_data_path(client, period, source_type) / filename
    if not path.exists():
        raise IngestionBlockedError(f"Source file not found: {path}")

    read = read_raw_with_header_detection(path)
    mappings = [
        FieldMapping(
            canonical_field=m["canonical_field"],
            source_column=m.get("source_column"),
            confidence=m.get("confidence"),
            reason=m.get("reason") or "",
            required=m.get("required", False),
            from_cache=True,
        )
        for m in stored.get("mapping", [])
    ]
    canonical_df, warnings = apply_mapping(read.df, mappings, source_type, filename)
    required = required_fields_for(source_type, recon_type)
    unmapped_required = [
        f for f in required
        if not any(m.canonical_field == f and m.mapped for m in mappings)
    ]
    unmapped_optional = [m.canonical_field for m in mappings if not m.required and not m.mapped]
    # Required-field gaps are a caveat, not a block — see the note in
    # normalize_source_file(). `blocked` is retained only for a stored result
    # written by an older build, where it genuinely meant "cannot proceed".
    status = STATUS_PARTIAL if (unmapped_required or unmapped_optional) else STATUS_OK
    result = IngestionResult(
        source_type=source_type, filename=filename, client=client, period=period,
        status=status, canonical_df=canonical_df, field_mappings=mappings,
        unmapped_required_fields=unmapped_required,
        row_count_in=len(read.df), row_count_out=len(canonical_df),
        warnings=list(stored.get("warnings") or []) + warnings,
        headers=stored.get("headers") or [], header_row=stored.get("header_row"),
        sheet_name=stored.get("sheet_name"), sheet_ambiguous=bool(stored.get("sheet_ambiguous")),
        classification=stored.get("classification"), model_used=stored.get("model_used"),
        llm_cached=True, header_signature="",
    )
    result.caveats = result.build_caveats(recon_type)
    return result


def load_canonical_pair(
    client: str,
    period: str,
    recon_type: str,
    selected_files: Optional[dict[str, str]] = None,
    *,
    client_id: Optional[int] = None,
    actor: str = "system",
    db_path=None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str], list[dict[str, Any]]]:
    """Return (books_df, portal_df, source_files, caveats) as CANONICAL frames.

    This is the runner's ingestion entry point. Every frame returned here
    has been through `normalize_source_file()` — the engine never receives
    a raw uploaded file.

    Raises IngestionBlockedError only when the run genuinely cannot proceed:
    a selected file is the wrong type / not financial data, or there is no
    books side at all (nothing to reconcile against). A missing portal source
    or missing columns is NOT fatal — the run proceeds and returns caveats
    describing what couldn't be checked, so the report can state its own
    limits.
    """
    from src.data_paths import source_data_path

    sel = dict(selected_files or {})
    caveats: list[dict[str, Any]] = []

    def _pick(source_types: list[str]) -> Optional[tuple[str, str]]:
        for st in source_types:
            fn = sel.get(st)
            if fn and (source_data_path(client, period, st) / fn).exists():
                return st, fn
        return None

    if recon_type == "GST":
        books_sources, portal_sources = ["tally"], ["gstr2b", "ims"]
    elif recon_type == "TDS":
        books_sources, portal_sources = ["tally"], ["form26as", "tds"]
    else:
        from src.module2.service import OTHER_SOURCES

        books_sources = ["tally"]
        # The runner pins the chosen 2C source via "__source_key" so a run
        # reconciles exactly one source at a time against the books side.
        pinned = sel.pop("__source_key", None)
        portal_sources = [pinned] if pinned else [key for key, _label in OTHER_SOURCES]

    books_pick = _pick(books_sources)
    if books_pick is None:
        # Without a books side there is nothing to reconcile against — this is
        # the one missing-source case that genuinely cannot produce a report.
        raise IngestionBlockedError(
            f"No books-side file selected for {client}/{period}. "
            "Choose your internal books file on the Upload screen."
        )
    portal_pick = _pick(portal_sources)

    books_df, books_caveats = _canonical_frame_for(
        client, period, books_pick[0], books_pick[1], recon_type=recon_type,
        client_id=client_id, actor=actor, db_path=db_path,
    )
    caveats.extend(books_caveats)

    if portal_pick is None:
        # No portal side: reconcile the books against nothing. The engine still
        # produces a report — every books record classifies as "Not in Portal",
        # which is factually what is known — and the caveat says the portal
        # comparison itself was never made. This is materially different from
        # the run failing, and the report must say so.
        portal_df = books_df.iloc[0:0].copy()
        caveats.append({
            "code": "no_portal_source",
            "label": "No portal file",
            "detail": (
                "No portal-side file was provided, so nothing could be matched against "
                "the books. Every books record is reported as 'Not in Portal', and the "
                "portal-side checks (missing in books, amount differences) were not run."
            ),
            "fields": [],
            "source_type": None,
            "filename": None,
        })
    else:
        portal_df, portal_caveats = _canonical_frame_for(
            client, period, portal_pick[0], portal_pick[1], recon_type=recon_type,
            client_id=client_id, actor=actor, db_path=db_path,
        )
        caveats.extend(portal_caveats)

    source_files = sorted(
        {books_pick[1]} | ({portal_pick[1]} if portal_pick else set())
    )
    return books_df, portal_df, source_files, caveats