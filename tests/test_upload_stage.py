"""The Files list's stage, from FACTS — and the poll that keeps it live.

Two things are tested here:

* ``derive_stage`` — one stage per upload, derived from the triage run row and
  the extraction rows. The case that started this: a file whose triage run and
  extraction had both FINISHED still read "Working — 31 of 31 pages read",
  because the label came from the persisted chain state, which was stale. That
  file must read "Ready to review".
* ``InvoiceReviewState.poll`` — the timer that keeps the rows live: it starts
  when something is running, never starts a second loop, stops by itself, and
  survives a failing read by keeping the last known stage.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import data_paths
from src import db as recon_db
from src.auth import access
from src.auth import service as auth_service
from src.invoice_extract import demo_seed
from src.invoice_extract import review_api
from setu.state import invoice_review_state as review_state_mod
from setu.state.invoice_review_state import InvoiceReviewState as S
from setu.state.upload_stage import (
    STAGE_ATTENTION,
    STAGE_EXTRACTING,
    STAGE_FAILED,
    STAGE_QUEUED,
    STAGE_READING,
    STAGE_READY,
    Stage,
    StageFacts,
    derive_stage,
    progress_banner,
)


# ---------------------------------------------------------------------------
# derive_stage — one test per stage
# ---------------------------------------------------------------------------


def test_stage_queued_before_anything_starts():
    stage = derive_stage(StageFacts())
    assert stage.key == STAGE_QUEUED
    assert stage.running is True and stage.needs_attention is False
    assert stage.show_progress is False


def test_stage_reading_pages_counts_them():
    stage = derive_stage(StageFacts(
        started=True, run_status="running", chain_state="triage_running",
        pages_ok=12, pages_total=31,
    ))
    assert stage.key == STAGE_READING
    assert stage.label == "Reading pages (12 of 31)"
    assert stage.running is True
    assert (stage.done, stage.total, stage.percent) == (12, 31, 39)


def test_stage_extracting_counts_documents_done():
    stage = derive_stage(StageFacts(
        started=True, run_status="complete", chain_state="extraction_running",
        pages_ok=31, pages_total=31, documents=11, documents_terminal=4, documents_running=1,
    ))
    assert stage.key == STAGE_EXTRACTING
    assert stage.label == "Extracting (4 of 11 documents done)"
    assert stage.running is True
    assert (stage.done, stage.total, stage.percent) == (4, 11, 36)


def test_stage_ready_when_a_stale_chain_state_still_says_queued():
    """THE case: triage complete, extraction complete, upload row still queued."""
    stage = derive_stage(StageFacts(
        started=True, run_status="complete", chain_state="queued",
        pages_ok=31, pages_total=31, documents=23, documents_terminal=23,
        documents_complete=11,
    ))
    assert stage.key == STAGE_READY
    assert stage.label == "Ready to review"
    assert stage.running is False and stage.needs_attention is False


def test_stage_attention_for_failed_pages_and_for_documents_waiting():
    failed_pages = derive_stage(StageFacts(
        started=True, run_status="partial", pages_ok=30, pages_total=31, pages_failed=1,
        documents=23, documents_terminal=23,
    ))
    assert failed_pages.key == STAGE_ATTENTION
    assert failed_pages.needs_attention is True and failed_pages.running is False

    waiting = derive_stage(StageFacts(
        started=True, run_status="complete", pages_ok=31, pages_total=31,
        documents=11, documents_terminal=3, documents_queued=8,
    ))
    assert waiting.key == STAGE_ATTENTION

    interrupted = derive_stage(StageFacts(
        started=True, run_status="interrupted", chain_state="interrupted",
        pages_ok=7, pages_total=31, documents=0,
    ))
    assert interrupted.key == STAGE_ATTENTION


def test_stage_failed_when_nothing_could_be_read():
    stage = derive_stage(StageFacts(started=True, run_status="failed", pages_failed=31))
    assert stage.key == STAGE_FAILED
    assert stage.needs_attention is True


def test_percent_is_never_a_division_by_zero():
    assert Stage(key=STAGE_READING, label="x", detail="", running=True,
                 needs_attention=False, show_progress=True, done=0, total=0).percent == 0
    assert Stage(key=STAGE_READING, label="x", detail="", running=True,
                 needs_attention=False, show_progress=True, done=9, total=3).percent == 100


def test_progress_banner_speaks_for_every_running_upload_and_vanishes_when_idle():
    reading = Stage(key=STAGE_READING, label="Reading pages (7 of 31)", detail="",
                    running=True, needs_attention=False)
    extracting = Stage(key=STAGE_EXTRACTING, label="Extracting (2 of 9 documents done)",
                       detail="", running=True, needs_attention=False)
    queued = Stage(key=STAGE_QUEUED, label="Queued", detail="", running=True,
                   needs_attention=False)
    ready = Stage(key=STAGE_READY, label="Ready to review", detail="", running=False,
                  needs_attention=False)

    assert progress_banner([queued], ["a.pdf"]).startswith("a.pdf is queued")
    assert "reading pages (7 of 31)" in progress_banner([reading], ["a.pdf"]).lower()
    assert "extracting (2 of 9 documents done)" in progress_banner([extracting], ["a.pdf"]).lower()
    assert progress_banner([reading, extracting], ["a.pdf", "b.pdf"]).startswith(
        "2 file(s) in progress")
    assert progress_banner([ready], ["a.pdf"]) == ""
    assert progress_banner([], []) == ""


# ---------------------------------------------------------------------------
# The poll, against the demo-seeded scratch database
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    (root / "data").mkdir(parents=True)
    db_path = root / "scratch.db"
    assert demo_seed.main(["--db", str(db_path)]) == 0
    monkeypatch.setattr(recon_db, "DB_PATH", db_path)
    monkeypatch.setattr(data_paths, "DATA_ROOT", root / "data")
    review_state_mod._RENDER_CACHE.clear()
    monkeypatch.setattr(
        auth_service, "current_user", lambda *_a, **_kw: dict(access.SYSTEM_VIEWER)
    )
    return db_path


def state_with_the_seeded_file() -> S:
    """The demo's finished upload, as the Files list builds it."""
    state = S(_reflex_internal_init=True)
    state.parent_state = SimpleNamespace(
        username="test:poll", session_token="test-session-token",
        viewer_title="", viewer_kind="", viewer_image_src="",
        viewer_page=1, viewer_page_count=0, dirty_substates=set(), dirty_vars=set(),
        _mark_dirty=lambda: None, get_name=lambda: "shim",
    )
    state.upload_client_id = 1
    state._load_jobs()
    return state


def test_a_finished_upload_shows_the_counts_line_and_no_bar(scratch):
    state = state_with_the_seeded_file()
    assert state.job_rows, "the demo file must be on the list"
    row = state.job_rows[0]
    assert row.stage_key == STAGE_READY
    assert row.show_progress is False
    assert "to review" in row.counts_line and "excluded" in row.counts_line
    assert row.elapsed_label == ""          # only while running
    # Nothing is running, so there is nothing to show and nothing to poll.
    assert state.any_job_running is False
    assert state.live_progress_banner == ""


def test_the_poll_starts_when_an_upload_is_running_and_updates_only_the_rows(scratch, monkeypatch):
    state = state_with_the_seeded_file()
    state.job_rows[0].running = True                  # as a running stage would leave it
    state.job_rows[0].stage_key = STAGE_READING
    big = {"viewer_image_src": "data:image/png;base64,AAAA", "page_info_rows": [{"page": 1}]}
    state.parent_state.viewer_image_src = big["viewer_image_src"]

    state.start_progress_poll()
    assert state.poll_active is True and state.poll_token == 1

    before = list(state.jobs)                         # the full model is NOT the target
    state.poll()

    assert state.poll_active is False                 # the read says nothing is running
    assert state.jobs == before                       # untouched by the tick
    assert state.parent_state.viewer_image_src == big["viewer_image_src"]  # untouched
    assert state.job_rows[0].stage_key == STAGE_READY


def test_a_second_poll_loop_cannot_start_behind_a_re_entry(scratch):
    state = state_with_the_seeded_file()
    state.start_progress_poll()
    first = state.poll_token
    state.start_progress_poll()                       # the page was re-entered
    assert state.poll_token == first + 1              # this page owns ONE loop
    state.stop_progress_poll()                        # leaving the page stops it
    assert state.poll_active is False
    rows_before = list(state.job_rows)
    state.poll()                                      # a stray tick does nothing
    assert state.job_rows == rows_before


def test_a_failing_read_keeps_the_last_known_stage_and_keeps_trying(scratch, monkeypatch):
    state = state_with_the_seeded_file()
    state.start_progress_poll()
    state.poll_active = True
    known = state.job_rows[0].stage_key

    def boom(*_a, **_kw):
        raise RuntimeError("database is busy")

    monkeypatch.setattr(review_api, "get_upload_progress", boom)
    state.poll()                                      # must not raise

    assert state.job_rows[0].stage_key == known       # the last known stage stands
    assert state.poll_active is True                  # and the loop tries again
