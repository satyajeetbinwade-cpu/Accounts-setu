"""Phase 5a STEP 6 — the review screen's own tests.

Every test drives ``InvoiceReviewState`` directly against a DEMO-SEEDED scratch
database (never the repository's), with the parent state shimmed: the shared
upload/viewer fields live on a base state in a real session, and a bare class
instance has no tree to attach them to.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import bootstrap
from src import data_paths
from src import db as recon_db
from src.invoice_extract import demo_seed
from src.invoice_extract import doc_actions
from src.invoice_extract import extract as extract_mod
from src.invoice_extract import pipeline as pipeline_mod
from src.invoice_extract import service as ie
from setu.state import invoice_review_state as review_state_mod
from setu.state.invoice_review_state import InvoiceReviewState as S


# ---------------------------------------------------------------------------
# A demo-seeded SCRATCH database
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    """A scratch data root + a demo-seeded database, wired into the services."""
    root = tmp_path / "scratch"
    (root / "data").mkdir(parents=True)
    db_path = root / "scratch.db"
    assert demo_seed.main(["--db", str(db_path)]) == 0
    monkeypatch.setattr(recon_db, "DB_PATH", db_path)
    monkeypatch.setattr(data_paths, "DATA_ROOT", root / "data")
    # The render cache is process-wide: a test must not read another test's page.
    review_state_mod._RENDER_CACHE.clear()
    return db_path


def new_state() -> S:
    """A state with the shared base fields on a shim.

    In a session those fields belong to the parent state; a bare instance has no
    tree, so the shim stands in for it (and for ``username``).
    """
    state = S(_reflex_internal_init=True)
    state.parent_state = SimpleNamespace(
        username="test:review", viewer_title="", viewer_kind="", viewer_image_src="",
        viewer_page=1, viewer_page_count=0, dirty_substates=set(), dirty_vars=set(),
        _mark_dirty=lambda: None, get_name=lambda: "shim",
    )
    return state


def open_upload(state: S, upload_id: int = 1) -> S:
    state.open_upload_id = upload_id
    state._load_upload()
    state._load_review(first=True)
    return state


def doc_of(state: S, ordinal: int):
    return [d for d in state.documents if d.ordinal == ordinal][0]


def goto(state: S, ordinal: int) -> S:
    state._select(doc_of(state, ordinal).document_id)
    return state


def capture_corrections(monkeypatch) -> list[tuple[str, str | None, int]]:
    """Record every decision the screen takes, in order."""
    seen: list[tuple[str, str | None, int]] = []

    def fake(field_id, action, *, actor, value_after=None, reason_code=None, db_path=None):
        seen.append((action, reason_code, int(field_id)))
        return {"field_id": field_id, "action": action}

    monkeypatch.setattr(review_state_mod.confidence_mod, "record_correction", fake)
    return seen


# ===========================================================================
# 1. Needs-you ordering
# ===========================================================================


def test_needs_you_cards_follow_the_models_own_order(scratch):
    state = goto(open_upload(new_state()), 1)
    kinds = [c.kind for c in state.cards]
    # A real gap outranks a failed check, which outranks a low score.
    assert kinds == ["missing_required", "check_failed"]
    assert state.cards[0].field_key == "line.1.hsn_sac"
    assert state.cards[1].title and state.cards[1].check_key


def test_a_failed_checks_fields_are_not_cards_of_their_own(scratch):
    state = goto(open_upload(new_state()), 6)
    implicated = {k for c in state.cards for k in c.implicated}
    own = {c.field_key for c in state.cards if c.kind != "check_failed"}
    assert "line.1.amount" in implicated
    assert not (implicated & own), "a field named by a cause must not ask again"


def test_direction_never_renders_as_a_value_card(scratch):
    state = goto(open_upload(new_state()), 1)
    assert all(c.kind != "direction_unsettled" for c in state.cards)
    assert state.doc_needs_a_human is True


# ===========================================================================
# 2. Next issue across documents
# ===========================================================================


def test_next_issue_moves_to_the_next_document_that_asks(scratch):
    state = open_upload(new_state())
    state.current_index = 0  # start at document #1, which asks
    state.next_issue()
    assert state.current_index > 0
    assert state.cards, "Next issue must land on a document that asks something"
    target = state.documents[state.current_index]
    assert target.needs_you or not target.reviewed


def test_next_issue_wraps_and_keeps_going(scratch):
    state = open_upload(new_state())
    state.current_index = len(state.documents) - 1
    state.next_issue()
    assert state.current_index != len(state.documents) - 1


# ===========================================================================
# 3. Every decision type logs its own action code
# ===========================================================================


def test_each_decision_records_its_action_code(scratch, monkeypatch):
    seen = capture_corrections(monkeypatch)
    state = open_upload(new_state())

    # Confirm and Not printed write through the service layer's own
    # single-transaction writer (field row + log row together) — read them
    # back from the log table itself.
    goto(state, 6)
    first = state.cards[0]
    state.set_field_input("500", first.input_key)
    state.confirm_card(first.input_key)
    second = next(c for c in state.cards if c.field_key and c.field_key != first.field_key)
    state.reject_card(second.input_key, "not_on_document")

    doc_id = state.current_document_id
    conn = sqlite3.connect(scratch)
    rows = conn.execute(
        "SELECT action, reason_code, value_after FROM invoice_field_corrections "
        "WHERE document_id = ? ORDER BY correction_id",
        (doc_id,),
    ).fetchall()
    conn.close()
    assert [(row[0], row[1]) for row in rows] == [
        ("confirmed_as_is", "correct"), ("marked_not_printed", "not_on_document"),
    ]
    assert rows[0][2] == "500"          # the TYPED value is what got stored

    # Not applicable and the settle-edit still log through record_correction.
    goto(state, 2)
    state.not_applicable_card(state.cards[0].field_key, "not_applicable_here")
    goto(state, 3)
    settled = state.settled[0]
    state.confirm_settled(settled.field_key, "correct")
    state.set_edit_value("999")
    state.edit_settled(settled.field_key, "ocr_error")

    assert [action for action, _, _ in seen] == [
        "marked_not_applicable", "confirmed_as_is", "edited",
    ]
    assert seen[0][1] == "not_applicable_here"
    assert seen[2][1] == "ocr_error"
    assert all(field_id for _, _, field_id in seen)


def test_an_edit_without_a_value_is_refused(scratch, monkeypatch):
    seen = capture_corrections(monkeypatch)
    state = goto(open_upload(new_state()), 1)
    state.set_edit_value("")
    state.edit_settled(state.settled[0].field_key, "other")
    assert seen == []
    assert "must record the value" in state.refusal


# ===========================================================================
# 4. Read-only gating
# ===========================================================================


def test_an_exported_upload_takes_no_decisions(scratch, monkeypatch):
    seen = capture_corrections(monkeypatch)
    conn = sqlite3.connect(scratch)
    conn.execute("UPDATE invoice_uploads SET status='exported' WHERE upload_id=1")
    conn.commit()
    conn.close()

    state = goto(open_upload(new_state()), 1)
    assert state.open_read_only is True
    assert state.open_read_only_note
    state.confirm_card(state.cards[0].input_key)
    state.reject_card(state.cards[0].input_key, "not_on_document")
    state.recategorise("supporting")
    state.mark_reviewed()
    assert seen == []
    assert "exported" in state.refusal


def test_read_only_does_not_leak_into_another_upload(scratch):
    state = open_upload(new_state())
    assert state.open_read_only is False


# ===========================================================================
# 5. The counters
# ===========================================================================


def test_to_review_excludes_excluded_documents(scratch):
    state = open_upload(new_state())
    # The demo has 8 documents, one of them not a voucher (excluded).
    assert len(state.documents) == 8
    assert sum(1 for d in state.documents if d.excluded) == 1
    assert state.to_review_count == 6
    assert state.needs_you_count == sum(
        1 for d in state.documents if d.needs_you and not d.excluded
    )
    assert state.needs_you_count <= state.to_review_count


def test_marking_one_reviewed_moves_both_numbers(scratch):
    state = open_upload(new_state())
    before = state.to_review_count
    clean = doc_of(state, 3)
    state._select(clean.document_id)
    state.mark_reviewed()
    state._load_review()
    assert not state.cards
    assert state.to_review_count == before - 1
    assert state.reviewed_count == 1
    assert state.flash


# ===========================================================================
# 6. Mark reviewed gating
# ===========================================================================


def test_mark_reviewed_is_gated_by_the_cards(scratch):
    state = goto(open_upload(new_state()), 1)
    assert state.cards
    assert state.can_mark_reviewed is False
    state._select(doc_of(state, 3).document_id)
    assert not state.cards
    assert state.can_mark_reviewed is True


# ===========================================================================
# 7. The flag: ON renders the flow, OFF leaves the legacy page alone
# ===========================================================================


def test_the_page_branches_on_the_flag_exactly_once():
    source = Path(review_state_mod.__file__).parent.parent / "views" / "invoice_extract_page.py"
    text = source.read_text()
    assert text.count("InvoiceReviewState.v2_enabled") == 1, (
        "the v2 flag must be read in ONE place; a second branch is how the two "
        "screens drift apart"
    )
    assert "_legacy_invoice_page" in text


def test_the_legacy_page_still_builds(scratch):
    """The legacy body is a component factory that must keep working untouched."""
    from setu.views.invoice_extract_page import _legacy_invoice_page

    rendered = str(_legacy_invoice_page())
    # The legacy screen's own furniture, still there — and none of the v2 flow's.
    assert "Review Queue" in rendered
    assert "Client #" not in rendered[:200]
    assert "What needs you" not in rendered


# ===========================================================================
# 8. Recategorise and exclude: logged, and reversible
# ===========================================================================


def test_recategorise_is_logged(scratch):
    state = goto(open_upload(new_state()), 3)
    state.recategorise("supporting")
    assert state.doc_category == "supporting"
    rows = doc_actions.list_document_log(state.current_document_id)
    assert any(r["action"] == "recategorised" or "category" in str(r["action"]) for r in rows), rows


def test_excluding_a_document_is_logged_and_reversible(scratch):
    state = open_upload(new_state())
    target = doc_of(state, 3)
    before = state.to_review_count
    state._select(target.document_id)
    state.exclude_document("test: wrong page")
    assert state.to_review_count == before - 1
    actions = [r["action"] for r in doc_actions.list_document_log(target.document_id)]
    assert any("exclud" in a for a in actions), actions

    state._select(target.document_id)
    state.include_document()
    assert state.to_review_count == before
    actions = [r["action"] for r in doc_actions.list_document_log(target.document_id)]
    assert any("include" in a or "restore" in a for a in actions), actions


# ===========================================================================
# 9. The flag ON against an EMPTY database
# ===========================================================================


def test_the_flow_renders_with_an_empty_database(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    (root / "data").mkdir(parents=True)
    db_path = root / "empty.db"
    bootstrap.run_migrations(db_path=str(db_path), data_root=root / "data")
    monkeypatch.setattr(recon_db, "DB_PATH", db_path)
    monkeypatch.setattr(data_paths, "DATA_ROOT", root / "data")

    # The page itself: the whole flow builds with nothing to show.
    from setu.views.invoice_review_page import invoice_review_flow

    assert "Upload & extract" in str(invoice_review_flow())

    state = new_state()
    state.upload_client_id = 0
    state._load_jobs()
    assert state.jobs == []
    assert state.poll_active is False
    assert open_upload(new_state(), 1).documents == []


def test_the_flag_off_leaves_the_services_refusing(tmp_path, monkeypatch):
    """Belt and braces: with the flag OFF the v2 services refuse, so the legacy
    path cannot be reached by accident through a v2 call."""
    root = tmp_path / "off"
    (root / "data").mkdir(parents=True)
    db_path = root / "off.db"
    bootstrap.run_migrations(db_path=str(db_path), data_root=root / "data")
    monkeypatch.setattr(recon_db, "DB_PATH", db_path)
    monkeypatch.setattr(data_paths, "DATA_ROOT", root / "data")
    from src.invoice_extract.service import InvoiceExtractError

    with pytest.raises(InvoiceExtractError):
        extract_mod.require_enabled()
    with pytest.raises(InvoiceExtractError):
        pipeline_mod.start_v2_pipeline(1, actor="test")


# ===========================================================================
# Upload → pipeline, so a file can start the flow at all
# ===========================================================================


def test_a_v2_upload_is_stored_and_queued(scratch):
    """Re-upload the demo file's own bytes: the pipeline reads a real PDF."""
    conn = sqlite3.connect(scratch)
    filename, blob = conn.execute(
        "SELECT filename, file_bytes FROM invoice_uploads WHERE upload_id = 1"
    ).fetchone()
    conn.close()
    result = ie.upload_invoice_v2(
        client_id=1, filename="probe_" + filename, file_bytes=blob,
        batch_id=None, actor="test",
    )
    assert result["status"] == "queued"
    assert result["queued"] is True
    assert result["run_id"]
    stored = ie.get_upload(result["upload_id"])
    assert stored and stored["extraction_path"] == "v2"


# ===========================================================================
# The 2026-27 fix round: a decision STORES, and the screen CHANGES
# ===========================================================================


def test_confirm_stores_the_typed_value_and_settles(scratch):
    """Confirm goes through ONE service call: value on the field row (typed
    text, not just the guess), human-reviewed, labelled "You confirmed" —
    and a later review-model run (what a re-run hands the screen) still
    shows it settled, never back in needs-you."""
    from src.invoice_extract import rules as rules_mod

    state = goto(open_upload(new_state()), 2)
    card = state.cards[0]
    assert card.field_key == "invoice_total"
    state.set_field_input("1,180.00", card.input_key)
    state.confirm_card(card.input_key)

    row = next((s for s in state.settled if s.field_key == "invoice_total"), None)
    assert row is not None and row.value == "1,180.00"
    assert row.tag == "You confirmed"
    assert not any(c.field_key == "invoice_total" for c in state.cards)
    assert card.input_key not in state.field_inputs, "the box clears after confirm"

    model = rules_mod.get_review_model(state.current_document_id)
    hit = [s for s in model["settled"] if s["field_key"] == "invoice_total"]
    assert hit and hit[0].get("human_label") == "You confirmed"
    assert hit[0]["score"] is None, "a person's decision carries no score"
    assert not any(i.get("field_key") == "invoice_total" for i in model["needs_you"])


def test_not_printed_is_an_answer_with_no_value(scratch):
    state = goto(open_upload(new_state()), 2)
    card = state.cards[0]
    state.reject_card(card.input_key, "not_on_document")

    row = next((s for s in state.settled if s.field_key == "invoice_total"), None)
    assert row is not None and row.value == ""
    assert row.tag == "Not printed (you confirmed)"
    assert not state.cards
    assert state.can_mark_reviewed, "no question stands any more"


def test_confirm_without_text_writes_nothing(scratch):
    state = goto(open_upload(new_state()), 2)
    card = state.cards[0]
    state.set_field_input("", card.input_key)
    state.confirm_card(card.input_key)

    assert state.cards and state.cards[0].field_key == "invoice_total"
    assert state.input_warnings[card.input_key] == (
        "Type the value, or use Not printed on the document."
    )
    conn = sqlite3.connect(scratch)
    count = conn.execute(
        "SELECT COUNT(*) FROM invoice_field_corrections WHERE document_id = ?",
        (state.current_document_id,),
    ).fetchone()[0]
    conn.close()
    assert count == 0, "an empty confirm must not write anything"


def test_accepting_a_failed_check_drops_its_card(scratch):
    import json as json_mod

    state = goto(open_upload(new_state()), 1)
    check_card = next(c for c in state.cards if c.kind == "check_failed")
    state.accept_check(check_card.check_key)

    assert not any(c.kind == "check_failed" for c in state.cards)
    line = next(chk for chk in state.checks if chk.check_key == check_card.check_key)
    assert line.status == "fail", "the original status stays as computed"
    assert line.accepted

    conn = sqlite3.connect(scratch)
    raw = conn.execute(
        "SELECT c.inputs_json FROM invoice_doc_checks c "
        "JOIN invoice_doc_extractions e ON e.extraction_id = c.extraction_id "
        "WHERE e.document_id = ? AND c.check_key = ?",
        (state.current_document_id, check_card.check_key),
    ).fetchone()
    conn.close()
    stored = json_mod.loads(raw[0])
    assert stored["accepted_by"] == "test:review" and stored["accepted_at"]


def test_typed_text_does_not_leak_between_documents(scratch):
    state = goto(open_upload(new_state()), 2)
    card = state.cards[0]
    state.set_field_input("typed on doc 2", card.input_key)
    assert state.field_inputs[card.input_key] == "typed on doc 2"

    goto(state, 3)
    assert state.field_inputs == {}, "a new document starts empty"
    goto(state, 2)
    assert card.input_key not in state.field_inputs


def test_a_skipped_document_shows_plain_words_and_asks_nothing(scratch):
    state = goto(open_upload(new_state()), 7)
    assert state.doc_category == "supporting"
    assert state.plain_note.startswith("This is a supporting document")
    assert state.plain_note.endswith("no values to check here.")
    assert not state.cards and not state.checks and not state.settled
    assert state.doc_needs_a_human is False, "no direction card for a kept page"
    assert state.can_mark_reviewed


def test_not_applicable_lands_in_its_own_card_with_an_override(scratch):
    state = goto(open_upload(new_state()), 2)
    state.not_applicable_card(state.cards[0].field_key, "not_applicable_here")
    assert not state.cards
    assert any(row.field_key == "invoice_total" for row in state.not_applicable)


def test_a_cause_card_resolves_to_a_real_field_row(scratch, monkeypatch):
    """``rules.field_boxes()`` must carry the field ROW, not just its rectangle.

    A failed check's card has no field row of its own — the decision it takes
    is logged against the first field it names, looked up in ``field_boxes``.
    Before that SELECT carried ``field_id`` the lookup returned None, the
    card's ``field_id`` was 0, and every decision on the card was refused with
    "This item has no field row to record a decision against."
    """
    from src.invoice_extract import rules as rules_mod

    seen = capture_corrections(monkeypatch)
    # Document #6 (handwriting) is the demo whose failed checks NAME fields —
    # those are the cards the lookup has to resolve.
    state = goto(open_upload(new_state()), 6)

    boxes = rules_mod.field_boxes(state.current_document_id)
    assert boxes, "the document's fields must have boxes"
    assert all(box.get("field_id") for box in boxes.values()), (
        "every box must name its field row"
    )

    cause = next(c for c in state.cards if c.kind == "check_failed" and c.implicated)
    assert cause.implicated, "the check names the fields it is about"
    assert cause.field_id > 0, "the cause card must resolve to a real field row"

    # The decision the card's buttons take — it used to be refused.
    assert state._log(
        cause.field_id, "marked_not_applicable", reason_code="not_applicable_here",
    ) is True
    assert [action for action, _, _ in seen] == ["marked_not_applicable"]
    assert all(field_id for _, _, field_id in seen)
    assert "no field row" not in state.refusal


# ===========================================================================
# The 2026-10-14 widget round: date picker, file-wide direction, focus outline
# ===========================================================================


def test_a_direction_answer_covers_the_file(scratch):
    """Answering one document applies the same direction to the rest of the
    upload — except where the rule or the person already decided.

    Demo: #1 asks (likely_purchase), #5 is undetermined and eligible, and
    #2/#4/#6 already carry a RULE decision (the client's GSTIN matched), which
    must never be overridden or cascaded.
    """
    from src.invoice_extract import rules as rules_mod

    state = goto(open_upload(new_state()), 1)
    state.settle_direction("sales")

    assert "1 other document" in state.flash
    inherited = rules_mod.get_review_model(5)["direction"]
    assert inherited["direction"] == "sales"
    assert inherited["inherited"] is True, "the file's answer is marked inherited"
    assert rules_mod.get_review_model(1)["direction"]["inherited"] is False

    for decided in (2, 4, 6):
        row = rules_mod.get_review_model(decided)["direction"]
        assert row["direction"] == "purchase", "a rule-decided direction stands"
        assert row["inherited"] is False


def test_changing_an_inherited_direction_moves_only_that_document(scratch):
    """An inherited document can be changed on its own: it becomes that
    document's own answer, and nothing else in the file moves."""
    from src.invoice_extract import rules as rules_mod

    state = goto(open_upload(new_state()), 1)
    state.settle_direction("sales")

    goto(state, 5)
    assert state.doc_direction_inherited is True
    assert state.doc_direction_label.endswith("same as the rest of this file")

    state.settle_direction("purchase")
    assert "other document" not in state.flash, "a correction does not re-cascade"
    assert rules_mod.get_review_model(5)["direction"]["direction"] == "purchase"
    assert rules_mod.get_review_model(5)["direction"]["inherited"] is False
    assert rules_mod.get_review_model(1)["direction"]["direction"] == "sales"


def test_a_date_field_asks_with_a_picker_that_stores_the_date_form():
    """The picker holds ISO and hands the SAME string to the confirm path,
    which is the pipeline's own date form (``normalisers.normalise_date`` reads
    it back); the reviewer reads it as dd-mm-yyyy."""
    from setu.state import invoice_review_state as review_state_mod

    state = new_state()
    key = "9:invoice_date"
    state.set_date_input("2026-04-17", key)

    assert state.field_inputs[key] == "2026-04-17", "what Confirm stores"
    assert state.date_inputs[key] == "2026-04-17", "what the picker shows"
    assert state.date_labels[key] == "17-04-2026", "dd-mm-yyyy for the reviewer"
    assert review_state_mod.nz_mod.normalise_date(state.field_inputs[key]) == "2026-04-17"

    state._clear_input(key)
    assert state.field_inputs == {} and state.date_inputs == {} and state.date_labels == {}


def test_a_date_guess_is_parsed_and_a_non_date_is_left_out():
    """A read's guess is parsed into the picker — including the weekday form
    and the American month-first shape the pipeline's normaliser cannot read —
    and a value that is not a date leaves the picker empty."""
    from setu.state import invoice_review_state as review_state_mod

    parse = review_state_mod._iso_from_guess
    assert parse("Fri Apr 17 2026") == "2026-04-17"
    assert parse("27-Apr-26") == "2026-04-27"
    assert parse("03-04-2026") == "2026-04-03", "day-first, never month-first"
    assert parse("2026-04-13") == "2026-04-13"
    assert parse("CASH MEMO Brought of") == ""
    assert parse("") == ""

    # Only fields the pipeline declares as dates get a picker. A reference
    # no. + date is TEXT "as printed" — a picker would drop the number.
    assert review_state_mod._is_date_field("invoice_date") is True
    assert review_state_mod._is_date_field("reference_no_date") is False
    assert review_state_mod._is_date_field("invoice_total") is False


def test_the_focused_box_is_a_thin_outline_not_a_fill():
    """The marker for "the card you are on" is an outline with a transparent
    fill: the value inside the box has to stay readable, and the label rides in
    a tag OUTSIDE the box (see ``components._region_box``)."""
    from setu.state import invoice_review_state as review_state_mod

    focused = review_state_mod._box_style("focused")
    assert focused["background"] == "transparent"
    assert focused["border"] == f"2px solid {review_state_mod.t.Color.ACCENT.value}"
    assert "box_shadow" not in focused, "no glow over the field"
    assert focused["scroll_margin_top"], "room for the tag when scrolled to"

    # The change is scoped to the focus marker; the other boxes keep their fill.
    assert review_state_mod._box_style("active")["background"] != "transparent"


# ===========================================================================
# The 2026-10-14 widget round: date picker, file-wide direction, focus outline
# ===========================================================================


def test_a_direction_answer_covers_the_file(scratch):
    """Answering one document applies the same direction to the rest of the
    upload — except where the rule or the person already decided.

    Demo: #1 asks (likely_purchase), #5 is undetermined and eligible, and
    #2/#4/#6 already carry a RULE decision (the client's GSTIN matched), which
    must never be overridden or cascaded.
    """
    from src.invoice_extract import rules as rules_mod

    state = goto(open_upload(new_state()), 1)
    state.settle_direction("sales")

    assert "1 other document" in state.flash
    inherited = rules_mod.get_review_model(5)["direction"]
    assert inherited["direction"] == "sales"
    assert inherited["inherited"] is True, "the file's answer is marked inherited"
    assert rules_mod.get_review_model(1)["direction"]["inherited"] is False

    for decided in (2, 4, 6):
        row = rules_mod.get_review_model(decided)["direction"]
        assert row["direction"] == "purchase", "a rule-decided direction stands"
        assert row["inherited"] is False


def test_changing_an_inherited_direction_moves_only_that_document(scratch):
    """An inherited document can be changed on its own: it becomes that
    document's own answer, and nothing else in the file moves."""
    from src.invoice_extract import rules as rules_mod

    state = goto(open_upload(new_state()), 1)
    state.settle_direction("sales")

    goto(state, 5)
    assert state.doc_direction_inherited is True
    assert state.doc_direction_label.endswith("same as the rest of this file")

    state.settle_direction("purchase")
    assert "other document" not in state.flash, "a correction does not re-cascade"
    assert rules_mod.get_review_model(5)["direction"]["direction"] == "purchase"
    assert rules_mod.get_review_model(5)["direction"]["inherited"] is False
    assert rules_mod.get_review_model(1)["direction"]["direction"] == "sales"


def test_a_date_field_asks_with_a_picker_that_stores_the_date_form():
    """The picker holds ISO and hands the SAME string to the confirm path,
    which is the pipeline's own date form (``normalisers.normalise_date`` reads
    it back); the reviewer reads it as dd-mm-yyyy."""
    from setu.state import invoice_review_state as review_state_mod

    state = new_state()
    key = "9:invoice_date"
    state.set_date_input("2026-04-17", key)

    assert state.field_inputs[key] == "2026-04-17", "what Confirm stores"
    assert state.date_inputs[key] == "2026-04-17", "what the picker shows"
    assert state.date_labels[key] == "17-04-2026", "dd-mm-yyyy for the reviewer"
    assert review_state_mod.nz_mod.normalise_date(state.field_inputs[key]) == "2026-04-17"

    state._clear_input(key)
    assert state.field_inputs == {} and state.date_inputs == {} and state.date_labels == {}


def test_a_date_guess_is_parsed_and_a_non_date_is_left_out():
    """A read's guess is parsed into the picker — including the weekday form
    and the American month-first shape the pipeline's normaliser cannot read —
    and a value that is not a date leaves the picker empty."""
    from setu.state import invoice_review_state as review_state_mod

    parse = review_state_mod._iso_from_guess
    assert parse("Fri Apr 17 2026") == "2026-04-17"
    assert parse("27-Apr-26") == "2026-04-27"
    assert parse("03-04-2026") == "2026-04-03", "day-first, never month-first"
    assert parse("2026-04-13") == "2026-04-13"
    assert parse("CASH MEMO Brought of") == ""
    assert parse("") == ""

    # Only fields the pipeline declares as dates get a picker. A reference
    # no. + date is TEXT "as printed" — a picker would drop the number.
    assert review_state_mod._is_date_field("invoice_date") is True
    assert review_state_mod._is_date_field("reference_no_date") is False
    assert review_state_mod._is_date_field("invoice_total") is False


def test_the_focused_box_is_a_thin_outline_not_a_fill():
    """The marker for "the card you are on" is an outline with a transparent
    fill: the value inside the box has to stay readable, and the label rides in
    a tag OUTSIDE the box (see ``components._region_box``)."""
    from setu.state import invoice_review_state as review_state_mod

    focused = review_state_mod._box_style("focused")
    assert focused["background"] == "transparent"
    assert focused["border"] == f"2px solid {review_state_mod.t.Color.ACCENT.value}"
    assert "box_shadow" not in focused, "no glow over the field"
    assert focused["scroll_margin_top"], "room for the tag when scrolled to"

    # The change is scoped to the focus marker; the other boxes keep their fill.
    assert review_state_mod._box_style("active")["background"] != "transparent"
