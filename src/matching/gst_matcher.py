"""GST matching engine — books (Tally) vs portal (GSTR-2B / IMS).

Disposable PoC. All tunable values come from config/matching_rules.yaml.
No rate, threshold or statutory value appears in this Python source.

The client claims Input Tax Credit on purchases. The portal holds supplier
declarations. This engine classifies which ITC is supported and which is
at risk. The dangerous error is a false positive (calling something Matched
when it is not), so every design choice favours flagging genuine matches as
exceptions over passing mismatches as clean.

Known limitations — documented, not solved:
  - One books entry split across several portal records (or the reverse) is
    not matched as a group. GST is predominantly one-to-one, so this is
    accepted for Phase 1 and surfaces as paired exceptions.
  - Section 17(5) blocked-credit determination is not made. The 2B
    eligibility marker is carried through but not interpreted.
  - Section 16(4) time-limit eligibility is not evaluated.
  - ISD credit distribution is not reconciled.
  - IMS accept/reject/pending status is carried through where present but
    does not drive matching.
  - Only the adjacent period is checked for timing differences, and only
    when that file is supplied.
  - Place of supply is not independently validated; split mismatches are
    detected but not adjudicated.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import pandas as pd
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLASSIFICATIONS = ("Matched", "Not in Books", "Not in Portal", "Amount Difference")

DIFFERENCE_TYPES = (
    "Tax Split Mismatch",
    "Taxable Value Difference",
    "Tax Amount Difference",
    "Rate Mismatch",
    "Document Type Mismatch",
    "Timing Difference",
    "Rounding",
    "Duplicate in Books",
    "Unexplained",
)

# Credit-note-like document types whose values should be ITC-negative.
_CREDIT_NOTE_TYPES = {"Credit Note"}

# CREDIT notes are a SEPARATE document class: the portal carries them on its
# B2B-CDNR sheet (reported by the report's own Credit Notes card, never
# reconciled as invoices). Books rows carrying a credit-note marker must be
# kept OUT of the invoice-matching pool, or every one of them becomes a false
# "Not in Portal" exception that contradicts that label.
#
# DEBIT notes are deliberately NOT excluded: a debit note (additional freight,
# rate revision, etc.) is a genuine charge whose ITC the client claims, and the
# portal carries its counterpart on the same B2B-CDNR sheet. Excluding the
# books half left the portal half as a nameless "Not in Books" exception and
# the books half unmatched — two orphan rows where the correct answer is one
# Matched pair.
_NOTE_TYPES = {"Credit Note"}
_CREDIT_MARKER_RE = re.compile(r"^CN\s*[-/]", re.IGNORECASE)
_DEBIT_MARKER_RE = re.compile(r"^DN\s*[-/]", re.IGNORECASE)

# Document types routed to separate matching pools.
_REVERSE_CHARGE_TYPES = {"Reverse Charge"}
_IMPORT_TYPES = {"Import"}
_AMENDMENT_TYPES = {"Amendment"}
_ISD_TYPES = {"ISD"}

# GSTIN structural pattern: 2-digit state + 10-char PAN + 1 entity + 1 check
_GSTIN_RE = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][A-Z][0-9A-Z]$"
)

TAX_COMPONENTS = ["cgst", "sgst", "igst", "cess"]


# ---------------------------------------------------------------------------
# GSTIN helpers
# ---------------------------------------------------------------------------

def _validate_gstin_structure(gstin: str) -> tuple[bool, str]:
    """Return (is_valid, description). Does NOT reject — flags only."""
    g = str(gstin).strip().upper()
    if len(g) != 15:
        return False, f"GSTIN '{g}' has {len(g)} chars (expected 15)"
    if not _GSTIN_RE.match(g):
        return False, f"GSTIN '{g}' does not match structural pattern"
    return True, ""


def _has_gstin(value: Any) -> bool:
    """Whether a record actually captured a GSTIN (blank/NaN/None/'0' = no)."""
    s = str(value if value is not None else "").strip().upper()
    return s not in ("", "NAN", "NONE", "NAT", "0", "0.0")


def _gstin_structurally_invalid(gstin: str) -> bool:
    """True only when a GSTIN is PRESENT but GROSSLY malformed (wrong length).

    A 15-character GSTIN that merely fails the character pattern (e.g. a '0'
    typed for an 'O') is a single-character TYPO, not a structural defect: the
    supplier identity is still corroborated by other evidence (a fuzzy party
    name, or an exact match on both sides). Only a structurally malformed
    GSTIN — which cannot identify the supplier at all — attracts the distinct
    deduction.

    An ABSENT GSTIN (blank/NaN) is not "malformed": there is nothing to
    malform, and the missing-GSTIN fuzzy fallback already applies its own,
    separate confidence treatment. Penalising it here would push a legitimate
    fuzzy party match below the Medium band by coincidence.
    """
    s = str(gstin or "").strip().upper()
    if not _has_gstin(s):
        return False
    return len(s) != 15


# ---------------------------------------------------------------------------
# Invoice-number normalisation
# ---------------------------------------------------------------------------
#
# The rules live in `src/matching/invoice_keys.py` (pure, unit-tested) and the
# FY patterns come from config/matching_rules.yaml — never from this file.
# These thin wrappers keep the matcher's call sites unchanged while routing
# every key through the one implementation.

def _fy_patterns(config: dict) -> list[str]:
    return list(config.get("invoice_number_fy_patterns") or [])


def _normalize_invoice_number(raw: str, config: dict | None = None) -> str:
    """Uppercase alphanumerics, separators removed, leading zeros stripped,
    FY suffix removed FIRST. E.g. 'T11439/26-27' → 'T11439'."""
    from src.matching.invoice_keys import normalize_invoice_key

    return normalize_invoice_key(raw, _fy_patterns(config or {}))


def _trailing_numeric_key(raw: str, config: dict | None = None) -> str | None:
    """The numeric core of the invoice number, AFTER FY stripping. Returns
    None if no numeric segment exists."""
    from src.matching.invoice_keys import variant_invoice_key

    return variant_invoice_key(raw, _fy_patterns(config or {}))


def _fy_only_key(raw: str, config: dict | None = None) -> str:
    """The raw invoice number with the CONFIGURED financial-year tokens removed
    and separators dropped — leading zeros KEPT.

    When a books/portal pair agrees on this, the ONLY difference was the FY
    token: a deterministic rule the engine already applies, so the match IS an
    exact one ("T11439" vs "T11439/26-27"). When it still disagrees, a further
    derivation was needed to make the keys meet — leading-zero stripping, a
    supplier prefix, or reusing the numeric core — so it is a VARIANT and
    carries the lower baseline ("VE-234" vs "VE/26-27/0234").
    """
    from src.matching.invoice_keys import strip_fy_tokens

    s = strip_fy_tokens(raw, _fy_patterns(config or {})).upper()
    return re.sub(r"[\s/\\\-_.]+", "", s)


# ---------------------------------------------------------------------------
# Date / financial-year helpers
# ---------------------------------------------------------------------------

def _financial_year(date_str: str | None) -> str | None:
    """Return FY label like '2025-26' for a YYYY-MM-DD date."""
    if not date_str or pd.isna(date_str) or str(date_str) == "NaT":
        return None
    try:
        dt = pd.Timestamp(date_str)
    except Exception:
        return None
    y = dt.year
    if dt.month <= 3:
        return f"{y - 1}-{str(y)[-2:]}"
    return f"{y}-{str(y + 1)[-2:]}"


def _period_label(date_str: str | None) -> str | None:
    """Return YYYY-MM period label."""
    if not date_str or pd.isna(date_str) or str(date_str) == "NaT":
        return None
    try:
        dt = pd.Timestamp(date_str)
        return dt.strftime("%Y-%m")
    except Exception:
        return None


def _adjacent_periods(period: str) -> list[str]:
    """Return [previous_month, next_month] as YYYY-MM strings."""
    try:
        dt = pd.Timestamp(period + "-01")
    except Exception:
        return []
    prev = dt - pd.DateOffset(months=1)
    nxt = dt + pd.DateOffset(months=1)
    return [prev.strftime("%Y-%m"), nxt.strftime("%Y-%m")]


# ---------------------------------------------------------------------------
# Amount / tolerance helpers
# ---------------------------------------------------------------------------

def _within_tolerance(a: float, b: float, config: dict) -> bool:
    tol = config["amount_tolerance"]
    diff = abs(a - b)
    if diff <= float(tol["absolute"]):
        return True
    denom = max(abs(a), abs(b))
    if denom > 0 and (diff / denom * 100) <= float(tol["percent"]):
        return True
    return False


def _is_rounding(a: float, b: float, config: dict) -> bool:
    diff = abs(a - b)
    return 0 < diff <= float(config["rounding_tolerance"])


def _date_within_window(d1: str | None, d2: str | None, config: dict) -> bool:
    if not d1 or not d2 or pd.isna(d1) or pd.isna(d2):
        return True  # missing date → don't penalise
    try:
        dt1, dt2 = pd.Timestamp(d1), pd.Timestamp(d2)
        return abs((dt1 - dt2).days) <= int(config["date_tolerance_days"])
    except Exception:
        return True


def _date_diff_days(d1: str | None, d2: str | None) -> int | None:
    if not d1 or not d2 or pd.isna(d1) or pd.isna(d2):
        return None
    try:
        return abs((pd.Timestamp(d1) - pd.Timestamp(d2)).days)
    except Exception:
        return None


def _proximity(actual_diff: float, tolerance: float) -> float:
    """1.0 when exact, → 0.0 at tolerance edge."""
    if tolerance <= 0:
        return 1.0
    return max(0.0, 1.0 - actual_diff / tolerance)


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _score(baseline: float, config: dict, *, cross_period: bool = False,
           invalid_gstin: bool = False) -> float:
    """Identity confidence for a match: the PASS baseline plus the fixed,
    pass-independent penalties.

    Deliberately INDEPENDENT of how closely the two records' values agree.
    A large value gap drives the "Amount Difference" classification and the
    review priority — never the certainty of WHICH record matched. (Before
    this, the baseline was scaled by value proximity, so an exact
    GSTIN+invoice-number match with a large value gap collapsed to Low/0 and
    read identically to a record with no counterpart at all.)
    """
    s = float(baseline)
    if cross_period:
        s += float(config.get("cross_period_penalty", -12))
    if invalid_gstin:
        s += float(config["confidence_baselines"].get("invalid_gstin_penalty", -5))
    return max(0.0, min(100.0, round(s, 2)))


def _rank(baseline: float, prox: float, config: dict, **penalties) -> float:
    """Ordering score used ONLY to choose among candidates of the SAME pass —
    closeness of amount breaks ties ("pick the nearest counterpart"). It is
    never reported as confidence, so value agreement can rank without
    contaminating the identity score."""
    return round(_score(baseline, config, **penalties) * max(0.0, prox), 4)


def _band(score: float, config: dict) -> str:
    t = config["confidence_thresholds"]
    if score >= float(t["high"]):
        return "High"
    if score >= float(t["medium"]):
        return "Medium"
    return "Low"


# ---------------------------------------------------------------------------
# Effective-rate helpers
# ---------------------------------------------------------------------------

def _effective_rate(row: pd.Series) -> float | None:
    """Compute total-tax / taxable-value as a percentage."""
    tv = float(row.get("taxable_value", 0) or 0)
    if tv == 0:
        return None
    total = float(row.get("total_tax", 0) or 0)
    return round(abs(total / tv) * 100, 2)


def _rate_is_valid(rate: float | None, config: dict) -> bool:
    if rate is None:
        return True  # can't assess
    slabs = config.get("valid_rate_slabs", [])
    if not slabs:
        return True
    return any(abs(rate - float(s)) < 0.5 for s in slabs)


# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------

def _fmt(v: float) -> str:
    return f"₹{v:,.2f}"


def _row_json(row: pd.Series, exclude: list[str] | None = None) -> str:
    exclude = set(exclude or [])
    d = {}
    for k, v in row.items():
        if k in exclude:
            continue
        d[k] = None if pd.isna(v) else v
    return json.dumps(d, default=str)


def _make_result(
    classification: str, confidence: float, config: dict, *,
    books_record: str | None = None,
    portal_record: str | None = None,
    match_reason: str,
    difference_type: str | None = None,
    matched_record_ids: list | None = None,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "confidence_score": confidence,
        "confidence_band": _band(confidence, config),
        "books_record": books_record,
        "portal_record": portal_record,
        "match_reason": match_reason,
        "difference_type": difference_type,
        "matched_record_ids": matched_record_ids,
    }


# ---------------------------------------------------------------------------
# Value-at-risk semantics (D6)
# ---------------------------------------------------------------------------
#
# The headline and per-row amount must reflect the MONEY AT RISK, not the
# full invoice value. A ₹0.02 rounding difference on a ₹26,066 invoice is a
# ₹0.02 exposure, not a ₹26,066 one. These three figures are stored on every
# result so the Review UI reads them rather than re-deriving them:
#
#   difference   — signed (portal − books) on the field that differs;
#   itc_at_risk  — tax on Not in Books / Not in Portal items, plus the
#                  |tax difference| on Amount Difference items;
#   gross_value  — the invoice value, kept for Not in Books / Not in Portal
#                  only (there is no counterpart to net against).
#
# Classification is NOT changed by any of this.

def _record_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return record
    if isinstance(record, str) and record:
        try:
            parsed = json.loads(record)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _num(record: dict[str, Any], field: str) -> float:
    try:
        return float(record.get(field) or 0)
    except (TypeError, ValueError):
        return 0.0


def _tax_total(record: dict[str, Any]) -> float:
    return sum(_num(record, f) for f in TAX_COMPONENTS)


def annotate_value_at_risk(results: list[dict[str, Any]]) -> None:
    """Add `difference`, `itc_at_risk` and `gross_value` to every result.

    Pure and total: classification is never touched, and a result with no
    usable records gets zeros rather than a fabricated figure.
    """
    for r in results:
        books = _record_dict(r.get("books_record"))
        portal = _record_dict(r.get("portal_record"))
        classification = str(r.get("classification") or "")

        difference = 0.0
        itc_at_risk = 0.0
        gross_value = 0.0

        if classification == "Not in Books":
            gross_value = _num(portal, "invoice_value")
            itc_at_risk = _tax_total(portal)
        elif classification == "Not in Portal":
            gross_value = _num(books, "invoice_value")
            itc_at_risk = _tax_total(books)
        elif classification == "Amount Difference":
            # Signed (portal − books) on the field that differs. The engine
            # names the basis in difference_type; fall back to the largest
            # disagreeing basis so the figure always matches the issue.
            basis = _DIFF_BASIS_FIELD.get(str(r.get("difference_type") or ""))
            if basis is None:
                basis = _largest_differing_basis(books, portal)
            difference = round(_num(portal, basis) - _num(books, basis), 2)
            itc_at_risk = round(abs(_tax_total(portal) - _tax_total(books)), 2)

        r["difference"] = round(difference, 2)
        r["itc_at_risk"] = round(itc_at_risk, 2)
        r["gross_value"] = round(gross_value, 2)


# difference_type -> the field the difference is measured on.
_DIFF_BASIS_FIELD: dict[str, str] = {
    "Taxable Value Difference": "taxable_value",
    "Tax Amount Difference": "total_tax",
    "Tax Split Mismatch": "total_tax",
    "Rate Mismatch": "total_tax",
    "Rounding": "invoice_value",
    "Document Type Mismatch": "invoice_value",
    "Timing Difference": "invoice_value",
    "Duplicate in Books": "invoice_value",
    "Unexplained": "invoice_value",
}


def _largest_differing_basis(books: dict[str, Any], portal: dict[str, Any]) -> str:
    best_field, best = "invoice_value", 0.0
    for field in ("taxable_value", "total_tax", "invoice_value"):
        d = abs(_num(books, field) - _num(portal, field))
        if d > best:
            best, best_field = d, field
    return best_field


# ===================================================================
# PRE-PROCESSING (Section 2)
# ===================================================================

def _map_document_type(row: pd.Series, config: dict) -> str:
    """Map raw document-type field to normalised type using config."""
    source = str(row.get("source_type", "")).strip()
    doc_type_maps = config.get("document_type_map", {})
    type_map = doc_type_maps.get(source, {})

    raw_type = str(row.get("document_type", "")).strip()
    if raw_type and raw_type in type_map:
        return type_map[raw_type]

    # Derive from available indicators if no explicit type
    inv_val = float(row.get("invoice_value", 0) or 0)
    if inv_val < 0:
        return "Credit Note"
    return "Invoice"  # default assumption


def preprocess_portal(
    portal_df: pd.DataFrame, config: dict
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Pre-process portal records: type mapping, sign normalisation,
    amendment resolution, pool separation.

    Returns (b2b_pool, rc_pool, import_pool, summary_info).
    The summary_info dict contains counts for reporting.
    """
    df = portal_df.copy()
    df["_pid"] = range(len(df))
    _INTERNAL = ["_pid", "_norm_inv", "_trail_num", "_fy", "_doc_type"]

    # --- Document type mapping ---
    df["_doc_type"] = df.apply(lambda r: _map_document_type(r, config), axis=1)
    type_counts = df["_doc_type"].value_counts().to_dict()

    # --- Sign normalisation (ITC-negative for credit notes) ---
    sign_flipped = 0
    value_cols = ["taxable_value", "cgst", "sgst", "igst", "cess", "total_tax", "invoice_value"]
    for idx, row in df.iterrows():
        if row["_doc_type"] in _CREDIT_NOTE_TYPES:
            for col in value_cols:
                v = float(row.get(col, 0) or 0)
                if v > 0:
                    df.at[idx, col] = -v
                    sign_flipped += 1

    # --- Amendment resolution ---
    amendments = df[df["_doc_type"].isin(_AMENDMENT_TYPES)].copy()
    amendment_resolved = 0
    dangling_refs = 0
    superseded_pids: set[int] = set()
    amendment_notes: dict[int, str] = {}  # pid → note about what was superseded

    for _, amd in amendments.iterrows():
        # Try to find original by same GSTIN + invoice number
        orig_inv = str(amd.get("invoice_number", "")).strip()
        orig_gstin = str(amd.get("gstin", "")).strip().upper()
        originals = df[
            (~df["_pid"].isin(superseded_pids))
            & (~df["_doc_type"].isin(_AMENDMENT_TYPES))
            & (df["gstin"].astype(str).str.strip().str.upper() == orig_gstin)
            & (df["invoice_number"].astype(str).str.strip() == orig_inv)
        ]
        if not originals.empty:
            orig = originals.iloc[0]
            superseded_pids.add(orig["_pid"])
            amendment_resolved += 1
            old_val = float(orig.get("invoice_value", 0) or 0)
            new_val = float(amd.get("invoice_value", 0) or 0)
            amendment_notes[amd["_pid"]] = (
                f"Amendment: supersedes original invoice {orig_inv} "
                f"({_fmt(old_val)} revised to {_fmt(new_val)}); "
                f"the original was excluded from matching."
            )
        else:
            dangling_refs += 1
            amendment_notes[amd["_pid"]] = (
                f"Amendment references invoice {orig_inv} from {orig_gstin}, "
                f"but the original is not present in supplied data — "
                f"matched normally with dangling amendment reference noted."
            )

    # Remove superseded originals
    df = df[~df["_pid"].isin(superseded_pids)].copy()
    # Re-type amendments as Invoice for matching purposes (they replace the original)
    df.loc[df["_doc_type"].isin(_AMENDMENT_TYPES), "_doc_type"] = "Invoice"

    # Store amendment notes for later
    df["_amendment_note"] = df["_pid"].map(amendment_notes).fillna("")

    # --- Normalise invoice numbers ---
    df["_norm_inv"] = df["invoice_number"].apply(lambda v: _normalize_invoice_number(v, config))
    df["_trail_num"] = df["invoice_number"].apply(lambda v: _trailing_numeric_key(v, config))
    df["_fy"] = df["invoice_date"].apply(_financial_year)

    # --- Separate pools ---
    rc_mask = df["_doc_type"].isin(_REVERSE_CHARGE_TYPES)
    import_mask = df["_doc_type"].isin(_IMPORT_TYPES)
    isd_mask = df["_doc_type"].isin(_ISD_TYPES)
    b2b_mask = ~(rc_mask | import_mask | isd_mask)

    b2b_pool = df[b2b_mask].copy()
    rc_pool = df[rc_mask].copy()
    import_pool = df[import_mask].copy()

    summary = {
        "document_type_counts": type_counts,
        "amendments_resolved": amendment_resolved,
        "dangling_amendment_refs": dangling_refs,
        "sign_flipped_values": sign_flipped,
        "reverse_charge_count": int(rc_mask.sum()),
        "import_count": int(import_mask.sum()),
        "isd_count": int(isd_mask.sum()),
        "b2b_count": int(b2b_mask.sum()),
        "superseded_pids": list(superseded_pids),
    }

    return b2b_pool, rc_pool, import_pool, summary


def _books_note_type(invoice_number: Any, invoice_value: Any, taxable_value: Any) -> str | None:
    """The CREDIT-note marker on a BOOKS row, or None for an ordinary document.

    Two signals, either of which is enough:
      * a CN- invoice-number prefix (a credit note booked with a POSITIVE value),
      * a negative invoice value or taxable value (the sign convention).

    A DN- prefix is deliberately NOT a marker: a debit note is an ordinary
    charge and must match like any other purchase (Test Set 3 S3-F1).
    """
    s = str(invoice_number or "").strip()
    if _CREDIT_MARKER_RE.match(s):
        return "Credit Note"
    for v in (invoice_value, taxable_value):
        try:
            if float(v or 0) < 0:
                return "Credit Note"
        except (TypeError, ValueError):
            continue
    return None


def preprocess_books(books_df: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    """Pre-process books records: add internal keys, derive the document type,
    and tag + sign-normalise CREDIT notes so they can be isolated from the
    invoice pool (debit notes stay in the pool — see ``_books_note_type``)."""
    df = books_df.copy()
    df["_bid"] = range(len(df))
    df["_norm_inv"] = df["invoice_number"].apply(lambda v: _normalize_invoice_number(v, config))
    df["_trail_num"] = df["invoice_number"].apply(lambda v: _trailing_numeric_key(v, config))
    df["_fy"] = df["invoice_date"].apply(_financial_year)

    # Document type: the books export's own column when it carries one (rare),
    # else "Invoice" — the portal's "Regular" normalises to the same value, so
    # an ordinary purchase agrees and only a genuine type disagreement (an SEZ
    # supply booked as a domestic purchase) surfaces.
    df["_doc_type"] = "Invoice"
    if "document_type" in df.columns:
        df["_doc_type"] = df.apply(lambda r: _map_document_type(r, config or {}), axis=1)

    value_cols = ["taxable_value", "cgst", "sgst", "igst", "cess", "total_tax", "invoice_value"]
    for idx, row in df.iterrows():
        note_type = _books_note_type(
            row.get("invoice_number"), row.get("invoice_value"), row.get("taxable_value")
        )
        if note_type == "Credit Note":
            df.at[idx, "_doc_type"] = "Credit Note"
            # ITC-negative, the same convention preprocess_portal applies.
            for col in value_cols:
                v = float(row.get(col, 0) or 0)
                if v > 0:
                    df.at[idx, col] = -v
        elif _DEBIT_MARKER_RE.match(str(row.get("invoice_number") or "").strip()):
            # A debit note is labelled (so it agrees with the portal's own
            # "Debit Note" type) but stays in the pool and keeps its sign —
            # it is a charge, not a credit-note reversal.
            df.at[idx, "_doc_type"] = "Debit Note"

    df["_amendment_note"] = ""
    return df


# ===================================================================
# DUPLICATE DETECTION (Section 5)
# ===================================================================

def _books_duplicate_keys(books: pd.DataFrame) -> dict[tuple[str, str], int]:
    """(GSTIN, normalised invoice number) -> how many times books carry it.

    The number of times a key is duplicated is REPORTED to the matching passes
    so the SURPLUS copies can be labelled, but it is deliberately NOT emitted
    as its own set of result rows. The five matching passes already produce
    exactly one match plus N-1 residuals for a duplicated key (a portal record
    is matched at most once), so emitting the duplicate population separately
    re-emitted the same books rows — the S2-09 "4 rows instead of 2" defect.
    """
    if books.empty:
        return {}
    counts = books.groupby(["gstin", "_norm_inv"], dropna=False).size()
    return {
        (str(g).strip().upper(), str(k)): int(n)
        for (g, k), n in counts.items()
        if int(n) > 1
    }


# ===================================================================
# MATCHING PASSES
# ===================================================================

_EXCLUDE_COLS = ["_bid", "_pid", "_norm_inv", "_trail_num", "_fy",
                 "_doc_type", "_amendment_note"]


def _component_shares(record: pd.Series, total: float) -> dict[str, float]:
    """Each tax component's share of a record's own total tax."""
    if not total:
        return {c: 0.0 for c in TAX_COMPONENTS}
    return {c: abs(float(record.get(c, 0) or 0)) / abs(total) for c in TAX_COMPONENTS}


def _tax_split_differs(
    br: pd.Series, pr: pd.Series, total_tax_b: float, total_tax_p: float,
) -> bool:
    """True when the DISTRIBUTION of tax across CGST/SGST/IGST/Cess differs
    materially between the two sides — e.g. the books booked an inter-state
    supply as CGST+SGST while the portal shows IGST. Only meaningful when the
    components genuinely disagree (a within-tolerance residual is not a split
    difference), so a matched multi-rate invoice is never flagged."""
    sb = _component_shares(br, total_tax_b)
    sp = _component_shares(pr, total_tax_p)
    return any(abs(sb[c] - sp[c]) > 0.02 for c in TAX_COMPONENTS)


def _compare_pair(
    br: pd.Series, pr: pd.Series, config: dict,
) -> tuple[str, str | None, str]:
    """Compare a books-portal pair. Return (classification, difference_type,
    detail_fragment) describing what agrees and what doesn't.

    Every INDEPENDENT discrepancy is collected before a label is chosen: one
    discrepancy keeps its specific difference_type, and two or more collapse to
    the "Unexplained" catch-all with each issue enumerated in the reason. The
    old first-match-wins order silently discarded the split and document-type
    problems whenever a taxable-value difference was present (Test Set 3
    BSH/515 carried three simultaneous issues and surfaced only one).
    """
    tv_b = float(br.get("taxable_value", 0) or 0)
    tv_p = float(pr.get("taxable_value", 0) or 0)
    iv_b = float(br.get("invoice_value", 0) or 0)
    iv_p = float(pr.get("invoice_value", 0) or 0)

    components_agree = True
    component_details = []
    total_tax_b = 0.0
    total_tax_p = 0.0

    for comp in TAX_COMPONENTS:
        cb = float(br.get(comp, 0) or 0)
        cp = float(pr.get(comp, 0) or 0)
        total_tax_b += cb
        total_tax_p += cp
        if not _within_tolerance(cb, cp, config):
            components_agree = False
            component_details.append(f"{comp.upper()}: books {_fmt(cb)} vs portal {_fmt(cp)}")

    tv_agree = _within_tolerance(tv_b, tv_p, config)
    doc_type_b = str(br.get("_doc_type", "Invoice"))
    doc_type_p = str(pr.get("_doc_type", "Invoice"))
    doc_agree = doc_type_b == doc_type_p
    total_tax_agree = _within_tolerance(total_tax_b, total_tax_p, config)

    # Implied rates — used to decide whether a total-tax difference is an
    # independent tax error or merely the taxable-value difference flowing
    # through at the same rate (RTM/158: books under-booked the bill, tax
    # recomputed proportionately — that is ONE issue, Taxable Value Difference).
    rate_b = _effective_rate(br)
    rate_p = _effective_rate(pr)
    rates_differ = (rate_b is not None and rate_p is not None and abs(rate_b - rate_p) > 0.5)

    issues: list[tuple[str, str]] = []

    if not tv_agree:
        issues.append((
            "Taxable Value Difference",
            f"Taxable Value Difference: books {_fmt(tv_b)} vs portal {_fmt(tv_p)}, "
            f"difference {_fmt(abs(tv_b - tv_p))}.",
        ))

    if not total_tax_agree and rates_differ and tv_agree:
        issues.append((
            "Tax Amount Difference",
            f"Tax Amount Difference: {'; '.join(component_details) or _fmt(abs(total_tax_b - total_tax_p))}.",
        ))

    if not components_agree and _tax_split_differs(br, pr, total_tax_b, total_tax_p):
        issues.append((
            "Tax Split Mismatch",
            f"Tax Split Mismatch: total tax books {_fmt(total_tax_b)} vs portal "
            f"{_fmt(total_tax_p)}, but the components are distributed differently "
            f"— {'; '.join(component_details)}. Indicates a place-of-supply difference.",
        ))

    if not doc_agree:
        issues.append((
            "Document Type Mismatch",
            f"Document Type Mismatch: books has '{doc_type_b}', portal has '{doc_type_p}'.",
        ))

    if not issues:
        # Rate mismatch — only when the two sides imply DIFFERENT rates while
        # every component agreed. A blended rate across several buckets
        # (e.g. 2.5% + 9% = 15.6%) is a legitimate multi-rate invoice, not a
        # mismatch, when both sides agree on it.
        if rates_differ:
            return (
                "Amount Difference", "Rate Mismatch",
                f"Rate Mismatch: books implies {rate_b}%, portal implies {rate_p}%.",
            )

        # Rounding — a difference OUTSIDE the amount tolerance but inside the
        # rounding tolerance. A difference already inside the amount tolerance
        # is agreement, not a rounding difference.
        any_rounding = False
        for comp in TAX_COMPONENTS + ["taxable_value"]:
            cb = float(br.get(comp, 0) or 0)
            cp = float(pr.get(comp, 0) or 0)
            if not _within_tolerance(cb, cp, config) and _is_rounding(cb, cp, config):
                any_rounding = True

        if any_rounding:
            return (
                "Amount Difference", "Rounding",
                f"All values agree within rounding tolerance; "
                f"invoice value books {_fmt(iv_b)} vs portal {_fmt(iv_p)}.",
            )

        iv_diff = abs(iv_b - iv_p)
        detail = (
            f"taxable value, CGST, SGST, IGST and cess all agree"
            f"{' within ' + _fmt(float(config['amount_tolerance']['absolute'])) if iv_diff > 0 else ''}."
        )
        return "Matched", None, detail

    if len(issues) == 1:
        diff_type, detail = issues[0]
        return "Amount Difference", diff_type, detail

    # Two or more independent discrepancies — no single sub-type explains the
    # document, so it is the catch-all and every issue is enumerated.
    date_fragment = ""
    dd = _date_diff_days(br.get("invoice_date"), pr.get("invoice_date"))
    if dd:
        date_fragment = f" Dates differ by {dd} day(s)."
    detail = (
        "Multiple discrepancies on this document — "
        + " | ".join(d for _t, d in issues)
        + date_fragment
    )
    return "Amount Difference", "Unexplained", detail


def _core_counts(df: pd.DataFrame) -> dict[tuple[str, str], int]:
    """How many times each (GSTIN, numeric core) appears on one side.

    A numeric core that appears more than once within a GSTIN cannot be used
    as an exact key — it would be ambiguous — so the caller only accepts the
    core as an exact match when the count is exactly 1 on BOTH sides.
    """
    counts: dict[tuple[str, str], int] = {}
    for _, row in df.iterrows():
        core = row.get("_trail_num")
        if not core:
            continue
        key = (str(row.get("gstin", "")).strip().upper(), str(core))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _resolve_ambiguity(
    candidates: list[tuple[float, float, pd.Series, str]],
    config: dict,
) -> tuple[pd.Series, float, str, bool]:
    """Given candidates [(rank, identity_score, portal_row, reason_fragment), ...],
    select the best, apply the ambiguity penalty, and return (chosen,
    final_identity_score, ambiguity_note, forced_low).

    ``rank`` selects the counterpart (closest amount wins) and drives the
    ambiguity margin; ``identity_score`` is the pass-based confidence that is
    actually reported. Keeping them separate is what stops value agreement
    from contaminating the identity confidence.

    The note always reports the CHOSEN candidate's OWN rank against the
    runner-up's — never the penalised score, which would read as the chosen
    candidate having scored BELOW the runner-up (D4b).
    """
    candidates.sort(key=lambda x: -x[0])
    chosen_rank, chosen_identity, best_row, best_frag = candidates[0]
    final_score = chosen_identity
    forced_low = False
    note = ""

    if len(candidates) > 1:
        runner_rank = candidates[1][0]
        margin = float(config.get("ambiguity_margin", 5.0))
        penalty = float(config["confidence_baselines"].get("ambiguity_penalty", -8))

        # Scale penalty by closeness of runner-up
        gap = chosen_rank - runner_rank
        if gap < margin:
            forced_low = True
            final_score = chosen_identity + penalty
            note = (
                f" Ambiguous: {len(candidates)} portal candidates from this GSTIN; "
                f"chosen on highest score ({chosen_rank:.0f} vs runner-up {runner_rank:.0f}); "
                f"confidence forced to Low."
            )
        else:
            note = (
                f" {len(candidates)} candidates existed; "
                f"chosen with {gap:.0f}-point margin over runner-up."
            )

    final_score = max(0.0, min(100.0, round(final_score, 2)))
    return best_row, final_score, note, forced_low


def _match_pool(
    books: pd.DataFrame,
    portal: pd.DataFrame,
    config: dict,
    *,
    adjacent_portal: pd.DataFrame | None = None,
    pool_label: str = "B2B",
    duplicate_keys: dict[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    """Run all 5 matching passes on a single pool pair.
    Returns result dicts for every books and portal record."""

    results: list[dict[str, Any]] = []
    baselines = config["confidence_baselines"]
    matched_bids: set[int] = set()
    matched_pids: set[int] = set()
    adj_matched_pids: set[int] = set()

    def _unmatched_books():
        return books[~books["_bid"].isin(matched_bids)]

    def _unmatched_portal():
        return portal[~portal["_pid"].isin(matched_pids)]

    def _make_pair_result(
        br, pr, pass_name, baseline, *,
        extra_reason="", cross_period=False, forced_low=False,
        score_override: float | None = None,
    ):
        cls, diff_type, detail = _compare_pair(br, pr, config)
        gstin = str(br.get("gstin", "")).strip().upper()
        gstin_valid, gstin_note = _validate_gstin_structure(gstin)
        inv_b = str(br.get("invoice_number", ""))
        inv_p = str(pr.get("invoice_number", ""))

        if score_override is not None:
            # The ambiguity resolver already applied its penalty to this
            # score — recomputing from the baseline would silently discard it.
            score = max(0.0, min(100.0, round(float(score_override), 2)))
        else:
            score = _score(baseline, config, cross_period=cross_period,
                           invalid_gstin=_gstin_structurally_invalid(gstin))

        if cross_period and diff_type is None:
            diff_type = "Timing Difference"
            if cls == "Matched":
                pass  # still Matched but flagged

        reason_parts = [f"{pass_name}: GSTIN {gstin}, "]
        if inv_b == inv_p:
            reason_parts.append(f"invoice {inv_b}")
        else:
            reason_parts.append(f"invoice '{inv_b}' (books) / '{inv_p}' (portal)")
        reason_parts.append(f"; {detail}")
        if extra_reason:
            reason_parts.append(extra_reason)
        if not gstin_valid:
            reason_parts.append(f" Note: {gstin_note}.")

        amd_note = str(pr.get("_amendment_note", "")).strip()
        if amd_note:
            reason_parts.append(f" {amd_note}")

        reason = "".join(reason_parts)

        if forced_low:
            band = "Low"
        else:
            band = _band(score, config)

        return {
            "classification": cls,
            "confidence_score": score,
            "confidence_band": band,
            "books_record": _row_json(br, exclude=_EXCLUDE_COLS),
            "portal_record": _row_json(pr, exclude=_EXCLUDE_COLS),
            "match_reason": reason,
            "difference_type": diff_type,
            "matched_record_ids": None,
        }

    # ---- Pass 1: Exact (GSTIN + normalised invoice number) ----
    # The normalised key strips the FY suffix and separators, so a portal
    # "T11439/26-27" and a books "T11439" both key to "T11439". Where a
    # supplier prefixes the number ("PD/26-27/3386" vs "3386") the keys
    # differ, so the numeric core is ALSO accepted here — but only when it is
    # collision-free within the GSTIN on both sides, so it can never be
    # ambiguous. This is what makes a prefix-only difference an exact match
    # rather than a lower-confidence amount+date guess (D3/D4).
    baseline_exact = float(baselines["exact"])
    baseline_var = float(baselines["invoice_variant"])
    books_core_counts = _core_counts(books)
    portal_core_counts = _core_counts(portal)
    for _, br in _unmatched_books().iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        gstin_b = str(br.get("gstin", "")).strip().upper()
        norm_b = br["_norm_inv"]
        core_b = br.get("_trail_num")
        br_struct_invalid = _gstin_structurally_invalid(gstin_b)

        candidates = []
        for _, pr in _unmatched_portal().iterrows():
            pid = pr["_pid"]
            if pid in matched_pids:
                continue
            gstin_p = str(pr.get("gstin", "")).strip().upper()
            if gstin_p != gstin_b:
                continue
            core_p = pr.get("_trail_num")
            exact_key = pr["_norm_inv"] == norm_b
            core_key = (
                not exact_key
                and core_b
                and core_p == core_b
                and books_core_counts.get((gstin_b, core_b), 0) == 1
                and portal_core_counts.get((gstin_b, core_b), 0) == 1
            )
            if not (exact_key or core_key):
                continue
            iv_b = float(br.get("invoice_value", 0) or 0)
            iv_p = float(pr.get("invoice_value", 0) or 0)
            prox = _proximity(abs(iv_b - iv_p), float(config["amount_tolerance"]["absolute"]))
            # `exact_key` means the normalised keys coincide. When they only
            # coincide because LEADING ZEROS were stripped (the numbers still
            # differ once the configured FY token is removed), the pair relies
            # on a coercion that is genuinely ambiguous — "0234" and "234" are
            # distinct invoice numbers in some numbering systems — so it takes
            # the lower variant baseline and can never read the same as an
            # invoice whose numbers agree under the configured rules
            # (VE-234 vs RTM/145). A numeric-core match after the configured
            # FY/prefix rules stays an exact match (the D3/D4 path).
            variant = exact_key and (
                _fy_only_key(br.get("invoice_number"), config)
                != _fy_only_key(pr.get("invoice_number"), config)
            )
            id_baseline = baseline_var if variant else baseline_exact
            identity = _score(id_baseline, config, invalid_gstin=br_struct_invalid)
            rank = _rank(id_baseline, prox, config, invalid_gstin=br_struct_invalid)
            frag = ""
            if variant:
                frag = (
                    f" after invoice number variance ('{br.get('invoice_number', '')}' in books, "
                    f"'{pr.get('invoice_number', '')}' on portal"
                    + (f" — same numeric core {core_b})" if core_key else ")")
                )
            candidates.append((rank, identity, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for _r, _i, p, f in candidates if p["_pid"] == chosen["_pid"])
            r = _make_pair_result(
                br, chosen, "Matched on", baseline_exact,
                extra_reason=chosen_frag + amb_note, forced_low=forced,
                score_override=final_score,
            )
            results.append(r)
            matched_bids.add(bid)
            matched_pids.add(chosen["_pid"])

    # ---- Pass 2: Invoice number variant (GSTIN + trailing numeric + amount) ----
    baseline_var = float(baselines["invoice_variant"])
    for _, br in _unmatched_books().iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        gstin_b = str(br.get("gstin", "")).strip().upper()
        trail_b = br.get("_trail_num")
        if not trail_b:
            continue
        iv_b = float(br.get("invoice_value", 0) or 0)
        br_struct_invalid = _gstin_structurally_invalid(gstin_b)

        candidates = []
        for _, pr in _unmatched_portal().iterrows():
            pid = pr["_pid"]
            if pid in matched_pids:
                continue
            gstin_p = str(pr.get("gstin", "")).strip().upper()
            if gstin_p != gstin_b:
                continue
            trail_p = pr.get("_trail_num")
            if trail_p != trail_b:
                continue
            iv_p = float(pr.get("invoice_value", 0) or 0)
            if not _within_tolerance(iv_b, iv_p, config):
                continue
            prox = _proximity(abs(iv_b - iv_p), float(config["amount_tolerance"]["absolute"]))
            inv_b_str = str(br.get("invoice_number", ""))
            inv_p_str = str(pr.get("invoice_number", ""))
            frag = (
                f" after invoice number variance ('{inv_b_str}' in books, "
                f"'{inv_p_str}' on portal)"
            )
            identity = _score(baseline_var, config, invalid_gstin=br_struct_invalid)
            rank = _rank(baseline_var, prox, config, invalid_gstin=br_struct_invalid)
            candidates.append((rank, identity, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for _r, _i, p, f in candidates if p["_pid"] == chosen["_pid"])
            r = _make_pair_result(
                br, chosen, "Matched on GSTIN and amount", baseline_var,
                extra_reason=chosen_frag + amb_note, forced_low=forced,
                score_override=final_score,
            )
            results.append(r)
            matched_bids.add(bid)
            matched_pids.add(chosen["_pid"])

    # ---- Pass 3: Amount + date anchored (GSTIN + invoice value + date window) ----
    baseline_ad = float(baselines["amount_date"])
    for _, br in _unmatched_books().iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        gstin_b = str(br.get("gstin", "")).strip().upper()
        br_struct_invalid = _gstin_structurally_invalid(gstin_b)
        iv_b = float(br.get("invoice_value", 0) or 0)
        date_b = br.get("invoice_date")

        candidates = []
        for _, pr in _unmatched_portal().iterrows():
            pid = pr["_pid"]
            if pid in matched_pids:
                continue
            gstin_p = str(pr.get("gstin", "")).strip().upper()
            if gstin_p != gstin_b:
                continue
            iv_p = float(pr.get("invoice_value", 0) or 0)
            if not _within_tolerance(iv_b, iv_p, config):
                continue
            if not _date_within_window(date_b, pr.get("invoice_date"), config):
                continue
            prox = _proximity(abs(iv_b - iv_p), float(config["amount_tolerance"]["absolute"]))
            dd = _date_diff_days(date_b, pr.get("invoice_date"))
            frag = ""
            if dd and dd > 0:
                frag = f"; date differs by {dd} day(s), within the {config['date_tolerance_days']}-day window"
            identity = _score(baseline_ad, config, invalid_gstin=br_struct_invalid)
            rank = _rank(baseline_ad, prox, config, invalid_gstin=br_struct_invalid)
            candidates.append((rank, identity, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for _r, _i, p, f in candidates if p["_pid"] == chosen["_pid"])
            r = _make_pair_result(
                br, chosen, "Matched on GSTIN and amount+date", baseline_ad,
                extra_reason=chosen_frag + amb_note, forced_low=forced,
                score_override=final_score,
            )
            results.append(r)
            matched_bids.add(bid)
            matched_pids.add(chosen["_pid"])

    # ---- Pass 4: Fuzzy party name + amount + date ----
    baseline_fz = float(baselines["fuzzy_party"])
    threshold = float(config["fuzzy_threshold"])
    # When one side captured NO GSTIN at all there is nothing to key identity
    # on, so the party name carries more of the signal and the bar is relaxed
    # — but ONLY with an EXACT amount and an EXACT date, so a blank GSTIN can
    # never turn an approximate name into a loose match on drifting figures.
    missing_gstin_threshold = float(config.get("fuzzy_missing_gstin_threshold", threshold))
    for _, br in _unmatched_books().iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        gstin_b = str(br.get("gstin", "")).strip().upper()
        br_struct_invalid = _gstin_structurally_invalid(gstin_b)
        name_b = str(br.get("party_name", "")).strip()
        iv_b = float(br.get("invoice_value", 0) or 0)
        date_b = br.get("invoice_date")
        books_has_gstin = _has_gstin(gstin_b)
        if not name_b:
            continue

        candidates = []
        for _, pr in _unmatched_portal().iterrows():
            pid = pr["_pid"]
            if pid in matched_pids:
                continue
            iv_p = float(pr.get("invoice_value", 0) or 0)
            missing_gstin = not books_has_gstin or not _has_gstin(pr.get("gstin"))
            if missing_gstin:
                # Blank GSTIN: amount and date must agree EXACTLY, and the
                # party-name bar is the relaxed one.
                if abs(iv_b - iv_p) > 0.005:
                    continue
                if _date_diff_days(date_b, pr.get("invoice_date")) != 0:
                    continue
                fz_threshold = missing_gstin_threshold
            else:
                if not _within_tolerance(iv_b, iv_p, config):
                    continue
                if not _date_within_window(date_b, pr.get("invoice_date"), config):
                    continue
                fz_threshold = threshold
            name_p = str(pr.get("party_name", "")).strip()
            fz_score = fuzz.token_sort_ratio(name_b.lower(), name_p.lower())
            if fz_score < fz_threshold:
                continue
            prox = _proximity(abs(iv_b - iv_p), float(config["amount_tolerance"]["absolute"]))
            frag = (
                f" via fuzzy party name match ({fz_score:.0f}%: "
                f"'{name_b}' ↔ '{name_p}'"
                + (", one side had no GSTIN captured, matched on exact amount and date"
                   if missing_gstin else "")
                + ")"
            )
            # The structural-GSTIN deduction belongs to EVERY pass, not just
            # the exact ones: a match whose GSTIN is grossly malformed is a
            # weaker identity match than an ordinary fuzzy-name match, and must
            # not land on the same number by coincidence.
            identity = _score(baseline_fz, config, invalid_gstin=br_struct_invalid)
            rank = _rank(baseline_fz, prox, config, invalid_gstin=br_struct_invalid)
            candidates.append((rank, identity, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for _r, _i, p, f in candidates if p["_pid"] == chosen["_pid"])
            r = _make_pair_result(
                br, chosen, "Matched on party name and amount", baseline_fz,
                extra_reason=chosen_frag + amb_note, forced_low=forced,
                score_override=final_score,
            )
            results.append(r)
            matched_bids.add(bid)
            matched_pids.add(chosen["_pid"])

    # ---- Pass 5: Cross-period (re-run passes 1-3 against adjacent period) ----
    if adjacent_portal is not None and config.get("cross_period_matching", True):
        baseline_xp = float(baselines["cross_period"])
        adj = adjacent_portal.copy()
        adj_period = None
        if not adj.empty:
            adj_period = _period_label(str(adj.iloc[0].get("invoice_date", "")))

        for _, br in _unmatched_books().iterrows():
            bid = br["_bid"]
            if bid in matched_bids:
                continue
            gstin_b = str(br.get("gstin", "")).strip().upper()
            br_struct_invalid = _gstin_structurally_invalid(gstin_b)
            norm_b = br["_norm_inv"]
            trail_b = br.get("_trail_num")
            iv_b = float(br.get("invoice_value", 0) or 0)
            date_b = br.get("invoice_date")
            books_period = _period_label(date_b)

            candidates = []
            for _, pr in adj.iterrows():
                pid = pr["_pid"]
                if pid in adj_matched_pids:
                    continue
                gstin_p = str(pr.get("gstin", "")).strip().upper()
                if gstin_p != gstin_b:
                    continue
                iv_p = float(pr.get("invoice_value", 0) or 0)

                # Try exact match
                if pr["_norm_inv"] == norm_b:
                    prox = _proximity(abs(iv_b - iv_p), float(config["amount_tolerance"]["absolute"]))
                    frag = (
                        f" Timing Difference: not present in the {books_period} 2B, "
                        f"matched to the {adj_period} 2B on GSTIN and invoice number. "
                        f"Supplier filed late."
                    )
                    identity = _score(baseline_xp, config, cross_period=True, invalid_gstin=br_struct_invalid)
                    rank = _rank(baseline_xp, prox, config, cross_period=True, invalid_gstin=br_struct_invalid)
                    candidates.append((rank, identity, pr, frag))
                    continue

                # Try trailing numeric + amount
                if trail_b and pr.get("_trail_num") == trail_b and _within_tolerance(iv_b, iv_p, config):
                    prox = _proximity(abs(iv_b - iv_p), float(config["amount_tolerance"]["absolute"]))
                    frag = (
                        f" Timing Difference: matched to {adj_period} 2B on GSTIN "
                        f"and trailing invoice number + amount."
                    )
                    identity = _score(baseline_xp, config, cross_period=True, invalid_gstin=br_struct_invalid)
                    rank = _rank(baseline_xp, prox, config, cross_period=True, invalid_gstin=br_struct_invalid)
                    candidates.append((rank, identity, pr, frag))
                    continue

                # Try amount + date
                if _within_tolerance(iv_b, iv_p, config):
                    prox = _proximity(abs(iv_b - iv_p), float(config["amount_tolerance"]["absolute"]))
                    frag = (
                        f" Timing Difference: matched to {adj_period} 2B on GSTIN + amount."
                    )
                    identity = _score(baseline_xp, config, cross_period=True, invalid_gstin=br_struct_invalid)
                    rank = _rank(baseline_xp, prox, config, cross_period=True, invalid_gstin=br_struct_invalid)
                    candidates.append((rank, identity, pr, frag))

            if candidates:
                chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
                chosen_frag = next(f for _r, _i, p, f in candidates if p["_pid"] == chosen["_pid"])
                r = _make_pair_result(
                    br, chosen, "Cross-period match", baseline_xp,
                    extra_reason=chosen_frag + amb_note,
                    cross_period=True, forced_low=forced,
                    score_override=final_score,
                )
                results.append(r)
                matched_bids.add(bid)
                adj_matched_pids.add(chosen["_pid"])

    # ---- Unmatched residuals ----
    has_adjacent = adjacent_portal is not None
    for _, br in _unmatched_books().iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        gstin = str(br.get("gstin", "")).strip().upper()
        inv = str(br.get("invoice_number", ""))
        iv = float(br.get("invoice_value", 0) or 0)
        gstin_valid, gstin_note = _validate_gstin_structure(gstin)

        reason = (
            f"Not in Portal: GSTIN {gstin}, invoice {inv}, {_fmt(iv)} — "
            f"no candidate in any pass"
        )
        if not has_adjacent:
            reason += ", and no adjacent-period file was supplied to check for late filing."
        else:
            reason += f" including cross-period check."
        if not gstin_valid:
            reason += f" Note: {gstin_note}."

        # Surplus copy of a duplicated books invoice: reported as an exception
        # in its own right (the second ITC claim has no portal support), NOT as
        # another paired comparison. The one portal record was matched exactly
        # once by the passes above, so no comparison is re-emitted here.
        duplicate_count = int((duplicate_keys or {}).get((gstin, str(br.get("_norm_inv") or "")), 0))
        duplicate_diff_type = None
        if duplicate_count > 1:
            duplicate_diff_type = "Duplicate in Books"
            reason += (
                f" Duplicate in Books: this GSTIN + invoice number appears "
                f"{duplicate_count} times in the books, and one copy was already "
                f"matched. This surplus copy is the additional ITC claim and is "
                f"reported separately rather than compared a second time."
            )

        results.append(_make_result(
            "Not in Portal", 0.0, config,
            books_record=_row_json(br, exclude=_EXCLUDE_COLS),
            portal_record=None,
            match_reason=reason,
            difference_type=duplicate_diff_type,
        ))
        matched_bids.add(bid)

    for _, pr in _unmatched_portal().iterrows():
        pid = pr["_pid"]
        if pid in matched_pids:
            continue
        gstin = str(pr.get("gstin", "")).strip().upper()
        inv = str(pr.get("invoice_number", ""))
        iv = float(pr.get("invoice_value", 0) or 0)

        amd_note = str(pr.get("_amendment_note", "")).strip()
        reason = (
            f"Not in Books: GSTIN {gstin}, invoice {inv}, {_fmt(iv)} — "
            f"no matching book entry found across any pass."
        )
        if amd_note:
            reason += f" {amd_note}"

        results.append(_make_result(
            "Not in Books", 0.0, config,
            books_record=None,
            portal_record=_row_json(pr, exclude=_EXCLUDE_COLS),
            match_reason=reason,
        ))
        matched_pids.add(pid)

    return results


# ===================================================================
# PUBLIC API
# ===================================================================

def match_gst(
    books_df: pd.DataFrame,
    portal_df: pd.DataFrame,
    config: dict,
    *,
    adjacent_portal_df: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    """Run GST reconciliation: books vs portal.

    Args:
        books_df: Canonical GST DataFrame from load_gst_pair() — books side.
        portal_df: Canonical GST DataFrame from load_gst_pair() — portal side.
        config: The 'gst' block from matching_rules.yaml.
        adjacent_portal_df: Optional portal DataFrame for an adjacent period,
            enabling cross-period (timing difference) matching.

    Returns:
        List of result dicts shaped for match_results table.
    """
    # --- Pre-processing ---
    books = preprocess_books(books_df, config)
    b2b_portal, rc_portal, import_portal, summary = preprocess_portal(portal_df, config)

    # CREDIT notes are a SEPARATE document class. The portal carries them on
    # its B2B-CDNR sheet (declared by the report's own Credit Notes card and
    # never reconciled as invoices), so a books CREDIT-note row must be kept
    # OUT of the invoice-matching pool — otherwise every one of them becomes a
    # false "Not in Portal" exception that contradicts that label. Debit notes
    # stay in the pool (see `_books_note_type`).
    note_mask = books["_doc_type"].isin(_NOTE_TYPES)
    note_count = int(note_mask.sum())
    if note_count:
        print(
            f"[GST] {note_count} books credit note row(s) excluded from the "
            f"invoice-matching pool (reported separately by the Credit Notes card)."
        )
        books = books[~note_mask].copy()

    # The SAME exclusion applies to the portal side. A GSTR-2B carries credit
    # notes on its B2B-CDNR sheet; once their note number/date/value are parsed
    # they have a usable identity, so without this they would enter the invoice
    # pool and surface as false "Not in Books" exceptions — contradicting the
    # Credit Notes card that already reports them. Debit notes are untouched.
    portal_credit_mask = b2b_portal["_doc_type"].isin(_CREDIT_NOTE_TYPES)
    portal_credit_count = int(portal_credit_mask.sum())
    if portal_credit_count:
        print(
            f"[GST] {portal_credit_count} portal credit note row(s) excluded from "
            f"the invoice-matching pool (reported separately by the Credit Notes card)."
        )
        b2b_portal = b2b_portal[~portal_credit_mask].copy()

    _print_preprocessing_summary(summary)

    # Pre-process adjacent portal if supplied
    adj_b2b = None
    if adjacent_portal_df is not None and not adjacent_portal_df.empty:
        adj_b2b, _, _, adj_summary = preprocess_portal(adjacent_portal_df, config)
        print(f"[GST] Adjacent period portal: {len(adj_b2b)} B2B records available for cross-period matching.")

    # --- Duplicate detection (books-internal) ---
    # Only the duplicate KEYS are needed. The five matching passes below already
    # produce exactly ONE match plus N-1 residuals for a duplicated key (a
    # portal record is matched at most once), so the surplus copy is labelled as
    # a residual rather than re-emitted as its own comparison — which is what
    # produced the S2-09 "4 rows instead of 2" defect.
    duplicate_keys = _books_duplicate_keys(books)

    # --- Match each pool ---
    # For simplicity in PoC, books are not separated into pools — all books
    # match against b2b. RC and import pools match against the same books.
    # A production system would separate books by voucher type.
    all_results: list[dict[str, Any]] = []

    b2b_results = _match_pool(
        books, b2b_portal, config,
        adjacent_portal=adj_b2b,
        pool_label="B2B",
        duplicate_keys=duplicate_keys,
    )
    all_results.extend(b2b_results)

    # If RC or import portal records exist, try to match them too
    if not rc_portal.empty:
        rc_results = _match_pool(books, rc_portal, config, pool_label="Reverse Charge")
        all_results.extend(rc_results)

    if not import_portal.empty:
        imp_results = _match_pool(books, import_portal, config, pool_label="Import")
        all_results.extend(imp_results)

    # --- Account for superseded originals ---
    # Superseded records were removed from matching but need representation.
    # They are covered by their amendment's match_reason noting what was
    # superseded — no separate result row needed since the amendment
    # replaced them in the pool. The summary reports the count.

    # Value-at-risk semantics (D6) — stored on every result so the Review UI
    # reads them rather than re-deriving them. Classification is untouched.
    annotate_value_at_risk(all_results)

    return all_results


def invoice_key_collisions(
    books_df: pd.DataFrame, portal_df: pd.DataFrame, config: dict,
) -> list[dict[str, Any]]:
    """Report invoice-number keys that collide within one GSTIN on either side.

    A collision means two DIFFERENT raw invoice numbers derive the same key
    for the same supplier, so the key cannot tell them apart. Such a key must
    be REPORTED, never silently used — the caller surfaces these as run notes.
    """
    from src.matching.invoice_keys import find_key_collisions

    out: list[dict[str, Any]] = []
    for df, side in ((books_df, "books"), (portal_df, "portal")):
        if df is None or df.empty:
            continue
        records = df.to_dict("records")
        out.extend(find_key_collisions(
            records,
            lambda v: _normalize_invoice_number(v, config),
            side=side,
        ))
        out.extend(find_key_collisions(
            records,
            lambda v: _trailing_numeric_key(v, config),
            side=side,
        ))
    return out


def _print_preprocessing_summary(summary: dict) -> None:
    print(f"[GST Pre-processing]")
    print(f"  Document types: {summary['document_type_counts']}")
    print(f"  Amendments resolved: {summary['amendments_resolved']}")
    print(f"  Dangling amendment refs: {summary['dangling_amendment_refs']}")
    print(f"  Sign-flipped values: {summary['sign_flipped_values']}")
    print(f"  Pools — B2B: {summary['b2b_count']}, "
          f"RC: {summary['reverse_charge_count']}, "
          f"Import: {summary['import_count']}, "
          f"ISD: {summary['isd_count']}")
