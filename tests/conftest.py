"""Shared test helpers.

Identity is ``end_clients.client_id`` everywhere now, so any test that runs a
reconciliation must first seed a client and lay its fixtures out under
``data/<client_id>/<period>/<source_type>/``.

ISOLATION FROM THE REAL DATABASE (session-wide, automatic):

* ``db/poc.db`` is hashed before the first test and again after the last one;
  a changed hash FAILS the run, whatever else passed.
* the file is made read-only for the session where the OS allows it;
* ``sqlite3.connect`` is wrapped so that opening the repository's own database
  for WRITE during a test raises immediately, naming the test — reading it
  (``mode=ro`` / ``uri=True``) stays allowed.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import traceback
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db as _recon_db  # noqa: E402

#: The REPOSITORY's database — a constant, so an override cannot hide it.
REPO_DB = Path(_recon_db.REPO_DB_PATH).expanduser()


def _sha256(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_read_only_open(database, args, kwargs) -> bool:
    """Is this connect() call asking for read-only access?"""
    if kwargs.get("uri") and "mode=ro" in str(database):
        return True
    if args and str(args[0]).lower() in ("ro", "r"):
        return True
    return False


@pytest.fixture(scope="session", autouse=True)
def real_database_is_isolated():
    """Make the repository's database untouchable for the whole run."""
    before = _sha256(REPO_DB)
    original_mode = None
    if REPO_DB.exists():
        original_mode = REPO_DB.stat().st_mode
        try:
            os.chmod(REPO_DB, 0o444)
        except OSError:  # pragma: no cover - filesystem said no
            original_mode = None
    original_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            candidate = Path(str(database)).expanduser()
            is_repo = candidate.exists() and candidate.resolve() == REPO_DB.resolve()
        except (OSError, ValueError):
            is_repo = False
        if is_repo and not _is_read_only_open(database, args, kwargs):
            frames = "".join(traceback.format_stack(limit=8)[:-1])
            raise RuntimeError(
                "TEST DB ISOLATION: this test opened the repository's own "
                f"database for WRITE ({REPO_DB}). Pass an explicit temporary "
                "path, or open it read-only (uri=True, mode=ro).\n" + frames
            )
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = guarded_connect
    try:
        yield
    finally:
        sqlite3.connect = original_connect
        if original_mode is not None:
            os.chmod(REPO_DB, original_mode)
        after = _sha256(REPO_DB)
        if after != before:
            raise AssertionError(
                f"THE REAL DATABASE CHANGED DURING THE TEST RUN: {REPO_DB}\n"
                f"  before: {before}\n  after:  {after}"
            )


def seed_client(
    db_path,
    legal_name: str = "Test Client",
    *,
    pan: str | None = "AAAPL1234C",
    tan: str | None = None,
    gstin: str | None = None,
) -> int:
    """Create one F2 client and return its ``client_id`` (1 on a fresh DB)."""
    from src.clients import service as clients

    return clients.create_client(
        legal_name=legal_name, pan=pan, tan=tan, gstin=gstin,
        actor="test", db_path=db_path,
    )
