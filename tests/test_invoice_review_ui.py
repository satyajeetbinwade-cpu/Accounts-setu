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
    state = goto(open_upload(new_state()), 1)

    card = state.cards[0]
    state.set_field_input("1234", card.field_key)
    state.confirm_card(card.field_key)
    state.reject_card(card.field_key, "not_on_document")
    state.not_applicable_card(card.field_key, "not_applicable_here")

    settled = state.settled[0]
    state.confirm_settled(settled.field_key, "correct")
    state.set_edit_value("999")
    state.edit_settled(settled.field_key, "ocr_error")

    assert [action for action, _, _ in seen] == [
        "confirmed_as_is", "marked_not_printed", "marked_not_applicable",
        "confirmed_as_is", "edited",
    ]
    assert seen[1][1] == "not_on_document"
    assert seen[2][1] == "not_applicable_here"
    assert seen[4][1] == "ocr_error"
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
    state.confirm_card(state.cards[0].field_key)
    state.reject_card(state.cards[0].field_key, "not_on_document")
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
