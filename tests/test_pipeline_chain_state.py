"""The pipeline's PERSISTED chain state — including the restart case.

The window this exists for: the triage run row is finalised, the extraction has
not started, and nothing is running. Before this table that window looked like a
finished upload to every poller, and it is what made the chained-pipeline test
fail nine times in ten.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src import bootstrap
from src import db as recon_db
from src.invoice_extract import demo_seed
from src.invoice_extract import pipeline

REPO_DB = Path(recon_db.REPO_DB_PATH)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    from src import data_paths

    root = tmp_path / "scratch"
    (root / "data").mkdir(parents=True)
    db_path = root / "scratch.db"
    assert demo_seed.main(["--db", str(db_path)]) == 0
    monkeypatch.setattr(recon_db, "DB_PATH", db_path)
    monkeypatch.setattr(data_paths, "DATA_ROOT", root / "data")
    return db_path


def test_a_registered_chain_is_NOT_finished(seeded):
    real_before = sha256(REPO_DB)
    pipeline.register_chain(1, 1, actor="test", db_path=seeded)
    assert pipeline.chain_state(run_id=1, db_path=seeded) == pipeline.CHAIN_QUEUED
    # The state a poller must treat as unfinished even with nothing running.
    pipeline.set_chain_state(1, pipeline.CHAIN_TRIAGE_DONE, upload_id=1, db_path=seeded)
    state = pipeline.chain_state(run_id=1, db_path=seeded)
    assert state == pipeline.CHAIN_TRIAGE_DONE
    assert state in pipeline.CHAIN_RUNNING_STATES
    assert sha256(REPO_DB) == real_before


def test_a_restart_marks_a_running_chain_interrupted_not_complete(seeded):
    real_before = sha256(REPO_DB)
    for state in (pipeline.CHAIN_TRIAGE_RUNNING, pipeline.CHAIN_EXTRACTION_RUNNING):
        pipeline.register_chain(2, 1, actor="test", db_path=seeded)
        pipeline.set_chain_state(2, state, upload_id=1, db_path=seeded)

        recovered = pipeline.recover_interrupted_chains(db_path=seeded)
        assert 2 in recovered
        after = pipeline.chain_state(run_id=2, db_path=seeded)
        assert after == pipeline.CHAIN_INTERRUPTED
        assert after not in pipeline.CHAIN_RUNNING_STATES
        assert after != pipeline.CHAIN_COMPLETE
        assert after != pipeline.CHAIN_FAILED
    assert sha256(REPO_DB) == real_before


def test_resume_continues_only_the_unfinished_piece(seeded):
    real_before = sha256(REPO_DB)
    pipeline.register_chain(3, 1, actor="test", db_path=seeded)
    pipeline.set_chain_state(3, pipeline.CHAIN_EXTRACTION_RUNNING, upload_id=1, db_path=seeded)
    pipeline.recover_interrupted_chains(db_path=seeded)

    result = pipeline.resume_pipeline(1, actor="test", db_path=seeded)
    assert result["resumed"] is True
    # The demo upload's triage is complete, so only the EXTRACTION leg resumes.
    assert result["leg"] == "extraction"
    state = pipeline.chain_state(run_id=3, db_path=seeded)
    assert state in (pipeline.CHAIN_EXTRACTION_RUNNING, pipeline.CHAIN_COMPLETE,
                     pipeline.CHAIN_FAILED)
    assert sha256(REPO_DB) == real_before


def test_resume_refuses_a_chain_that_is_not_interrupted(seeded):
    pipeline.register_chain(4, 1, actor="test", db_path=seeded)
    pipeline.set_chain_state(4, pipeline.CHAIN_COMPLETE, upload_id=1, db_path=seeded)
    result = pipeline.resume_pipeline(1, actor="test", db_path=seeded)
    assert result["resumed"] is False
    assert result["state"] == pipeline.CHAIN_COMPLETE


def test_the_progress_call_reports_the_chain_state(seeded):
    from src.invoice_extract import review_api
    from src.invoice_extract import triage

    # The progress call reads the chain of the upload's OWN run, so the state
    # has to be written against that run id — not an arbitrary one.
    run_id = int(triage.get_triage(1)["run"]["run_id"])
    pipeline.register_chain(run_id, 1, actor="test", db_path=seeded)
    pipeline.set_chain_state(run_id, pipeline.CHAIN_TRIAGE_DONE, upload_id=1, db_path=seeded)
    progress = review_api.get_upload_progress(1)
    assert progress["chain_state"] == pipeline.CHAIN_TRIAGE_DONE


def test_the_waiter_does_not_return_while_the_chain_is_pending(seeded):
    """The exact regression: nothing is unfinished, yet the upload is NOT done."""
    import time

    from src.invoice_extract import extract

    pipeline.register_chain(6, 1, actor="test", db_path=seeded)
    pipeline.set_chain_state(6, pipeline.CHAIN_TRIAGE_DONE, upload_id=1, db_path=seeded)
    started = time.monotonic()
    counts = extract.wait_for_extraction(1, db_path=seeded, timeout=2.0, poll=0.05)
    waited = time.monotonic() - started
    assert waited >= 1.5, "a pending chain must keep the waiter waiting"
    assert counts is not None


def test_a_pre_migration_database_gains_the_table_through_the_framework(tmp_path, monkeypatch):
    """A database migrated to 6 only has no chain table; migration 7 adds it.

    Built the honest way: the REAL boot path runs with the migration list cut to
    the first six, then the full list runs again on the same database — exactly
    what a release branch does to an existing database.
    """
    from src.migrations import migrate as migrate_mod

    all_migrations = list(migrate_mod.MIGRATIONS)
    assert len(all_migrations) == 7
    assert all_migrations[-1].version == 7 and all_migrations[-1].name == "invoice_pipeline_chains"

    def tables(db_path):
        conn = recon_db.get_connection(db_path)
        try:
            return {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()

    db_path = tmp_path / "pre_migration.db"
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", all_migrations[:6])
    bootstrap.run_migrations(db_path=str(db_path), data_root=tmp_path / "data", allow_real_db=True)
    # Make it look like a database this branch has never touched: the boot's
    # recovery safety net may have created the table, so drop it — migration 7
    # is what must bring it back, and the RECORDED version is the proof.
    conn = recon_db.get_connection(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS invoice_pipeline_chains")
        conn.commit()
    finally:
        conn.close()
    assert "invoice_pipeline_chains" not in tables(db_path)

    # The release branch's migration, through the framework, on the same file.
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", all_migrations)
    bootstrap.run_migrations(db_path=str(db_path), data_root=tmp_path / "data", allow_real_db=True)
    assert "invoice_pipeline_chains" in tables(db_path)
    conn = recon_db.get_connection(db_path)
    try:
        versions = {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
    finally:
        conn.close()
    assert 7 in versions, "migration 7 must be RECORDED, not merely re-created by the safety net"

    # And the chain state works on that database immediately.
    pipeline.register_chain(9, 1, actor="test", db_path=db_path)
    assert pipeline.chain_state(run_id=9, db_path=db_path) == pipeline.CHAIN_QUEUED
    # ...and the runtime safety net stays harmless on a migrated database.
    pipeline.ensure_schema(db_path)
    assert pipeline.chain_state(run_id=9, db_path=db_path) == pipeline.CHAIN_QUEUED
