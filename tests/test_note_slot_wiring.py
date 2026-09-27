"""The Reconcile flow can actually SUPPLY a note register.

The gap this file closes: slice 2 registered `credit_notes` as an ingestible
source, and `load_note_frames` reads `selected_files["credit_notes"]` — but
Reconcile built its Stage-2 slots from `RECON_SOURCE_TYPES`, which deliberately
excludes the note register. So the filename could never reach the run, and the
note pass could only ever be triggered from tests.

Two claims are pinned here, and they pull in opposite directions on purpose:

1. The note register IS offered as a Stage-2 slot for GST, and IS carried into
   `selected_files` so `load_note_frames` can find it.
2. It is NEVER part of the invoice sides — not in `RECON_SOURCE_TYPES`, not
   counted in readiness, and submitting it alone cannot start a run.

Run:

    venv/bin/python -m pytest tests/test_note_slot_wiring.py -v
"""

from __future__ import annotations

import pytest

from src.shared import discovery


# --- Slot ordering (pure) --------------------------------------------------


def test_the_note_register_is_offered_for_gst():
    assert discovery.note_slot_types("GST") == ["credit_notes"]


@pytest.mark.parametrize("recon_type", ["TDS", "OTHER", ""])
def test_the_note_register_is_not_offered_for_other_recon_types(recon_type):
    """The note pass matches against the portal's B2B-CDNR note sheet, which
    only a GST run has — offering the slot elsewhere would collect a file
    nothing ever reads."""
    assert discovery.note_slot_types(recon_type) == []


def test_required_slots_come_before_the_optional_note_slot():
    types = discovery.slot_types_for("GST")
    assert types == ["tally", "gstr2b", "ims", "credit_notes"]
    assert types.index("credit_notes") == len(discovery.RECON_SOURCE_TYPES["GST"])


def test_the_note_register_is_never_an_invoice_side():
    """THE structural guard. `RECON_SOURCE_TYPES` drives which files can be
    chosen as the books or portal side of an INVOICE run, so the note register
    must never appear in it for any recon type."""
    for recon_type, sources in discovery.RECON_SOURCE_TYPES.items():
        for note in discovery.NOTE_SOURCE_TYPES:
            assert note not in sources, f"{note} leaked into RECON_SOURCE_TYPES[{recon_type}]"


def test_the_note_register_is_not_a_books_side_source():
    """`BOOKS_SOURCE_TYPES` drives the "a run needs a books side" requirement.
    The register is books-SIDE but is not the books side."""
    assert "credit_notes" not in discovery.BOOKS_SOURCE_TYPES


def test_the_note_register_is_not_a_portal_source():
    portals = discovery.portal_source_types("GST")
    assert "credit_notes" not in portals


def test_every_gst_slot_offered_has_a_label_and_a_hint():
    """A slot with no label renders as a raw folder key, and a slot with no
    hint leaves the user guessing what to put in it.

    Scoped to the GST slots on purpose: the 2C "Other" sources have labels but
    no hints, which is a pre-existing gap unrelated to the note register and
    out of scope here. Asserting it globally would pin a requirement nobody
    has agreed to.
    """
    for source_type in discovery.slot_types_for("GST"):
        assert discovery.source_type_label(source_type) != source_type
        assert discovery.source_type_hint(source_type)


def test_the_note_slot_is_labelled_as_both_credit_and_debit():
    """The register's own voucher-type column reads "Debit Note" while the rows
    are credit notes RECEIVED. A label saying only "Credit Note" would make a
    user think their file is in the wrong place."""
    label = discovery.source_type_label("credit_notes")
    assert "Credit" in label and "Debit" in label
    assert "received" in label.lower(), "the direction is what makes the label unambiguous"


def test_the_note_slot_hint_says_it_is_optional():
    """The hint is the only place the UI explains that skipping this slot is
    fine — a run without it still reconciles invoices."""
    hint = discovery.source_type_hint("credit_notes")
    assert "optional" in hint.lower()


# --- Readiness (the note slot must never block a run) ---------------------


def _slot(source_type, *, resolved=True, code="ok", label=None, message=""):
    """A stand-in SlotRow — `_compute_readiness` only reads these attributes."""
    from types import SimpleNamespace

    return SimpleNamespace(
        source_type=source_type,
        resolved=resolved,
        code=code,
        label=label or discovery.source_type_label(source_type),
        message=message,
    )


def _readiness(slots, *, period="2025-04", recon_type="GST"):
    """Run the REAL `_compute_readiness` against a stand-in self, the same way
    `tests/test_reconcile_matched_filter.py` exercises `_matches` unbound."""
    from types import SimpleNamespace

    from setu.state.reconcile_state import ReconcileState

    state = SimpleNamespace(
        slots=slots, ctx_period=period, ctx_recon_type=recon_type,
        books_ok=False, portal_ok=False, ready=False, blockers=[],
    )
    ReconcileState._compute_readiness(state)
    return state


def test_a_run_is_ready_without_any_note_register_at_all():
    """THE claim the whole slice rests on. A GST run with the books side and a
    portal source, and no note register, must be READY — the register is
    optional, so its absence cannot gate reconciliation."""
    state = _readiness([_slot("tally"), _slot("gstr2b")])
    assert state.ready is True
    assert state.blockers == []


def test_a_broken_note_register_does_not_block_a_run():
    """A wrong-typed file in the note slot (say the Purchase Register dropped
    in by mistake) is surfaced on its own card, but must not stop the run —
    the invoice reconciliation does not depend on it."""
    state = _readiness(
        [_slot("tally"), _slot("gstr2b"), _slot("credit_notes", resolved=False, code="wrong")]
    )
    assert state.ready is True
    assert state.blockers == []


def test_a_selected_note_register_also_leaves_the_run_ready():
    """Consistent either way: supplying the register must not change readiness."""
    state = _readiness([_slot("tally"), _slot("gstr2b"), _slot("credit_notes")])
    assert state.ready is True
    assert state.blockers == []


def test_a_broken_REQUIRED_slot_still_blocks():
    """The contrast that proves the exclusion above is deliberate rather than
    an accident of the note slot never being inspected."""
    state = _readiness(
        [_slot("tally", resolved=False, code="wrong", message="looks like the wrong file type"),
         _slot("gstr2b")]
    )
    assert state.ready is False
    assert any("Books / Purchase Register" in b for b in state.blockers)


def test_the_note_slot_is_flagged_optional_and_not_books_side():
    """What the State stamps onto the SlotRow for the note register."""
    note_types = set(discovery.note_slot_types("GST"))
    assert "credit_notes" in note_types
    # `is_books` drives the "Books side" pill and must be False for the note
    # slot, or the slot reads as the mandatory books side.
    assert "credit_notes" not in set(discovery.BOOKS_SOURCE_TYPES)


# --- The slot chip must not cry wolf --------------------------------------


def _slot_info(monkeypatch, stored, source_type="credit_notes"):
    """Run the REAL `_slot_info` with a stubbed stored result."""
    from types import SimpleNamespace

    from setu.state import reconcile_state as rs

    monkeypatch.setattr(rs.ingestion_ai, "get_stored_result", lambda *a, **k: stored)
    return rs.ReconcileState._slot_info(
        SimpleNamespace(ctx_client="C", ctx_period="2026-08"), source_type, "Note Register.xlsx"
    )


def _entry(field, column=None, confidence=None, required=False):
    return {
        "canonical_field": field, "source_column": column,
        "confidence": confidence, "required": required,
    }


def test_absent_optional_fields_are_not_reported_as_needing_review(monkeypatch):
    """A note register carries no invoice_number, document_type, igst or cess —
    four canonical fields that are absent BY DESIGN for an intra-state register.
    None is required, so none may read as "needs review": that produced an
    unfixable "⚠ 4 field(s) need review" on a perfectly good file, which invites
    the user to go fix a mapping there is nothing wrong with."""
    stored = {
        "status": "ok",
        "row_count_out": 2,
        "unmapped_required": [],
        "caveats": [],
        "mapping": [
            _entry("invoice_number"), _entry("document_type"),
            _entry("igst"), _entry("cess"),
            _entry("gstin", "GSTIN/UIN", required=True),
            _entry("note_reference", "note_reference", required=True),
        ],
    }
    info = _slot_info(monkeypatch, stored)
    assert info["needs_review"] == 0
    assert info["code"] == "ok"
    assert info["chip_label"] == "✓ 2 rows, fully mapped"


def test_a_low_confidence_mapping_still_reads_as_needing_review(monkeypatch):
    """The contrast that proves the change did not silence the case the chip
    exists for: a genuine low-confidence AI mapping must still be flagged."""
    stored = {
        "status": "ok",
        "row_count_out": 2,
        "unmapped_required": [],
        "caveats": [],
        "mapping": [
            _entry("party_name", "Particulars", confidence=0.40),
            _entry("note_reference", "note_reference", required=True),
        ],
    }
    info = _slot_info(monkeypatch, stored)
    assert info["needs_review"] == 1
    assert info["code"] == "review"
    assert info["chip_label"] == "⚠ 1 field(s) need review"


def test_a_required_field_with_no_column_still_uses_the_partial_chip(monkeypatch):
    """A REQUIRED gap must keep its own, stronger chip — the change above must
    not downgrade a real problem to "fully mapped"."""
    stored = {
        "status": "partial",
        "row_count_out": 2,
        "unmapped_required": ["taxable_value"],
        "caveats": [],
        "mapping": [_entry("taxable_value", None, required=True)],
    }
    info = _slot_info(monkeypatch, stored)
    assert info["code"] == "partial"
    assert info["required_missing"] == 1
    assert "required field(s) unmapped" in info["chip_label"]
