"""The Reconcile upload page is a row per document — and choosing a file in a
row is the WHOLE job.

What this pins down (the design decision behind the Stage-2 rebuild): the page
used to ask the user to name each file's slot in a dropdown and then confirm an
"Upload & normalize" button. The classify-then-file ordering was the user doing
a job the engine already does, and it produced a dead end — drop a file before
picking a slot and the page refused it with "Pick a slot for this file first."

So Stage 2 gives every document its own row, and the row's file input does
everything the old button did, in one action:

    save to <client>/<period>/<source_type>  ->  normalize  ->  verdict

These tests drive the real `upload_slot` handler and the real `_refile` helper,
unbound, the same way `tests/test_note_slot_wiring.py` drives `_slot_info`:
`ReconcileState()` cannot be instantiated outside a running app, so the state
is a stand-in and only the unit under test is real.

Run:

    venv/bin/python -m pytest tests/test_reconcile_upload_row.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from setu.state import reconcile_state as rs


# --- Harness ---------------------------------------------------------------


class _Upload:
    """The bit of rx.UploadFile the handler touches: `.filename` and an
    awaitable `.read()`."""

    def __init__(self, filename: str, data: bytes = b"col\n1\n"):
        self.filename = filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


def _state(tmp_path, monkeypatch, *, period="2026-08", client_id=1, **extra):
    """A stand-in self whose storage root is tmp_path, with the real
    `_set_slot_error` bound (it only reads/writes `slot_errors`)."""
    monkeypatch.setattr(
        rs, "source_data_path",
        lambda client_id, period, source_type: _mk(tmp_path, client_id, period, source_type),
    )
    calls: list[tuple] = []
    st = SimpleNamespace(
        ctx_client_id=client_id,
        ctx_period=period,
        ctx_recon_type="GST",
        selections={},
        slot_errors={},
        slot_busy="",
        upload_error="",
        error="",
        flash="",
        rail_return=True,
        slots=[],
        normalised=calls,
        _normalize=lambda source_type, filename: calls.append((source_type, filename)),
        _load_slots=lambda: None,
    )
    # Bind the real helper to this stand-in (unbound access returns a plain
    # function, which would otherwise take `self` from the first argument).
    st._set_slot_error = lambda source_type, message: rs.ReconcileState._set_slot_error(
        st, source_type, message
    )
    st.__dict__.update(extra)
    return st, calls


def _mk(root, client_id, period, source_type) -> Path:
    path = root / str(client_id) / str(period) / str(source_type)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(handler, st, source_type, files):
    """Drain the async generator the way Reflex does, collecting yields."""
    async def drive():
        out = []
        async for _ in handler(st, source_type, files):
            out.append(_)
        return out
    return asyncio.run(drive())


# --- Choosing a file uploads AND normalizes --------------------------------


def test_choosing_a_file_saves_it_and_normalizes_it_in_one_action(tmp_path, monkeypatch):
    """The guarantee. One call, no second confirmation: the bytes land in the
    document's folder and the file is normalized."""
    st, normalised = _state(tmp_path, monkeypatch)

    _run(rs.ReconcileState.upload_slot.fn, st, "gstr2b", [_Upload("GSTR2B_082026.xlsx")])

    saved = tmp_path / "1" / "2026-08" / "gstr2b" / "GSTR2B_082026.xlsx"
    assert saved.exists(), "the file must be saved to the document's own folder"
    assert normalised == [("gstr2b", "GSTR2B_082026.xlsx")], "and normalized, unprompted"


def test_the_file_goes_to_the_slot_the_ROW_names_not_a_dropdown(tmp_path, monkeypatch):
    """The row's identity decides the destination — that is what removes the
    'pick a slot first' dead end."""
    st, normalised = _state(tmp_path, monkeypatch)

    _run(rs.ReconcileState.upload_slot.fn, st, "tally", [_Upload("PurchaseRegister.xlsx")])

    assert (tmp_path / "1" / "2026-08" / "tally" / "PurchaseRegister.xlsx").exists()
    assert not (tmp_path / "1" / "2026-08" / "gstr2b").exists()
    assert normalised == [("tally", "PurchaseRegister.xlsx")]


def test_the_row_is_marked_busy_before_the_model_call_starts(tmp_path, monkeypatch):
    """Normalizing is the slow part, so the row has to paint 'reading it'
    first. The handler is a generator and yields exactly there."""
    st, _ = _state(tmp_path, monkeypatch)
    seen: list[str] = []

    def normalize(source_type, filename):
        seen.append(st.slot_busy)          # what the browser would be showing

    st._normalize = normalize
    yields = _run(rs.ReconcileState.upload_slot.fn, st, "gstr2b", [_Upload("a.xlsx")])

    assert yields, "the handler must yield so the busy state reaches the browser"
    assert seen == ["gstr2b"], "normalize runs while the row is still marked busy"
    assert st.slot_busy == "", "and the row is released afterwards"


def test_a_failed_normalize_lands_on_the_row_not_in_a_global_banner(tmp_path, monkeypatch):
    """Each row owns its own failure, so one bad file cannot read as a
    page-level error about something else."""
    st, _ = _state(tmp_path, monkeypatch)

    def boom(source_type, filename):
        st.error = "AI ingestion couldn't read it: model unavailable"

    st._normalize = boom
    _run(rs.ReconcileState.upload_slot.fn, st, "tally", [_Upload("a.xlsx")])

    assert "model unavailable" in st.slot_errors["tally"]
    assert st.error == "", "the global error must not double-report it"


def test_nothing_selected_is_reported_on_the_row(tmp_path, monkeypatch):
    st, normalised = _state(tmp_path, monkeypatch)

    _run(rs.ReconcileState.upload_slot.fn, st, "gstr2b", [])

    assert st.slot_errors["gstr2b"] == "Choose a file to upload."
    assert normalised == []


def test_an_invalid_period_refuses_before_writing_anything(tmp_path, monkeypatch):
    """The period is the folder the engine reads, so an unfiled period must
    fail loudly instead of stranding the file somewhere unreachable."""
    st, normalised = _state(tmp_path, monkeypatch, period="-")

    _run(rs.ReconcileState.upload_slot.fn, st, "gstr2b", [_Upload("a.xlsx")])

    assert "real period" in st.slot_errors["gstr2b"]
    assert normalised == []


# --- Moving a mis-slotted file --------------------------------------------


def test_refile_moves_the_file_into_the_row_it_belongs_in(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    src = _mk(tmp_path, 1, "2026-08", "tally") / "GSTR2B.xlsx"
    src.write_bytes(b"x")

    assert rs._refile(1, "2026-08", "tally", "gstr2b", "GSTR2B.xlsx") == ""

    assert (tmp_path / "1" / "2026-08" / "gstr2b" / "GSTR2B.xlsx").exists()
    assert not src.exists()


def test_refile_refuses_to_overwrite_a_file_already_in_the_target(tmp_path, monkeypatch):
    """The copy already in the target row may be the one the user wants, so
    this asks instead of clobbering it."""
    _state(tmp_path, monkeypatch)
    src = _mk(tmp_path, 1, "2026-08", "tally") / "GSTR2B.xlsx"
    src.write_bytes(b"misplaced")
    existing = _mk(tmp_path, 1, "2026-08", "gstr2b") / "GSTR2B.xlsx"
    existing.write_bytes(b"the real one")

    message = rs._refile(1, "2026-08", "tally", "gstr2b", "GSTR2B.xlsx")

    assert "already in GSTR-2B" in message
    assert src.read_bytes() == b"misplaced", "nothing moved"
    assert existing.read_bytes() == b"the real one", "nothing overwritten"


# --- The row's own chrome --------------------------------------------------


def _slot_state(**extra):
    """A stand-in for the parts of State that `_load_slots` and the real
    `_compute_readiness` touch."""
    st = SimpleNamespace(
        ctx_client_id=1, ctx_period="2026-08", ctx_recon_type="GST",
        selections={}, slot_errors={}, slot_busy="", slots=[],
        books_ok=False, portal_ok=False, ready=False, blockers=[],
        _slot_info=lambda source_type, filename: {
            "code": "empty", "chip_label": "— no file yet", "chip_variant": "placeholder",
            "rows": 0, "needs_review": 0, "required_missing": 0, "message": "",
            "caveats": [], "resolved": False, "suggested_source_type": "",
        },
    )
    st._compute_readiness = lambda: rs.ReconcileState._compute_readiness(st)
    st.__dict__.update(extra)
    return st


def test_every_row_gets_its_own_upload_input(tmp_path, monkeypatch):
    """One file input per row means no shared widget to disambiguate — the
    identity of the row IS the destination."""
    state = _slot_state()
    monkeypatch.setattr(rs, "_list_files", lambda *a, **k: [])
    rs.ReconcileState._load_slots(state)

    ids = [s.upload_id for s in state.slots]
    assert ids == ["rc_up_tally", "rc_up_gstr2b", "rc_up_ims", "rc_up_credit_notes"]
    assert len(set(ids)) == len(ids), "ids must be unique or the browser mixes rows up"


def test_the_note_row_reads_as_optional_not_as_the_books_side(tmp_path, monkeypatch):
    """`is_books`/`is_note` drive the row's side pill, so the optional note
    register must not claim to be the mandatory books side."""
    state = _slot_state()
    monkeypatch.setattr(rs, "_list_files", lambda *a, **k: [])
    rs.ReconcileState._load_slots(state)

    by_type = {s.source_type: s for s in state.slots}
    assert by_type["tally"].is_books and not by_type["tally"].is_note
    assert not by_type["gstr2b"].is_books and not by_type["gstr2b"].is_note
    assert by_type["credit_notes"].is_note and not by_type["credit_notes"].is_books


# --- Naming the row a wrong file belongs in -------------------------------


def test_the_engine_can_name_the_slot_a_mis_filed_document_belongs_in():
    """Drives the row's one-click "move it" instead of making the user work
    out which slot they meant."""
    from src.ingestion_ai.normalizer import suggested_slot

    assert suggested_slot("tally", "GSTR-2B") == "gstr2b"
    assert suggested_slot("tally", "IMS export") == "ims"
    # Already right, unknown, or has no slot in this app -> nothing to suggest.
    assert suggested_slot("gstr2b", "GSTR-2B") is None
    assert suggested_slot("tally", "GSTR-1") is None
    assert suggested_slot("tally", "") is None


def test_a_wrong_slot_verdict_carries_the_suggestion_into_the_row(monkeypatch):
    """`_slot_info` surfaces it from the classification stored at normalize
    time, which is how the row knows what to offer."""
    stored = {
        "status": "wrong_slot",
        "message": "This file looks like a GSTR-2B export, but you uploaded it as the Books.",
        "classification": {"document_type": "GSTR-2B", "confidence": 98},
        "mapping": [], "caveats": [], "unmapped_required": [], "row_count_out": 0,
    }
    monkeypatch.setattr(rs.ingestion_ai, "get_stored_result", lambda *a, **k: stored)

    info = rs.ReconcileState._slot_info(
        SimpleNamespace(ctx_client_id=1, ctx_period="2026-08"), "tally", "GSTR2B.xlsx"
    )

    assert info["code"] == "wrong"
    assert info["suggested_source_type"] == "gstr2b"


def test_a_suggestion_for_a_slot_this_run_does_not_show_is_dropped(monkeypatch):
    """A TDS run must not offer to move a file into a GST-only row."""
    state = _slot_state(
        ctx_recon_type="TDS",
        _slot_info=lambda source_type, filename: {
            "code": "wrong", "chip_label": "✕ looks like the wrong file type",
            "chip_variant": "danger", "rows": 0, "needs_review": 0,
            "required_missing": 0, "message": "", "caveats": [], "resolved": False,
            "suggested_source_type": "gstr2b",
        },
    )
    monkeypatch.setattr(rs, "_list_files", lambda *a, **k: [])
    rs.ReconcileState._load_slots(state)

    assert state.slots, "a TDS run still has rows"
    assert all(s.suggested_source_type == "" for s in state.slots)


# --- Removing a row clears the selection, never the file -------------------


def test_removing_a_row_keeps_the_users_file_on_disk(tmp_path, monkeypatch):
    """Remove empties the row; it does not delete anything the user supplied —
    the row's file picker can hand it straight back."""
    st, _ = _state(tmp_path, monkeypatch)
    saved = _mk(tmp_path, 1, "2026-08", "gstr2b") / "GSTR2B.xlsx"
    saved.write_bytes(b"x")
    st.selections["gstr2b"] = "GSTR2B.xlsx"

    rs.ReconcileState.remove_slot.fn(st, "gstr2b")

    assert "gstr2b" not in st.selections
    assert saved.exists()
