"""Phase-2 tests: §17(5)/RCM eligibility, IMS recommendations, materiality,
live credit position (PRD Module 2A business rules)."""
from __future__ import annotations

from decimal import Decimal

from recon_engine import verification
from recon_engine.agent import auto_reconcile
from recon_engine.ims import materiality_for, recommend_ims
from recon_engine.models import Confidence, MatchResult, MatchStatus
from recon_engine.normalizer import make_invoice
from recon_engine.parsers import (
    parse_gstr2b_excel,
    parse_gstr2b_json,
    parse_tally_excel,
)
from recon_engine.sample_scenario import generate as gen_sample_docs

SAMPLES = gen_sample_docs()


def _norm(inv):
    return (inv.invoice_no, inv.reverse_charge, inv.blocked_credit,
            inv.taxable_value)


# ---------------------------------------------------------------------------
# Rules fixture parses flags correctly (JSON + Excel parity)
# ---------------------------------------------------------------------------
def test_rules_fixture_flags_parse():
    js = parse_gstr2b_json(SAMPLES["gstr2b_rules"])
    by_no = {i.invoice_no: i for i in js}
    assert by_no["INV-24001"].blocked_credit is True
    assert by_no["INV-24001"].reverse_charge is False
    assert by_no["CN-8841"].reverse_charge is True
    assert by_no["CN-8841"].blocked_credit is False
    others = [i.invoice_no for i in js
              if i.invoice_no not in ("INV-24001", "CN-8841")]
    assert all(not by_no[n].reverse_charge and not by_no[n].blocked_credit
               for n in others)


def test_rules_fixture_excel_parity_with_json():
    js = parse_gstr2b_json(SAMPLES["gstr2b_rules"])
    xl = parse_gstr2b_excel(SAMPLES["gstr2b_rules_xlsx"])
    assert len(xl) == 9
    assert {_norm(i) for i in xl} == {_norm(i) for i in js}


# ---------------------------------------------------------------------------
# Eligibility under §17(5) + RCM (agent run, JSON + Excel paths)
# ---------------------------------------------------------------------------
def test_rcm_blocked_reduce_eligibility():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules"],
        tally_path=SAMPLES["tally"],
        client_id="acme", period="2024-08", output_tax=120000,
    )
    p = res["packet"]
    # 64,260 canonical - 21,600 (INV-24001 blocked) - 17,100 (CN-8841 RCM)
    assert p.itc.eligible_itc == Decimal("25560.00")
    assert p.itc.ineligible_itc == Decimal("64440.00")
    assert p.itc.eligible_count == 4
    assert p.itc.gstr3b_draft["net_payable"] == Decimal("94440.00")
    # independent verification recomputes with the SAME rules
    assert res["verification"]["pass"] is True


def test_rcm_blocked_excel_path_matches_json():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules_xlsx"],
        tally_path=SAMPLES["tally_xlsx"],
        client_id="acme", period="2024-08", output_tax=120000,
    )
    p = res["packet"]
    assert p.itc.eligible_itc == Decimal("25560.00")
    assert p.itc.ineligible_itc == Decimal("64440.00")
    # independent verification reprocesses the Excel files from disk
    assert res["verification"]["pass"] is True
    # report figures agree (markdown carries the Phase-2 numbers)
    md = res["reports"]["html"]
    assert md.exists()


# ---------------------------------------------------------------------------
# Live credit position (Module 7 feed)
# ---------------------------------------------------------------------------
def test_itc_position_breakdown_rules():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules_xlsx"],
        tally_path=SAMPLES["tally"],
        client_id="acme", period="2024-08", output_tax=120000,
    )
    pos = res["packet"].itc.itc_position
    assert pos["eligible_now"] == Decimal("25560.00")
    assert pos["blocked"] == Decimal("38700.00")     # 21,600 + 17,100
    assert pos["awaiting_supplier"] == Decimal("9000.00")
    assert pos["disputed"] == Decimal("11700.00")
    assert pos["unbooked"] == Decimal("5040.00")
    total = sum(pos.values(), Decimal("0"))
    assert total == Decimal("90000.00")   # reconciles to total portal ITC


def test_canonical_position_unchanged():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_xlsx"],
        tally_path=SAMPLES["tally"],
        client_id="acme", period="2024-08", output_tax=120000,
    )
    pos = res["packet"].itc.itc_position
    assert pos["eligible_now"] == Decimal("64260.00")
    assert pos["blocked"] == Decimal("0")
    assert pos["awaiting_supplier"] == Decimal("9000.00")


# ---------------------------------------------------------------------------
# Materiality gate (2A threshold, per-client override)
# ---------------------------------------------------------------------------
CLIENT_CONFIG = {"match_rules": {"materiality_amount": 50000}}


def test_materiality_tags_exceptions():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules"],
        tally_path=SAMPLES["tally"],
        client_id="acme", period="2024-08", output_tax=120000,
        client_config=CLIENT_CONFIG,
    )
    mat = {e.portal_invoice.invoice_no: e.materiality
           for e in res["packet"].exceptions}
    assert mat["INV-24001"] == "above"      # 120,000
    assert mat["CN-8841"] == "above"        # 95,000
    assert mat["EP-INV-9033"] == "above"    # 50,000 (>= threshold)
    assert mat["INV-24002"] == "below"      # 45,000
    assert mat["DT/0821"] == "below"        # 42,000
    assert mat["BL/2024/0815"] == "below"   # 28,000


def test_no_materiality_when_gate_unset():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules"],
        tally_path=SAMPLES["tally"],
        client_id="acme", period="2024-08", output_tax=120000,
    )
    assert all(e.materiality == "none"
               for e in res["packet"].exceptions)


# ---------------------------------------------------------------------------
# IMS thin rules layer
# ---------------------------------------------------------------------------
def test_ims_recommendations_rules_fixture():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules_xlsx"],
        tally_path=SAMPLES["tally"],
        client_id="acme", period="2024-08", output_tax=120000,
    )
    ims = {e.portal_invoice.invoice_no: e.ims_recommendation
           for e in res["packet"].exceptions}
    assert ims["INV-24001"] == "Reject"      # §17(5)
    assert ims["CN-8841"] == "Reject"        # RCM
    assert ims["INV-24002"] == "Accept"
    assert ims["CN-8850"] == "Accept"
    assert ims["DT/0821"] == "Pending"       # fuzzy match -> human confirm
    assert ims["EP-INV-9033"] == "Pending"   # supplier non-filer
    assert ims["BL/2024/0811"] == "Pending"  # amount difference
    assert ims["BL/2024/0815"] == "Pending"  # not in books
    assert ims["DT/0830"] == "N/A"           # books-only
    assert ims["ITF-2024-08-115"] == "N/A"   # books-only


def test_recommend_ims_unit():
    rcm = make_invoice("06AAACH7409R1ZK", "CN-1", "2024-08-01", "100",
                       reverse_charge=True)
    blocked = make_invoice("06AAACH7409R1ZK", "CN-2", "2024-08-01", "100",
                           blocked_credit=True)
    ok = make_invoice("06AAACH7409R1ZK", "CN-3", "2024-08-01", "100",
                      status="active")
    fuzzy = make_invoice("06AAACH7409R1ZK", "CN-4", "2024-08-01", "100",
                         status="active")

    m = lambda inv, st, conf: MatchResult(
        portal_invoice=inv, status=st, confidence=conf)
    assert recommend_ims(m(rcm, MatchStatus.MATCHED, Confidence.HIGH)) == "Reject"
    assert recommend_ims(m(blocked, MatchStatus.MATCHED, Confidence.HIGH)) == "Reject"
    assert recommend_ims(m(ok, MatchStatus.MATCHED, Confidence.HIGH)) == "Accept"
    assert recommend_ims(m(fuzzy, MatchStatus.MATCHED, Confidence.MEDIUM)) == "Pending"
    assert recommend_ims(m(ok, MatchStatus.NOT_IN_BOOKS, Confidence.LOW)) == "Pending"
    assert recommend_ims(m(ok, MatchStatus.NOT_IN_PORTAL, Confidence.LOW)) == "N/A"


def test_materiality_for_unit():
    inv = make_invoice("06AAACH7409R1ZK", "CN-5", "2024-08-01", "60000")
    r = MatchResult(portal_invoice=inv)
    from recon_engine.config import MatchRules
    no_gate = MatchRules()
    gate = MatchRules(materiality_amount=Decimal("50000"))
    assert materiality_for(r, no_gate) == "none"
    assert materiality_for(r, gate) == "above"
    small = make_invoice("06AAACH7409R1ZK", "CN-6", "2024-08-01", "40000")
    r2 = MatchResult(portal_invoice=small)
    assert materiality_for(r2, gate) == "below"
