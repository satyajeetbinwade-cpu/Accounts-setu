"""The isolation contract: nothing under tests may write db/poc.db.

Session-level: tests/conftest.py hashes the repository's database before the run,
makes it read-only, wraps ``sqlite3.connect`` to refuse a WRITE open, and fails
the run if the hash moved. These tests pin the pieces a session guard cannot:

* the library guard (migrate / bootstrap) refuses the repository's database
  unless the caller says ``allow_real_db=True``;
* the AI-models audit path — the one that wrote 52 rows into the real database
  during a full suite — now records the edit in the database it was TOLD to use;
* the same holds across a fresh interpreter (the child-process path).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src import bootstrap
from src import data_paths
from src import db as recon_db
from src.migrations import migrate

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_DB = Path(recon_db.REPO_DB_PATH)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# The library guard
# ---------------------------------------------------------------------------


def test_the_migration_runner_refuses_the_repository_database():
    with pytest.raises(RuntimeError, match="repository's own"):
        migrate.run(dry_run=True)
    # The explicit opt-in is how the boot path and `main.py --migrate` pass it —
    # it is NOT exercised here: a test must never migrate the real database.
    assert "allow_real_db" in migrate.run.__doc__


def test_boot_migrations_refuse_the_repository_database():
    with pytest.raises(RuntimeError, match="repository's own"):
        bootstrap.run_migrations()


def test_the_demo_seed_refuses_the_repository_database(capsys):
    from src.invoice_extract import demo_seed

    exit_code = demo_seed.main(["--db", str(REPO_DB)])
    assert exit_code != 0
    assert "repository's real database" in capsys.readouterr().err


def test_a_tool_can_always_ask_who_a_path_belongs_to():
    assert recon_db.path_is_repo_db() is True
    assert recon_db.path_is_repo_db(REPO_DB) is True
    assert recon_db.path_is_repo_db("/tmp/not-the-repo.db") is False


# ---------------------------------------------------------------------------
# The writer: the AI-models audit entry must land where it was told to
# ---------------------------------------------------------------------------


def _entries(db_path: Path) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            return int(conn.execute("SELECT COUNT(*) FROM edit_history_entries").fetchone()[0])
        except sqlite3.Error:
            return -1
    finally:
        conn.close()


def test_an_ai_models_audit_entry_goes_to_the_database_it_was_given(tmp_path, monkeypatch):
    """The exact leak from the full suite, pinned.

    ``set_provider_key`` used to log its F4 edit through a connection that fell
    back to the app default — 52 such rows once landed in the real db/poc.db.
    """
    from src.ai_models import service as ai_models

    scratch = tmp_path / "scratch.db"
    bootstrap.run_migrations(db_path=str(scratch), data_root=tmp_path / "data")
    monkeypatch.setattr(data_paths, "DATA_ROOT", tmp_path / "data")
    before_entries = _entries(scratch)
    before_real = sha256(REPO_DB)

    ai_models.set_provider_key("openrouter", "sk-or-v1-testonly1234", actor="test", db_path=scratch)

    assert _entries(scratch) >= before_entries + 1, "the edit must be recorded in the given db"
    assert sha256(REPO_DB) == before_real, "the repository's database must not move"


_CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from src.ai_models import service as ai_models
ai_models.set_provider_key("openrouter", "sk-or-v1-child012345", actor="child",
                            db_path=Path({db!r}))
print("CHILD_OK")
"""


def test_the_child_process_path_leaves_the_real_database_alone(tmp_path, monkeypatch):
    """A fresh interpreter, the same call: the real database must not move."""
    from src.ai_models import service as ai_models  # noqa: F401 - keep the import honest

    scratch = tmp_path / "child.db"
    bootstrap.run_migrations(db_path=str(scratch), data_root=tmp_path / "data")
    before = sha256(REPO_DB)
    env = dict(os.environ)
    env.pop("SETU_DB_PATH", None)
    env.pop("SETU_DATA_ROOT", None)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD.format(repo=str(REPO_ROOT), db=str(scratch))],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "CHILD_OK" in proc.stdout
    assert sha256(REPO_DB) == before
    assert _entries(scratch) >= 1
