"""Reconcile → Review — deterministic derivation (approval-grade, read-only).

Everything here is DERIVED from existing ``match_results`` records and the C1
rules the engine already used. Nothing in this module:

* changes matching, classification or confidence,
* changes the data model,
* changes review-state semantics (Mark reviewed / Clear review / note).

It exists so the *presentation* layer can explain, group and format what the
engine already decided — and so every figure the UI renders comes from one
deterministic service instead of being computed inline in a component.

The one deliberate naming choice: causes are DISPLAY GROUPINGS only. They are
worded with "Likely …" and never change classification, priority or routing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Label mapping — the ONE dictionary of field names. No raw keys reach the UI.
# ---------------------------------------------------------------------------

FIELD_LABELS: dict[str, str] = {
    # identity
    "gstin": "GSTIN",
    "party_name": "Supplier",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "pan": "PAN",
    "deductee_name": "Deductee",
    "section": "Section",
    "challan_number": "Challan number",
    "certificate_number": "Certificate number",
    "deposit_date": "Deposit date",
    "reference": "Reference",
    "date": "Date",
    "narration": "Narration",
    # amounts
    "taxable_value": "Taxable value",
    "igst": "IGST",
    "cgst": "CGST",
    "sgst": "SGST",
    "cess": "Cess",
    "rounding_adjustment": "Rounding adjustment",
    "total_tax": "Total tax",
    "invoice_value": "Invoice value",
    "amount_paid_credited": "Amount paid / credited",
    "tax_deducted": "Tax deducted",
    "tax_deposited": "Tax deposited",
    "amount": "Amount",
}

# Fields the UI should NEVER render as a comparison row (raw passthrough).
_HIDDEN_FIELDS = {"original_row", "source_file", "source_type", "_doc_type", "_source_sheet"}

_IDENTITY_KEYS = {"gstin", "party_name", "invoice_number", "invoice_date", "pan", "deductee_name",
                  "section", "challan_number", "certificate_number", "deposit_date", "reference",
                  "date", "narration"}
_AMOUNT_KEYS = {"taxable_value", "igst", "cgst", "sgst", "cess", "rounding_adjustment",
                "total_tax", "invoice_value", "amount_paid_credited", "tax_deducted",
                "tax_deposited", "amount"}

# The universe of fields that BELONG to each recon type. A field outside this
# set is never shown, even when it carries a value — this is what keeps the
# TDS-only columns (pan, section, tax_deducted, …) out of a GST view when the
# books export carries the GST∪TDS union schema.
FIELD_UNIVERSE: dict[str, set[str]] = {
    "GST": {
        "gstin", "party_name", "invoice_number", "invoice_date", "taxable_value",
        "igst", "cgst", "sgst", "cess", "rounding_adjustment", "total_tax",
        "invoice_value", "place_of_supply", "supply_attract_reverse_charge",
        "document_type", "reverse_charge", "note_number",
    },
    "TDS": {
        "deductee_name", "pan", "section", "challan_number", "deposit_date",
        "certificate_number", "amount_paid_credited", "tax_deducted",
        "tax_deposited", "date_of_payment", "tds_rate",
    },
    "OTHER": {"reference", "party_name", "date", "amount", "narration"},
}

# Ordered, per-recon-type field specs. Only these are shown (plus any extra key
# actually present on a side, appended sotto "Other").
FIELD_SPECS: dict[str, list[tuple[str, str, str]]] = {
    "GST": [
        ("gstin", "Identity", "GSTIN"),
        ("party_name", "Identity", "Supplier"),
        ("invoice_number", "Identity", "Invoice number"),
        ("invoice_date", "Identity", "Invoice date"),
        ("taxable_value", "Amounts", "Taxable value"),
        ("igst", "Amounts", "IGST"),
        ("cgst", "Amounts", "CGST"),
        ("sgst", "Amounts", "SGST"),
        ("cess", "Amounts", "Cess"),
        ("rounding_adjustment", "Amounts", "Rounding adjustment"),
        ("total_tax", "Amounts", "Total tax"),
        ("invoice_value", "Amounts", "Invoice value"),
    ],
    "TDS": [
        ("deductee_name", "Identity", "Deductee"),
        ("pan", "Identity", "PAN"),
        ("section", "Identity", "Section"),
        ("challan_number", "Identity", "Challan number"),
        ("deposit_date", "Identity", "Deposit date"),
        ("certificate_number", "Identity", "Certificate number"),
        ("amount_paid_credited", "Amounts", "Amount paid / credited"),
        ("tax_deducted", "Amounts", "Tax deducted"),
        ("tax_deposited", "Amounts", "Tax deposited"),
    ],
    "OTHER": [
        ("reference", "Identity", "Reference"),
        ("party_name", "Identity", "Supplier"),
        ("date", "Identity", "Date"),
        ("amount", "Amounts", "Amount"),
        ("narration", "Identity", "Narration"),
    ],
}

# The amount bases a difference can be measured on, per recon type, in
# priority order.
_AMOUNT_BASES_BY_RECON: dict[str, tuple[str, ...]] = {
    "GST": ("taxable_value", "total_tax", "invoice_value"),
    "TDS": ("tax_deducted", "tax_deposited", "amount_paid_credited"),
    "OTHER": ("amount",),
}

# difference_type → the amount base the engine flagged.
_DIFF_BASIS: dict[str, str] = {
    "Taxable Value Difference": "taxable_value",
    "Tax Amount Difference": "total_tax",
    "Tax Split Mismatch": "total_tax",
    "Rate Mismatch": "tax_deducted",
    "Rounding": "invoice_value",
    "Duplicate in Books": "invoice_value",
    "Duplicate in Portal": "invoice_value",
    "Timing Difference": "invoice_value",
    # Multi-factor / type disagreements are measured on the full invoice value
    # so the Difference column is Portal invoice value − Books invoice value
    # (a BSH/515-style row must read the ₹1,180 full gap, not the ₹1,000
    # taxable-only sub-component).
    "Unexplained": "invoice_value",
    "Document Type Mismatch": "invoice_value",
    "Short Deduction": "tax_deducted",
    "Excess Deduction": "tax_deducted",
}

# ---------------------------------------------------------------------------
# Cause vocabulary (display grouping only — never routing)
# ---------------------------------------------------------------------------

CAUSE_LABELS: dict[str, str] = {
    "likely_ingestion_gap": "Likely data-mapping gap",
    "rounding_only": "Rounding only",
    "below_materiality": "Below materiality",
    "value_difference": "Value difference",
    "document_type_mismatch": "Document type mismatch",
    "timing_difference": "Timing difference",
    "missing_in_books": "Not in books",
    "missing_in_portal": "Not in portal",
}

# The two groups that must be visible at a glance.
CAUSE_GROUPS: dict[str, list[str]] = {
    "gap": ["likely_ingestion_gap"],
    "judgement": ["rounding_only", "below_materiality", "value_difference",
                  "document_type_mismatch", "timing_difference",
                  "missing_in_books", "missing_in_portal"],
}
CAUSE_GROUP_LABELS = {
    "gap": "Likely data-mapping gap",
    "judgement": "Needs accountant judgement",
}
CAUSE_GROUP_HINTS = {
    "gap": "Books did not carry the tax split — fix the mapping, not the entry.",
    "judgement": "Real differences and missing entries a person has to decide on.",
}

# Cause → Foundation semantic role (charts). No new hex is ever introduced.
CAUSE_COLOR_ROLE: dict[str, str] = {
    "likely_ingestion_gap": "ai",
    "rounding_only": "neutral",
    "below_materiality": "neutral",
    "value_difference": "danger",
    "document_type_mismatch": "ai",
    "timing_difference": "ai",
    "missing_in_books": "danger",
    "missing_in_portal": "accent",
}

CLASSIFICATION_COLOR_ROLE: dict[str, str] = {
    "Matched": "rule",
    "Amount Difference": "ai",
    "Not in Books": "danger",
    "Not in Portal": "accent",
    "Missing": "danger",
    "Late Deposit": "accent",
}

GST_BUCKETS = ["Matched", "Amount Difference", "Not in Books", "Not in Portal"]
_TDS_BUCKET_OF = {
    "Matched": "Matched",
    "Amount Difference": "Amount Difference",
    "Not in Books": "Missing",
    "Not in Portal": "Late Deposit",
}

DEFAULT_MATERIALITY = 10000.0
DEFAULT_TOLERANCE = 10.0
DEFAULT_ROUNDING_TOLERANCE = 2.0

# Does Module 8's materiality gate apply to BULK REVIEW MARKING?
#
# Module 8 DOES gate bulk actions by materiality — but its gate sits on batch
# EXCEPTION RESOLUTION (`action_center.eligible_for_batch` →
# `module2.resolve_exception(accepted)`), which is a financial decision about
# an exception. Marking a record *reviewed* is review-state bookkeeping, not a
# financial decision: the per-item "Mark reviewed" control has never been
# materiality-gated, and the build prompt requires review-state semantics to
# be UNCHANGED.
#
# So the same rule is applied by NOT gating review marking (otherwise per-item
# and bulk review would diverge), and the "add no new gating" branch is taken.
# The flag is explicit so this decision is inspectable rather than implicit.
BULK_REVIEW_MATERIALITY_GATED = False

# Above this many selected rows the bulk action is disabled with a reason —
# a real, always-visible guard (disabled-with-inline-reason, 3.4).
BULK_SELECTION_CAP = 500

# ---------------------------------------------------------------------------
# Formatting — Indian grouping, en-IN digits, "24 Aug 2026"
# ---------------------------------------------------------------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_UNAVAILABLE = "—"


def to_number(v: Any) -> Optional[float]:
    """Coerce a record value to a float, or None. Tolerant of "₹1,234.50",
    "1234", 2.0, 1_234.5 and the empty/NaN sentinels real exports carry."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f  # NaN guard
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "nat", "-", "null"):
        return None
    s = s.replace(",", "").replace("₹", "").replace("$", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def format_money(v: Any, *, dash: str = _UNAVAILABLE) -> str:
    """₹ with Indian digit grouping (en-IN) and 2 decimals.

    ``0.0`` and ``0`` both render as "₹0.00" — the tolerant numeric compare is
    what stops them being reported as a difference, not the display.
    """
    n = to_number(v)
    if n is None:
        return dash
    neg = n < 0
    # NOTE: format WITHOUT a comma first — Python's "," uses Western grouping
    # (thousands), which is not what an Indian user expects.
    whole, frac = f"{abs(n):.2f}".split(".")
    if len(whole) > 3:
        last3, rest = whole[-3:], whole[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        whole = ",".join(groups) + "," + last3
    out = f"₹{whole}.{frac}"
    return f"−{out}" if neg else out


def format_signed_money(v: Any) -> str:
    """A signed difference for the table: +₹x / −₹x, "—" when zero/absent."""
    n = to_number(v)
    if n is None:
        return _UNAVAILABLE
    n = round(n, 2)
    if n == 0:
        return _UNAVAILABLE
    body = format_money(abs(n))
    return f"+{body}" if n > 0 else f"−{body}"


def format_date(v: Any) -> str:
    """ISO / dd-mm-yyyy / dd/mm/yyyy → "24 Aug 2026". Anything else is passed
    through trimmed (never fabricated)."""
    if v in (None, ""):
        return _UNAVAILABLE
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return _UNAVAILABLE
    date_part = s.split("T")[0].split(" ")[0]
    for sep in ("-", "/"):
        bits = date_part.split(sep)
        if len(bits) == 3 and all(b.isdigit() for b in bits):
            a, b, c = bits
            if len(a) == 4:
                y, m, d = int(a), int(b), int(c)
            else:
                d, m, y = int(a), int(b), int(c)
            if 1 <= m <= 12 and 1 <= d <= 31:
                return f"{d:02d} {_MONTHS[m - 1]} {y:04d}"
    return s


def format_period(period: str) -> str:
    """"2026-08" → "Aug 2026". Anything else is passed through."""
    s = str(period or "")
    bits = s.split("-")
    if len(bits) == 2 and bits[0].isdigit() and bits[1].isdigit() and 1 <= int(bits[1]) <= 12:
        return f"{_MONTHS[int(bits[1]) - 1]} {bits[0]}"
    return s or _UNAVAILABLE


def format_confidence(band: str, score: Any = None) -> str:
    """Short, honest label for the match-confidence badge."""
    try:
        s = int(float(score))
    except (TypeError, ValueError):
        s = None
    if s is not None:
        return f"{band or 'Match'} — {s}%"
    return band or "Match"


def clean(v: Any) -> str:
    """pandas NaN is truthy — never let the literal "nan" reach a label."""
    if v is None:
        return ""
    if isinstance(v, float) and v != v:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def load_record(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return record
    if not record:
        return {}
    try:
        out = json.loads(record)
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Record value helpers
# ---------------------------------------------------------------------------

def component_tax_total(record: dict[str, Any]) -> float:
    """Total tax for a record: the stored total_tax when present, else the sum
    of the tax components. Mirror of the engine's own helper."""
    if not record:
        return 0.0
    tt = to_number(record.get("total_tax"))
    if tt:
        return abs(tt)
    return sum(abs(to_number(record.get(k)) or 0.0) for k in ("cgst", "sgst", "igst", "cess"))


def basis_value(record: dict[str, Any], basis: str) -> float:
    if not record:
        return 0.0
    if basis == "total_tax":
        return component_tax_total(record)
    return abs(to_number(record.get(basis)) or 0.0)


def item_value(record: dict[str, Any], recon_type: str) -> float:
    """The money value a record stands for, per recon type."""
    if not record:
        return 0.0
    if recon_type == "TDS":
        for k in ("amount_paid_credited", "tax_deducted", "tax_deposited"):
            v = to_number(record.get(k))
            if v:
                return abs(v)
        return 0.0
    if recon_type == "OTHER":
        for k in ("amount", "invoice_value"):
            v = to_number(record.get(k))
            if v:
                return abs(v)
        return 0.0
    for k in ("invoice_value", "taxable_value"):
        v = to_number(record.get(k))
        if v:
            return abs(v)
    return component_tax_total(record)


def party_of(record: dict[str, Any]) -> str:
    for k in ("party_name", "deductee_name", "vendor_name", "supplier_name", "name", "party"):
        v = clean(record.get(k))
        if v:
            return v
    return ""


def gstin_of(record: dict[str, Any]) -> str:
    for k in ("gstin", "gstin_of_supplier", "pan"):
        v = clean(record.get(k))
        if v:
            return v
    return ""


def reference_of(record: dict[str, Any]) -> str:
    for k in ("invoice_number", "challan_number", "voucher_number", "reference", "note_number"):
        v = clean(record.get(k))
        if v:
            return v
    return ""


def date_of(record: dict[str, Any]) -> str:
    for k in ("invoice_date", "deposit_date", "date", "voucher_date"):
        v = clean(record.get(k))
        if v:
            return v
    return ""


def difference_basis(difference_type: str) -> str:
    return _DIFF_BASIS.get(clean(difference_type), "")


def item_difference(
    classification: str,
    difference_type: str,
    books: dict[str, Any],
    portal: dict[str, Any],
    recon_type: str,
) -> tuple[float, str]:
    """The flagged money difference for one item, and the basis it is on.

    * Not in Books  → the portal's own value (it was never seen in the books).
    * Not in Portal → the books' own value.
    * Amount Difference → the amount base the engine named, falling back to the
      largest disagreing basis so the figure always matches the issue label.
    """
    c = clean(classification)
    if c == "Not in Books":
        return round(item_value(portal, recon_type), 2), "portal value"
    if c == "Not in Portal":
        return round(item_value(books, recon_type), 2), "books value"

    bases = _AMOUNT_BASES_BY_RECON.get(recon_type, _AMOUNT_BASES_BY_RECON["GST"])
    named = difference_basis(difference_type)
    if named in bases:
        d = abs(basis_value(books, named) - basis_value(portal, named))
        if d > 0:
            return round(d, 2), named
    best_basis, best = "", 0.0
    for basis in bases:
        d = abs(basis_value(books, basis) - basis_value(portal, basis))
        if d > best:
            best, best_basis = d, basis
    if best:
        return round(best, 2), best_basis
    return round(abs(item_value(books, recon_type) - item_value(portal, recon_type)), 2), bases[0]


# ---------------------------------------------------------------------------
# CAUSE RULES — the pure, deterministic function (never AI)
# ---------------------------------------------------------------------------

def classify_cause(
    classification: str,
    difference_type: str,
    books: dict[str, Any],
    portal: dict[str, Any],
    *,
    difference: Optional[float] = None,
    recon_type: str = "GST",
    tolerance: float = DEFAULT_TOLERANCE,
    rounding_tolerance: float = DEFAULT_ROUNDING_TOLERANCE,
    materiality: float = DEFAULT_MATERIALITY,
) -> str:
    """Classify one item into a display-only cause.

    * likely_ingestion_gap — books taxable value AND total tax are both zero,
      and the books invoice total ties to the portal within C1 tolerance. The
      signature of a GST books export that did not carry the tax split. (GST
      only — TDS records have no taxable value, so the signature cannot apply.)
    * document_type_mismatch / timing_difference — a NON-amount disagreement.
      It gets its own cause because its value gap is typically nil, so calling
      it a value difference mislabels a zero-gap row (OFH/12).
    * rounding_only        — |difference| <= rounding tolerance.
    * below_materiality    — a small difference the engine could NOT otherwise
      characterise (no difference_type). A difference it HAS characterised is a
      real value difference and is reported as one however small — otherwise a
      genuine ₹1,180 gap (BSH/515, Unexplained) is grouped away from the rows
      it belongs with.
    * value_difference     — any other Amount Difference.
    * missing_in_books / missing_in_portal — straight from the classification.

    Pure and total: no I/O, no globals beyond the constants, and every branch
    is covered by ``tests/test_review_cause.py``.
    """
    c = clean(classification)
    if c == "Not in Books":
        return "missing_in_books"
    if c == "Not in Portal":
        return "missing_in_portal"

    # A NON-AMOUNT disagreement gets its own cause. Folding it into "value
    # difference" tagged a row whose value gap was nil as if it were a value
    # gap, and left the genuine value gap in a different bucket.
    dt = clean(difference_type)
    if dt == "Document Type Mismatch":
        return "document_type_mismatch"
    if dt == "Timing Difference":
        return "timing_difference"

    if difference is None:
        difference, _ = item_difference(c, difference_type, books, portal, recon_type)
    diff = abs(float(difference or 0.0))

    if recon_type == "GST":
        books_taxable = abs(to_number((books or {}).get("taxable_value")) or 0.0)
        books_tax = component_tax_total(books or {})
        invoice_gap = abs(
            basis_value(books or {}, "invoice_value") - basis_value(portal or {}, "invoice_value")
        )
        if books_taxable == 0 and books_tax == 0 and invoice_gap <= tolerance:
            return "likely_ingestion_gap"
    if diff <= rounding_tolerance:
        return "rounding_only"
    if not dt and diff < materiality:
        return "below_materiality"
    return "value_difference"


def cause_group(cause: str) -> str:
    return "gap" if cause in CAUSE_GROUPS["gap"] else "judgement"


# ---------------------------------------------------------------------------
# Comparison table (field | books | portal | difference)
# ---------------------------------------------------------------------------

@dataclass
class ComparisonField:
    key: str
    label: str
    section: str        # "Identity" | "Amounts" | "Other"
    books: str
    portal: str
    difference: str
    differs: bool
    is_material: bool
    overridden: bool = False


def _pretty_key(key: str) -> str:
    return FIELD_LABELS.get(key) or key.replace("_", " ").capitalize()


def _normalize_compare(v: Any) -> str:
    """Normalize a value for an equality check: numbers compare numerically
    (so 0 == 0.0 and 26066 == 26066.0), everything else compares as trimmed
    text with dates normalized."""
    n = to_number(v)
    if n is not None:
        return f"{n:.4f}"
    s = clean(v)
    if s and any(ch.isdigit() for ch in s):
        d = format_date(s)
        if d != s:
            return d
    return s


def compare_fields(
    books: dict[str, Any],
    portal: dict[str, Any],
    recon_type: str,
    *,
    materiality: float = DEFAULT_MATERIALITY,
    tolerance: float = DEFAULT_TOLERANCE,
    overridden: set[str] | None = None,
    include_all: bool = False,
) -> list[ComparisonField]:
    """Build the Field | Books | Portal | Difference rows.

    * Only fields relevant to the recon type are ordered first (the spec list);
      any other in-universe key actually present is appended under "Other".
    * By default a field empty on BOTH sides is omitted entirely — that is how
      the TDS-only fields disappear from a GST item. ``include_all=True``
      (the "Show all fields" toggle) keeps them.
    * A numeric field only reads as a DIFFERENCE when |books − portal| exceeds
      the C1 tolerance (so 0 vs 0.0 and 26066 vs 26066.0 are NOT flagged).
      Non-numeric fields differ only when their normalized text differs.
    * ``is_material`` marks the ones at/above the C1 materiality threshold.
    """
    overridden = overridden or set()
    books = books or {}
    portal = portal or {}

    universe = FIELD_UNIVERSE.get(recon_type, FIELD_UNIVERSE["GST"])
    spec = FIELD_SPECS.get(recon_type, FIELD_SPECS.get("GST", []))
    ordered: list[tuple[str, str, str]] = list(spec)
    seen = {k for k, _, _ in ordered}

    # Any field OUTSIDE the recon type's universe is ignored entirely — that is
    # how empty (and populated) TDS-only columns disappear from a GST view.
    # Within the universe, extras are appended so nothing mapped is silently
    # dropped.
    extra_keys: list[str] = []
    for record in (books, portal):
        for k in record:
            if k in _HIDDEN_FIELDS or k in seen or str(k).startswith("_"):
                continue
            if k not in universe:
                continue
            if k not in extra_keys:
                extra_keys.append(k)
    for k in extra_keys:
        section = "Amounts" if k in _AMOUNT_KEYS else ("Identity" if k in _IDENTITY_KEYS else "Other")
        ordered.append((k, section, _pretty_key(k)))

    rows: list[ComparisonField] = []
    for key, section, label in ordered:
        b_raw = books.get(key)
        p_raw = portal.get(key)
        b_num = to_number(b_raw)
        p_num = to_number(p_raw)
        is_amount = key in _AMOUNT_KEYS

        b_disp = clean(b_raw)
        p_disp = clean(p_raw)

        if is_amount and (b_num is not None or p_num is not None):
            b_disp = format_money(b_num) if b_num is not None else _UNAVAILABLE
            p_disp = format_money(p_num) if p_num is not None else _UNAVAILABLE
            delta = (b_num or 0.0) - (p_num or 0.0)
            # Numeric compare with the C1 tolerance: 0 == 0.0, 26066 == 26066.0.
            differs = abs(delta) > tolerance
            diff_disp = format_signed_money(delta) if differs else _UNAVAILABLE
            material = differs and abs(delta) >= materiality
        else:
            if section == "Identity" and key.endswith("date"):
                b_disp = format_date(b_disp) if b_disp else _UNAVAILABLE
                p_disp = format_date(p_disp) if p_disp else _UNAVAILABLE
            b_disp = b_disp or _UNAVAILABLE
            p_disp = p_disp or _UNAVAILABLE
            # Identity text must match exactly; a field present on only one side
            # is not a "difference" here (it is a missing side, already surfaced
            # by the classification), so it is shown without a Differs label.
            if clean(b_raw) == "" or clean(p_raw) == "":
                differs = False
            else:
                differs = _normalize_compare(b_raw) != _normalize_compare(p_raw)
            diff_disp = "Differs" if differs else _UNAVAILABLE
            material = False

        # Hide a field that is empty on both sides, unless "Show all fields".
        both_empty = (b_disp == _UNAVAILABLE) and (p_disp == _UNAVAILABLE) and b_num is None and p_num is None
        if both_empty and not include_all:
            continue

        overridden_here = key in overridden
        rows.append(ComparisonField(
            key=key,
            label=label,
            section=section,
            books=b_disp,
            portal=p_disp,
            difference=diff_disp,
            differs=bool(differs),
            is_material=bool(material),
            overridden=overridden_here,
        ))
    return rows


def true_differences(rows: list[ComparisonField]) -> list[ComparisonField]:
    return [r for r in rows if r.differs]


# ---------------------------------------------------------------------------
# Verdict + suggested next step
# ---------------------------------------------------------------------------

def verdict_text(item: dict[str, Any]) -> str:
    """One plain sentence, from a deterministic template per cause."""
    cause = item.get("cause", "")
    party = item.get("party") or "this supplier"
    inv = item.get("reference") or "this invoice"
    invoice_value = format_money(item.get("portal_value") or item.get("books_value"))
    diff = format_money(item.get("difference"))

    if cause == "likely_ingestion_gap":
        return (
            f"Books has no taxable value or tax recorded for {inv}, but the invoice total "
            f"({invoice_value}) agrees with the portal. This usually means the books export "
            "did not carry the tax split, not a real difference."
        )
    if cause == "rounding_only":
        return (
            f"Books and the portal agree to the rupee; only a rounding residual of {diff} "
            "remains. Nothing to correct unless your rounding policy says otherwise."
        )
    if cause == "below_materiality":
        return (
            f"The difference of {diff} is below the materiality threshold set in Rules, so it "
            "does not change the reconciliation position. Record it only if you want a nil-effect note."
        )
    if cause == "value_difference":
        return (
            f"Books and the portal disagree by {diff} on {party} · {inv}. This needs an "
            "accountant's judgement before filing."
        )
    if cause == "document_type_mismatch":
        return (
            f"The amounts on {inv} from {party} agree, but the document type does not — the "
            "supplier filed it under a different supply type from the one booked. The tax "
            "treatment follows the portal's type, so this needs a judgement before filing."
        )
    if cause == "timing_difference":
        return (
            f"{inv} from {party} is in a different period on the two sides. This is usually a "
            "timing difference rather than an error, but it moves the period the ITC falls in."
        )
    if cause == "missing_in_books":
        return (
            f"The portal shows {inv} from {party} worth {invoice_value}, but no matching entry "
            "was found in the books across any pass. This usually means it is booked under "
            "another ledger, or not booked at all."
        )
    if cause == "missing_in_portal":
        return (
            f"Books records {inv} from {party} worth {invoice_value}, but the supplier's "
            "invoice was not found in the portal file. This usually means the supplier has not "
            "filed it yet, or it falls under reverse charge."
        )
    return f"{party} · {inv} needs a look."


_NEXT_STEPS: dict[str, dict[str, str]] = {
    "likely_ingestion_gap": {
        "text": "Check how the Purchase Register tax columns were mapped in Format Registry, then re-run.",
        "link": "/format-registry",
        "link_label": "Open Format Registry",
    },
    "value_difference": {
        "text": "Compare with the supplier invoice.",
        "badge": "Module 3",
        "badge_text": "Corrective entry coming with Module 3",
    },
    "rounding_only": {
        "text": "No action needed unless your rounding policy requires a written-off residual.",
        "badge": "Module 3",
        "badge_text": "Corrective entry coming with Module 3",
    },
    "below_materiality": {
        "text": "No action needed — the difference is below the materiality threshold.",
        "badge": "Module 3",
        "badge_text": "Corrective entry coming with Module 3",
    },
    "document_type_mismatch": {
        "text": "Check the supply type filed in the portal against the voucher type in the books.",
        "badge": "Module 3",
        "badge_text": "Corrective entry coming with Module 3",
    },
    "timing_difference": {
        "text": "Confirm the period the document belongs to before claiming the ITC.",
        "badge": "Module 3",
        "badge_text": "Corrective entry coming with Module 3",
    },
    "missing_in_books": {
        "text": "Confirm it is booked under another ledger, or ask the supplier.",
        "badge": "Module 1",
        "badge_text": "Information request coming with Module 1",
    },
    "missing_in_portal": {
        "text": "Check whether the supplier has filed, or RCM applies.",
        "badge": "Module 1",
        "badge_text": "Information request coming with Module 1",
    },
}


def next_step(cause: str) -> dict[str, str]:
    return dict(_NEXT_STEPS.get(cause, {"text": "Review the record and decide the treatment."}))


# ---------------------------------------------------------------------------
# Data Quality & Scope Notes
# ---------------------------------------------------------------------------

@dataclass
class QualityNote:
    key: str
    title: str
    what: str
    why: str
    how: str
    link: str = ""
    link_label: str = ""


def data_quality_notes(
    items: list[dict[str, Any]],
    *,
    recon_type: str,
    source_files: list[str],
    caveats: list[dict[str, Any]] | None = None,
    run_notes: list[dict[str, Any]] | None = None,
) -> list[QualityNote]:
    """Run-specific, deterministic notes — each with what / why / how to fix.
    Non-applicable notes are simply not produced.

    `run_notes` DECLARE what was deliberately not reconciled (portal credit
    notes, IMS coverage) — distinct from `caveats`, which describe checks that
    could not run. Both are surfaced so nothing is silently omitted (D7).
    """
    notes: list[QualityNote] = []
    total = len(items)
    if not total:
        return notes

    gap_items = [i for i in items if i.get("cause") == "likely_ingestion_gap"]
    if gap_items:
        n = len(gap_items)
        notes.append(QualityNote(
            key="books_zero_tax",
            title=f"Books taxable value is zero on {n} of {total} items",
            what=(
                f"{n} items carry a taxable value and total tax of ₹0.00 in the books, while the "
                "portal shows real taxable value and tax on the same invoices."
            ),
            why=(
                "The invoice totals tie on both sides, so this is the books export not carrying "
                "the tax split — not a real difference in what was purchased. Counting it as a "
                "value difference would overstate what needs attention."
            ),
            how=(
                "Check how the Purchase Register's taxable-value and tax columns were mapped in "
                "Format Registry, then re-run the reconciliation."
            ),
            link="/format-registry",
            link_label="Open Format Registry",
        ))

    rounding_rows = [
        i for i in items
        if abs(to_number(i.get("rounding_adjustment")) or 0.0) > 0.004
    ]
    if rounding_rows:
        n = len(rounding_rows)
        notes.append(QualityNote(
            key="rounding_adjustments",
            title=f"Rounding adjustments on {n} invoice(s)",
            what=(
                f"{n} source rows carry a non-zero rounding/round-off residual. The engine keeps "
                "it out of the taxable and tax totals and folds it into the invoice value."
            ),
            why="Rounding residuals are normal in Indian invoices but a persistent pattern can hide a mapping offset.",
            how="No action needed unless the residuals are large or one-sided; if so, check the 'Other Amt.' column mapping.",
        ))

    if recon_type != "TDS":
        notes.append(QualityNote(
            key="tds_not_reconciled",
            title="TDS is not reconciled in this run",
            what="This run reconciled GST only. No Form 26AS or TDS return was part of it.",
            why="Deduction, deposit and 26AS reflection cannot be checked without those files, so any TDS position is unverified.",
            how="Upload the Form 26AS and TDS source files and run a TDS reconciliation.",
            link="/reconcile",
            link_label="Start a TDS reconciliation",
        ))

    no_books = [i for i in items if i.get("cause") == "missing_in_books"]
    if no_books:
        notes.append(QualityNote(
            key="no_books_side",
            title=f"{len(no_books)} record(s) have no books side",
            what="These were seen only on the portal, so no books value could be compared.",
            why="They widen the difference without a counterpart amount to net against.",
            how="Confirm whether they are booked under another ledger or genuinely missing.",
        ))

    no_portal = [i for i in items if i.get("cause") == "missing_in_portal"]
    if no_portal:
        notes.append(QualityNote(
            key="no_portal_side",
            title=f"{len(no_portal)} record(s) have no portal side",
            what="These were seen only in the books, so no portal value could be compared.",
            why="They may be unfiled supplier invoices, or invoices filed in a different period.",
            how="Check whether the supplier has filed, or whether the invoice belongs to another period.",
        ))

    for c in (caveats or []):
        notes.append(QualityNote(
            key=f"caveat_{c.get('code') or c.get('label')}",
            title=c.get("label") or "Check not performed",
            what=c.get("detail") or "",
            why="Without this check the report can only state the limitation, not a conclusion.",
            how="Provide the missing column/file and re-run the reconciliation.",
        ))

    # Run notes (D7) — declarations of what was deliberately NOT reconciled.
    # Each note carries its OWN why/how. A single shared blob previously made
    # the IMS coverage note repeat the credit-note note verbatim — text that
    # describes a missing register, not what IMS status is or requires.
    _RUN_NOTE_TEXT = {
        "credit_notes_not_reconciled": (
            "A reconciliation that silently omits a class of documents would overstate "
            "its own completeness.",
            "Upload the missing source (e.g. a books-side credit/debit-note register) "
            "and re-run if these documents need to be reconciled.",
        ),
        "ims_coverage": (
            "IMS status shows what a supplier has filed and what the recipient has "
            "actioned in the portal — useful context, but it does not drive matching "
            "in this build.",
            "No action is required for this reconciliation; review Pending / No Action "
            "items directly in the IMS portal if needed.",
        ),
    }
    for n in (run_notes or []):
        code = n.get("code") or ""
        why, how = _RUN_NOTE_TEXT.get(code, (
            "This class of document was deliberately left out of this reconciliation, "
            "so the report states the omission rather than implying full coverage.",
            "Treat this as a declared scope limit; include the source in a later run "
            "if these documents need to be reconciled.",
        ))
        notes.append(QualityNote(
            key=f"run_note_{code or n.get('title')}",
            title=n.get("title") or "Not reconciled",
            what=n.get("detail") or "",
            why=why,
            how=how,
        ))

    if source_files:
        notes.append(QualityNote(
            key="source_files",
            title=f"Source files used ({len(source_files)})",
            what=", ".join(str(f) for f in source_files),
            why="The report reconciles exactly these files for this period and nothing else.",
            how="Replace a file and re-run if the source changed.",
        ))

    notes.append(QualityNote(
        key="scope",
        title="What this report covers",
        what="This reconciles the source files above for this period only.",
        why="It does not perform a statutory filing and does not pull live portal data.",
        how="File the return from your filing workflow once the exceptions are decided.",
    ))
    return notes


# ---------------------------------------------------------------------------
# Headline / cause / KPI aggregation
# ---------------------------------------------------------------------------

def drop_zero_slices(slices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove every zero-value segment from a chart dataset (§2).

    A pie/donut segment with ``value = 0`` still draws a stroke, which paints
    a visible sliver for a bucket that does not exist. Filtering here — before
    the data reaches any chart component — is the fix, not a cosmetic patch.
    Also drops entries with no name, so a malformed slice can't render blank.
    """
    out: list[dict[str, Any]] = []
    for s in slices or []:
        if not clean(s.get("name")):
            continue
        if (to_number(s.get("value")) or 0.0) <= 0:
            continue
        out.append(s)
    return out


def _bucket_for(recon_type: str, classification: str) -> str:
    c = clean(classification)
    if recon_type == "TDS" and c in _TDS_BUCKET_OF:
        return _TDS_BUCKET_OF[c]
    return c


def build_review_model(
    run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    materiality: float = DEFAULT_MATERIALITY,
    tolerance: float = DEFAULT_TOLERANCE,
    rounding_tolerance: float = DEFAULT_ROUNDING_TOLERANCE,
    itc_eligible: Optional[float] = None,
    itc_claimed: Optional[float] = None,
    itc_blocked: Optional[float] = None,
    itc_reverse_charge: Optional[float] = None,
) -> dict[str, Any]:
    """Turn raw result rows into the Review screen's full data model.

    ``rows`` are plain dicts with at least: result_id, fingerprint,
    classification, difference_type, confidence_band, confidence_score,
    books_record, portal_record, match_reason, reviewed, reviewer_note,
    overridden_fields. Everything returned here is deterministic.
    """
    recon_type = clean(run.get("recon_type")) or "GST"
    items: list[dict[str, Any]] = []

    for row in rows:
        books = load_record(row.get("books_record"))
        portal = load_record(row.get("portal_record"))
        classification = clean(row.get("classification"))
        difference_type = clean(row.get("difference_type"))
        reviewed = bool(row.get("reviewed"))

        difference, basis = item_difference(classification, difference_type, books, portal, recon_type)
        cause = classify_cause(
            classification, difference_type, books, portal,
            difference=difference,
            recon_type=recon_type,
            tolerance=tolerance,
            rounding_tolerance=rounding_tolerance,
            materiality=materiality,
        )
        combined = {**portal, **books}
        party = party_of(combined) or party_of(portal) or party_of(books)
        gstin = gstin_of(combined)
        reference = reference_of(combined)
        rec_date = date_of(combined)

        # §1 — the TAX this record stands for, independent of its gross value.
        # Portal (GSTR-2B) is the ITC evidence side, so it leads; a books-only
        # record (Not in Portal) falls back to its own tax.
        record_tax = component_tax_total(portal) or component_tax_total(books)

        books_value = item_value(books, recon_type) if books else 0.0
        portal_value = item_value(portal, recon_type) if portal else 0.0

        # ONE plain issue label per row — never a chip farm.
        if cause == "likely_ingestion_gap" and basis == "taxable_value":
            issue = "Taxable value differs"
        elif cause == "likely_ingestion_gap":
            issue = "Tax and taxable value differ"
        elif classification == "Not in Books":
            issue = "Not in books"
        elif classification == "Not in Portal":
            issue = "Not in portal"
        elif difference_type:
            issue = difference_type
        else:
            issue = classification or "Needs review"

        match_key = []
        if gstin:
            match_key.append("GSTIN")
        if reference:
            match_key.append("invoice no.")
        if rec_date and classification == "Amount Difference":
            match_key.append("date")
        match_key_label = " + ".join(match_key) if match_key else "—"

        items.append({
            "result_id": int(row.get("result_id") or 0),
            "fingerprint": clean(row.get("fingerprint")),
            "classification": classification,
            "bucket": _bucket_for(recon_type, classification),
            "difference_type": difference_type,
            "confidence_band": clean(row.get("confidence_band")),
            "confidence_score": int(row.get("confidence_score") or 0),
            "confidence_label": format_confidence(clean(row.get("confidence_band")), row.get("confidence_score")),
            "match_reason": clean(row.get("match_reason")),
            "party": party,
            "gstin": gstin,
            "reference": reference,
            "date": rec_date,
            "date_label": format_date(rec_date),
            "cause": cause,
            "cause_label": CAUSE_LABELS.get(cause, cause),
            "cause_group": cause_group(cause),
            "issue": issue,
            "basis": basis,
            "difference": difference,
            "difference_display": format_signed_money(difference),
            # Value-at-risk semantics (D6) — read from the stored result when
            # present, so the UI never re-derives a different figure.
            "itc_at_risk": round(float(row.get("itc_at_risk") or 0.0), 2),
            "gross_value": round(float(row.get("gross_value") or 0.0), 2),
            "tax": round(record_tax, 2),
            "books_value": round(books_value, 2),
            "portal_value": round(portal_value, 2),
            "books_display": format_money(books_value),
            "portal_display": format_money(portal_value),
            "match_key_label": match_key_label,
            "reviewed": reviewed,
            "review_stale": bool(row.get("review_stale")),
            "reviewer_note": clean(row.get("reviewer_note")),
            "material": difference >= materiality,
            "rounding_adjustment": to_number((combined or {}).get("rounding_adjustment")) or 0.0,
            "supplier_key": party or "Unknown supplier",
            "books": books,
            "portal": portal,
            "_overridden": set(row.get("overridden_fields") or []),
        })

    total = len(items)
    exceptions = [i for i in items if i["bucket"] != "Matched"]
    matched_count = total - len(exceptions)

    attention_value = round(sum(i["difference"] for i in exceptions), 2)
    reviewed_count = sum(1 for i in items if i["reviewed"])

    # ------------------------------------------------------------------
    # §1 — ITC AT STAKE (the headline figure)
    #
    # The money genuinely at risk is the INPUT TAX across EVERY exception
    # bucket (Not in Books + Not in Portal + Amount Difference) — not their
    # gross invoice value, which is far larger and not what a reviewer can
    # reclaim or lose. Gross invoice value stays available (supplier chart,
    # detail table) but is never a top-line number.
    #
    #   itc_at_stake_tax = SUM(tax) over ALL exception rows  ← the headline
    #   period_itc_total = SUM(tax) over every invoice in the run
    #   itc_at_stake_pct = itc_at_stake_tax / period_itc_total
    #
    # The headline MUST equal the sum of the "Split by cause" buckets shown on
    # the same screen. It previously bound to the "Not in Books" bucket alone,
    # so a run whose exposure also sat in Amount-Difference and Not-in-Portal
    # rows printed a headline roughly 1/4 of the real total while the cause bar
    # (and the integrity recount) read the full sum.
    # ------------------------------------------------------------------
    itc_at_stake_tax = round(sum(i["tax"] for i in exceptions), 2)
    period_itc_total = round(sum(i["tax"] for i in items), 2)
    itc_at_stake_pct = (
        round(itc_at_stake_tax / period_itc_total * 100.0, 2) if period_itc_total else 0.0
    )

    # The gross invoice value on the exception items — ONE computation shared
    # by the KPI strip, the integrity footer and the report (which must equal
    # the Excel's SUMIF over the invoice-value column). Each row contributes
    # the SAME single-side invoice value the report renders (portal-first, the
    # 2B being the ITC evidence), never a row's sub-component difference.
    exception_gross_value = round(
        sum((i["portal_value"] or i["books_value"]) for i in exceptions), 2
    )
    # Tax at stake per exception bucket — the KPI cards lead with these, never
    # with gross invoice value.
    def _bucket_tax(names: tuple[str, ...]) -> float:
        return round(sum(i["tax"] for i in items if i["bucket"] in names), 2)

    def _bucket_gross(names: tuple[str, ...]) -> float:
        return round(sum(i["difference"] for i in items if i["bucket"] in names), 2)

    # Cause split — `value` is the gross difference (unchanged); `tax_value`
    # is the same split measured in tax so the bar ties to the §1 headline.
    cause_stat: dict[str, dict[str, Any]] = {}
    for i in exceptions:
        s = cause_stat.setdefault(i["cause"], {"cause": i["cause"], "label": CAUSE_LABELS.get(i["cause"], i["cause"]),
                                               "count": 0, "value": 0.0, "tax_value": 0.0, "group": i["cause_group"],
                                               "color_role": CAUSE_COLOR_ROLE.get(i["cause"], "neutral")})
        s["count"] += 1
        s["value"] = round(s["value"] + i["difference"], 2)
        s["tax_value"] = round(s["tax_value"] + i["tax"], 2)
    cause_segments = sorted(cause_stat.values(), key=lambda s: s["value"], reverse=True)

    group_stat: dict[str, dict[str, Any]] = {}
    for seg in cause_segments:
        g = group_stat.setdefault(seg["group"], {"group": seg["group"],
                                                 "label": CAUSE_GROUP_LABELS[seg["group"]],
                                                 "count": 0, "value": 0.0, "tax_value": 0.0})
        g["count"] += seg["count"]
        g["value"] = round(g["value"] + seg["value"], 2)
        g["tax_value"] = round(g["tax_value"] + seg["tax_value"], 2)
    group_segments = [group_stat[g] for g in ("gap", "judgement") if g in group_stat]

    # Classification donut. EVERY slice is weighted by the SAME basis so the
    # chart can never invert:
    #   * count — the record count (the chart's centre label already reads
    #     "Records N", so this is the intended basis), and
    #   * value — the TOTAL TAX of the records in the bucket, a like-for-like
    #     monetary total on every slice.
    # The previous figure summed each item's *difference*, which is a sub-rupee
    # residual for a matched invoice but a gross value for a Not-in-Books one.
    # Weighting one side by a diagnostic residual and another by a gross total
    # made a clean run (35/39 matched) render almost entirely red. The per-item
    # `difference` is still on the items for the cause bar and the detail table.
    buckets = GST_BUCKETS if recon_type != "TDS" else ["Matched", "Amount Difference", "Missing", "Late Deposit"]
    classification_slices = []
    for name in buckets:
        bucket_items = [i for i in items if i["bucket"] == name]
        classification_slices.append({
            "key": name,
            "name": name,
            "count": len(bucket_items),
            "value": round(sum(i["tax"] for i in bucket_items), 2),
            "color_role": CLASSIFICATION_COLOR_ROLE.get(name, "neutral"),
        })

    # Top suppliers by unmatched value.
    sup_stat: dict[str, dict[str, Any]] = {}
    for i in exceptions:
        s = sup_stat.setdefault(i["supplier_key"], {"party": i["supplier_key"], "value": 0.0, "count": 0})
        s["value"] = round(s["value"] + i["difference"], 2)
        s["count"] += 1
    supplier_rows = sorted(sup_stat.values(), key=lambda s: (-s["value"], s["party"]))

    # Confidence counts (match confidence, never severity).
    confidence_counts = {"High": 0, "Medium": 0, "Low": 0}
    for i in exceptions:
        band = i["confidence_band"] or "Low"
        confidence_counts[band] = confidence_counts.get(band, 0) + 1

    # Review state counts.
    review_counts = {"reviewed": reviewed_count, "unreviewed": total - reviewed_count}

    kpi = {
        "attention_value": attention_value,
        "attention_display": format_money(attention_value),
        # §1 — the headline is the TAX at stake, with its share of the period's
        # ITC. `attention_*` / `gross_value*` remain available as secondary
        # figures but are never the top-line "requiring attention" number.
        "itc_at_stake_tax": itc_at_stake_tax,
        "itc_at_stake_display": format_money(itc_at_stake_tax),
        "itc_at_stake_pct": itc_at_stake_pct,
        "itc_at_stake_pct_display": f"{itc_at_stake_pct:.2f}%",
        "period_itc_total": period_itc_total,
        "period_itc_total_display": format_money(period_itc_total),
        "exception_count": len(exceptions),
        "total_count": total,
        "matched_count": matched_count,
        "portal_invoice_count": sum(1 for i in items if i["portal"]),
        "reviewed_count": reviewed_count,
        "unreviewed_count": total - reviewed_count,
        # Value-at-risk run aggregates (D6). `gross_value` is the gross invoice
        # value of ALL exception items (the ONE shared computation above);
        # `itc_at_risk` is the tax genuinely at risk across every exception.
        "gross_value": exception_gross_value,
        "gross_value_display": format_money(exception_gross_value),
        "itc_at_risk": round(sum(i["itc_at_risk"] for i in exceptions), 2),
        "itc_at_risk_display": format_money(sum(i["itc_at_risk"] for i in exceptions)),
        "amount_difference_count": sum(1 for i in exceptions if i["bucket"] == "Amount Difference"),
        "amount_difference_value": round(
            sum(i["difference"] for i in exceptions if i["bucket"] == "Amount Difference"), 2
        ),
        # Tax at stake per bucket — the KPI cards show THESE.
        "amount_difference_tax": _bucket_tax(("Amount Difference",)),
        "amount_difference_gross": _bucket_gross(("Amount Difference",)),
        "not_in_books_tax": _bucket_tax(("Not in Books", "Missing")),
        "not_in_books_gross": _bucket_gross(("Not in Books", "Missing")),
        "not_in_portal_tax": _bucket_tax(("Not in Portal", "Late Deposit")),
        "not_in_portal_gross": _bucket_gross(("Not in Portal", "Late Deposit")),
        "not_in_books_count": sum(1 for i in exceptions if i["bucket"] in ("Not in Books", "Missing")),
        "not_in_books_value": round(
            sum(i["difference"] for i in exceptions if i["bucket"] in ("Not in Books", "Missing")), 2
        ),
        "not_in_portal_count": sum(1 for i in exceptions if i["bucket"] in ("Not in Portal", "Late Deposit")),
        "not_in_portal_value": round(
            sum(i["difference"] for i in exceptions if i["bucket"] in ("Not in Portal", "Late Deposit")), 2
        ),
    }

    # ITC eligibility split — built from the run's OWN eligibility markers
    # (§17(5) blocked credit and reverse charge), never from
    # (claimed − eligible), which is a genuinely different quantity and was
    # mislabelled "Ineligible" on the Review screen (§2).
    itc = None
    if recon_type == "GST" and itc_claimed is not None:
        eligible = round(float(itc_eligible or 0.0), 2)
        blocked = round(float(itc_blocked or 0.0), 2)
        reverse_charge = round(float(itc_reverse_charge or 0.0), 2)
        ineligible = round(blocked + reverse_charge, 2)
        slices = drop_zero_slices([
            {"name": "Eligible ITC", "value": eligible, "color_role": "rule"},
            {"name": "Ineligible ITC", "value": ineligible, "color_role": "danger"},
        ])
        total_itc = round(sum(s["value"] for s in slices), 2)
        itc = {
            "eligible": eligible,
            "ineligible": ineligible,
            "blocked": blocked,
            "reverse_charge": reverse_charge,
            "claimed": round(float(itc_claimed or 0.0), 2),
            "total": total_itc,
            "total_display": format_money(total_itc),
            "eligible_display": format_money(eligible),
            # Zero-value segments are dropped BEFORE the chart sees them, so a
            # nil bucket can never paint a sliver.
            "slices": slices,
            "single": len(slices) == 1,
            "single_label": (slices[0]["name"] if slices else ""),
            "single_value": (slices[0]["value"] if slices else 0.0),
            "single_display": (format_money(slices[0]["value"]) if slices else format_money(0)),
            "single_color_role": (slices[0]["color_role"] if slices else "neutral"),
            "single_percent": 100.0 if slices else 0.0,
            "empty_note": (
                "No ITC was recorded for this run." if not slices else ""
            ),
        }

    return {
        "recon_type": recon_type,
        "items": items,
        "kpi": kpi,
        "cause_segments": cause_segments,
        "group_segments": group_segments,
        "classification_slices": classification_slices,
        "suppliers": supplier_rows,
        "confidence_counts": confidence_counts,
        "review_counts": review_counts,
        "itc": itc,
    }


def headline_integrity(model: dict[str, Any]) -> dict[str, Any]:
    """Re-derive the headline from the exception ROWS and compare it to the
    figure the screen actually displays.

    This is a genuinely independent recount: ``recount`` sums each exception
    row's own tax, while ``headline`` is the exact value bound into the
    headline (`kpi["itc_at_stake_tax"]`, which also drives the Excel/HTML
    headline). The two must agree to the paisa. The previous check compared
    the row recount against a separate internal cause total — never against the
    displayed headline — so a headline bound to only one bucket passed a
    recount the display never reached.
    """
    items = model.get("items") or []
    kpi = model.get("kpi") or {}
    exceptions = [i for i in items if i.get("bucket") != "Matched"]
    recount = round(sum(float(i.get("tax") or 0.0) for i in exceptions), 2)
    headline = round(float(kpi.get("itc_at_stake_tax") or 0.0), 2)
    cause_sum = round(sum(float(s.get("tax_value") or 0.0)
                          for s in (model.get("cause_segments") or [])), 2)
    return {
        "headline": headline,
        "recount": recount,
        "cause_sum": cause_sum,
        "exception_count": len(exceptions),
        "ties": abs(headline - recount) < 0.05 and abs(recount - cause_sum) < 0.05,
    }
