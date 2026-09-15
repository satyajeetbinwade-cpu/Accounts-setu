"""Unit tests for deterministic ITC computation."""
from datetime import date
from decimal import Decimal

from recon_engine import make_invoice, match_engine, compute_itc


def _run(portal_specs, book_specs, output_tax="120000"):
    portal = [make_invoice(**p) for p in portal_specs]
    books = [make_invoice(**b) for b in book_specs]
    results = match_engine(portal, books)
    return compute_itc(results, Decimal(output_tax))


def test_eligible_and_ineligible():
    itc = _run(
        portal_specs=[
            dict(gstin="29ABCDE1234F1Z5", invoice_no="INV-1", date="2026-08-03",
                 taxable_value=100000, cgst=9000, sgst=9000, status="active"),
            dict(gstin="27PQRST5678G1Z3", invoice_no="INV-2", date="2026-08-05",
                 taxable_value=75000, igst=13500, status="non_filer"),
        ],
        book_specs=[
            dict(gstin="29ABCDE1234F1Z5", invoice_no="INV-1", date="2026-08-03",
                 taxable_value=100000, cgst=9000, sgst=9000),
            dict(gstin="27PQRST5678G1Z3", invoice_no="INV-2", date="2026-08-05",
                 taxable_value=75000, igst=13500),
        ],
    )
    assert itc.eligible_itc == Decimal("18000")
    assert itc.ineligible_itc == Decimal("13500")
    # GSTR-3B draft: output tax - eligible ITC
    assert itc.gstr3b_draft["net_payable"] == Decimal("102000")
