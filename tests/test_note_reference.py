"""Note identity — reference extraction, normalisation and classification.

Pins the behaviour that `src/matching/note_reference.py` encodes, using the
FIVE narration spellings observed in one client's own register, plus the
classification rule's two halves:

  * a row citing a supplier credit note IS a credit note, whatever its
    voucher-type label says (the reference register reads "Debit Note" on
    every row while every narration cites a credit note), and
  * a row citing nothing keeps today's genuine-debit-note treatment.

Also pins the ONE thing that must not be swapped for the invoice patterns:
the invoice FY patterns are unanchored and would collapse two different
supplier notes ("CD-12/04-2025" and "CD-12/05-2025") onto a single key.

Run:

    venv/bin/python -m pytest tests/test_note_reference.py -v
"""

from __future__ import annotations

import re

import pytest

from src.config_loader import load_config
from src.matching.note_reference import (
    CREDIT_NOTE,
    DEBIT_NOTE,
    DEFAULT_CITATION_PATTERNS,
    DEFAULT_CN_MARKER_PATTERNS,
    DEFAULT_EXTRACT_PATTERN,
    DEFAULT_NORMALISATION,
    SIGNAL_CITATION,
    SIGNAL_CN_MARKER,
    SIGNAL_DEDICATED_REFERENCE,
    SIGNAL_NEGATIVE_VALUE,
    SIGNAL_NONE,
    citation_count,
    classify_note,
    extract_note_reference,
    normalise_note_reference,
)


@pytest.fixture(scope="module")
def notes_cfg() -> dict:
    return load_config()["gst"]["notes"]


# --- Config contract --------------------------------------------------------


def test_config_block_is_present_and_enabled(notes_cfg):
    assert notes_cfg["enabled"] is True
    assert notes_cfg["reference_sources"] == ["dedicated_column", "narration"]
    assert notes_cfg["report_key_collisions"] is True


def test_config_matches_module_defaults(notes_cfg):
    """Behaviour must not depend on whether a config block was passed, so the
    live config and the module's own fallback defaults must stay identical."""
    assert list(notes_cfg["citation_patterns"]) == list(DEFAULT_CITATION_PATTERNS)
    assert list(notes_cfg["cn_marker_patterns"]) == list(DEFAULT_CN_MARKER_PATTERNS)
    assert notes_cfg["reference_extract_pattern"] == DEFAULT_EXTRACT_PATTERN
    for key, value in DEFAULT_NORMALISATION.items():
        assert notes_cfg["normalise"][key] == value, key


def test_never_reference_aliases_guard_the_voucher_number(notes_cfg):
    """`Voucher No.` is the client's own booking number. Mapping it to the
    note reference would fail SILENTLY (every note unmatched), so it is
    explicitly excluded and must not also appear as a reference alias."""
    columns = notes_cfg["columns"]
    assert "voucher no." in columns["never_reference_aliases"]
    assert "voucher no." not in columns["reference_aliases"]
    assert "voucher ref. no." in columns["reference_aliases"]


def test_note_date_is_the_reference_date_not_the_booking_date(notes_cfg):
    """The note's own date lags the booking by a period, so the fallback pass
    must read Voucher Ref. Date rather than the booking Date."""
    assert notes_cfg["match"]["date_field_books"] == "voucher_ref_date"
    assert "voucher ref. date" in notes_cfg["columns"]["note_date_aliases"]
    assert "voucher ref. date" not in notes_cfg["columns"]["booking_date_aliases"]


# --- Extraction: the five observed spellings of ONE client's file -----------

@pytest.mark.parametrize(
    "narration,expected_raw",
    [
        ("CREDIT NOTE NO.  CD-12/04-2025", "CD-12/04-2025"),   # double space, "NO."
        ("CREDIT NOTE NO. SRI-312/25-26", "SRI-312/25-26"),    # FY-style suffix
        ("CREDIT NOTE NO. 50", "50"),                          # bare number
        ("Credit Note No CD-197/04-2025", "CD-197/04-2025"),   # different case, no period
        ("CREDIT NOTE NO. CN/193/25-26", "CN/193/25-26"),      # reference contains "CN/"
    ],
)
def test_reference_extracted_from_every_observed_spelling(notes_cfg, narration, expected_raw):
    token = extract_note_reference(
        narration, notes_cfg["citation_patterns"], notes_cfg["reference_extract_pattern"]
    )
    assert token == expected_raw


def test_extraction_returns_none_without_a_citation(notes_cfg):
    assert extract_note_reference("Being purchase booked", notes_cfg["citation_patterns"]) is None


def test_extraction_cannot_harvest_a_stray_word(notes_cfg):
    """A reference must contain a digit, so the word after the citation cannot
    be taken as a reference."""
    assert extract_note_reference(
        "CREDIT NOTE received", notes_cfg["citation_patterns"], notes_cfg["reference_extract_pattern"]
    ) is None


def test_citation_count_merges_overlapping_patterns(notes_cfg):
    """Two configured patterns can hit the SAME phrase; the count must reflect
    distinct citations, not raw regex matches."""
    assert citation_count("CREDIT NOTE NO. CD-1/04-2025", notes_cfg["citation_patterns"]) == 1
    two = "CREDIT NOTE NO. CD-1/04-2025 and CREDIT NOTE NO. CD-2/04-2025"
    assert citation_count(two, notes_cfg["citation_patterns"]) == 2


# --- Normalisation: one function, both sides --------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CD-12/04-2025", "CD12042025"),
        ("CD-12/05-2025", "CD12052025"),
        ("SRI-312/25-26", "SRI312"),
        ("CN/193/25-26", "CN193"),
        ("50", "50"),
        ("CD-197/04-2025", "CD197042025"),
    ],
)
def test_normalisation_produces_expected_key(raw, expected):
    assert normalise_note_reference(raw) == expected


def test_books_and_portal_spellings_reduce_to_one_key(notes_cfg):
    """Same note, cited two different ways → ONE join key, which is the whole
    point of sharing the normaliser across both sides."""
    spec = notes_cfg["normalise"]
    patterns = notes_cfg["citation_patterns"]
    extract = notes_cfg["reference_extract_pattern"]

    first = extract_note_reference("CREDIT NOTE NO.  CD-12/04-2025", patterns, extract)
    second = extract_note_reference("Credit Note No CD-12/04-2025", patterns, extract)
    assert first == second == "CD-12/04-2025"
    assert normalise_note_reference(first, spec) == normalise_note_reference(second, spec) == "CD12042025"


def test_distinct_notes_do_not_collapse_to_one_key(notes_cfg):
    """THE regression this module exists to prevent.

    `invoice_number_fy_patterns` is unanchored, so reusing it here would match
    the interior "12/04" and "12/05" and merge two different supplier notes
    into one key. The anchored period patterns must keep them apart.
    """
    spec = notes_cfg["normalise"]
    april = normalise_note_reference("CD-12/04-2025", spec)
    may = normalise_note_reference("CD-12/05-2025", spec)
    assert april != may, "distinct supplier notes collapsed onto one key"

    # Proof the note patterns are NOT the invoice ones: applying the invoice
    # list to the same reference DOES collapse it.
    collapsed = "CD-12/04-2025"
    for pattern in load_config()["gst"]["invoice_number_fy_patterns"]:
        collapsed = re.sub(pattern, "", collapsed)
    assert collapsed.replace("/", "").replace("-", "") == "CD2025"
    assert normalise_note_reference("CD-12/04-2025", spec) != "CD2025"


def test_period_suffix_is_stripped_only_from_the_tail(notes_cfg):
    """An FY suffix is not part of the note identity; an interior period token
    is."""
    spec = notes_cfg["normalise"]
    assert normalise_note_reference("SRI-312/25-26", spec) == normalise_note_reference("SRI-312", spec)
    assert normalise_note_reference("CN/193/25-26", spec) == "CN193"


def test_normalisation_is_idempotent(notes_cfg):
    spec = notes_cfg["normalise"]
    for raw in ("CD-12/04-2025", "SRI-312/25-26", "CN/193/25-26", "50"):
        once = normalise_note_reference(raw, spec)
        assert normalise_note_reference(once, spec) == once


def test_blank_reference_normalises_to_empty():
    assert normalise_note_reference(None) == ""
    assert normalise_note_reference("   ") == ""


def test_malformed_configured_pattern_degrades_quietly():
    """A bad regex in config must not take the run down."""
    assert normalise_note_reference("CD-12", {"strip_trailing": "([unclosed"}) != ""


# --- Classification: the precedence rule ------------------------------------

# The four representative rows from the real register. EVERY row carries
# Voucher Type = "Debit Note" while citing a supplier credit note, so the
# voucher label must be overridden in 100% of cases.
REAL_ROWS = [
    ("CREDIT NOTE NO.  CD-12/04-2025", "5235"),
    ("CREDIT NOTE NO. SRI-312/25-26", "22583"),
    ("CREDIT NOTE NO. 50", "4711"),
    ("Credit Note No CD-197/04-2025", "5125"),
]


@pytest.mark.parametrize("narration,gross", REAL_ROWS)
def test_voucher_type_is_not_authoritative(notes_cfg, narration, gross):
    """"Debit Note" voucher + credit-note narration ⇒ CREDIT NOTE."""
    identity = classify_note(
        narration=narration,
        voucher_type="Debit Note",
        invoice_value=gross,
        taxable_value="4206",
        config=notes_cfg,
    )
    assert identity.kind == CREDIT_NOTE, identity
    assert identity.signal == SIGNAL_CITATION
    assert identity.reference, "a cited row must yield a join key"
    assert identity.voucher_type_raw == "Debit Note"
    assert not identity.warnings, identity.warnings


def test_row_without_a_citation_keeps_genuine_debit_note_treatment(notes_cfg):
    identity = classify_note(
        narration="Being goods purchased",
        voucher_type="Debit Note",
        invoice_value=5000,
        taxable_value=4237,
        config=notes_cfg,
    )
    assert identity.kind == DEBIT_NOTE
    assert identity.signal == SIGNAL_NONE
    assert identity.reference is None


def test_populated_dedicated_column_is_a_credit_note_reference(notes_cfg):
    """The column is blank in the observed file, but a future export may
    populate it — then it is the reference, with no citation needed."""
    identity = classify_note(
        reference_column="CD-55/04-2025",
        narration="",
        voucher_type="Debit Note",
        config=notes_cfg,
    )
    assert identity.kind == CREDIT_NOTE
    assert identity.signal == SIGNAL_DEDICATED_REFERENCE
    assert identity.source == "dedicated_column"
    assert identity.reference == "CD55042025"


def test_legacy_cn_marker_still_classifies_without_a_citation(notes_cfg):
    identity = classify_note(
        reference_column="CN-8891",
        narration="",
        voucher_type="Credit Note",
        config=notes_cfg,
    )
    assert identity.kind == CREDIT_NOTE
    assert identity.signal in (SIGNAL_DEDICATED_REFERENCE, SIGNAL_CN_MARKER)


def test_negative_value_still_classifies_without_a_citation(notes_cfg):
    identity = classify_note(
        narration="Being goods returned",
        voucher_type="Debit Note",
        invoice_value=-1180,
        config=notes_cfg,
    )
    assert identity.kind == CREDIT_NOTE
    assert identity.signal == SIGNAL_NEGATIVE_VALUE


def test_blank_row_is_a_debit_note(notes_cfg):
    identity = classify_note(config=notes_cfg)
    assert identity.kind == DEBIT_NOTE
    assert identity.signal == SIGNAL_NONE
    assert identity.citation_count == 0


def test_multiple_citations_use_the_first_and_warn(notes_cfg):
    """Q10 — first match wins, raw narration retained, run-note raised."""
    narration = "CREDIT NOTE NO. CD-1/04-2025 and CREDIT NOTE NO. CD-2/04-2025"
    identity = classify_note(narration=narration, voucher_type="Debit Note", config=notes_cfg)
    assert identity.kind == CREDIT_NOTE
    assert identity.reference_raw == "CD-1/04-2025"
    assert identity.citation_count == 2
    assert any("2 credit notes" in w for w in identity.warnings), identity.warnings


def test_citation_without_a_reference_warns_but_still_classifies(notes_cfg):
    """The row IS a credit note even if the reference cannot be extracted —
    never silently dropped, and never matched on a guessed key."""
    identity = classify_note(
        narration="Credit note received",
        voucher_type="Debit Note",
        config=notes_cfg,
    )
    assert identity.kind == CREDIT_NOTE
    assert identity.signal == SIGNAL_CITATION
    assert identity.reference is None
    assert any("no reference could be extracted" in w for w in identity.warnings), identity.warnings


def test_extracted_reference_that_looks_like_a_date_is_flagged(notes_cfg):
    identity = classify_note(
        narration="CREDIT NOTE DATED 12-04-2025",
        voucher_type="Debit Note",
        config=notes_cfg,
    )
    assert any("looks like a date" in w for w in identity.warnings), identity.warnings


def test_classification_is_config_driven_not_hardcoded(notes_cfg):
    """A client whose narration omits the connector entirely still works,
    because the pattern comes from config."""
    custom = {
        **notes_cfg,
        "citation_patterns": [r"(?i)\breceived\s+note\b"],
        "reference_extract_pattern": r"(?i)\s*((?=[\w./\-]*\d)[A-Za-z0-9][\w./\-]*)",
    }
    identity = classify_note(narration="Received note AB-77/2025", config=custom)
    assert identity.kind == CREDIT_NOTE
    assert identity.reference_raw == "AB-77/2025"
