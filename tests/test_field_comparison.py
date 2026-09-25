"""Field-comparison semantics (D5) — numeric, provenance-free, both-empty-free.

The changed-fields list must never report a field as changed when the two
sides are numerically equal (0.0 vs 0, 26066 vs 26066.0), must never compare
provenance fields (source_type, source_file), and must omit fields empty on
both sides.
"""

from __future__ import annotations

from setu.state.phase1_state import _diff_fields


def test_numerically_equal_values_are_not_changed():
    books = {"taxable_value": 0.0, "invoice_value": 26066.0, "cgst": 0}
    portal = {"taxable_value": 0, "invoice_value": 26066, "cgst": 0.0}
    assert _diff_fields(books, portal) == set()


def test_provenance_fields_are_never_compared():
    books = {"source_type": "tally", "source_file": "a.xlsx", "invoice_value": 100}
    portal = {"source_type": "gstr2b", "source_file": "b.xlsx", "invoice_value": 100}
    assert _diff_fields(books, portal) == set()


def test_fields_empty_on_both_sides_are_omitted():
    books = {"cess": None, "igst": "", "invoice_value": 100}
    portal = {"cess": "", "igst": None, "invoice_value": 100}
    assert _diff_fields(books, portal) == set()


def test_genuine_numeric_difference_is_reported():
    books = {"taxable_value": 100.0}
    portal = {"taxable_value": 200.0}
    assert _diff_fields(books, portal) == {"taxable_value"}


def test_small_numeric_difference_within_tolerance_is_not_reported():
    books = {"cgst": 1621.77}
    portal = {"cgst": 1621.75}
    assert _diff_fields(books, portal) == set()


def test_genuine_text_difference_is_reported():
    books = {"invoice_number": "T11439"}
    portal = {"invoice_number": "T11439/26-27"}
    assert _diff_fields(books, portal) == {"invoice_number"}


def test_private_fields_are_skipped():
    books = {"_doc_type": "Invoice", "invoice_value": 100}
    portal = {"_doc_type": "Credit Note", "invoice_value": 100}
    assert _diff_fields(books, portal) == set()
