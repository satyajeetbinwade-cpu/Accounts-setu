"""Note-to-note matching pass.

Pins the note-level behaviour the design specifies:

  * three passes — GSTIN + normalised reference, reference VARIANT (containment),
    then GSTIN + date window + value tolerance;
  * the adjacent-period pass, because a note's own date lags the booking by a
    period (a March note lives in the March GSTR-2B);
  * the same FOUR classification buckets an invoice gets, as their own set;
  * absolute-value comparison, so a double-negative cannot fabricate a
    difference;
  * a kind disagreement is REPORTED (Document Type Mismatch), not orphaned;
  * notes never touch the invoice pool.

Run:

    venv/bin/python -m pytest tests/test_note_matcher.py -v
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.config_loader import load_config
from src.matching.note_matcher import (
    NOTE_CLASSIFICATIONS,
    _note_config,
    as_note_frame,
    match_notes,
    summarise_notes,
)

BOOKS_GSTIN = "07AAAAA0000A1ZZ"
OTHER_GSTIN = "07BBBBB0000B1ZZ"


@pytest.fixture(scope="module")
def gst_config() -> dict:
    return load_config()["gst"]


def books_row(reference="CD-12/04-2025", *, kind="Credit Note", date="2025-03-26",
              value=5235.0, taxable=4840.02, tax=394.98, gstin=BOOKS_GSTIN, party="Supplier A"):
    """A books note-register row (the shape `parse_note_register` produces)."""
    return {
        "gstin": gstin, "note_reference": reference, "note_kind": kind,
        "note_date": date, "party_name": party,
        "taxable_value": taxable, "invoice_value": value,
        "cgst": round(tax / 2, 2), "sgst": round(tax / 2, 2), "igst": None, "cess": None,
        "source_type": "credit_notes", "source_file": "Credit Note.xlsx",
    }


def portal_row(reference="CD-12/04-2025", *, kind="Credit Note", date="2025-03-26",
               value=5235.0, taxable=4840.02, tax=394.98, gstin=BOOKS_GSTIN, party="Supplier A"):
    """A portal B2B-CDNR note row (note number in `invoice_number`)."""
    return {
        "gstin": gstin, "invoice_number": reference, "document_type": kind,
        "invoice_date": date, "party_name": party,
        "taxable_value": taxable, "invoice_value": value,
        "cgst": round(tax / 2, 2), "sgst": round(tax / 2, 2), "igst": None, "cess": None,
        "source_type": "gstr2b", "source_file": "2B.xlsx",
    }


def frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def by_reference(results):
    return {str(r.get("note_reference")): r for r in results}


# --- Frame preparation ------------------------------------------------------


def test_portal_note_number_becomes_the_shared_join_key(gst_config):
    prepared = as_note_frame(frame([portal_row()]), gst_config)
    assert prepared.iloc[0]["note_reference"] == "CD12042025"
    assert prepared.iloc[0]["note_kind"] == "Credit Note"


def test_both_sides_reduce_to_the_same_key(gst_config):
    books = as_note_frame(frame([books_row()]), gst_config)
    portal = as_note_frame(frame([portal_row()]), gst_config)
    assert books.iloc[0]["note_reference"] == portal.iloc[0]["note_reference"]


def test_portal_kind_aliases_are_understood(gst_config):
    prepared = as_note_frame(frame([
        portal_row(reference="A1", kind="CDN"),
        portal_row(reference="A2", kind="DN"),
    ]), gst_config)
    assert list(prepared["note_kind"]) == ["Credit Note", "Debit Note"]


def test_kind_falls_back_to_the_sign_convention(gst_config):
    prepared = as_note_frame(frame([{**portal_row(), "document_type": None, "invoice_value": -100}]), gst_config)
    assert prepared.iloc[0]["note_kind"] == "Credit Note"


def test_money_is_compared_on_absolute_values(gst_config):
    prepared = as_note_frame(frame([{**books_row(), "invoice_value": -5235.0}]), gst_config)
    assert prepared.iloc[0]["invoice_value"] == pytest.approx(5235.0)


def test_the_note_kind_reaches_doc_type_for_the_shared_comparison(gst_config):
    """`_compare_pair` reads `_doc_type`; without this a kind disagreement
    would be invisible."""
    prepared = as_note_frame(frame([books_row(kind="Debit Note")]), gst_config)
    assert prepared.iloc[0]["_doc_type"] == "Debit Note"


def test_empty_and_missing_frames_are_safe(gst_config):
    assert len(as_note_frame(None, gst_config)) == 0
    assert len(as_note_frame(pd.DataFrame(), gst_config)) == 0
    assert match_notes(None, None, gst_config) == []


# --- Pass 1: exact reference ------------------------------------------------


def test_exact_reference_match_is_matched(gst_config):
    results = match_notes(frame([books_row()]), frame([portal_row()]), gst_config)
    assert len(results) == 1
    result = results[0]
    assert result["classification"] == "Matched"
    assert result["difference_type"] is None
    assert result["confidence_band"] == "High"
    assert result["confidence_score"] == pytest.approx(98.0)
    assert result["note_side"] == "both"
    assert result["note_tax"] == pytest.approx(394.98)
    assert result["note_value"] == pytest.approx(5235.0)
    assert "Reference match" in result["match_reason"]


def test_spelling_variants_still_match_on_the_first_pass(gst_config):
    """Case / separator differences are the shared normaliser's job, so they
    must not even reach the variant pass.

    NOTE the inputs are REFERENCES, not narrations: a citation prefix is
    stripped by EXTRACTION (see note_reference.extract_note_reference), never
    by normalisation, so `parse_note_register` always stores a bare reference.
    """
    results = match_notes(
        frame([books_row("CD-12/04-2025")]),
        frame([portal_row("cd-12/04/2025")]),
        gst_config,
    )
    assert results[0]["classification"] == "Matched"
    assert results[0]["confidence_score"] == pytest.approx(98.0)
    assert "Reference match" in results[0]["match_reason"]


# --- Pass 2: reference variant ----------------------------------------------


def test_reference_variant_matches_at_the_variant_baseline(gst_config):
    """A reference the normaliser cannot reconcile but that CONTAINS the
    other still matches — at the lower variant baseline."""
    results = match_notes(
        frame([books_row("CD-12/04-2025")]),          # -> CD12042025
        frame([portal_row("CD-12/04-2025-REV")]),     # -> CD12042025REV
        gst_config,
    )
    assert len(results) == 1
    assert results[0]["classification"] == "Matched"
    assert results[0]["confidence_score"] == pytest.approx(90.0)
    assert results[0]["confidence_band"] == "Medium"
    assert "Reference variant" in results[0]["match_reason"]


# --- Pass 3: GSTIN + date + value -------------------------------------------


def test_unrelated_references_fall_back_to_gstin_date_and_value(gst_config):
    results = match_notes(
        frame([books_row("CD-999")]),
        frame([portal_row("ZZ-888")]),
        gst_config,
    )
    assert len(results) == 1
    assert results[0]["classification"] == "Matched"
    assert results[0]["confidence_score"] == pytest.approx(80.0)
    assert "No reference agreed" in results[0]["match_reason"]


def test_the_fallback_respects_the_date_window(gst_config):
    results = match_notes(
        frame([books_row("CD-999", date="2025-03-01")]),
        frame([portal_row("ZZ-888", date="2025-05-01")]),
        gst_config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}


def test_the_fallback_respects_the_value_tolerance(gst_config):
    results = match_notes(
        frame([books_row("CD-999", value=5235.0)]),
        frame([portal_row("ZZ-888", value=9000.0)]),
        gst_config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}


# --- Classification --------------------------------------------------------


def test_a_value_gap_is_an_amount_difference(gst_config):
    results = match_notes(
        frame([books_row(value=5000.0, taxable=4600.0, tax=400.0)]),
        frame([portal_row(value=5235.0, taxable=4840.02, tax=394.98)]),
        gst_config,
    )
    assert len(results) == 1
    assert results[0]["classification"] == "Amount Difference"
    assert results[0]["difference_type"]


def test_confidence_is_pass_based_not_value_based(gst_config):
    """A reference that matches EXACTLY stays High confidence even with a big
    value gap — how far the amounts differ drives the classification, never
    the certainty of WHICH note matched."""
    results = match_notes(
        frame([books_row(value=1000.0, taxable=900.0, tax=100.0)]),
        frame([portal_row(value=5235.0)]),
        gst_config,
    )
    assert results[0]["classification"] == "Amount Difference"
    assert results[0]["confidence_band"] == "High"


def test_kind_disagreement_is_reported_not_orphaned(gst_config):
    """A CN never MATCHES a DN — but the reference agreed, so the disagreement
    is a finding, not two unrelated orphans."""
    results = match_notes(
        frame([books_row(kind="Credit Note")]),
        frame([portal_row(kind="Debit Note")]),
        gst_config,
    )
    assert len(results) == 1, "the pair must be reported once, not orphaned twice"
    assert results[0]["classification"] == "Amount Difference"
    assert results[0]["difference_type"] == "Document Type Mismatch"
    assert "Kind disagreement" in results[0]["match_reason"]


def test_kind_agreement_can_be_turned_off(gst_config):
    """With the kind taken OUT of the comparison, a CN may match a DN when the
    reference and values agree — the lenient mode."""
    config = {**gst_config, "notes": {**gst_config["notes"],
                                     "match": {**gst_config["notes"]["match"],
                                               "require_kind_agreement": False}}}
    results = match_notes(frame([books_row()]), frame([portal_row(kind="Debit Note")]), config)
    assert len(results) == 1
    assert results[0]["classification"] == "Matched"
    assert results[0]["difference_type"] is None


def test_absolute_values_stop_a_double_negative_fabricating_a_difference(gst_config):
    """Both sides are ITC-negative for a credit note; comparing absolutes is
    what keeps the signs from inventing a mismatch."""
    results = match_notes(
        frame([{**books_row(), "invoice_value": -5235.0, "taxable_value": -4840.02}]),
        frame([{**portal_row(), "invoice_value": 5235.0, "taxable_value": 4840.02}]),
        gst_config,
    )
    assert results[0]["classification"] == "Matched"


# --- Ambiguity -------------------------------------------------------------


def test_ambiguous_candidates_force_low_confidence(gst_config):
    results = match_notes(
        frame([books_row()]),
        frame([portal_row(), portal_row()]),  # the same note twice in the portal
        gst_config,
    )
    assert results[0]["confidence_band"] == "Low"
    assert "Ambiguous" in results[0]["match_reason"]


# --- Residuals -------------------------------------------------------------


def test_a_books_note_with_no_portal_counterpart_is_not_in_portal(gst_config):
    results = match_notes(frame([books_row()]), frame([]), gst_config)
    assert len(results) == 1
    assert results[0]["classification"] == "Not in Portal"
    assert results[0]["portal_record"] is None
    assert results[0]["note_side"] == "books"
    assert results[0]["note_tax"] == pytest.approx(394.98)


def test_a_portal_note_with_no_books_counterpart_is_not_in_books(gst_config):
    results = match_notes(frame([]), frame([portal_row()]), gst_config)
    assert len(results) == 1
    assert results[0]["classification"] == "Not in Books"
    assert results[0]["books_record"] is None
    assert results[0]["note_side"] == "portal"


def test_a_note_with_no_gstin_is_never_keyed_on_one(gst_config):
    """NEITHER side has a GSTIN, so there is no supplier anchor at all and a
    short reference does not identify a document — this engine prefers a flagged
    genuine pair over a clean-looking mismatch."""
    results = match_notes(
        frame([books_row(gstin="")]), frame([portal_row(gstin="")]), gst_config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}


# --- Pass 1 hardening ------------------------------------------------------


def test_a_blank_gstin_on_one_side_still_matches(gst_config):
    """A register that omits the GSTIN used to make its notes PERMANENTLY
    unmatchable — one document reported as both Not in Portal and Not in
    Books. With the reference, amount and date all agreeing, it matches."""
    results = match_notes(
        frame([books_row(gstin="")]), frame([portal_row()]), gst_config,
    )
    assert len(results) == 1
    assert results[0]["classification"] == "Matched"
    # ...and is scored as the weaker tier it is, not as a full exact match.
    assert results[0]["confidence_score"] == pytest.approx(90.0)  # 98 - 8
    assert results[0]["confidence_band"] == "Medium"
    assert "no GSTIN" in results[0]["match_reason"]


def test_a_blank_gstin_with_a_differing_amount_is_not_accepted(gst_config):
    """Without the supplier key the amount must corroborate the reference."""
    results = match_notes(
        frame([books_row(gstin="", value=5235.0)]),
        frame([portal_row(value=9000.0)]),
        gst_config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}
    assert "amounts do not agree" in results[0]["match_reason"]


def test_a_reference_reused_in_a_later_year_is_not_the_same_document(gst_config):
    """THE cross-year trap: the normaliser strips the FY suffix, so
    "SRI-312/25-26" and "SRI-312/26-27" share one key. Without a date guard the
    two documents would be silently paired."""
    results = match_notes(
        frame([books_row("SRI-312/25-26", date="2025-03-26")]),
        frame([portal_row("SRI-312/26-27", date="2026-03-26")]),
        gst_config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}
    # The near-miss is REPORTED, so "no portal note matched" is not misleading.
    assert "DIFFERENT document" in results[0]["match_reason"]


def test_a_late_booking_within_the_guard_still_matches(gst_config):
    """The guard rejects a different YEAR, not a late booking."""
    results = match_notes(
        frame([books_row(date="2025-03-26")]),
        frame([portal_row(date="2025-07-04")]),   # 100 days — inside the guard
        gst_config,
    )
    assert len(results) == 1
    assert results[0]["classification"] == "Matched"
    assert results[0]["confidence_score"] == pytest.approx(98.0)


def test_the_date_guard_is_configurable(gst_config):
    config = {**gst_config, "notes": {**gst_config["notes"],
                                     "match": {**gst_config["notes"]["match"],
                                               "reference_date_guard_days": 10}}}
    results = match_notes(
        frame([books_row(date="2025-03-26")]),
        frame([portal_row(date="2025-07-04")]),
        config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}


def test_a_missing_date_cannot_disqualify_a_reference_match(gst_config):
    results = match_notes(
        frame([books_row(date=None)]), frame([portal_row(date="2026-03-26")]), gst_config,
    )
    assert results[0]["classification"] == "Matched"


def test_the_fallback_tier_still_requires_a_gstin(gst_config):
    """The date+value tier has no reference to lean on, so with no supplier key
    on either side it must not fire."""
    results = match_notes(
        frame([books_row("CD-999", gstin="")]),
        frame([portal_row("ZZ-888", gstin="")]),
        gst_config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}


def test_d3_the_portal_note_date_is_the_suppliers_own_date(gst_config):
    """The confirmed D.3 decision, pinned in config so it cannot drift: the
    portal field read is the SUPPLIER's note date, so a 5-day window compares
    like with like."""
    match = gst_config["notes"]["match"]
    assert match["date_field_portal"] == "note_date"
    assert match["date_field_books"] == "voucher_ref_date"
    assert match["date_window_days"] == 5
    assert match["reference_date_guard_days"] == 180
    assert match["confidence_baselines"]["missing_gstin_penalty"] == -8


def test_gstin_is_part_of_the_key(gst_config):
    results = match_notes(
        frame([books_row(gstin=BOOKS_GSTIN)]),
        frame([portal_row(gstin=OTHER_GSTIN)]),
        gst_config,
    )
    assert {r["classification"] for r in results} == {"Not in Portal", "Not in Books"}


def test_one_portal_note_is_matched_at_most_once(gst_config):
    """Two books rows citing the SAME note: exactly one pairs, and the surplus
    is a real exception in its own right (a second ITC claim)."""
    results = match_notes(
        frame([books_row(), books_row()]),
        frame([portal_row()]),
        gst_config,
    )
    assert sorted(r["classification"] for r in results) == ["Matched", "Not in Portal"]


# --- Adjacent period -------------------------------------------------------


def test_a_note_issued_in_the_prior_period_matches_against_the_adjacent_file(gst_config):
    """The whole reason the adjacent pass exists: a March note is in the March
    2B, not in the April run's file."""
    results = match_notes(
        frame([books_row(date="2025-03-26")]),
        frame([]),                                   # nothing in this period
        gst_config,
        adjacent_portal_notes=frame([portal_row(date="2025-03-26")]),
    )
    assert len(results) == 1
    assert results[0]["classification"] == "Matched"
    assert results[0]["confidence_score"] == pytest.approx(86.0)  # 98 + cross_period -12
    assert "ADJACENT period" in results[0]["match_reason"]


def test_unmatched_adjacent_notes_are_not_this_runs_exceptions(gst_config):
    """An unmatched note from the adjacent file belongs to THAT period and is
    reported by that period's run — never as this run's Not in Books."""
    results = match_notes(
        frame([books_row("CD-1/04-2025")]),
        frame([portal_row("CD-1/04-2025")]),
        gst_config,
        adjacent_portal_notes=frame([portal_row("SOMEONE-ELSES-NOTE")]),
    )
    assert sorted(r["classification"] for r in results) == ["Matched"]
    assert len(results) == 1


def test_the_adjacent_pass_can_be_disabled(gst_config):
    config = {**gst_config, "notes": {**gst_config["notes"],
                                     "match": {**gst_config["notes"]["match"],
                                               "adjacent_period": False}}}
    results = match_notes(
        frame([books_row()]), frame([]), config,
        adjacent_portal_notes=frame([portal_row()]),
    )
    assert [r["classification"] for r in results] == ["Not in Portal"]
    # The reason must say the adjacent check was DISABLED, not that no file was
    # supplied — a supplied-but-ignored file is a different statement.
    assert "adjacent-period check is disabled" in results[0]["match_reason"]


# --- Summary figures -------------------------------------------------------


def test_summary_separates_gross_from_at_risk(gst_config):
    results = match_notes(
        frame([books_row("CD-1/04-2025"), books_row("CD-2/04-2025", value=1000.0, tax=100.0)]),
        frame([portal_row("CD-1/04-2025")]),
        gst_config,
    )
    summary = summarise_notes(results)
    assert summary["total"] == 2
    assert summary["counts"]["Matched"] == 1
    assert summary["counts"]["Not in Portal"] == 1
    assert summary["note_tax_gross"] == pytest.approx(394.98 + 100.0)
    assert summary["note_tax_at_risk"] == pytest.approx(100.0)
    # The matched note's own tax stays with its class.
    assert summary["tax_by_class"]["Matched"] == pytest.approx(394.98)


# --- Structural separation -------------------------------------------------


def test_results_use_only_the_note_classifications(gst_config):
    results = match_notes(
        frame([books_row(), books_row("CD-2/04-2025", value=1000.0)]),
        frame([portal_row(), portal_row("CD-3/04-2025", value=500.0)]),
        gst_config,
    )
    assert results
    for result in results:
        assert result["classification"] in NOTE_CLASSIFICATIONS


def test_every_result_carries_the_note_level_figures(gst_config):
    results = match_notes(frame([books_row()]), frame([portal_row()]), gst_config)
    for result in results:
        for key in ("note_reference", "note_kind", "note_date", "note_tax",
                    "note_value", "note_side"):
            assert key in result, key


# --- Config plumbing -------------------------------------------------------


def test_note_overrides_reach_the_shared_helpers(gst_config):
    """The reused helpers read the top-level keys, so the note overrides must
    be lifted onto them or they would be silently ignored."""
    match = gst_config["notes"]["match"]
    lifted = _note_config(gst_config)
    assert lifted["date_tolerance_days"] == match["date_window_days"]
    assert lifted["confidence_baselines"]["reference_variant"] == \
        match["confidence_baselines"]["reference_variant"]
    assert lifted["confidence_baselines"]["cross_period_penalty"] == \
        match["confidence_baselines"]["cross_period_penalty"]
    # ...and the base keys the helpers read are still present.
    for key in ("amount_tolerance", "rounding_tolerance", "confidence_thresholds"):
        assert key in lifted
