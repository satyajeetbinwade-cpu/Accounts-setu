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

def _score(baseline: float, config: dict, *, proximity_factor: float = 1.0,
           cross_period: bool = False, fuzzy: bool = False,
           invalid_gstin: bool = False) -> float:
    baselines = config["confidence_baselines"]
    s = baseline * proximity_factor
    if cross_period:
        s += float(config.get("cross_period_penalty", -12))
    if invalid_gstin:
        s -= 5.0  # small penalty; still matched, but flagged
    return max(0.0, min(100.0, round(s, 2)))


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


def preprocess_books(books_df: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    """Pre-process books records: add internal keys, normalise signs for
    credit notes (negative invoice_value treated as credit note)."""
    df = books_df.copy()
    df["_bid"] = range(len(df))
    df["_norm_inv"] = df["invoice_number"].apply(lambda v: _normalize_invoice_number(v, config))
    df["_trail_num"] = df["invoice_number"].apply(lambda v: _trailing_numeric_key(v, config))
    df["_fy"] = df["invoice_date"].apply(_financial_year)

    # Derive document type from sign
    df["_doc_type"] = "Invoice"
    value_cols = ["taxable_value", "cgst", "sgst", "igst", "cess", "total_tax", "invoice_value"]
    for idx, row in df.iterrows():
        inv_val = float(row.get("invoice_value", 0) or 0)
        if inv_val < 0:
            df.at[idx, "_doc_type"] = "Credit Note"

    df["_amendment_note"] = ""
    return df


# ===================================================================
# DUPLICATE DETECTION (Section 5)
# ===================================================================

def _detect_books_duplicates(books: pd.DataFrame, config: dict) -> list[dict[str, Any]]:
    """Find duplicate invoices in books (same GSTIN + normalised invoice number).
    Returns findings with classification='Amount Difference',
    difference_type='Duplicate in Books', portal_record=None."""
    results: list[dict[str, Any]] = []
    _EXCLUDE = ["_bid", "_norm_inv", "_trail_num", "_fy", "_doc_type", "_amendment_note"]

    grouped = books.groupby(["gstin", "_norm_inv"], dropna=False)
    for (gstin, norm_inv), group in grouped:
        if len(group) <= 1:
            continue
        gstin_str = str(gstin).strip().upper()
        orig_invs = group["invoice_number"].unique()
        inv_display = ", ".join(str(x) for x in orig_invs)
        total_claimed = group["total_tax"].astype(float).sum()
        bids = list(group["_bid"])
        baselines = config["confidence_baselines"]
        score = _score(float(baselines["exact"]), config)

        for _, row in group.iterrows():
            reason = (
                f"Duplicate in Books: GSTIN {gstin_str}, invoice '{inv_display}' "
                f"appears {len(group)} times in books claiming total ITC of {_fmt(total_claimed)}. "
                f"Duplicate ITC claims are a material error."
            )
            results.append(_make_result(
                "Amount Difference", score, config,
                books_record=_row_json(row, exclude=_EXCLUDE),
                portal_record=None,
                match_reason=reason,
                difference_type="Duplicate in Books",
                matched_record_ids=bids,
            ))

    return results


# ===================================================================
# MATCHING PASSES
# ===================================================================

_EXCLUDE_COLS = ["_bid", "_pid", "_norm_inv", "_trail_num", "_fy",
                 "_doc_type", "_amendment_note"]


def _compare_pair(
    br: pd.Series, pr: pd.Series, config: dict,
) -> tuple[str, str | None, str]:
    """Compare a books-portal pair. Return (classification, difference_type,
    detail_fragment) describing what agrees and what doesn't."""
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

    # --- Tax split mismatch (highest-value finding) ---
    total_tax_agree = _within_tolerance(total_tax_b, total_tax_p, config)
    if total_tax_agree and not components_agree:
        detail = (
            f"Tax Split Mismatch: total tax agrees at {_fmt(total_tax_b)}, "
            f"but components differ — {'; '.join(component_details)}. "
            f"Indicates a place-of-supply difference."
        )
        return "Amount Difference", "Tax Split Mismatch", detail

    if not doc_agree:
        detail = (
            f"Document Type Mismatch: books has '{doc_type_b}', portal has '{doc_type_p}'."
        )
        return "Amount Difference", "Document Type Mismatch", detail

    if not tv_agree:
        detail = (
            f"Taxable Value Difference: books {_fmt(tv_b)} vs portal {_fmt(tv_p)}, "
            f"difference {_fmt(abs(tv_b - tv_p))}."
        )
        return "Amount Difference", "Taxable Value Difference", detail

    if not components_agree:
        detail = (
            f"Tax Amount Difference: {'; '.join(component_details)}."
        )
        return "Amount Difference", "Tax Amount Difference", detail

    # Check rate mismatch — only when the two sides imply DIFFERENT rates.
    # A blended rate across several buckets (e.g. 2.5% + 9% = 15.6%) is a
    # legitimate multi-rate invoice, not a mismatch, when both sides agree on
    # it. The previous code flagged any rate that wasn't a single standard
    # slab, turning every multi-rate match into a false "Rate Mismatch".
    rate_b = _effective_rate(br)
    rate_p = _effective_rate(pr)
    if rate_b is not None and rate_p is not None:
        if abs(rate_b - rate_p) > 0.5:
            detail = (
                f"Rate Mismatch: books implies {rate_b}%, portal implies {rate_p}%."
            )
            return "Amount Difference", "Rate Mismatch", detail

    # Check for rounding — a difference OUTSIDE the amount tolerance but
    # inside the rounding tolerance. A difference already inside the amount
    # tolerance is agreement, not a rounding difference; the previous code
    # flagged every sub-rupee difference as "Rounding", turning exact matches
    # (e.g. a ₹0.02 tax residual) into exceptions.
    iv_diff = abs(iv_b - iv_p)
    any_rounding = False
    for comp in TAX_COMPONENTS + ["taxable_value"]:
        cb = float(br.get(comp, 0) or 0)
        cp = float(pr.get(comp, 0) or 0)
        if not _within_tolerance(cb, cp, config) and _is_rounding(cb, cp, config):
            any_rounding = True

    if any_rounding:
        detail = (
            f"All values agree within rounding tolerance; "
            f"invoice value books {_fmt(iv_b)} vs portal {_fmt(iv_p)}."
        )
        return "Amount Difference", "Rounding", detail

    # Full match
    detail = (
        f"taxable value, CGST, SGST, IGST and cess all agree"
        f"{' within ' + _fmt(float(config['amount_tolerance']['absolute'])) if iv_diff > 0 else ''}."
    )
    return "Matched", None, detail


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
    candidates: list[tuple[float, pd.Series, str]],
    config: dict,
) -> tuple[pd.Series, float, str, bool]:
    """Given scored candidates [(score, portal_row, reason_fragment), ...],
    select best, apply ambiguity penalty, return (chosen, final_score,
    ambiguity_note, forced_low).

    The note always reports the CHOSEN candidate's own score against the
    runner-up's — never the penalised score, which would read as the chosen
    candidate having scored BELOW the runner-up (D4b). The penalty is applied
    to the returned score so the stored confidence reflects the ambiguity.
    """
    candidates.sort(key=lambda x: -x[0])
    chosen_score, best_row, best_frag = candidates[0]
    final_score = chosen_score
    forced_low = False
    note = ""

    if len(candidates) > 1:
        runner_score = candidates[1][0]
        margin = float(config.get("ambiguity_margin", 5.0))
        penalty = float(config["confidence_baselines"].get("ambiguity_penalty", -8))

        # Scale penalty by closeness of runner-up
        gap = chosen_score - runner_score
        if gap < margin:
            forced_low = True
            final_score = chosen_score + penalty
            note = (
                f" Ambiguous: {len(candidates)} portal candidates from this GSTIN; "
                f"chosen on highest score ({chosen_score:.0f} vs runner-up {runner_score:.0f}); "
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

        iv_b = float(br.get("invoice_value", 0) or 0)
        iv_p = float(pr.get("invoice_value", 0) or 0)
        diff = abs(iv_b - iv_p)
        prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))

        if score_override is not None:
            # The ambiguity resolver already applied its penalty to this
            # score — recomputing from the baseline would silently discard it.
            score = max(0.0, min(100.0, round(float(score_override), 2)))
        else:
            score = _score(baseline, config, proximity_factor=prox,
                           cross_period=cross_period,
                           invalid_gstin=not gstin_valid)

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
    books_core_counts = _core_counts(books)
    portal_core_counts = _core_counts(portal)
    for _, br in _unmatched_books().iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        gstin_b = str(br.get("gstin", "")).strip().upper()
        norm_b = br["_norm_inv"]
        core_b = br.get("_trail_num")

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
            diff = abs(iv_b - iv_p)
            prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
            sc = _score(baseline_exact, config, proximity_factor=prox)
            frag = ""
            if core_key:
                frag = (
                    f" after invoice number variance ('{br.get('invoice_number', '')}' in books, "
                    f"'{pr.get('invoice_number', '')}' on portal — same numeric core {core_b})"
                )
            candidates.append((sc, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for s, p, f in candidates if p["_pid"] == chosen["_pid"])
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
            diff = abs(iv_b - iv_p)
            prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
            sc = _score(baseline_var, config, proximity_factor=prox)
            inv_b_str = str(br.get("invoice_number", ""))
            inv_p_str = str(pr.get("invoice_number", ""))
            frag = (
                f" after invoice number variance ('{inv_b_str}' in books, "
                f"'{inv_p_str}' on portal)"
            )
            candidates.append((sc, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for s, p, f in candidates if p["_pid"] == chosen["_pid"])
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
            diff = abs(iv_b - iv_p)
            prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
            sc = _score(baseline_ad, config, proximity_factor=prox)
            dd = _date_diff_days(date_b, pr.get("invoice_date"))
            frag = ""
            if dd and dd > 0:
                frag = f"; date differs by {dd} day(s), within the {config['date_tolerance_days']}-day window"
            candidates.append((sc, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for s, p, f in candidates if p["_pid"] == chosen["_pid"])
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
    for _, br in _unmatched_books().iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        name_b = str(br.get("party_name", "")).strip()
        iv_b = float(br.get("invoice_value", 0) or 0)
        date_b = br.get("invoice_date")
        if not name_b:
            continue

        candidates = []
        for _, pr in _unmatched_portal().iterrows():
            pid = pr["_pid"]
            if pid in matched_pids:
                continue
            iv_p = float(pr.get("invoice_value", 0) or 0)
            if not _within_tolerance(iv_b, iv_p, config):
                continue
            if not _date_within_window(date_b, pr.get("invoice_date"), config):
                continue
            name_p = str(pr.get("party_name", "")).strip()
            fz_score = fuzz.token_sort_ratio(name_b.lower(), name_p.lower())
            if fz_score < threshold:
                continue
            diff = abs(iv_b - iv_p)
            prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
            sc = _score(baseline_fz, config, proximity_factor=prox, fuzzy=True)
            frag = (
                f" via fuzzy party name match ({fz_score:.0f}%: "
                f"'{name_b}' ↔ '{name_p}')"
            )
            candidates.append((sc, pr, frag))

        if candidates:
            chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
            chosen_frag = next(f for s, p, f in candidates if p["_pid"] == chosen["_pid"])
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
                    diff = abs(iv_b - iv_p)
                    prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
                    sc = _score(baseline_xp, config, proximity_factor=prox, cross_period=True)
                    frag = (
                        f" Timing Difference: not present in the {books_period} 2B, "
                        f"matched to the {adj_period} 2B on GSTIN and invoice number. "
                        f"Supplier filed late."
                    )
                    candidates.append((sc, pr, frag))
                    continue

                # Try trailing numeric + amount
                if trail_b and pr.get("_trail_num") == trail_b and _within_tolerance(iv_b, iv_p, config):
                    diff = abs(iv_b - iv_p)
                    prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
                    sc = _score(baseline_xp, config, proximity_factor=prox, cross_period=True)
                    frag = (
                        f" Timing Difference: matched to {adj_period} 2B on GSTIN "
                        f"and trailing invoice number + amount."
                    )
                    candidates.append((sc, pr, frag))
                    continue

                # Try amount + date
                if _within_tolerance(iv_b, iv_p, config):
                    diff = abs(iv_b - iv_p)
                    prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
                    sc = _score(baseline_xp, config, proximity_factor=prox, cross_period=True)
                    frag = (
                        f" Timing Difference: matched to {adj_period} 2B on GSTIN + amount."
                    )
                    candidates.append((sc, pr, frag))

            if candidates:
                chosen, final_score, amb_note, forced = _resolve_ambiguity(candidates, config)
                chosen_frag = next(f for s, p, f in candidates if p["_pid"] == chosen["_pid"])
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

        results.append(_make_result(
            "Not in Portal", 0.0, config,
            books_record=_row_json(br, exclude=_EXCLUDE_COLS),
            portal_record=None,
            match_reason=reason,
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

    _print_preprocessing_summary(summary)

    # Pre-process adjacent portal if supplied
    adj_b2b = None
    if adjacent_portal_df is not None and not adjacent_portal_df.empty:
        adj_b2b, _, _, adj_summary = preprocess_portal(adjacent_portal_df, config)
        print(f"[GST] Adjacent period portal: {len(adj_b2b)} B2B records available for cross-period matching.")

    # --- Duplicate detection (books-internal) ---
    dup_results = _detect_books_duplicates(books, config)

    # --- Match each pool ---
    # For simplicity in PoC, books are not separated into pools — all books
    # match against b2b. RC and import pools match against the same books.
    # A production system would separate books by voucher type.
    all_results = list(dup_results)

    b2b_results = _match_pool(
        books, b2b_portal, config,
        adjacent_portal=adj_b2b,
        pool_label="B2B",
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
