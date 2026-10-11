"""Stage 2's rows tell the truth, and a re-upload cannot destroy a file.

Two things this file pins down, both of which the redesigned Upload page
depends on:

1. **The states the row can be in.** Each one is derived from what the engine
   actually stored, and the two failures that used to read as SUCCESS are now
   their own state: a header-only workbook and a hard-blocked read both fell
   through `_slot_info` to `ok` with `resolved=True`, so the row said
   "✓ 0 rows, fully mapped" and unlocked Stage 3. They are now "unreadable",
   which blocks — except on the note register, which never blocks anything.

   The row also shows an identification percentage ONLY when a model produced
   one. A deterministic parser has no confidence to report, and printing a
   number for it would be a number the user could act on that nothing measured.

2. **Replacing a file under the same name.** The upload key is the file's
   coordinates, not its bytes, so writing over a file used to destroy the
   previous copy AND leave its verdict sitting on the key — a failed re-read
   would keep showing "fully mapped" about bytes the engine never read.

Run:

    venv/bin/python -m pytest tests/test_reconcile_row_states.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import data_paths as dp
from src.shared import discovery
from setu.state import reconcile_state as rs


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the REAL data root at a throwaway tree.

    `store_upload` resolves `source_data_path` inside `src.data_paths`, so
    patching that module's DATA_ROOT is what actually keeps an upload out of
    the repo's own `data/` tree — patching the state module's imported name
    would leave the writer untouched.
    """
    monkeypatch.setattr(dp, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    return tmp_path


def _stored(**over):
    """A stored ingestion result, with the fields `_slot_info` reads."""
    base = {
        "status": "ok",
        "row_count_out": 1204,
        "mapping": [],
        "unmapped_required": [],
        "caveats": [],
        "classification": {},
        "model_used": None,
        "llm_cached": False,
        "notes": [],
        "message": None,
    }
    base.update(over)
    return base


def _info(monkeypatch, stored, *, source_type="gstr2b", row_error="", filename="GSTR2B.xlsx"):
    """Run the REAL `_slot_info` with a stubbed stored result."""
    monkeypatch.setattr(rs.ingestion_ai, "get_stored_result", lambda *a, **k: stored)
    return rs.ReconcileState._slot_info(
        SimpleNamespace(ctx_client_id=1, ctx_period="2026-08"),
        source_type,
        filename,
        row_error=row_error,
    )


def _mapping_entry(field, column=None, confidence=None, required=False, **extra):
    entry = {
        "canonical_field": field, "source_column": column,
        "confidence": confidence, "required": required,
    }
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# An empty read and a blocked read are NOT success
# ---------------------------------------------------------------------------


def test_an_empty_workbook_is_unreadable_not_fully_mapped(monkeypatch):
    """THE regression. A header-only sheet (or one whose every row was blank)
    used to fall through to `ok`: the row advertised "✓ 0 rows, fully mapped"
    and, because `ok` is resolved, it counted as supplied and unlocked Stage 3
    on the strength of a file with nothing in it."""
    info = _info(monkeypatch, _stored(status="empty", row_count_out=0))

    assert info["code"] == "failed"
    assert info["resolved"] is False
    assert info["message"] == "No usable table found in this file."


def test_a_hard_blocked_read_with_nothing_named_is_unreadable(monkeypatch):
    info = _info(monkeypatch, _stored(status="blocked", unmapped_required=[]))

    assert info["code"] == "failed"
    assert info["resolved"] is False


def test_a_blocked_read_that_names_its_missing_columns_is_still_a_partial_report(monkeypatch):
    """`blocked` with fields named is a DIFFERENT outcome: the engine knows
    exactly which columns it could not map, so the user has something to act
    on and the report carries the gap. It still runs."""
    info = _info(monkeypatch, _stored(status="blocked", unmapped_required=["invoice_number"]))

    assert info["code"] == "partial"
    assert info["resolved"] is True
    assert info["required_missing"] == 1
    assert "invoice_number" in " ".join(info["detail_lines"])


def test_a_parse_or_model_error_reads_as_unreadable(monkeypatch):
    """A normalize that raised leaves NOTHING stored — so without the row's own
    error it is indistinguishable from "never normalized", which offers the
    wrong action (there is nothing to nudge; there is something to retry)."""
    info = _info(
        monkeypatch, None, row_error="AI ingestion couldn't read GSTR2B.xlsx: bad zip"
    )

    assert info["code"] == "failed"
    assert info["resolved"] is False
    # The engine's own words are carried on the row as `error` and shown
    # verbatim; the row does not paraphrase them away.
    assert info["message"] == ""


def test_a_file_that_was_never_read_is_not_confused_with_one_that_failed(monkeypatch):
    info = _info(monkeypatch, None)

    assert info["code"] == "not_ingested"
    assert info["resolved"] is False


def test_the_unreadable_state_says_the_original_file_is_untouched(monkeypatch):
    info = _info(monkeypatch, _stored(status="empty"))

    assert "untouched" in info["status_text"]


# ---------------------------------------------------------------------------
# A percentage is shown only when a model produced one
# ---------------------------------------------------------------------------


def test_a_model_confidence_is_shown(monkeypatch):
    info = _info(
        monkeypatch,
        _stored(model_used="gpt-4o", classification={"document_type": "GSTR-2B", "confidence": 97}),
    )

    assert info["identification"] == "identified 97%"
    assert "identified 97%" in info["status_text"]
    assert "1,204 rows" in info["status_text"], "row count is grouped for reading"


def test_a_deterministic_parser_shows_no_percentage(monkeypatch):
    """The GSTR-2B/IMS/books layouts are parsed by rules. They report no
    confidence — the number in `classification` here is deliberately present
    and must still be ignored, because `model_used is None`."""
    info = _info(
        monkeypatch,
        _stored(model_used=None, classification={"document_type": "GSTR-2B", "confidence": 97}),
    )

    assert info["identification"] == "layout recognised"
    assert "%" not in info["status_text"]


def test_a_model_that_reported_no_confidence_shows_no_percentage(monkeypatch):
    info = _info(monkeypatch, _stored(model_used="gpt-4o", classification={"document_type": "GSTR-2B"}))

    assert info["identification"] == "layout recognised"
    assert "%" not in info["status_text"]


def test_an_unidentifiable_file_never_invents_a_confidence(monkeypatch):
    """The mockup's "Couldn't identify it (41% sure)" — but only when a model
    actually said 41%."""
    rule_based = _info(
        monkeypatch,
        _stored(
            status="unrecognized", model_used=None,
            classification={"document_type": "", "confidence": 41},
        ),
    )
    assert "Couldn't identify it." in rule_based["status_text"]
    assert "%" not in rule_based["status_text"]

    model_based = _info(
        monkeypatch,
        _stored(
            status="unrecognized", model_used="gpt-4o",
            classification={"document_type": "", "confidence": 41},
        ),
    )
    assert "41% sure" in model_based["status_text"]


# ---------------------------------------------------------------------------
# Shape reused — its own state, using the sentence the engine already wrote
# ---------------------------------------------------------------------------


_REUSE_SENTENCE = (
    "Mapping reused from cache for this exact file shape — no model call made."
)


def test_a_reused_mapping_is_its_own_state(monkeypatch):
    info = _info(monkeypatch, _stored(llm_cached=True, notes=[_REUSE_SENTENCE]))

    assert info["code"] == "reused"
    assert info["resolved"] is True, "a reused mapping is still a mapped file"
    # The row shows the engine's own sentence rather than a paraphrase of it.
    assert _REUSE_SENTENCE in info["status_text"]


def test_a_trusted_profile_mapping_also_reads_as_reused(monkeypatch):
    """`llm_cached` is only set by the shape cache; a trusted profile shows up
    per field instead, and it is the same outcome for the user."""
    sentence = (
        "Mapping reused from a trusted profile for this exact file shape — "
        "no model call and no manual step needed."
    )
    info = _info(
        monkeypatch,
        _stored(
            llm_cached=False,
            notes=[sentence],
            mapping=[_mapping_entry("invoice_number", "Inv No", 1.0, from_trusted_profile=True)],
        ),
    )

    assert info["code"] == "reused"
    assert sentence in info["status_text"]


def test_a_fully_mapped_file_is_not_called_reused_without_evidence(monkeypatch):
    info = _info(monkeypatch, _stored(mapping=[_mapping_entry("invoice_number", "Inv No", 0.9)]))

    assert info["code"] == "ok"


# ---------------------------------------------------------------------------
# The note register is exempt from readiness — including when unreadable
# ---------------------------------------------------------------------------


def _slot_row(source_type, *, resolved, code, message=""):
    return SimpleNamespace(
        source_type=source_type,
        resolved=resolved,
        code=code,
        label=discovery.source_type_label(source_type),
        message=message,
    )


def _readiness(slots, *, period="2026-08", recon_type="GST"):
    """Run the REAL `_compute_readiness` against a stand-in self."""
    state = SimpleNamespace(
        slots=slots, ctx_period=period, ctx_recon_type=recon_type,
        books_ok=False, portal_ok=False, ready=False, blockers=[], still_needed="",
    )
    rs.ReconcileState._compute_readiness(state)
    return state


def test_an_unreadable_note_register_never_blocks_a_run():
    """The whole point of the note register being optional. A corrupt note
    file is the user's problem to fix, not a reason to stop reconciling
    invoices — the report says note matching could not be attempted."""
    state = _readiness([
        _slot_row("tally", resolved=True, code="ok"),
        _slot_row("gstr2b", resolved=True, code="ok"),
        _slot_row("credit_notes", resolved=False, code="failed"),
    ])

    assert state.ready is True
    assert state.blockers == []
    assert "credit_notes" not in " ".join(state.blockers)


def test_an_unreadable_required_row_does_block():
    """The contrast that proves the exemption is deliberate: the SAME code on
    a row that is not the note register stops the run."""
    state = _readiness([
        _slot_row("tally", resolved=False, code="failed", message="No usable table found"),
        _slot_row("gstr2b", resolved=True, code="ok"),
    ])

    assert state.ready is False
    assert any("Books / Purchase Register" in b for b in state.blockers)
    assert "No usable table found" in " ".join(state.blockers)


def test_a_blocked_required_row_is_named_in_still_needed_only_when_it_is_not_a_blocker():
    """A row with nothing to say for itself lands in "Still needed"; a row with
    a specific failure lands in the blockers list instead, so the two lines
    never repeat each other."""
    state = _readiness([_slot_row("tally", resolved=True, code="ok")])

    assert state.ready is False
    assert state.blockers == []
    assert state.still_needed.startswith("Still needed:")
    assert "GSTR-2B" in state.still_needed


def test_nothing_is_still_needed_once_the_run_can_go():
    state = _readiness([
        _slot_row("tally", resolved=True, code="ok"),
        _slot_row("gstr2b", resolved=True, code="ok"),
    ])

    assert state.ready is True
    assert state.still_needed == ""


# ---------------------------------------------------------------------------
# Harness for driving an upload/normalize handler the way Reflex does
# ---------------------------------------------------------------------------


def _slot_dir(root, client_id, period, source_type) -> Path:
    path = root / str(client_id) / str(period) / str(source_type)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _upload_state(sandbox, monkeypatch, *, period="2026-08", client_id=1, **extra):
    monkeypatch.setattr(rs, "source_data_path", lambda c, p, s: _slot_dir(sandbox, c, p, s))
    order: list[tuple] = []
    st = SimpleNamespace(
        ctx_client_id=client_id, ctx_period=period, ctx_recon_type="GST",
        selections={}, slot_errors={}, slot_busy="", expanded_rows=[],
        upload_error="", error="", flash="", rail_return=True, slots=[],
        order=order,
        _normalize=lambda source_type, filename: order.append(("normalize", source_type, filename)),
        _load_slots=lambda: None,
    )
    st._set_slot_error = lambda source_type, message: rs.ReconcileState._set_slot_error(
        st, source_type, message
    )
    st.__dict__.update(extra)
    return st, order


def _drive(handler, st, *args):
    async def run():
        return [chunk async for chunk in handler(st, *args)]
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# "Normalize it now" / "Try again" must never look frozen
# ---------------------------------------------------------------------------


def test_normalizing_marks_the_row_busy_and_yields_before_the_call(sandbox, monkeypatch):
    """A normalization can be a live model call (~15-25s). Without the busy
    flag set and flushed first, the row looks like a dead button."""
    st, _ = _upload_state(sandbox, monkeypatch)
    st.selections["gstr2b"] = "GSTR2B.xlsx"
    seen: list[str] = []
    st._normalize = lambda source_type, filename: seen.append(st.slot_busy)

    yields = _drive(rs.ReconcileState.normalize_slot.fn, st, "gstr2b")

    assert yields, "the handler must yield so the busy state reaches the browser"
    assert seen == ["gstr2b"], "normalize runs while the row is still marked busy"
    assert st.slot_busy == "", "and the row is released afterwards"


def test_the_busy_flag_is_cleared_even_when_normalizing_explodes(sandbox, monkeypatch):
    """A `finally`, not a happy-path reset: a row stuck spinning forever is
    indistinguishable from a broken button."""
    st, _ = _upload_state(sandbox, monkeypatch)
    st.selections["tally"] = "Books.xlsx"

    def boom(source_type, filename):
        raise RuntimeError("engine fell over")

    st._normalize = boom

    with pytest.raises(RuntimeError):
        _drive(rs.ReconcileState.normalize_slot.fn, st, "tally")

    assert st.slot_busy == ""


def test_normalizing_with_no_file_chosen_does_nothing(sandbox, monkeypatch):
    st, order = _upload_state(sandbox, monkeypatch)

    assert _drive(rs.ReconcileState.normalize_slot.fn, st, "gstr2b") == []
    assert order == []


# ---------------------------------------------------------------------------
# Which file a row is about
# ---------------------------------------------------------------------------


def _slot_state(**extra):
    from src.shared import discovery as disc

    st = SimpleNamespace(
        ctx_client_id=1, ctx_period="2026-08", ctx_recon_type="GST",
        selections={}, slot_errors={}, slot_busy="", slots=[], slot_rows={},
        expanded_rows=[], books_ok=False, portal_ok=False, ready=False,
        blockers=[], still_needed="",
        _slot_info=lambda source_type, filename, **k: {
            "code": "empty", "chip_label": "— no file yet", "chip_variant": "placeholder",
            "rows": 0, "needs_review": 0, "required_missing": 0, "message": "",
            "caveats": [], "resolved": False, "suggested_source_type": "",
            "status_text": "", "identification": "", "primary_label": "",
            "detail_lines": [],
        },
    )
    st._compute_readiness = lambda: rs.ReconcileState._compute_readiness(st)
    st._effective_file = lambda source_type, files: rs.ReconcileState._effective_file(
        st, source_type, files
    )
    st._row_state = rs.ReconcileState._row_state
    st.__dict__.update(extra)
    assert disc  # imported for symmetry with the other harnesses
    return st


def test_a_row_with_no_answer_preselects_the_newest_file_on_disk(monkeypatch):
    """The mockup's "two candidates" row promises "the newest is selected" —
    and a file that is already on disk must not leave the row reading "waiting"
    while a usable file sits in the folder."""
    state = _slot_state()
    monkeypatch.setattr(rs, "_list_files", lambda *a, **k: ["old.xlsx", "new.xlsx"])
    monkeypatch.setattr(rs, "_newest_file", lambda *a, **k: "new.xlsx")

    rs.ReconcileState._load_slots(state)

    assert state.selections["gstr2b"] == "new.xlsx"


def test_a_cleared_row_is_not_refilled_by_the_preselect(monkeypatch):
    """Remove records an EXPLICIT empty string. If the preselect treated that
    as "never answered" the row would refill itself and Remove would look
    broken."""
    state = _slot_state(selections={"gstr2b": ""})
    monkeypatch.setattr(rs, "_list_files", lambda *a, **k: ["new.xlsx"])
    monkeypatch.setattr(rs, "_newest_file", lambda *a, **k: "new.xlsx")

    rs.ReconcileState._load_slots(state)

    assert state.selections["gstr2b"] == ""
    row = state.slot_rows["gstr2b"]
    assert row.selected == ""
    assert row.state == "waiting"


def test_the_note_row_left_empty_is_its_own_state_with_its_own_word(monkeypatch):
    state = _slot_state()
    monkeypatch.setattr(rs, "_list_files", lambda *a, **k: [])
    monkeypatch.setattr(rs, "_newest_file", lambda *a, **k: "")

    rs.ReconcileState._load_slots(state)

    by_type = {s.source_type: s for s in state.slots}
    assert by_type["tally"].state == "waiting"
    assert by_type["credit_notes"].state == "optional"
    assert by_type["tally"].action_label == "Choose file"
    assert by_type["credit_notes"].action_label == "Add it anyway"


def test_is_note_and_is_optional_are_separate_flags(monkeypatch):
    """They agree today (both are only ever true for `credit_notes`) but they
    mean different things: `is_note` decides how the document is HANDLED (no
    model re-run, exempt from readiness) and `is_optional` how it is SHOWN (the
    "optional" pill, the skipped-row wording). One flag for both would make a
    second optional slot silently become a note register."""
    state = _slot_state()
    monkeypatch.setattr(rs, "_list_files", lambda *a, **k: [])
    monkeypatch.setattr(rs, "_newest_file", lambda *a, **k: "")

    rs.ReconcileState._load_slots(state)

    by_type = {s.source_type: s for s in state.slots}
    assert by_type["credit_notes"].is_note and by_type["credit_notes"].is_optional
    assert not by_type["tally"].is_note and not by_type["tally"].is_optional
    assert not by_type["gstr2b"].is_note and not by_type["gstr2b"].is_optional


def test_a_wrong_file_get_its_own_choose_a_different_file_label():
    assert rs._action_label("wrong", "GSTR2B.xlsx", False) == "Choose a different file"
    assert rs._action_label("empty", "", False) == "Choose file"
    assert rs._action_label("empty", "", True) == "Add it anyway"
    assert rs._action_label("ok", "GSTR2B.xlsx", False) == "Replace"


# ---------------------------------------------------------------------------
# Every state the state can produce is one the row component renders
# ---------------------------------------------------------------------------


def test_every_row_state_is_a_known_state():
    """The view `rx.match`es on `state` for a row's outline, tint, lead icon
    and actions. A state nobody wrote a branch for would fall to the default
    and render as a waiting row — silently losing the verdict."""
    produced = {
        rs.ReconcileState._row_state(code, busy, is_optional)
        for code in rs.SLOT_CODES
        for busy in (False, True)
        for is_optional in (False, True)
    }
    assert produced <= set(rs.ROW_STATES)
    assert {"waiting", "optional", "uploading", "ok", "reused", "review",
            "partial", "wrong", "not_ingested", "failed"} <= set(rs.ROW_STATES)
