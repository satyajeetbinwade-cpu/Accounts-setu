"""Tests for the independent verification module (cross-check layer)."""
from decimal import Decimal

from recon_engine import make_invoice
from recon_engine.config import Settings
from recon_engine.engine import ReconciliationEngine
from recon_engine.verification import (
    independent_classify,
    independent_itc,
    independent_totals,
    load_raw_books,
    load_raw_portal,
    verify_packet,
)


def test_independent_classifier_reproduces_engine():
    from recon_engine.parsers import parse_gstr2b_json, parse_tally_purchase_rows

    portal = parse_gstr2b_json("sample_data/gstr2b_sample_full.json")
    books = parse_tally_purchase_rows(load_raw_books())
    packet = ReconciliationEngine(Settings()).run_sync(
        client_id="c", period="2024-08",
        portal_invoices=portal, book_invoices=books, output_tax="120000")

    portal_rows = load_raw_portal()
    indep = independent_classify(portal_rows, load_raw_books())

    # Normalise both sides (raw JSON may carry trailing spaces, e.g. "INV-24002 ")
    def _norm(s: str) -> str:
        return s.replace(" ", "").upper()

    engine_statuses = {
        _norm(e.portal_invoice.invoice_no): e.status.value
        for e in packet.exceptions
        if e.status.value in ("matched", "not_in_books", "amount_diff")
    }
    indep_statuses = {
        _norm(s["invoice_no"]): s["status"] for s in indep["portal_status"]
    }
    assert engine_statuses == indep_statuses


def test_independent_itc_matches_engine():
    from recon_engine.parsers import parse_gstr2b_json, parse_tally_purchase_rows

    portal = parse_gstr2b_json("sample_data/gstr2b_sample_full.json")
    books = parse_tally_purchase_rows(load_raw_books())
    packet = ReconciliationEngine(Settings()).run_sync(
        client_id="c", period="2024-08",
        portal_invoices=portal, book_invoices=books, output_tax="120000")

    portal_rows = load_raw_portal()
    indep = independent_classify(portal_rows, load_raw_books())
    itc = independent_itc(indep["portal_status"], portal_rows)
    assert itc["eligible_itc"] == packet.itc.eligible_itc == Decimal("64260")
    assert itc["ineligible_itc"] == packet.itc.ineligible_itc == Decimal("25740")


def test_verify_packet_all_pass():
    from recon_engine.parsers import parse_gstr2b_json, parse_tally_purchase_rows

    portal = parse_gstr2b_json("sample_data/gstr2b_sample_full.json")
    books = parse_tally_purchase_rows(load_raw_books())
    packet = ReconciliationEngine(Settings()).run_sync(
        client_id="c", period="2024-08",
        portal_invoices=portal, book_invoices=books, output_tax="120000")
    res = verify_packet(packet)
    assert res["pass"] is True


def test_independent_totals_matches_sample():
    tot = independent_totals(load_raw_portal(), load_raw_books())
    assert tot["portal_count"] == 9
    assert tot["book_count"] == 10
    assert tot["portal_sum"] == Decimal("590000")
    assert tot["book_sum"] == Decimal("587640")
