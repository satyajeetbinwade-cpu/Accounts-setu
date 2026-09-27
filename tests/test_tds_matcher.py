"""Characterisation tests for the TDS matching engine (SRB-3 T1).

Pins CURRENT behaviour of src/matching/tds_matcher.py before T2/T3/T5/T7
touch it. No logic changes here — any surprising behaviour found while
writing these is called out in a comment and reported to Dwight separately,
not fixed in this file.

Covers:
  * match_tds_deductor — Pass 1 (challan-anchored), Pass 2 (deductee-level),
    Pass 3 (aggregated), via one small books/portal fixture pair.
  * match_tds_deductee — the cross-quarter retry (exact + aggregated),
    including a Q4 -> Q1 financial-year boundary.
  * _classify_amount_diff — every branch (Rate Mismatch, Rounding, Short
    Deduction, Excess Deduction, Unexplained).
  * validate_section_rates — per-entry rate check, missing/invalid PAN,
    single-transaction threshold, annual aggregate threshold, salary (192)
    skip, unknown-section skip.

Run:

    venv/bin/python -m pytest tests/test_tds_matcher.py -v
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.config_loader import load_config
from src.matching.tds_matcher import (
    match_tds_deductor,
    match_tds_deductee,
    validate_section_rates,
    _classify_amount_diff,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tds"


@pytest.fixture(scope="module")
def tds_config() -> dict:
    return load_config()["tds"]


def _read(name: str) -> pd.DataFrame:
    return pd.read_csv(FIXTURES / name, dtype={"challan_number": "string"})


def by_pan(results: list[dict], pan: str) -> list[dict]:
    """All results touching a given PAN, on either side of the match."""
    out = []
    for r in results:
        for side_key in ("books_record", "portal_record"):
            raw = r.get(side_key)
            if raw and f'"pan": "{pan}"' in raw:
                out.append(r)
                break
    return out


# ---------------------------------------------------------------------------
# match_tds_deductor — Pass 1 (challan), Pass 2 (deductee-level), Pass 3 (aggregated)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def deductor_results(tds_config) -> list[dict]:
    books = _read("deductor_books.csv")
    portal = _read("deductor_portal.csv")
    return match_tds_deductor(books, portal, tds_config)


def test_pass1_challan_exact_match(deductor_results):
    """AAAPA1111A / CH001 — books and portal agree exactly on challan."""
    rows = by_pan(deductor_results, "AAAPA1111A")
    assert len(rows) == 1
    assert rows[0]["classification"] == "Matched"
    assert rows[0]["difference_type"] is None
    assert "challan CH001" in rows[0]["match_reason"]


def test_pass1_challan_rounding(deductor_results):
    """AAAPA6666F / CH006 — 1 rupee gap, within the 2.0 rounding tolerance,
    classified via the challan-pass's own rounding check (not
    _classify_amount_diff, which is only reached for out-of-tolerance diffs)."""
    rows = by_pan(deductor_results, "AAAPA6666F")
    assert len(rows) == 1
    assert rows[0]["classification"] == "Amount Difference"
    assert rows[0]["difference_type"] == "Rounding"


def test_pass1_challan_excess_deduction(deductor_results):
    """AAAPA4444D / CH004 — books 1200 vs portal 1000; portal matches the
    194C default 2% rate on 50000, books doesn't -> Excess Deduction."""
    rows = by_pan(deductor_results, "AAAPA4444D")
    assert len(rows) == 1
    assert rows[0]["classification"] == "Amount Difference"
    assert rows[0]["difference_type"] == "Excess Deduction"


def test_pass1_challan_rate_mismatch(deductor_results):
    """AAAPA5555E / CH005 — 700 vs 690, neither side matches any configured
    194C rate (1000 @ 2%, 500 @ 1%) -> Rate Mismatch."""
    rows = by_pan(deductor_results, "AAAPA5555E")
    assert len(rows) == 1
    assert rows[0]["classification"] == "Amount Difference"
    assert rows[0]["difference_type"] == "Rate Mismatch"


def test_pass2_deductee_level_match(deductor_results, tds_config):
    """AAAPA2222B — no challan on either side; matched on PAN + section +
    amount + date (Pass 2), exact amounts -> Matched at the deductee_level
    baseline (proximity 1.0, no group-size penalty)."""
    rows = by_pan(deductor_results, "AAAPA2222B")
    assert len(rows) == 1
    assert rows[0]["classification"] == "Matched"
    assert rows[0]["confidence_score"] == pytest.approx(
        float(tds_config["confidence_baselines"]["deductee_level"])
    )


def test_pass3_aggregated_match(deductor_results, tds_config):
    """AAAPA3333C — two book entries (1000 + 1500) roll up to one portal
    line (2500) in the same quarter; individually neither book row is within
    tolerance of 2500, so this can only resolve via the aggregated pass."""
    rows = by_pan(deductor_results, "AAAPA3333C")
    assert len(rows) == 1
    r = rows[0]
    assert r["classification"] == "Matched"
    assert r["matched_record_ids"] is not None
    assert len(r["matched_record_ids"]) == 3  # 2 book ids + 1 portal id
    # group_size=2 penalty applied on top of the aggregated baseline.
    expected = float(tds_config["confidence_baselines"]["aggregated"]) - float(
        tds_config.get("aggregated_group_size_penalty", 2)
    )
    assert r["confidence_score"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# match_tds_deductee — cross-quarter retry, incl. Q4 -> Q1 FY boundary
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def deductee_results(tds_config) -> list[dict]:
    books = _read("deductee_books.csv")
    portal = _read("deductee_portal.csv")
    return match_tds_deductee(books, portal, tds_config)


def test_deductee_cross_quarter_exact_fy_boundary(deductee_results):
    """AAAPB1111X — books deposit 2025-03-28 (Q4-2024), portal deposit
    2025-04-05 (Q1-2025). Same-quarter passes find nothing; the Q4->Q1
    financial-year-boundary retry must classify this a Timing Difference,
    not Not in Books / Not in Portal."""
    rows = by_pan(deductee_results, "AAAPB1111X")
    assert len(rows) == 1
    r = rows[0]
    assert r["classification"] == "Matched"
    assert r["difference_type"] == "Timing Difference"
    assert "Timing difference" in r["match_reason"]


def test_deductee_cross_quarter_aggregated_fy_boundary(deductee_results):
    """AAAPB2222Y — two Q4-2024 book entries (2000+3000=5000) roll up to one
    Q1-2025 portal line (5000); only resolvable via the cross-quarter
    aggregated retry."""
    rows = by_pan(deductee_results, "AAAPB2222Y")
    assert len(rows) == 1
    r = rows[0]
    assert r["classification"] == "Matched"
    assert r["difference_type"] == "Timing Difference"
    assert r["matched_record_ids"] is not None
    assert len(r["matched_record_ids"]) == 3


def test_deductee_no_cross_quarter_when_disabled(tds_config):
    """quarter_boundary_matching=False suppresses the exact/aggregated
    cross-quarter retry: AAAPB2222Y's two book rows (2000+3000) are each
    too far in amount from the single 5000 portal row for the (unguarded,
    per-row) fuzzy pass to catch them either -> genuinely unmatched."""
    config = dict(tds_config)
    config["quarter_boundary_matching"] = False
    books = _read("deductee_books.csv")
    portal = _read("deductee_portal.csv")
    results = match_tds_deductee(books, portal, config)
    rows = by_pan(results, "AAAPB2222Y")
    assert len(rows) == 3  # 2 books rows "Not in Books" + 1 portal row "Not in Books"/"Not in Portal"
    classifications = {r["classification"] for r in rows}
    assert classifications == {"Not in Books", "Not in Portal"}
    assert all(r["difference_type"] is None for r in rows)


def test_deductee_fuzzy_pass_ignores_quarter_boundary_flag(tds_config):
    """Current behaviour worth flagging to Dwight: quarter_boundary_matching
    only gates the exact/aggregated cross-quarter RETRY (Pass 3). Pass 4
    (fuzzy, by name+section+amount) runs unconditionally and has no quarter
    awareness at all, so AAAPB1111X still matches via fuzzy name-matching
    even with the flag off -- the flag does not fully suppress cross-period
    matching the way its name implies."""
    config = dict(tds_config)
    config["quarter_boundary_matching"] = False
    books = _read("deductee_books.csv")
    portal = _read("deductee_portal.csv")
    results = match_tds_deductee(books, portal, config)
    rows = by_pan(results, "AAAPB1111X")
    assert len(rows) == 1
    assert rows[0]["classification"] == "Matched"
    assert rows[0]["difference_type"] is None
    assert "Fuzzy deductee match" in rows[0]["match_reason"]


# ---------------------------------------------------------------------------
# _classify_amount_diff — every branch, tested directly
# ---------------------------------------------------------------------------


def _book_row(section: str, amount: float, **extra) -> pd.Series:
    row = {"section": section, "amount_paid_credited": amount}
    row.update(extra)
    return pd.Series(row)


def test_classify_amount_diff_rate_mismatch(tds_config):
    """Neither books (700) nor portal (690) tax matches any configured
    194C rate (1000 @ 2%, 500 @ 1%) on a 50000 payment."""
    row = _book_row("194C", 50000)
    result = _classify_amount_diff(700, 690, row, tds_config)
    assert result == "Rate Mismatch"


def test_classify_amount_diff_rounding(tds_config):
    """No configured rate applies (unknown section) but the gap itself is
    within rounding_tolerance -> Rounding, independent of any rate table."""
    row = _book_row("194ZZZ", 50000)
    result = _classify_amount_diff(1001.0, 1000.0, row, tds_config)
    assert result == "Rounding"


def test_classify_amount_diff_short_deduction(tds_config):
    row = _book_row("194ZZZ", 50000)
    result = _classify_amount_diff(500.0, 700.0, row, tds_config)
    assert result == "Short Deduction"


def test_classify_amount_diff_excess_deduction(tds_config):
    row = _book_row("194ZZZ", 50000)
    result = _classify_amount_diff(700.0, 500.0, row, tds_config)
    assert result == "Excess Deduction"


def test_classify_amount_diff_unexplained_when_equal(tds_config):
    """Current behaviour: called with tax_books == tax_portal (a caller
    would normally only reach this function after detecting a real
    difference, so this path isn't hit via the matcher passes above — but
    the function itself has no such guard) -> falls through both the < and
    > checks to "Unexplained"."""
    row = _book_row("194ZZZ", 50000)
    result = _classify_amount_diff(1000.0, 1000.0, row, tds_config)
    assert result == "Unexplained"


# ---------------------------------------------------------------------------
# validate_section_rates
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def section_results(tds_config) -> list[dict]:
    books = pd.read_csv(
        FIXTURES / "validate_section_books.csv",
        dtype={"pan": "string"},
        keep_default_na=True,
    )
    return validate_section_rates(books, tds_config)


def _reasons_for(results: list[dict], pan_substr: str) -> list[str]:
    return [
        r["match_reason"] for r in results
        if pan_substr in (r.get("books_record") or "")
    ]


def test_short_deduction_per_entry(section_results):
    reasons = _reasons_for(section_results, "AAAPC1111A")
    assert any("Short Deduction" in r for r in reasons)


def test_excess_deduction_per_entry(section_results):
    reasons = _reasons_for(section_results, "AAAPC2222B")
    assert any("Excess Deduction" in r for r in reasons)


def test_salary_section_192_is_skipped(section_results):
    """Section 192 (salary) is explicitly excluded from per-entry
    validation, and its thresholds are both 0 in config -> zero results."""
    reasons = _reasons_for(section_results, "AAAPC3333C")
    assert reasons == []


def test_unknown_section_with_valid_pan_is_skipped(section_results):
    """A section absent from config.section_table can't be validated at
    all (KeyError caught) -> zero results, when the PAN itself is fine."""
    reasons = _reasons_for(section_results, "AAAPC4444D")
    assert reasons == []


def test_missing_pan_flagged_when_wrong_rate_applied(section_results):
    """Blank PAN (nullable-string dtype reads it back as pd.NA, printed as
    '<NA>') on an unknown section: the per-entry/threshold checks skip
    (KeyError), but the no-PAN check fires independently since it doesn't
    require the section to be configured."""
    no_pan_reasons = [
        r["match_reason"] for r in section_results
        if "PAN missing or invalid (<NA>)" in r["match_reason"]
    ]
    assert len(no_pan_reasons) == 1
    assert "₹1,000.00 was deducted" in no_pan_reasons[0]


def test_correct_no_pan_rate_raises_no_exception(section_results):
    """Blank PAN, tax_deducted already at the 20% no-PAN rate -> no
    violation (and the unknown section skips everything else)."""
    matches = [
        r for r in section_results
        if "PAN missing or invalid" in r["match_reason"] and "10,000.00 was deducted" in r["match_reason"]
    ]
    assert matches == []


def test_invalid_pan_format_flagged(section_results):
    """'INVALID1' fails the 5-alpha+4-digit+1-alpha PAN regex -> flagged
    even though it's non-blank."""
    reasons = [
        r["match_reason"] for r in section_results
        if "INVALID1" in r["match_reason"]
    ]
    assert len(reasons) == 1
    assert "PAN missing or invalid" in reasons[0]


def test_single_transaction_threshold_breach(section_results):
    """AAAPC6666F: 40000 under 194C exceeds the 30000 single-transaction
    threshold with zero TDS deducted. Note: this PAN ALSO trips the
    per-entry rate check (0 vs an expected non-zero rate) — current
    behaviour produces two separate exception rows for one input row."""
    reasons = _reasons_for(section_results, "AAAPC6666F")
    assert any("exceeds threshold of" in r for r in reasons)
    assert any("Short Deduction" in r for r in reasons)
    assert len(reasons) == 2


def test_annual_aggregate_threshold_breach(section_results):
    """AAAPC7777G: two 194A payments (45000 + 20000 = 65000) exceed the
    40000 annual aggregate threshold with zero total TDS. Also double-fires
    with two per-entry Short Deduction rows (one per input row) -- same
    current-behaviour note as the single-transaction case above."""
    reasons = _reasons_for(section_results, "AAAPC7777G")
    assert any("exceeding annual threshold of" in r for r in reasons)
    assert len(reasons) == 3


def test_section_192_has_no_threshold_config_effect():
    """Sanity check on the fixture premise: 192's thresholds are both 0 in
    the live config, which is *why* test_salary_section_192_is_skipped
    sees zero results (not just the explicit section=='192' skip)."""
    config = load_config()["tds"]
    entry = config["section_table"]["192"]
    assert entry.get("single_transaction_threshold", 0) == 0
    assert entry.get("annual_aggregate_threshold", 0) == 0
