"""The review screen's WORDS: the field glossary, the check names and the reasons.

Nothing here needs a database. The point is that every field the extraction can
produce, and every code that can put a card in front of a reviewer, has plain
words the reader can act on — and that a NEW field or check cannot slip through
without them.
"""

from __future__ import annotations

import pytest

from src.invoice_extract import checks as checks_mod
from src.invoice_extract import fields_v2 as fv2
from setu.state import review_glossary as g


# ===========================================================================
# 1. Every extraction field is described
# ===========================================================================


def test_every_registry_field_has_a_glossary_entry():
    """The keys come from the registry, not from a list kept beside the glossary."""
    assert g.missing_glossary_keys() == []
    # And the registry really is the source: it has the invoice and line keys.
    assert len(fv2.INVOICE_KEYS) >= 20
    assert set(fv2.LINE_FIELD_NAMES) == {"description", "hsn_sac", "quantity", "rate", "amount"}
    for key in fv2.INVOICE_KEYS:
        gloss = g.gloss_for(key)
        assert gloss is not None, f"{key} has no glossary entry"
        assert gloss.label and gloss.label != key, f"{key} is still shown as a key"
        assert gloss.meaning, f"{key} has no plain meaning"
    for name in fv2.LINE_FIELD_NAMES:
        key = fv2.line_key(2, name)
        assert g.gloss_for(key) is not None, f"{key} has no glossary entry"


def test_a_new_registry_key_without_a_gloss_is_caught(monkeypatch):
    """The guard is real: add a key to the registry and it lands in the list."""
    monkeypatch.setattr(fv2, "INVOICE_KEYS", tuple(fv2.INVOICE_KEYS) + ("brand_new_field",))
    assert g.missing_glossary_keys() == ["brand_new_field"]


def test_a_line_glossary_entry_is_numbered_by_document_order():
    assert g.label_for("line.2.hsn_sac") == "Line 2 — HSN/SAC code"
    assert g.label_for("line.1.amount") == "Line 1 — Amount"
    # An unknown key is shown as itself rather than as an empty chip.
    assert g.label_for("never_heard_of_it") == "never_heard_of_it"


def test_the_hsn_sac_meaning_names_hsn_and_sac():
    """The code that decides the tax rate is explained, not just labelled."""
    meaning = g.meaning_for("line.1.hsn_sac")
    assert "HSN" in meaning and "SAC" in meaning
    assert "GST" in meaning


def test_line_labels_and_meanings_survive_a_missing_number():
    # A stale or unnumbered key must not raise: the screen is showing it.
    for key in ("line.hsn_sac", "line.99.hsn_sac", "", "."):
        g.label_for(key)
        g.hint_for(key)
        g.meaning_for(key)


# ===========================================================================
# 2. Every reason and every check has plain words
# ===========================================================================


def test_every_check_key_has_both_a_positive_name_and_a_failure_title():
    """A check's NAME says what holding means; the failure has its own words."""
    assert checks_mod.CHECK_LABELS.keys() == checks_mod.REVIEW_FAIL_TITLES.keys()
    for key, label in checks_mod.CHECK_LABELS.items():
        assert label and "_" not in label, f"{key} is still a code"
        title = checks_mod.REVIEW_FAIL_TITLES[key]
        assert title and "_" not in title, f"{key} has no plain failure headline"
    assert checks_mod.CHECK_LABELS["handwriting_guard"] == "Amounts are printed, not handwritten"
    assert checks_mod.REVIEW_FAIL_TITLES["handwriting_guard"] == "Amounts on this document are handwritten"


def test_every_needs_you_kind_has_a_plain_sentence():
    from src.invoice_extract import rules as rules_mod  # noqa: F401  (the source)

    kinds = ("missing_required", "check_failed", "below_threshold", "direction_unsettled")
    for kind in kinds:
        text = g.reason_text(kind)
        assert text and text[0].isupper() and text.endswith("."), f"{kind}: {text!r}"


def test_every_decision_code_has_a_plain_sentence():
    for code in ("not_printed", "not_on_document", "not_applicable_here", "correct",
                 "ocr_error", "wrong_reader", "other"):
        assert g.reason_text(code), f"{code} has no plain sentence"


def test_reason_text_names_the_field_and_never_shows_a_key():
    text = g.reason_text("missing_required", field_label=g.label_for("line.1.hsn_sac"))
    assert "Line 1 — HSN/SAC code" in text
    for code in g.REASON_TEXT:
        assert "_" not in g.reason_text(code), f"{code} still shows a code"


def test_an_unknown_code_falls_back_to_nothing_not_to_a_key():
    assert g.reason_text("totally_unknown") == ""


def test_every_button_has_help_text_and_a_label():
    assert set(g.BUTTON_HELP) == {"confirm", "not_printed", "not_applicable"}
    for text in g.BUTTON_HELP.values():
        assert text and text[0].isupper()
    assert g.BUTTON_LABEL_CONFIRM == "Confirm value"
    assert g.BUTTON_LABEL_NOT_PRINTED == "Not printed on the document"
    assert g.BUTTON_LABEL_NOT_APPLICABLE == "Not applicable here"


@pytest.mark.parametrize("key", list(fv2.INVOICE_KEYS))
def test_no_glossary_text_is_a_placeholder(key):
    """Nothing in the glossary is an empty shell or a leftover code."""
    gloss = g.gloss_for(key)
    assert gloss is not None
    for text in (gloss.label, gloss.meaning, gloss.where):
        assert "TODO" not in text and "None" not in text
