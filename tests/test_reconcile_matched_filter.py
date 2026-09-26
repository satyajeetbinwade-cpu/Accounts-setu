"""Regression: the Review screen's Matched classification chip must show rows.

Bug (Run 2): selecting the "Matched N" classification chip set
``filter_classification = "Matched"``, but ``_matches`` applied the default
"exception list" rule (hide ``bucket == "Matched"``) BEFORE consulting the
classification filter — so the chip reported a count (e.g. 11) while the table
rendered "No items match the current filters."

These tests exercise the filter predicate directly on the real
``ReconcileState._matches`` implementation (unbound, with a lightweight stand-in
for ``self``), so the UI's chip -> table path can never regress again.
"""

from __future__ import annotations

import types

from setu.state.reconcile_state import ReconcileState


def _item(bucket: str, **over) -> dict:
    """A review-model item with every field ``_matches`` reads."""
    base = {
        "bucket": bucket,
        "cause": "matched_exactly" if bucket == "Matched" else "value_mismatch",
        "cause_group": "matched" if bucket == "Matched" else "value",
        "confidence_band": "High",
        "reviewed": False,
        "supplier_key": "acme",
        "party": "Acme Textiles",
        "gstin": "27AABCM1234K1Z9",
        "reference": "INV-1",
    }
    base.update(over)
    return base


def _state(**over) -> types.SimpleNamespace:
    """Every attribute ``_matches`` reads, defaulted to 'no filter'."""
    base = {
        "filter_view": "",
        "filter_classification": "",
        "filter_cause": "",
        "filter_cause_group": "",
        "filter_confidence": "",
        "filter_review_state": "",
        "filter_supplier": "",
        "active_only": False,
        "search_query": "",
    }
    base.update(over)
    return types.SimpleNamespace(**base)


def _matches(state, item) -> bool:
    # ``_matches`` is a plain method (not an @rx.event); call it unbound.
    return ReconcileState._matches(state, item)


def test_default_view_hides_matched():
    assert _matches(_state(), _item("Matched")) is False
    assert _matches(_state(), _item("Amount Difference")) is True


def test_matched_classification_chip_shows_matched_rows():
    # The regression: the chip sets filter_classification, not filter_view.
    st = _state(filter_classification="Matched")
    assert _matches(st, _item("Matched")) is True
    assert _matches(st, _item("Amount Difference")) is False
    assert _matches(st, _item("Not in Books")) is False
    assert _matches(st, _item("Not in Portal")) is False


def test_matched_view_filter_shows_matched_rows():
    # The KPI card / donut slice path sets filter_view.
    st = _state(filter_view="matched")
    assert _matches(st, _item("Matched")) is True
    assert _matches(st, _item("Amount Difference")) is False


def test_non_matched_classification_still_hides_matched():
    st = _state(filter_classification="Amount Difference")
    assert _matches(st, _item("Matched")) is False
    assert _matches(st, _item("Amount Difference")) is True


def test_matched_chip_combines_with_other_filters():
    st = _state(filter_classification="Matched", filter_confidence="Low")
    assert _matches(st, _item("Matched", confidence_band="Low")) is True
    assert _matches(st, _item("Matched", confidence_band="High")) is False


def test_reviewed_view_with_matched_chip_requires_reviewed():
    st = _state(filter_view="reviewed", filter_classification="Matched")
    assert _matches(st, _item("Matched", reviewed=True)) is True
    assert _matches(st, _item("Matched", reviewed=False)) is False


def test_clearing_the_matched_chip_restores_default_view():
    assert _matches(_state(filter_classification=""), _item("Matched")) is False
