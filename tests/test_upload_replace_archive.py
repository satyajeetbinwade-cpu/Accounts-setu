"""Replacing a file under the same name keeps the copy it replaced.

The upload key an ingestion result is stored under is the file's
COORDINATES — ``<client_id>/<period>/<source_type>/<filename>`` — not its
bytes. Two consequences, both fixed here:

* Writing over a file with the same name destroyed the previous copy. Users
  re-export under the same name all the time (a fix, a re-issued download), so
  this made "try the corrected file" a one-way door.
* The OLD result stayed on that key. If the re-read then failed, the row went
  on saying "fully mapped" — about bytes the engine never managed to read.

So a re-upload now archives the previous copy to
``data/<client_id>/<period>/_replaced/<source_type>/<name>.<stamp>`` and drops
the stored result first. The archive is a SIBLING of the slot folders, never a
child, so no reader can offer it back as a candidate file or mistake it for a
period or a slot.

Run:

    venv/bin/python -m pytest tests/test_upload_replace_archive.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import data_paths as dp
from src.shared import discovery
from setu.state import reconcile_state as rs


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the REAL data root at a throwaway tree.

    `store_upload` resolves `source_data_path` inside `src.data_paths`, so
    patching that module's DATA_ROOT is what actually keeps an upload out of
    the repo's own `data/` tree — patching the state module's imported name
    would leave the writer untouched.
    """
    monkeypatch.setattr(dp, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(discovery, "DATA_ROOT", tmp_path)
    return tmp_path


def _slot_dir(root, client_id, period, source_type) -> Path:
    path = root / str(client_id) / str(period) / str(source_type)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# store_upload: never destroy a file that already has that name
# ---------------------------------------------------------------------------


def test_a_re_upload_archives_the_copy_it_replaced(sandbox):
    first = dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v1")
    assert first.written is True and first.archived is None

    second = dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v2")

    assert second.written is True
    assert second.archived is not None
    assert second.archived.read_bytes() == b"v1", "the replaced copy is the one kept"
    assert second.path.read_bytes() == b"v2"
    assert second.archived.parent == sandbox / "1" / "2026-08" / dp.REPLACED_DIR_NAME / "gstr2b"


def test_identical_bytes_are_a_no_op(sandbox):
    dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"same")

    again = dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"same")

    assert again.written is False
    assert again.archived is None
    assert not (sandbox / "1" / "2026-08" / dp.REPLACED_DIR_NAME).exists(), (
        "nothing changed, so nothing should have been archived"
    )


def test_two_replacements_do_not_overwrite_each_other(sandbox):
    """The archive name carries a timestamp, which repeats within a second —
    so a second replacement of the same file must not land on the first's
    name and destroy it."""
    for payload in (b"v1", b"v2", b"v3", b"v4"):
        dp.store_upload(1, "2026-08", "tally", "Books.xlsx", payload)

    archive = sandbox / "1" / "2026-08" / dp.REPLACED_DIR_NAME / "tally"
    assert sorted(p.read_bytes() for p in archive.iterdir()) == [b"v1", b"v2", b"v3"]


def test_an_unsafe_filename_is_refused(sandbox):
    for name in ("", "   ", ".."):
        with pytest.raises(ValueError):
            dp.store_upload(1, "2026-08", "gstr2b", name, b"x")


def test_a_path_traversal_filename_is_flattened_into_its_slot(sandbox):
    """A browser sends a bare name, but nothing here should ever trust that:
    the path is reduced to its basename so it cannot escape the slot."""
    outcome = dp.store_upload(1, "2026-08", "gstr2b", "../../etc/passwd", b"x")

    assert outcome.path == sandbox / "1" / "2026-08" / "gstr2b" / "passwd"
    assert not (sandbox / "etc").exists()


# ---------------------------------------------------------------------------
# The archive must be invisible to every reader
# ---------------------------------------------------------------------------


def test_an_archived_copy_is_never_offered_as_a_candidate_file(sandbox):
    dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v1")
    dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v2")

    assert rs._list_files(1, "2026-08", "gstr2b") == ["GSTR2B.xlsx"]
    assert discovery.source_file_status(1, "2026-08", "GST")["gstr2b"]["files"] == ["GSTR2B.xlsx"]
    assert rs._newest_file(1, "2026-08", "gstr2b") == "GSTR2B.xlsx"


def test_the_archive_folder_is_not_a_source_type_and_not_a_period(sandbox):
    dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v1")
    dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v2")

    assert dp.REPLACED_DIR_NAME not in discovery.SOURCE_TYPE_LABELS
    # `list_periods` reads the CLIENT folder, so the archive (one level down,
    # under the period) can never be mistaken for a period.
    assert dp.REPLACED_DIR_NAME not in discovery.list_periods(1)


def test_a_period_whose_only_remaining_files_were_replaced_does_not_read_as_populated(
    sandbox,
):
    """`period_has_source_data` gates the "no data uploaded yet" empty state.
    Left unguarded it would walk into `_replaced/` and report the period as
    populated on the strength of a file the user already replaced."""
    slot_file = dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v1").path
    dp.store_upload(1, "2026-08", "gstr2b", "GSTR2B.xlsx", b"v2")
    slot_file.unlink()  # the slot is empty; only the archive still has a file

    assert discovery.period_has_source_data(1, "2026-08") is False


# ---------------------------------------------------------------------------
# Choosing a file drops the previous verdict for that key
# ---------------------------------------------------------------------------


class _Upload:
    def __init__(self, filename: str, data: bytes = b"col\n1\n"):
        self.filename = filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


def _upload_state(sandbox, monkeypatch, *, period="2026-08", client_id=1, **extra):
    monkeypatch.setattr(rs, "source_data_path", lambda c, p, s: _slot_dir(sandbox, c, p, s))
    order: list[tuple] = []
    st = SimpleNamespace(
        ctx_client_id=client_id, ctx_period=period, ctx_recon_type="GST",
        selections={}, slot_errors={}, slot_busy="", expanded_rows=[],
        upload_error="", error="", flash="", rail_return=True, slots=[],
        order=order,
        _normalize=lambda source_type, filename: order.append(("normalize", source_type, filename)),
        _load_slots=lambda: None,
    )
    st._set_slot_error = lambda source_type, message: rs.ReconcileState._set_slot_error(
        st, source_type, message
    )
    st.__dict__.update(extra)
    return st, order


def _drive(handler, st, *args):
    async def run():
        return [chunk async for chunk in handler(st, *args)]
    return asyncio.run(run())


def test_a_re_upload_drops_the_old_verdict_before_re_reading(sandbox, monkeypatch):
    """Without this, a re-read that fails leaves the row saying the OLD file
    was "fully mapped" — the row would describe bytes nobody read."""
    st, order = _upload_state(sandbox, monkeypatch)
    discarded: list[tuple] = []
    monkeypatch.setattr(
        rs.ingestion_ai, "discard_stored_result",
        lambda c, p, s, f: (discarded.append((c, p, s, f)), order.append(("discard", s, f))),
    )

    _drive(
        rs.ReconcileState.upload_slot.fn, st, "gstr2b",
        [_Upload("GSTR2B.xlsx", b"new bytes")],
    )

    assert discarded == [(1, "2026-08", "gstr2b", "GSTR2B.xlsx")]
    assert order == [
        ("discard", "gstr2b", "GSTR2B.xlsx"),
        ("normalize", "gstr2b", "GSTR2B.xlsx"),
    ], "the old result must go BEFORE the file is read again"


def test_the_archived_copy_is_reported_to_the_user(sandbox, monkeypatch):
    """A file quietly moving out of the slot it was in is exactly the kind of
    thing a user should not have to discover by looking at the volume."""
    st, _ = _upload_state(sandbox, monkeypatch)
    monkeypatch.setattr(rs.ingestion_ai, "discard_stored_result", lambda *a, **k: 1)
    _slot_dir(sandbox, 1, "2026-08", "gstr2b").joinpath("GSTR2B.xlsx").write_bytes(b"old")

    _drive(rs.ReconcileState.upload_slot.fn, st, "gstr2b", [_Upload("GSTR2B.xlsx", b"new")])

    assert "The copy that was already there is kept at _replaced/gstr2b/" in st.flash


def test_a_first_upload_says_nothing_about_an_archive(sandbox, monkeypatch):
    st, _ = _upload_state(sandbox, monkeypatch)
    monkeypatch.setattr(rs.ingestion_ai, "discard_stored_result", lambda *a, **k: 0)

    _drive(rs.ReconcileState.upload_slot.fn, st, "tally", [_Upload("Books.xlsx", b"fresh")])

    assert st.flash == "Books.xlsx uploaded and normalized."


def test_an_unsafe_filename_fails_on_the_row_not_the_page(sandbox, monkeypatch):
    st, order = _upload_state(sandbox, monkeypatch)
    monkeypatch.setattr(rs.ingestion_ai, "discard_stored_result", lambda *a, **k: 0)

    _drive(rs.ReconcileState.upload_slot.fn, st, "gstr2b", [_Upload("..", b"x")])

    assert "Couldn't save" in st.slot_errors["gstr2b"]
    assert order == [], "nothing is read when nothing was written"
