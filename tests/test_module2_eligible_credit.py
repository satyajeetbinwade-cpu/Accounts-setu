"""Characterisation tests for module2's live eligible-credit figure (SRB-3 T1).

Pins CURRENT behaviour of src.module2.service._compute_eligible_credit,
_is_blocked_credit and _is_reverse_charge before T3 changes
_is_reverse_charge to also read a reverse_charge flag. No logic changes
here; surprising behaviour is called out in a docstring and reported to
Dwight separately, not fixed in this file.

Follows the direct-call pattern already used in
tests/test_gst_recon_set3.py::test_eligible_credit_uses_portal_side_tax.

Run:

    venv/bin/python -m pytest tests/test_module2_eligible_credit.py -v
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src import bootstrap
from src.module2 import service as m2

PERIOD = "2025-04"
GSTIN = "09AABCS8888H1Z1"


@pytest.fixture()
def conn(tmp_path):
    """A fresh, isolated DB per test — eligible_credit_figures accumulates
    rows across calls, so tests must not share state."""
    db_path = tmp_path / "poc.db"
    bootstrap.init_all(db_path)
    c = sqlite3.connect(db_path)
    try:
        yield c
    finally:
        c.close()


def _result(classification, *, books=None, portal=None, run_id=1):
    return {
        "classification": classification,
        "books_record": json.dumps(books) if books is not None else None,
        "portal_record": json.dumps(portal) if portal is not None else None,
    }


def test_matched_clean_row_is_fully_eligible(conn):
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[_result("Matched", books={"total_tax": 1000.0, "gstin": GSTIN},
                          portal={"total_tax": 1000.0, "gstin": GSTIN})],
    )
    assert fig["total_itc_claimed"] == pytest.approx(1000.0)
    assert fig["matched_itc"] == pytest.approx(1000.0)
    assert fig["blocked_credit_itc"] == pytest.approx(0.0)
    assert fig["at_risk_itc"] == pytest.approx(0.0)
    assert fig["eligible_credit"] == pytest.approx(1000.0)


def test_blocked_credit_reduces_eligible_but_stays_in_matched(conn):
    """Section 17(5): a blocked-credit invoice is still 'Matched' (books and
    portal agree) but must not count as eligible."""
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[_result(
            "Matched",
            books={"total_tax": 1000.0, "gstin": GSTIN, "itc_eligible": False},
            portal={"total_tax": 1000.0, "gstin": GSTIN},
        )],
    )
    assert fig["matched_itc"] == pytest.approx(1000.0)
    assert fig["blocked_credit_itc"] == pytest.approx(1000.0)
    assert fig["eligible_credit"] == pytest.approx(0.0)


def test_reverse_charge_is_tracked_but_not_deducted_from_eligible(conn):
    """Current behaviour: reverse-charge ITC is tallied separately but is
    NOT subtracted from the headline eligible_credit figure (the docstring
    calls this out as intentional -- claimable once the recipient pays the
    tax -- but it means a reverse-charge row inflates eligible_credit unless
    the UI/report also nets it out separately). Worth flagging to Dwight as
    a "confirm this is really intended" item, not a bug fixed here."""
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[_result(
            "Matched",
            books={"total_tax": 1000.0, "gstin": GSTIN, "document_type": "Reverse Charge"},
            portal={"total_tax": 1000.0, "gstin": GSTIN},
        )],
    )
    assert fig["matched_itc"] == pytest.approx(1000.0)
    assert fig["reverse_charge_itc"] == pytest.approx(1000.0)
    assert fig["blocked_credit_itc"] == pytest.approx(0.0)
    assert fig["eligible_credit"] == pytest.approx(1000.0)


def test_not_in_portal_is_at_risk_not_eligible(conn):
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[_result("Not in Portal", books={"total_tax": 500.0, "gstin": GSTIN})],
    )
    assert fig["matched_itc"] == pytest.approx(0.0)
    assert fig["at_risk_itc"] == pytest.approx(500.0)
    assert fig["eligible_credit"] == pytest.approx(0.0)
    assert fig["total_itc_claimed"] == pytest.approx(500.0)


def test_amount_difference_is_at_risk(conn):
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[_result(
            "Amount Difference",
            books={"total_tax": 900.0, "gstin": GSTIN},
            portal={"total_tax": 1000.0, "gstin": GSTIN},
        )],
    )
    # value reads portal-first when present.
    assert fig["at_risk_itc"] == pytest.approx(1000.0)
    assert fig["matched_itc"] == pytest.approx(0.0)


def test_not_in_books_counts_toward_total_but_not_at_risk_or_matched(conn):
    """Current behaviour: "Not in Books" (portal has it, books don't) is
    counted in total_itc_claimed but lands in NEITHER matched_itc NOR
    at_risk_itc -- only "Not in Portal" and "Amount Difference" feed
    at_risk_itc. This asymmetry (books-missing money is invisible to both
    matched and at-risk buckets) is worth flagging to Dwight."""
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[_result("Not in Books", portal={"total_tax": 700.0, "gstin": GSTIN})],
    )
    assert fig["total_itc_claimed"] == pytest.approx(700.0)
    assert fig["matched_itc"] == pytest.approx(0.0)
    assert fig["at_risk_itc"] == pytest.approx(0.0)
    assert fig["eligible_credit"] == pytest.approx(0.0)


def test_tax_of_falls_back_to_component_sum_when_total_tax_missing(conn):
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[_result(
            "Matched",
            books={"cgst": 90.0, "sgst": 90.0, "igst": 0.0, "cess": 0.0, "gstin": GSTIN},
            portal={"cgst": 90.0, "sgst": 90.0, "igst": 0.0, "cess": 0.0, "gstin": GSTIN},
        )],
    )
    assert fig["matched_itc"] == pytest.approx(180.0)


def test_empty_run_yields_all_zero_figure(conn):
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD, results=[],
    )
    assert fig == {
        "figure_id": fig["figure_id"],
        "total_itc_claimed": 0.0,
        "matched_itc": 0.0,
        "at_risk_itc": 0.0,
        "blocked_credit_itc": 0.0,
        "reverse_charge_itc": 0.0,
        "eligible_credit": 0.0,
    }


def test_multiple_rows_aggregate(conn):
    fig = m2._compute_eligible_credit(
        conn, client_id=1, run_id=1, period=PERIOD,
        results=[
            _result("Matched", books={"total_tax": 1000.0, "gstin": GSTIN},
                     portal={"total_tax": 1000.0, "gstin": GSTIN}),
            _result("Not in Portal", books={"total_tax": 200.0, "gstin": GSTIN}),
            _result("Matched", books={"total_tax": 300.0, "gstin": GSTIN, "itc_eligible": False},
                     portal={"total_tax": 300.0, "gstin": GSTIN}),
        ],
    )
    assert fig["total_itc_claimed"] == pytest.approx(1500.0)
    assert fig["matched_itc"] == pytest.approx(1300.0)
    assert fig["at_risk_itc"] == pytest.approx(200.0)
    assert fig["blocked_credit_itc"] == pytest.approx(300.0)
    assert fig["eligible_credit"] == pytest.approx(1000.0)  # 1300 - 300


# ---------------------------------------------------------------------------
# _is_reverse_charge / _is_blocked_credit — pinned pre-T3 behaviour
# ---------------------------------------------------------------------------


def test_is_reverse_charge_reads_document_type_only():
    """Pre-T3 baseline: only document_type is read. A reverse_charge=True
    flag with no matching document_type is currently NOT detected -- this
    is exactly the gap T3 closes."""
    assert m2._is_reverse_charge({"document_type": "Reverse Charge"}) is True
    assert m2._is_reverse_charge({"document_type": "RCM"}) is True
    assert m2._is_reverse_charge({"document_type": "Invoice"}) is False
    assert m2._is_reverse_charge({"reverse_charge": True}) is False  # the T3 gap
    assert m2._is_reverse_charge(None) is False


def test_is_blocked_credit_checks_either_side_eligibility_keys():
    assert m2._is_blocked_credit({"itc_eligible": False}) is True
    assert m2._is_blocked_credit({"itc_eligible": True}) is False
    assert m2._is_blocked_credit({"itc_eligibility": "ineligible"}) is True
    assert m2._is_blocked_credit({"blocked_credit": "yes"}) is True
    assert m2._is_blocked_credit({"blocked_credit": "no"}) is False
    assert m2._is_blocked_credit({}) is False
    assert m2._is_blocked_credit(None) is False
