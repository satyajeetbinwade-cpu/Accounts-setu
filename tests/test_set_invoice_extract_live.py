"""The live-settings script: dry run, apply, idempotence, refusals, no secrets.

Every test uses its own freshly migrated scratch database — no model is called,
no key is read, and the repository's own database is never touched.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from src import bootstrap
from src.ai_models import service as ai_models
from src.settings import service as settings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "set_invoice_extract_live.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("set_invoice_extract_live", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_script()


@pytest.fixture()
def db(tmp_path):
    """A scratch database with every schema, seed and migration applied."""
    path = str(tmp_path / "live.db")
    bootstrap.run_migrations(db_path=path, data_root=tmp_path / "data")
    return path


def _legs(db_path: str, touchpoint: str) -> dict:
    return ai_models.effective_leg(touchpoint, db_path=db_path) or {}


def _assignment_snapshot(db_path: str) -> dict:
    return {touchpoint: _legs(db_path, touchpoint) for touchpoint, *_ in
            _load_script().ASSIGNMENTS}


# ===========================================================================
# Dry run
# ===========================================================================


def test_a_dry_run_changes_nothing(script, db, capsys):
    before_flags = {key: settings.get_setting_str(key, default=None, db_path=db) or ""
                    for key, _ in script.FLAGS}
    before_assign = _assignment_snapshot(db)

    assert script.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "nothing was written" in out

    for key, _desired in script.FLAGS:
        assert (settings.get_setting_str(key, default=None, db_path=db) or "") == before_flags[key]
    assert _assignment_snapshot(db) == before_assign


# ===========================================================================
# Apply, then apply again
# ===========================================================================


def test_apply_sets_both_flags_and_the_three_assignments(script, db, capsys):
    assert script.main(["--db", db, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "applied: 5 change(s)" in out

    for key, desired in script.FLAGS:
        assert settings.get_setting_str(key, default=None, db_path=db) == desired
    for touchpoint, pp, pm, fp, fm in script.ASSIGNMENTS:
        leg = _legs(db, touchpoint)
        assert leg["primary"] == {"provider": pp, "model": pm}
        assert leg["fallback"] == {"provider": fp, "model": fm}


def test_a_second_apply_is_a_no_op(script, db, capsys):
    assert script.main(["--db", db, "--apply"]) == 0
    capsys.readouterr()
    snapshot = _assignment_snapshot(db)

    assert script.main(["--db", db, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "applied: 0 change(s)" in out
    assert "[ok]" in out
    assert "will change" not in out
    assert _assignment_snapshot(db) == snapshot


# ===========================================================================
# Refusals
# ===========================================================================


def test_a_missing_database_is_refused_and_not_created(script, tmp_path, capsys):
    missing = tmp_path / "does_not_exist.db"
    assert script.main(["--db", str(missing)]) == 2
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert "never creates a database" in out
    assert not missing.exists()


def test_a_catalogue_refusal_is_reported_and_nothing_is_changed(script, db, capsys):
    """The catalogue says the triage model is text-only: the app refuses it, so
    the script reports which model and writes NOTHING — not even the flags."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_model_catalog "
            "(model_id, name, context_length, prompt_price, completion_price, modality) "
            "VALUES (?, ?, 0, 0, 0, 'text->text')",
            ("google/gemini-3-flash-preview", "gemini (marked text-only for this test)"),
        )
        conn.commit()
    finally:
        conn.close()
    before_assign = _assignment_snapshot(db)

    assert script.main(["--db", db, "--apply"]) == 1
    out = capsys.readouterr().out
    assert "refusing" in out
    assert "does not accept image input" in out
    assert "google/gemini-3-flash-preview" in out

    # No partial change: the flags are still unset and the legs untouched.
    for key, _desired in script.FLAGS:
        assert (settings.get_setting_str(key, default=None, db_path=db) or "") == ""
    assert _assignment_snapshot(db) == before_assign


# ===========================================================================
# Secrets
# ===========================================================================


def test_the_key_report_never_contains_a_key_value(script, db, capsys, monkeypatch):
    fake_openrouter = "sk-or-v1-THIS-IS-A-FAKE-KEY-0123456789"
    fake_deepseek = "sk-deepseek-THIS-IS-A-FAKE-KEY-0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_openrouter)
    monkeypatch.setenv("DEEPSEEK_API_KEY", fake_deepseek)

    assert script.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY present in environment: yes" in out
    assert "DEEPSEEK_API_KEY present in environment: yes" in out
    assert fake_openrouter not in out
    assert fake_deepseek not in out
    assert "sk-" not in out


def test_the_key_report_says_no_when_the_environment_has_none(script, db, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert script.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY present in environment: no" in out
    assert "DEEPSEEK_API_KEY present in environment: no" in out
