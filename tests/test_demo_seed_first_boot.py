"""Demo seeding is FIRST-BOOT ONLY.

The safety argument for the production deploy, pinned down. `docker`'s CMD runs
`python -m src.demo_seed` on every boot, and on a **migrated** volume the client
this script looks for is present under a different name: the bundled folder is
`Test Client`, the live roster says `Test`.

Name-matching alone therefore missed the existing row and called
`create_client(legal_name="Test Client", gstin="07AAACP9472A1ZJ")` — which
SUCCEEDS, because the live `Test` row carries no GSTIN and the unique index has
nothing to collide with. The result would have been a duplicate client for a
client that already existed, plus a second copy of its files.

So `main()` refuses to do anything once any client exists. These tests drive the
real `main()` against a throwaway database and data root.

Run:

    venv/bin/python -m pytest tests/test_demo_seed_first_boot.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import data_paths as dp
from src import db as core_db
from src.auth import access
from src.clients import service as clients
from src import demo_seed


def _seed_tree(root: Path) -> Path:
    """A stand-in for the image's ``/app/seed_data``: one demo folder."""
    src = root / "Test Client" / "2026-08" / "tally"
    src.mkdir(parents=True, exist_ok=True)
    (src / "PurchaseRegister.xlsx").write_bytes(b"demo bytes")
    return root


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A temp DB + temp data root, so nothing here can touch the real ones."""
    db = tmp_path / "poc.db"
    core_db.init_db(db)
    clients.init_clients(db)
    monkeypatch.setattr(core_db, "DB_PATH", db)
    monkeypatch.setattr(dp, "DATA_ROOT", tmp_path / "data")
    from src.shared import discovery

    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path / "data")
    seed = _seed_tree(tmp_path / "seed_data")
    monkeypatch.setattr(demo_seed, "SEED_ROOT", seed)
    return tmp_path


def _clients() -> list[dict]:
    return clients.list_clients(viewer=access.SYSTEM_VIEWER, include_inactive=True)


def test_it_seeds_a_genuinely_fresh_volume(sandbox):
    """The feature this guard must not disable: an empty roster is a first boot,
    and the demo picker depends on those clients existing."""
    assert _clients() == []

    assert demo_seed.main() == 0

    names = [c["legal_name"] for c in _clients()]
    assert "Test Client" in names
    cid = next(c["client_id"] for c in _clients() if c["legal_name"] == "Test Client")
    filed = sandbox / "data" / str(cid) / "2026-08" / "tally" / "PurchaseRegister.xlsx"
    assert filed.exists(), "the demo file is filed under the client id, never the name"


def test_it_does_nothing_once_any_client_exists(sandbox):
    """THE deploy guard. A volume with clients on it is not a fresh deployment,
    whatever the names are."""
    clients.create_client(legal_name="Test", pan="AAAPL1234C", actor="test", owner_user_id=1)

    assert demo_seed.main() == 0

    assert [c["legal_name"] for c in _clients()] == ["Test"]
    assert not (sandbox / "data").exists(), "nothing was copied"


def test_it_refuses_even_when_the_demo_client_exists_under_another_name(sandbox):
    """The exact live shape: the roster holds `Test`, the bundled folder is
    `Test Client`, and that row has NO GSTIN for the unique index to catch.
    Without the guard this created a second client."""
    clients.create_client(legal_name="Test", pan="AAAPL1234C", actor="test", owner_user_id=1)
    before = {c["client_id"] for c in _clients()}

    demo_seed.main()

    after = {c["client_id"] for c in _clients()}
    assert after == before, "a duplicate client was created for an existing one"
    assert len(after) == 1
    gstins = [c.get("gstin") for c in _clients()]
    assert gstins == [None], "the demo GSTIN was not attached to a second row"


def test_it_says_why_it_skipped(sandbox, capsys):
    clients.create_client(legal_name="Test", pan="AAAPL1234C", actor="test", owner_user_id=1)

    demo_seed.main()

    out = capsys.readouterr().out
    assert "already exist" in out and "no demo seeding" in out


def test_no_seed_data_at_all_is_still_a_no_op(sandbox, monkeypatch):
    """A real customer volume ships no seed data; that path must stay quiet."""
    monkeypatch.setattr(demo_seed, "SEED_ROOT", sandbox / "does-not-exist")

    assert demo_seed.main() == 0
    assert _clients() == []


def test_running_it_twice_changes_nothing(sandbox):
    demo_seed.main()
    first = [(c["client_id"], c["legal_name"]) for c in _clients()]

    demo_seed.main()

    assert [(c["client_id"], c["legal_name"]) for c in _clients()] == first
