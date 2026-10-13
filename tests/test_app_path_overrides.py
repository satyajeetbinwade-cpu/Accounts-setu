"""The scratch-run overrides (SETU_DB_PATH / SETU_DATA_ROOT) and their guard.

A local app run — for a screenshot, a smoke test, anything — must be able to
point the whole app at a COPY of the database and the data tree. These tests
prove the overrides work, that the DEFAULTS are unchanged when they are unset,
and that a mismatched pair is refused instead of writing into the real folders.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import data_paths, db as recon_db

REPO_ROOT = Path(__file__).resolve().parents[1]


def _child(body: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    environ = dict(os.environ)
    environ.pop("SETU_DB_PATH", None)
    environ.pop("SETU_DATA_ROOT", None)
    environ.update(env or {})
    src = f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n" + body
    return subprocess.run(
        [sys.executable, "-c", src], capture_output=True, text=True, env=environ,
    )


# ===========================================================================
# The defaults are unchanged
# ===========================================================================


def test_the_defaults_are_the_repository_paths():
    assert Path(recon_db.DB_PATH) == recon_db.REPO_DB_PATH
    assert Path(data_paths.DATA_ROOT) == data_paths.REPO_DATA_ROOT


def test_the_overrides_point_the_app_at_a_copy(tmp_path, monkeypatch):
    copy_db = tmp_path / "scratch" / "poc.db"
    copy_data = tmp_path / "scratch" / "data"
    copy_data.mkdir(parents=True)
    proc = _child(
        "from src import db\nfrom src import data_paths\n"
        "print(db.DB_PATH)\nprint(data_paths.DATA_ROOT)\n"
        "print(db.REPO_DB_PATH)\nprint(data_paths.REPO_DATA_ROOT)\n",
        env={"SETU_DB_PATH": str(copy_db), "SETU_DATA_ROOT": str(copy_data)},
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.split()
    # The app follows the override...
    assert Path(out[0]) == copy_db
    assert Path(out[1]) == copy_data
    # ...while the repository's own paths stay knowable, so the guard can
    # compare against them.
    assert Path(out[2]) == recon_db.REPO_DB_PATH
    assert Path(out[3]) == data_paths.REPO_DATA_ROOT


# ===========================================================================
# The guard
# ===========================================================================


def test_the_guard_accepts_the_default_layout():
    assert data_paths.assert_app_data_root() == data_paths.REPO_DATA_ROOT.resolve()


def test_the_guard_accepts_a_scratch_pair(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    (scratch / "data").mkdir(parents=True)
    monkeypatch.setattr(recon_db, "DB_PATH", scratch / "poc.db")
    monkeypatch.setattr(data_paths, "DATA_ROOT", scratch / "data")
    assert data_paths.assert_app_data_root() == (scratch / "data").resolve()


def test_the_guard_refuses_the_real_data_root_with_a_copied_database(tmp_path, monkeypatch):
    """The dangerous mismatch: a copy's database writing into the real data/."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(recon_db, "DB_PATH", scratch / "poc.db")
    monkeypatch.setattr(data_paths, "DATA_ROOT", data_paths.REPO_DATA_ROOT)
    with pytest.raises(RuntimeError, match="repository's real"):
        data_paths.assert_app_data_root()


def test_the_guard_refuses_an_unrelated_data_root(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    (scratch / "data").mkdir(parents=True)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.setattr(recon_db, "DB_PATH", scratch / "poc.db")
    monkeypatch.setattr(data_paths, "DATA_ROOT", elsewhere)
    with pytest.raises(RuntimeError, match="does not belong to the database"):
        data_paths.assert_app_data_root()


def test_the_app_import_path_calls_the_guard():
    source = (REPO_ROOT / "setu" / "setu.py").read_text()
    assert "assert_app_data_root()" in source
    # And it still never migrates.
    assert "run_migrations" not in source


def test_a_mismatched_scratch_run_refuses_to_import_the_app(tmp_path):
    """The whole point: `reflex run` with a copied DB but the real data/ must
    fail loudly instead of serving."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proc = _child(
        "import setu.setu\nprint('APP_OK')\n",
        env={"SETU_DB_PATH": str(scratch / "poc.db")},
    )
    assert proc.returncode != 0
    assert "repository's real" in proc.stderr
    assert "APP_OK" not in proc.stdout
