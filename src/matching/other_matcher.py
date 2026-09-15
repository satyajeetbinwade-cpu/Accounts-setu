"""2C "Other" matching — Bank, Vendor Ledger, 26AS, Opening Balances, Loan
Sheet, Salary.

REUSE-FIRST: this is NOT a separate engine. It layers six additional
matching-key configurations onto the SAME shared engine validated in
sub-phase A (2A/2B) — it imports and reuses gst_matcher's tolerance,
scoring, banding and result-construction helpers directly, so the
confidence semantics and the result shape are identical across 2A/2B/2C.

The six sources share a generic reference/date/amount identity. Each source
is a matching-key configuration (see Module 2's MatchingKeyConfig), not a
new code path — adding a seventh 2C source is a configuration change.

Result shape is identical to gst_matcher._make_result / tds_matcher, so
fingerprinting, persistence and the exception layer treat 2C uniformly.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from rapidfuzz import fuzz

# Reuse the shared engine's helpers directly — do not re-implement.
from src.matching.gst_matcher import (
    CLASSIFICATIONS,
    _band,
    _date_within_window,
    _is_rounding,
    _make_result,
    _normalize_invoice_number,
    _within_tolerance,
)

DIFFERENCE_TYPES = (
    "Amount Difference",
    "Timing Difference",
    "Rounding",
    "Unexplained",
)

# Generic canonical fields for a 2C record.
_REFERENCE_FIELDS = ("reference", "invoice_number", "voucher_number", "challan_number")
_AMOUNT_FIELDS = ("amount", "invoice_value", "amount_paid_credited", "tax_deducted")
_DATE_FIELDS = ("date", "invoice_date", "deposit_date")
_PARTY_FIELDS = ("party_name", "deductee_name", "narration")


def _first(row: pd.Series, fields: tuple[str, ...]) -> Any:
    for f in fields:
        v = row.get(f)
        if v is not None and not (isinstance(v, float) and pd.isna(v)) and v != "":
            return v
    return None


def _amount(row: pd.Series) -> float:
    v = _first(row, _AMOUNT_FIELDS)
    try:
        return abs(float(v)) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _reference(row: pd.Series) -> str:
    v = _first(row, _REFERENCE_FIELDS)
    return _normalize_invoice_number(v) if v is not None else ""


def _date(row: pd.Series) -> Any:
    return _first(row, _DATE_FIELDS)


def _party(row: pd.Series) -> str:
    v = _first(row, _PARTY_FIELDS)
    return str(v).strip() if v is not None else ""


def match_other(
    books_df: pd.DataFrame, portal_df: pd.DataFrame, config: dict,
) -> list[dict[str, Any]]:
    """Match a 2C books side against its portal side using the shared engine.

    Passes (mirroring the shared engine's structure, on the generic 2C key):
      1. exact reference + amount
      2. reference variant + amount
      3. amount + date window
      4. fuzzy party/narration + amount + date
    Residuals are emitted as Not in Books / Not in Portal.
    """
    results: list[dict[str, Any]] = []
    portal_used: set[int] = set()
    books_used: set[int] = set()

    books = books_df.reset_index(drop=True)
    portal = portal_df.reset_index(drop=True)

    # Pass 1 — exact reference + amount.
    for bi, brow in books.iterrows():
        bref, bamt = _reference(brow), _amount(brow)
        for pi, prow in portal.iterrows():
            if pi in portal_used:
                continue
            if _reference(prow) == bref and _within_tolerance(bamt, _amount(prow), config):
                results.append(_make_result(
                    "Matched", 98.0, config,
                    books_record=_row_json(brow), portal_record=_row_json(prow),
                    match_reason=f"Exact reference match on '{bref}' with amount within tolerance.",
                ))
                portal_used.add(pi)
                books_used.add(bi)
                break

    # Pass 2 — reference variant + amount.
    for bi, brow in books.iterrows():
        if bi in books_used:
            continue
        bref, bamt = _reference(brow), _amount(brow)
        for pi, prow in portal.iterrows():
            if pi in portal_used:
                continue
            pref = _reference(prow)
            if bref and pref and (bref in pref or pref in bref) and _within_tolerance(bamt, _amount(prow), config):
                results.append(_make_result(
                    "Matched", 90.0, config,
                    books_record=_row_json(brow), portal_record=_row_json(prow),
                    match_reason=f"Reference variant match ('{bref}' ~ '{pref}') with amount within tolerance.",
                ))
                portal_used.add(pi)
                books_used.add(bi)
                break

    # Pass 3 — amount + date window.
    for bi, brow in books.iterrows():
        if bi in books_used:
            continue
        bamt = _amount(brow)
        for pi, prow in portal.iterrows():
            if pi in portal_used:
                continue
            if _within_tolerance(bamt, _amount(prow), config) and _date_within_window(_date(brow), _date(prow), config):
                diff = abs(bamt - _amount(prow))
                if _is_rounding(bamt, _amount(prow), config):
                    results.append(_make_result(
                        "Amount Difference", 80.0, config,
                        books_record=_row_json(brow), portal_record=_row_json(prow),
                        match_reason=f"Amounts differ by ₹{diff:,.2f} — within rounding tolerance.",
                        difference_type="Rounding",
                    ))
                else:
                    results.append(_make_result(
                        "Matched", 80.0, config,
                        books_record=_row_json(brow), portal_record=_row_json(prow),
                        match_reason="Amount and date within tolerance (no reference key available).",
                    ))
                portal_used.add(pi)
                books_used.add(bi)
                break

    # Pass 4 — fuzzy party/narration + amount + date.
    threshold = float(config.get("fuzzy_threshold", 80))
    for bi, brow in books.iterrows():
        if bi in books_used:
            continue
        bparty, bamt = _party(brow), _amount(brow)
        best_pi, best_score = None, 0.0
        for pi, prow in portal.iterrows():
            if pi in portal_used:
                continue
            if not _within_tolerance(bamt, _amount(prow), config):
                continue
            if not _date_within_window(_date(brow), _date(prow), config):
                continue
            score = fuzz.token_set_ratio(bparty, _party(prow)) if bparty else 0.0
            if score > best_score:
                best_pi, best_score = pi, score
        if best_pi is not None and best_score >= threshold:
            prow = portal.loc[best_pi]
            results.append(_make_result(
                "Matched", 65.0, config,
                books_record=_row_json(brow), portal_record=_row_json(prow),
                match_reason=f"Fuzzy party/narration match ({best_score:.0f}%) with amount and date within tolerance.",
            ))
            portal_used.add(best_pi)
            books_used.add(bi)

    # Residuals.
    for bi, brow in books.iterrows():
        if bi not in books_used:
            results.append(_make_result(
                "Not in Portal", 0.0, config,
                books_record=_row_json(brow), portal_record=None,
                match_reason="Books record has no counterpart on the portal side.",
            ))
    for pi, prow in portal.iterrows():
        if pi not in portal_used:
            results.append(_make_result(
                "Not in Books", 0.0, config,
                books_record=None, portal_record=_row_json(prow),
                match_reason="Portal record has no counterpart in the books.",
            ))

    return results


def _row_json(row: pd.Series) -> str:
    import json

    d = {}
    for k, v in row.items():
        if str(k).startswith("_"):
            continue
        d[k] = None if (isinstance(v, float) and pd.isna(v)) else v
    return json.dumps(d, default=str)