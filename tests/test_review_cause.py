"""Unit tests for the Review screen's cause classifier + boundaries.

These cover the pure, deterministic function only — no DB, no UI. They are the
guard that the display grouping can never drift into a silent classification
change: the function is total over the engine's own vocabulary and every rule
has an explicit boundary case.
"""

from __future__ import annotations

from src.reconciliation import review


def _books(**over):
    base = {
        "gstin": "24DCNPB1786A1ZX",
        "invoice_number": "7953",
        "taxable_value": 1000.0,
        "cgst": 90.0,
        "sgst": 90.0,
        "igst": 0.0,
        "cess": 0.0,
        "total_tax": 180.0,
        "invoice_value": 1180.0,
    }
    base.update(over)
    return base


def _portal(**over):
    base = dict(_books())
    base.update(over)
    return base


# --- missing sides (straight from classification) ---------------------------

def test_missing_in_books_from_classification():
    assert review.classify_cause("Not in Books", "", {}, _portal()) == "missing_in_books"


def test_missing_in_portal_from_classification():
    assert review.classify_cause("Not in Portal", "", _books(), {}) == "missing_in_portal"


# --- likely_ingestion_gap ---------------------------------------------------

def test_ingestion_gap_signature():
    # Books zero taxable + zero tax, invoice totals tie within tolerance.
    books = _books(taxable_value=0, cgst=0, sgst=0, igst=0, cess=0, total_tax=0, invoice_value=26066)
    portal = _portal(taxable_value=22090.0, cgst=0, sgst=0, igst=3976.2, total_tax=3976.2, invoice_value=26066.0)
    assert review.classify_cause("Amount Difference", "Taxable Value Difference", books, portal) == "likely_ingestion_gap"


def test_ingestion_gap_invoice_gap_on_boundary_still_gap():
    # invoice gap exactly at tolerance (10.0) → still the ingestion signature
    books = _books(taxable_value=0, cgst=0, sgst=0, igst=0, cess=0, total_tax=0, invoice_value=1000.0)
    portal = _portal(taxable_value=900.0, cgst=0, sgst=0, igst=0, total_tax=0, invoice_value=1010.0)
    assert review.classify_cause("Amount Difference", "Taxable Value Difference", books, portal) == "likely_ingestion_gap"


def test_ingestion_gap_just_outside_tolerance_is_not_gap():
    # invoice gap 10.01 > 10.0 → no longer the ingestion signature
    books = _books(taxable_value=0, cgst=0, sgst=0, igst=0, cess=0, total_tax=0, invoice_value=1000.0)
    portal = _portal(taxable_value=5000.0, cgst=0, sgst=0, igst=0, total_tax=0, invoice_value=1010.01)
    assert review.classify_cause("Amount Difference", "Taxable Value Difference", books, portal) != "likely_ingestion_gap"


def test_zero_taxable_but_tax_recorded_is_not_ingestion_gap():
    # tax recorded → the export DID carry tax, so not the mapping-gap signature
    books = _books(taxable_value=0, cgst=90.0, sgst=90.0, total_tax=180.0, invoice_value=1180.0)
    portal = _portal(taxable_value=1000.0, cgst=90.0, sgst=90.0, total_tax=180.0, invoice_value=1180.0)
    assert review.classify_cause("Amount Difference", "Taxable Value Difference", books, portal) != "likely_ingestion_gap"


def test_ingestion_gap_never_fires_on_a_tds_run():
    """A TDS record has no taxable value, so the GST mapping-gap signature must
    not apply — a TDS short deduction is a real value difference."""
    books = {"deductee_name": "A Ltd", "pan": "AAAPA1234A", "section": "194C",
             "amount_paid_credited": 100000, "tax_deducted": 1000, "tax_deposited": 1000}
    portal = {"deductee_name": "A Ltd", "pan": "AAAPA1234A", "section": "194C",
              "amount_paid_credited": 100000, "tax_deducted": 2000, "tax_deposited": 1000}
    cause = review.classify_cause(
        "Amount Difference", "Short Deduction", books, portal,
        recon_type="TDS", materiality=500.0,
    )
    assert cause != "likely_ingestion_gap"
    assert cause == "value_difference"  # ₹1,000 ≥ the ₹500 TDS materiality used here


def test_tds_difference_basis_is_tax_deducted():
    books = {"amount_paid_credited": 100000, "tax_deducted": 1000, "tax_deposited": 1000}
    portal = {"amount_paid_credited": 100000, "tax_deducted": 2000, "tax_deposited": 1000}
    diff, basis = review.item_difference("Amount Difference", "Short Deduction", books, portal, "TDS")
    assert basis == "tax_deducted"
    assert diff == 1000.0


# --- rounding_only ---------------------------------------------------------

def test_rounding_only_at_boundary():
    books = _books(invoice_value=1180.0)
    portal = _portal(invoice_value=1182.0)
    assert review.classify_cause(
        "Amount Difference", "Rounding", books, portal, difference=2.0
    ) == "rounding_only"


def test_rounding_just_over_boundary_not_rounding():
    books = _books(invoice_value=1180.0)
    portal = _portal(invoice_value=1182.01)
    cause = review.classify_cause("Amount Difference", "Rounding", books, portal, difference=2.01)
    assert cause != "rounding_only"
    assert cause in ("below_materiality", "value_difference")


# --- below_materiality -----------------------------------------------------

def test_below_materiality_just_under_threshold():
    """'Below materiality' is reserved for a small difference the engine could
    NOT characterise (no difference_type)."""
    books = _books(taxable_value=1000.0, total_tax=180.0, invoice_value=1180.0)
    portal = _portal(taxable_value=1000.0, total_tax=180.0, invoice_value=1180.0)
    # force a difference of 9,999.99 (< 10,000 materiality, > rounding tolerance)
    cause = review.classify_cause(
        "Amount Difference", "", books, portal,
        difference=9999.99, materiality=10000.0,
    )
    assert cause == "below_materiality"


def test_characterised_sub_materiality_difference_is_a_value_difference():
    """A difference the engine HAS characterised (a specific difference_type) is
    a real value difference however small — grouping it under 'below
    materiality' hid BSH/515's genuine ₹1,180 gap (Run 6 QA Finding 2)."""
    books = _books(taxable_value=1000.0, total_tax=180.0, invoice_value=1180.0)
    portal = _portal(taxable_value=1000.0, total_tax=180.0, invoice_value=1180.0)
    assert review.classify_cause(
        "Amount Difference", "Unexplained", books, portal,
        difference=1180.0, materiality=10000.0,
    ) == "value_difference"


def test_document_type_mismatch_has_its_own_cause():
    """A NON-amount disagreement must not be tagged a value difference — the row
    with a Document Type Mismatch has no value gap at all (OFH/12)."""
    books = _books(taxable_value=14000.0, total_tax=2520.0, invoice_value=16520.0)
    portal = _portal(taxable_value=14000.0, total_tax=2520.0, invoice_value=16520.0)
    assert review.classify_cause(
        "Amount Difference", "Document Type Mismatch", books, portal, difference=0.0,
    ) == "document_type_mismatch"
    assert review.classify_cause(
        "Amount Difference", "Timing Difference", books, portal, difference=0.0,
    ) == "timing_difference"
    for cause in ("document_type_mismatch", "timing_difference"):
        assert cause in review.CAUSE_LABELS
        assert review.cause_group(cause) == "judgement"


def test_materiality_boundary_exactly_at_threshold_is_value_difference():
    books, portal = _books(), _portal()
    cause = review.classify_cause(
        "Amount Difference", "Tax Amount Difference", books, portal,
        difference=10000.0, materiality=10000.0,
    )
    assert cause == "value_difference"


# --- value_difference ------------------------------------------------------

def test_value_difference_above_materiality():
    books = _books(taxable_value=1000.0, cgst=90.0, sgst=90.0, total_tax=180.0, invoice_value=1180.0)
    portal = _portal(taxable_value=15000.0, igst=2700.0, cgst=0.0, sgst=0.0, total_tax=2700.0, invoice_value=17700.0)
    # taxable-value difference 14,000 ≥ 10,000 materiality → a real value difference
    assert review.classify_cause("Amount Difference", "Taxable Value Difference", books, portal) == "value_difference"


# --- numeric tolerance (the "not a difference" rule) -----------------------

def test_numeric_compare_treats_zero_float_and_int_zero_equal():
    books = _books(cess=0.0)
    portal = _portal(cess=0)
    rows = review.compare_fields(books, portal, "GST")
    cess = next(r for r in rows if r.key == "cess")
    assert cess.differs is False


def test_numeric_compare_treats_int_and_float_amounts_equal():
    books = _books(invoice_value=26066)
    portal = _portal(invoice_value=26066.0)
    rows = review.compare_fields(books, portal, "GST")
    iv = next(r for r in rows if r.key == "invoice_value")
    assert iv.differs is False
    assert iv.is_material is False


def test_real_amount_difference_is_flagged_and_material():
    books = _books(taxable_value=0.0, total_tax=0.0, invoice_value=26066)
    portal = _portal(taxable_value=22090.0, total_tax=3976.2, invoice_value=26066.0)
    rows = review.compare_fields(books, portal, "GST")
    tv = next(r for r in rows if r.key == "taxable_value")
    assert tv.differs is True
    assert tv.is_material is True


# --- empty-field hiding ----------------------------------------------------

def test_tds_only_fields_hidden_on_a_gst_item():
    books = _books(pan=None, section=None, tax_deducted=None, tax_deposited=None)
    portal = _portal()
    rows = review.compare_fields(books, portal, "GST")
    keys = {r.key for r in rows}
    assert "pan" not in keys
    assert "section" not in keys
    assert "tax_deducted" not in keys
    assert "tax_deposited" not in keys
    assert "original_row" not in keys
    assert "source_file" not in keys


# --- cause groups + totality ----------------------------------------------

def test_every_cause_maps_into_a_group():
    for cause in review.CAUSE_LABELS:
        assert review.cause_group(cause) in ("gap", "judgement")


def test_every_cause_has_a_verdict_and_next_step():
    for cause in review.CAUSE_LABELS:
        item = {"cause": cause, "party": "X", "reference": "1", "difference": 10.0,
                "portal_value": 100.0, "books_value": 100.0}
        assert review.verdict_text(item)
        assert review.next_step(cause)["text"]


def test_cause_is_display_only_and_does_not_touch_classification():
    """The classifier takes classification in and never returns one."""
    out = review.classify_cause("Amount Difference", "Rounding", _books(), _portal(), difference=0.5)
    assert out in review.CAUSE_LABELS
    assert out not in ("Matched", "Amount Difference", "Not in Books", "Not in Portal")


# --- formatting ------------------------------------------------------------

def test_money_uses_indian_grouping():
    assert review.format_money(722707.0) == "₹7,22,707.00"
    assert review.format_money(821221) == "₹8,21,221.00"
    assert review.format_money(1234.5) == "₹1,234.50"
    assert review.format_money(123) == "₹123.00"


def test_money_zero_and_missing():
    assert review.format_money(0) == "₹0.00"
    assert review.format_money(0.0) == "₹0.00"
    assert review.format_money(None) == "—"
    assert review.format_money("") == "—"


def test_signed_money():
    assert review.format_signed_money(22090.0) == "+₹22,090.00"
    assert review.format_signed_money(-1234.0) == "−₹1,234.00"
    assert review.format_signed_money(0) == "—"


def test_date_formatting():
    assert review.format_date("2026-08-24") == "24 Aug 2026"
    assert review.format_date("24-08-2026") == "24 Aug 2026"
    assert review.format_date("24/08/2026") == "24 Aug 2026"
    assert review.format_date("") == "—"


def test_period_formatting():
    assert review.format_period("2026-08") == "Aug 2026"
    assert review.format_period("-") == "-"


def test_no_raw_keys_leak_for_spec_fields():
    for spec in review.FIELD_SPECS.values():
        for key, section, label in spec:
            assert section in ("Identity", "Amounts"), key
            assert "_" not in label, label
            assert label == review.FIELD_LABELS.get(key), key
