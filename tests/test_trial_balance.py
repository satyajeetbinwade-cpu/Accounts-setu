"""Tests for the Tally Trial Balance parser + GST position analysis."""
from decimal import Decimal

from recon_engine.parsers.trial_balance import (
    parse_trial_balance, tb_runs_balance,
)
from recon_engine.gst_position import analyze_gst_position

SAMPLE = "sample_data/SampleTB-AI.xlsx"


def _near(a: str, b: str, tol: str = "0.01") -> bool:
    return abs(Decimal(a) - Decimal(b)) <= Decimal(tol)


def test_parse_tb_finds_gst_ledgers():
    rows = parse_trial_balance(SAMPLE)
    names = {r.particulars for r in rows}
    assert "Input CGST @ 9%" in names
    assert "Output IGST @ 18%" in names
    assert "CGST (Claim Next Year)" in names


def test_tb_balances():
    rows = parse_trial_balance(SAMPLE)
    bal = tb_runs_balance(rows)
    assert bal["pass"] is True
    assert Decimal(bal["grand_total_debit"]) == Decimal(bal["grand_total_credit"])


def test_gst_position_totals():
    rows = parse_trial_balance(SAMPLE)
    gp = analyze_gst_position(rows)
    t = gp["totals"]
    # Input ITC = CGST 7232.236 + IGST 1770.4544 + SGST 7232.236
    assert _near(t["itc_availed"], "16234.9264")
    # Output = IGST@18 329168.0304 + IGST@28 15126.496
    assert _near(t["output_tax"], "344294.5264")
    # Carry forward ITC asset
    assert Decimal(t["itc_carry_forward"]) > Decimal("390000")
