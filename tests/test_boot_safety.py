"""Boot-path safety tests. No network, and never the real database.

Every test works on a COPY of a database plus its own ``data/`` folder, so the
suite can prove that `--help`, `--check` and a refused bare start change nothing
— byte for byte.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import main as main_mod
from src import bootstrap
from src.migrations import migrate

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> str:
    """A digest of every file under ``root``, so a move/rename is visible."""
    parts: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        parts.append(f"{path.relative_to(root)}:{_digest(path)}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


@pytest.fixture()
def copy_db(tmp_path):
    """A scratch app DB + its own data/ folder, both OUTSIDE the repository."""
    root = tmp_path / "app"
    (root / "db").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    db = root / "db" / "poc.db"
    bootstrap.run_migrations(db_path=db, data_root=root / "data")
    # A stray name-keyed folder, so a migration would have something to rename.
    (root / "data" / "Some Client" / "2026-08" / "gstr2b").mkdir(parents=True)
    (root / "data" / "Some Client" / "2026-08" / "gstr2b" / "f.xlsx").write_text("x")
    return root, db


@pytest.fixture()
def legacy_db(tmp_path):
    """A database in its PRE-migration shape: the legacy schema, no migrations.

    ``src.db.init_db`` is what the boot path calls before the migration, so this
    is exactly the state a real "needs migrating" database is in.
    """
    from src import db as recon_db

    root = tmp_path / "legacy"
    (root / "db").mkdir(parents=True)
    (root / "data" / "Some Client" / "2026-08" / "gstr2b").mkdir(parents=True)
    (root / "data" / "Some Client" / "2026-08" / "gstr2b" / "f.xlsx").write_text("x")
    db = root / "db" / "poc.db"
    recon_db.init_db(db)
    conn = sqlite3.connect(db)
    try:
        assert not migrate.already_migrated(conn)
    finally:
        conn.close()
    return root, db


_CHILD_MAIN = """
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
# Point the app at the COPY before main.py runs — the same trick
# tests/test_client_id_migration.py uses, so a child can never touch the real
# db/ or data/ tree.
from src import db as recon_db
from src import data_paths
recon_db.DB_PATH = Path({db!r})
data_paths.use_data_root(Path({data!r}))
import runpy
sys.argv = [{main!r}] + {args!r}
runpy.run_path({main!r}, run_name="__main__")
"""


def _run_main(args: list[str], *, db: Path, data: Path, allow_boot: bool = False):
    """Run main.py in a SUBPROCESS against the COPY, with a private environment."""
    environ = dict(os.environ)
    environ.pop(main_mod.ALLOW_ENV, None)
    if allow_boot:
        environ[main_mod.ALLOW_ENV] = "1"
    script = _CHILD_MAIN.format(
        repo=str(REPO_ROOT), db=str(db), data=str(data),
        main=str(REPO_ROOT / "main.py"), args=list(args),
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=environ, cwd=str(REPO_ROOT),
    )


# ===========================================================================
# 1. --help changes nothing
# ===========================================================================


def test_help_prints_usage_and_exits_without_touching_anything(copy_db, monkeypatch, capsys):
    root, db = copy_db
    before_db, before_data = _digest(db), _tree_digest(root / "data")

    # In-process: main() must exit through argparse, having imported nothing.
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: python main.py" in out
    assert "--migrate" in out and "--check" in out

    assert _digest(db) == before_db
    assert _tree_digest(root / "data") == before_data


def test_help_against_the_real_repo_tree_does_not_migrate(copy_db):
    """The incident this whole change is for: `--help` used to run the boot."""
    root, db = copy_db
    before_db, before_data = _digest(db), _tree_digest(root / "data")

    result = _run_main(["--help"], db=db, data=root / "data")
    assert result.returncode == 0
    assert "usage: python main.py" in result.stdout

    assert _digest(db) == before_db
    assert _tree_digest(root / "data") == before_data
    # And the stray folder is still name-keyed: no migration ran.
    assert (root / "data" / "Some Client").is_dir()


# ===========================================================================
# 2. --check is read-only
# ===========================================================================


def test_check_reports_status_and_changes_nothing(copy_db, capsys):
    root, db = copy_db
    before_db, before_data = _digest(db), _tree_digest(root / "data")

    status = bootstrap.migration_status(db)
    assert status["pending"] == []
    assert status["current"] is True
    assert status["applied"] == sorted(status["required"])
    assert status["data_root"] == str((root / "data").resolve())

    assert _digest(db) == before_db
    assert _tree_digest(root / "data") == before_data


def test_check_on_a_pre_migration_copy_lists_the_pending_versions_and_exits_1(tmp_path, capsys):
    """A pre-migration database: --check must REPORT, never fix."""
    root = tmp_path / "old"
    root.mkdir()
    db = root / "poc.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    before = _digest(db)

    status = bootstrap.migration_status(db)
    assert status["applied"] == []
    assert status["pending"] == status["required"]
    assert status["current"] is False
    assert _digest(db) == before        # still no schema_migrations table


# ===========================================================================
# 3. A bare start refuses, and only the opt-in lets it migrate
# ===========================================================================


def test_a_bare_start_refuses_without_the_env_var(copy_db):
    root, db = copy_db
    before_db, before_data = _digest(db), _tree_digest(root / "data")

    result = _run_main([], db=db, data=root / "data")
    assert result.returncode == main_mod.EXIT_REFUSED
    assert "Refusing to start" in result.stderr
    assert "--migrate" in result.stderr and "--check" in result.stderr
    assert main_mod.ALLOW_ENV in result.stderr

    # Nothing changed: no migration, no folder rename.
    assert _digest(db) == before_db
    assert _tree_digest(root / "data") == before_data
    assert (root / "data" / "Some Client").is_dir()


def test_a_container_start_with_the_env_var_still_migrates(legacy_db):
    """The Dockerfile CMD sets SETU_ALLOW_BOOT_MIGRATE=1 and runs a BARE
    `python main.py`. That path must still migrate — otherwise Fly could never
    boot — and it must be the ONLY bare path that does.
    """
    root, db = legacy_db
    result = _run_main([], db=db, data=root / "data", allow_boot=True)
    assert result.returncode == 0, result.stderr
    assert "Database initialized at:" in result.stdout
    # It really migrated: the folder moved and the schema is current.
    assert not (root / "data" / "Some Client").exists()
    assert any(p.name.isdigit() for p in (root / "data").iterdir())
    assert bootstrap.migration_status(db)["current"] is True


def test_check_and_migrate_together_are_refused(copy_db, capsys):
    root, db = copy_db
    before = _digest(db)
    assert main_mod.main(["--check", "--migrate"]) == main_mod.EXIT_REFUSED
    assert "pass one of them" in capsys.readouterr().err
    assert _digest(db) == before


def test_unknown_flag_fails_without_touching_anything(copy_db):
    root, db = copy_db
    before_db, before_data = _digest(db), _tree_digest(root / "data")
    result = _run_main(["--nope"], db=db, data=root / "data")
    assert result.returncode == 2                     # argparse's own error code
    assert _digest(db) == before_db
    assert _tree_digest(root / "data") == before_data


# ===========================================================================
# 4. The app import path checks the version and never migrates
# ===========================================================================


def test_importing_the_app_against_a_pre_migration_copy_raises_and_changes_nothing(tmp_path):
    root = tmp_path / "app"
    (root / "db").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    db = root / "db" / "poc.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE end_clients (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO end_clients (name) VALUES ('Before')")
    conn.commit()
    conn.close()
    before_db, before_data = _digest(db), _tree_digest(root / "data")

    with pytest.raises(bootstrap.SchemaMigrationPending) as exc:
        bootstrap.ensure_schema_current(db)
    # The message is the instruction, and the details travel as a note.
    assert str(exc.value) == bootstrap.MIGRATION_HINT
    assert "python main.py --migrate" in str(exc.value)

    assert _digest(db) == before_db
    assert _tree_digest(root / "data") == before_data
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # No schema was created and no row was touched.
        assert conn.execute("SELECT COUNT(*) FROM end_clients").fetchone()[0] == 1
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'"
        ).fetchone()
    finally:
        conn.close()


def test_the_app_entry_point_only_checks():
    """A source-level guard: setu.py must not reach any migrating function."""
    source = (REPO_ROOT / "setu" / "setu.py").read_text()
    assert "ensure_schema_current" in source
    assert "run_migrations" not in source
    assert "migrate.run" not in source
    assert "apply_folder_moves" not in source


# ===========================================================================
# 5. The migration's own guards
# ===========================================================================


def test_the_data_root_must_belong_to_the_database(copy_db):
    root, db = copy_db
    before = _tree_digest(root / "data")
    elsewhere = root / "not-the-data-root"
    elsewhere.mkdir()

    with pytest.raises(RuntimeError, match="not the one that belongs to"):
        migrate.run(db_path=db, data_root=elsewhere)
    assert _tree_digest(root / "data") == before


def test_the_data_root_guard_allows_the_expected_layout(copy_db):
    root, db = copy_db
    # data/ beside db/ IS the deployed layout, and db's sibling data is the
    # scratch layout. Both must be accepted.
    assert bootstrap.expected_data_root(db) == root / "data"
    assert bootstrap.expected_data_root(root / "scratch.db") == root / "data"


def test_a_sandbox_data_root_is_refused_for_the_app_database(monkeypatch, tmp_path):
    """Only the REAL database gets the temp/sandbox refusal (a scratch DB
    legitimately lives in a temp folder)."""
    sandbox = tmp_path / "setu_sandbox_123" / "data"
    sandbox.mkdir(parents=True)
    db = tmp_path / "db" / "poc.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db); conn.execute("CREATE TABLE t (x)"); conn.commit(); conn.close()
    # The sandbox clause is keyed off the REPOSITORY's own database (computed
    # from the source tree), so point that at this temporary one.
    monkeypatch.setattr(migrate, "_app_database_path", lambda: db.resolve())
    with pytest.raises(RuntimeError, match="temp or\n? ?sandbox|sandbox"):
        migrate._guard_data_root(db, sandbox)


def test_a_migration_with_no_backup_refuses_before_moving_anything(legacy_db):
    root, db = legacy_db
    before = _tree_digest(root / "data")

    # backup=False on a database that still needs migrating → the guard fires
    # BEFORE the folder move, so a folder can never be renamed without a
    # readable backup to roll back to.
    with pytest.raises(RuntimeError, match="backup"):
        migrate.run(db_path=db, data_root=root / "data", backup=False)

    assert _tree_digest(root / "data") == before
    assert (root / "data" / "Some Client").is_dir()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # The migration did NOT run: the database is still pre-migration.
        assert not migrate.already_migrated(conn)
    finally:
        conn.close()


def test_an_unreadable_backup_is_refused(tmp_path):
    broken = tmp_path / "not-a-database.db"
    broken.write_text("this is not sqlite")
    with pytest.raises(RuntimeError, match="not readable"):
        migrate._require_readable_backup(broken)
    with pytest.raises(RuntimeError, match="was not written"):
        migrate._require_readable_backup(tmp_path / "missing.db")
    with pytest.raises(RuntimeError, match="without a database backup"):
        migrate._require_readable_backup(None)


def test_the_backup_exists_and_is_readable_after_a_real_migration(legacy_db):
    """The happy path: the guard passes and the backup it demanded is there."""
    root, db = legacy_db
    report = migrate.run(db_path=db, data_root=root / "data")
    assert report["mode"] == "apply"
    backup = Path(report["backup"])
    assert backup.exists()
    conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        conn.close()
    # And the folder move happened, so the backup was drawn on a real move.
    assert not (root / "data" / "Some Client").exists()
    assert any(p.name.isdigit() for p in (root / "data").iterdir())


def test_backups_dir_is_gitignored():
    """The tests above write backup files next to the database; in the repo that
    lives under db/, which must stay out of git."""
    result = subprocess.run(
        ["git", "check-ignore", "-v", "db/backups/x.db"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, "db/ must be git-ignored"
