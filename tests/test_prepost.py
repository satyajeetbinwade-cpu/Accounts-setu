"""Regression tests: sample scenario + pre/post report generation."""
import csv
from decimal import Decimal

from recon_engine import make_invoice, match_engine
from recon_engine.config import Settings
from recon_engine.engine import ReconciliationEngine
from recon_engine.models import Confidence, MatchStatus
from recon_engine.parsers import parse_gstr2b_json, parse_tally_purchase_rows
from recon_engine.prepost_report import generate_report
from recon_engine.sample_scenario import generate as gen_docs

from recon_engine.parsers.trial_balance import parse_trial_balance
from recon_engine.gst_position import analyze_gst_position

SAMPLE = "sample_data"


def test_fuzzy_invoice_equal_value_is_matched_medium():
    """Regression: fuzzy invoice no with equal value must be MATCHED (Medium),
    not NOT_IN_BOOKS."""
    portal = [make_invoice("24AAACJ7417J1ZY", "DT/0821", "2024-08-12", 42000)]
    books = [make_invoice("24AAACJ7417J1ZY", "DT-0821", "2024-08-13", 42000)]
    res = match_engine(portal, books)
    assert res[0].status == MatchStatus.MATCHED
    assert res[0].confidence == Confidence.MEDIUM


def test_sample_docs_parse_and_reconcile():
    outs = gen_docs(SAMPLE)
    portal = parse_gstr2b_json(outs["gstr2b"])
    books = parse_tally_purchase_rows(
        list(csv.DictReader(open(outs["tally"]))))
    assert len(portal) == 9
    assert len(books) == 10

    packet = ReconciliationEngine(Settings()).run_sync(
        client_id="client_001", period="2024-08",
        portal_invoices=portal, book_invoices=books,
        output_tax="120000")
    s = packet.summary
    assert s["matched"] == 7, s
    assert s["amount_diff"] == 1, s
    assert s["not_in_books"] == 1, s
    assert s["not_in_portal"] == 2, s
    assert packet.itc.eligible_itc == Decimal("64260")
    assert packet.itc.ineligible_itc == Decimal("25740")
    assert packet.itc.gstr3b_draft["net_payable"] == Decimal("55740")
    assert packet.validation["pass"] is True
    assert len(packet.drafted_jvs) == 1


def test_generate_report_writes_files(tmp_path=None):
    res = generate_report()
    assert res["pre"]["gross_gap"] == Decimal("2360.00")
    assert res["post"]["matched"] == 7
    assert res["post"]["net_payable"] == Decimal("55740")
    for path in res["reports"].values():
        assert path.exists(), path


def test_html_report_contains_visualisation(tmp_path=None):
    """The HTML dashboard renders the full picture (KPIs, charts, gates)."""
    res = generate_report()
    html_path = res["reports"]["html"]
    assert html_path.exists(), html_path
    body = html_path.read_text(encoding="utf-8")
    # KPI + charts + tables all present in one self-contained file
    assert "<svg" in body                          # inline SVG charts
    assert "Classification" in body
    assert "Input tax credit split" in body
    assert "Eligible ITC" in body
    assert "Net GST payable" in body
    assert "F5 data-integrity" in body
    assert "independent" in body.lower() or "verification" in body
    assert "Matched" in body
    assert "55,740" in body          # net payable shown
    assert "64,260" in body          # eligible ITC shown
    assert "</html>" in body
