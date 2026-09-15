"""Deterministic Tier-A fuzzy invoice matching.

Pure rule-based matching -- the LLM plays NO role here. Confidence scoring
follows the architecture HTML: High (exact), Medium (fuzzy within tolerance),
Low (no acceptable match).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from .config import MatchRules
from .ims import apply_meta
from .models import Confidence, Invoice, MatchResult, MatchStatus


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,                # deletion
                cur[j - 1] + 1,             # insertion
                prev[j - 1] + (ca != cb),   # substitution
            ))
        prev = cur
    return prev[-1]


def date_window_ok(inv_date: date, other_date: date, window_days: int) -> bool:
    return abs((inv_date - other_date).days) <= window_days


def value_within_tolerance(a: Decimal, b: Decimal, tolerance: float) -> bool:
    """Taxable value within percentage tolerance (v1 default +-1%)."""
    if b == 0:
        return a == 0
    return abs((a - b) / b) <= Decimal(str(tolerance))


def _score_candidate(
    portal: Invoice,
    book: Invoice,
    rules: MatchRules,
) -> tuple[float, int, int, float | None, list[str]]:
    """Score one (portal, book) candidate. Higher score = better candidate.
    Returns (score, edit_distance, date_delta_days, value_delta_pct, reasons).
    Returns score = -1.0 when GSTINs mismatch and exact GSTIN is required.
    """
    reasons: list[str] = []
    if rules.require_gstin_exact and portal.gstin != book.gstin:
        return (-1.0, 99, abs((portal.date - book.date).days), None, ["GSTIN mismatch"])

    edit_dist = levenshtein(portal.invoice_no, book.invoice_no)
    date_delta = abs((portal.date - book.date).days)
    val_delta_pct: float | None = None
    if book.taxable_value != 0:
        val_delta_pct = float(abs(portal.taxable_value - book.taxable_value)
                              / abs(book.taxable_value))

    # Invoice number is the PRIMARY match key. GSTIN + invoice no must be the
    # matched pair; date and value only refine confidence. Without an acceptable
    # invoice edit distance this candidate cannot match at all.
    if edit_dist > rules.invoice_edit_max:
        return (-1.0, edit_dist, date_delta, val_delta_pct,
                [f"invoice no {edit_dist} edits away (max {rules.invoice_edit_max})"])

    score = 0.0
    score += 100.0 - edit_dist * 5.0
    reasons.append(f"invoice no within {edit_dist} edit(s)")

    if date_window_ok(portal.date, book.date, rules.date_window_days):
        score += 30.0 - min(date_delta, rules.date_window_days) * 2.0
        reasons.append(f"date within {date_delta}d window")
    else:
        reasons.append(f"date {date_delta}d outside window (max {rules.date_window_days}d)")

    if val_delta_pct is not None and val_delta_pct <= rules.value_tolerance:
        score += 40.0 - min(val_delta_pct * 100.0, rules.value_tolerance * 100.0)
        reasons.append(f"value within {val_delta_pct:.2%} tolerance")
    else:
        reasons.append("value outside tolerance")

    return (score, edit_dist, date_delta, val_delta_pct, reasons)


def _mk_forward(
    portal: Invoice,
    book: Optional[Invoice],
    status: MatchStatus,
    confidence: Confidence,
    score: float,
    edit_dist: int,
    date_delta: int,
    value_pct: float | None,
    reasons: list[str],
    rules: MatchRules | None = None,
) -> MatchResult:
    return MatchResult(
        portal_invoice=portal,
        book_invoice=book,
        status=status,
        confidence=confidence,
        match_score=round(score, 2),
        edit_distance=edit_dist,
        date_delta=date_delta,
        value_delta_pct=round(value_pct, 4) if value_pct is not None else None,
        reasons=reasons,
        evidence={
            "match_score": round(score, 2),
            "edit_distance": edit_dist,
            "date_delta_days": date_delta,
            "value_delta_pct": round(value_pct, 4) if value_pct is not None else None,
        },
    )


def _mk_unmatched_book(book: Invoice,
                      rules: MatchRules | None = None) -> MatchResult:
    return MatchResult(
        portal_invoice=book,   # matched row: the "portal side" is empty
        book_invoice=book,
        status=MatchStatus.NOT_IN_PORTAL,
        confidence=Confidence.LOW,
        match_score=0.0,
        edit_distance=99,
        date_delta=99,
        value_delta_pct=None,
        reasons=["In books but absent from portal (check reverse charge / non-ITC)"],
        evidence={"invoice": book.invoice_no, "gstin": book.gstin,
                  "taxable_value": str(book.taxable_value)},
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def match_engine(
    portal_invoices: list[Invoice],
    book_invoices: list[Invoice],
    rules: MatchRules | None = None,
) -> list[MatchResult]:
    """Match GSTR-2B (portal) invoices against Tally books.

    Strategy (deterministic):
      1. Bucket books by GSTIN for fast lookup.
      2. For each portal invoice, find the best candidate within its GSTIN
         bucket; a candidate already claimed by an earlier portal row is skipped.
      3. Classify:
            - ALL keys align  -> Matched (High)
            - invoice no (+GSTIN) match but value differs -> Amount Diff (Medium)
            - otherwise -> Not in Books (Low)
      4. Unclaimed book invoices -> Not in Portal (Low).
    """
    rules = rules or MatchRules()
    results: list[MatchResult] = []
    claimed: set[int] = set()

    by_gstin: dict[str, list[tuple[int, Invoice]]] = {}
    for idx, book in enumerate(book_invoices):
        by_gstin.setdefault(book.gstin, []).append((idx, book))

    def candidates_for(book_gstin: str) -> list[tuple[int, Invoice]]:
        if rules.require_gstin_exact:
            return by_gstin.get(book_gstin, [])
        return [(i, b) for items in by_gstin.values() for i, b in items]

    for portal in portal_invoices:
        best_score = -1.0
        best_idx: int | None = None
        best_info: tuple[int, int, float | None, list[str]] = (99, 99, None, [])
        for idx, book in candidates_for(portal.gstin):
            if idx in claimed:
                continue
            score, ed, dd, vp, reasons = _score_candidate(portal, book, rules)
            if score > best_score:
                best_score, best_idx, best_info = score, idx, (ed, dd, vp, reasons)

        if best_idx is None or best_score < 0:
            results.append(_mk_forward(portal, None, MatchStatus.NOT_IN_BOOKS,
                                       Confidence.LOW, 0.0, 99, 99, None,
                                       ["No acceptable match found in books"],
                                       rules))
            continue

        claimed.add(best_idx)
        book = book_invoices[best_idx]
        ed, dd, vp, reasons = best_info

        # --- Classification -------------------------------------------------
        # Invoice no (+GSTIN) is the primary key. Once an acceptable candidate
        # is found, the tie-breakers are date & value:
        #   * invoice key ok AND value ok      -> MATCHED
        #        High : exact invoice + date in window
        #        Med  : fuzzy invoice OR date outside window
        #   * invoice key ok BUT value differs -> AMOUNT_DIFF (Medium)
        #   * otherwise                        -> NOT_IN_BOOKS (Low)
        invoice_exact = (book.invoice_no == portal.invoice_no)
        invoice_ok = invoice_exact or ed <= rules.invoice_edit_max
        date_ok = date_window_ok(portal.date, book.date, rules.date_window_days)
        value_ok = value_within_tolerance(portal.taxable_value, book.taxable_value,
                                          rules.value_tolerance)

        if invoice_ok and value_ok:
            conf = (Confidence.HIGH if (invoice_exact and date_ok)
                    else Confidence.MEDIUM)
            results.append(_mk_forward(portal, book, MatchStatus.MATCHED,
                                       conf, best_score, ed, dd, vp, reasons,
                                       rules))
        elif invoice_ok and vp is not None and vp > rules.value_tolerance:
            results.append(_mk_forward(portal, book, MatchStatus.AMOUNT_DIFF,
                                       Confidence.MEDIUM, best_score, ed, dd, vp,
                                       reasons + ["GSTIN + invoice matched but value differs"],
                                       rules))
        else:
            results.append(_mk_forward(portal, book, MatchStatus.NOT_IN_BOOKS,
                                       Confidence.LOW, best_score, ed, dd, vp, reasons,
                                       rules))

    # Unclaimed book invoices -> Not in Portal
    for idx, book in enumerate(book_invoices):
        if idx not in claimed:
            results.append(_mk_unmatched_book(book, rules))

    # Phase-2 meta: materiality + IMS recommendation on every result
    return [apply_meta(r, rules) for r in results]
