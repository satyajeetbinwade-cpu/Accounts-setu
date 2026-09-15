"""Unit tests for the deterministic Tier-A matcher."""
import pytest
from datetime import date
from decimal import Decimal

from recon_engine import make_invoice, match_engine, MatchRules, MatchStatus, Confidence


@pytest.fixture
def rules():
    return MatchRules()


def test_exact_match(rules):
    portal = [make_invoice("29ABCDE1234F1Z5", "INV-1001", "2026-08-03", 100000, cgst=9000, sgst=9000)]
    books = [make_invoice("29ABCDE1234F1Z5", "INV-1001", "2026-08-03", 100000, cgst=9000, sgst=9000)]
    res = match_engine(portal, books, rules)
    assert len(res) == 1
    assert res[0].status == MatchStatus.MATCHED
    assert res[0].confidence == Confidence.HIGH


def test_fuzzy_invoice_number(rules):
    portal = [make_invoice("29ABCDE1234F1Z5", "INV-1002 ", "2026-08-11", 50000, cgst=4500, sgst=4500)]
    books = [make_invoice("29ABCDE1234F1Z5", "INV-1002", "2026-08-10", 50000, cgst=4500, sgst=4500)]
    res = match_engine(portal, books, rules)
    # trailing-space difference normalises away -> matched
    assert res[0].status == MatchStatus.MATCHED


def test_amount_diff(rules):
    portal = [make_invoice("29ABCDE1234F1Z5", "INV-1001", "2026-08-03", 100000, cgst=9000, sgst=9000)]
    books = [make_invoice("29ABCDE1234F1Z5", "INV-1001", "2026-08-03", 99000, cgst=8910, sgst=8910)]
    res = match_engine(portal, books, rules)
    assert res[0].status == MatchStatus.AMOUNT_DIFF


def test_not_in_books_and_not_in_portal(rules):
    portal = [make_invoice("29ABCDE1234F1Z5", "PORTP0001", "2026-08-03", 10000)]
    books = [make_invoice("29ABCDE1234F1Z5", "BOOKB0007", "2026-08-03", 5000)]
    res = match_engine(portal, books, rules)
    statuses = {r.status for r in res}
    assert MatchStatus.NOT_IN_BOOKS in statuses
    assert MatchStatus.NOT_IN_PORTAL in statuses


def test_gstin_mismatch_never_matches(rules):
    portal = [make_invoice("29ABCDE1234F1Z5", "INV-1", "2026-08-03", 10000)]
    books = [make_invoice("27PQRST5678G1Z3", "INV-1", "2026-08-03", 10000)]
    res = match_engine(portal, books, rules)
    assert res[0].status == MatchStatus.NOT_IN_BOOKS
