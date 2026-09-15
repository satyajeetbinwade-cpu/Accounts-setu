"""Unit tests for the F5 integrity / validation layer."""
from decimal import Decimal

from recon_engine import make_invoice, match_engine
from recon_engine.integrity import (
    control_totals_check, recon_of_recon, validate_structure,
)


def test_control_totals_match():
    invs = [make_invoice("29ABCDE1234F1Z5", "INV-1", "2026-08-03", 10000)]
    rep = control_totals_check("tally", 1, Decimal("10000"), invs)
    assert rep["pass"] is True


def test_control_totals_mismatch():
    invs = [make_invoice("29ABCDE1234F1Z5", "INV-1", "2026-08-03", 9000)]
    rep = control_totals_check("tally", 1, Decimal("10000"), invs)
    assert rep["pass"] is False
    assert "sum" in rep["issue"]


def test_structural_validation_catches_bad_gstin():
    invs = [make_invoice("INVALID", "INV-1", "2026-08-03", 10000)]
    rep = validate_structure(invs)
    assert rep["pass"] is False
    assert any("GSTIN" in e or "gstin" in e for err in rep["errors"]
               for e in err["errors"])


def test_recon_of_recon():
    portal = [
        make_invoice("29ABCDE1234F1Z5", "INV-1", "2026-08-03", 10000),
        make_invoice("27PQRST5678G1Z3", "INV-2", "2026-08-05", 5000),
    ]
    books = [
        make_invoice("29ABCDE1234F1Z5", "INV-1", "2026-08-03", 10000),
    ]
    results = match_engine(portal, books)
    rep = recon_of_recon(results)
    assert rep["pass"] is True
