"""Tests for resolve_duplicate_gstin.py — the one-GSTIN-one-client repair tool.

The tool is meant to run against a live database, so the properties that matter
are the safety ones: it writes nothing without --apply, it never deletes a row
that anything points at, it refuses instead of guessing when both rows own data,
and it takes a backup before touching anything.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolve_duplicate_gstin as tool  # noqa: E402

GSTIN = "07AAECK9054D1ZK"


def _db(tmp_path: Path, *, rows, coa=(), runs=(), rules=()) -> Path:
    """A miniature client master plus the three shapes of reference:
    an id column, a legacy NAME column, and an unrelated integer column."""
    db = tmp_path / "poc.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE end_clients (
            client_id INTEGER PRIMARY KEY AUTOINCREMENT, legal_name TEXT NOT NULL,
            pan TEXT, tan TEXT, gstin TEXT, is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE chart_of_accounts (id INTEGER PRIMARY KEY, client_id INTEGER);
        CREATE TABLE runs (run_id INTEGER PRIMARY KEY, client TEXT, rule_id INTEGER);
        """
    )
    for client_id, name, gstin in rows:
        con.execute(
            "INSERT INTO end_clients (client_id, legal_name, pan, gstin, is_active)"
            " VALUES (?,?,?,?,1)",
            (client_id, name, "AAECK9054D", gstin),
        )
    for i, client_id in enumerate(coa, start=1):
        con.execute("INSERT INTO chart_of_accounts (id, client_id) VALUES (?,?)", (i, client_id))
    for i, name in enumerate(runs, start=1):
        con.execute("INSERT INTO runs (run_id, client) VALUES (?,?)", (i, name))
    for i, rule_id in enumerate(rules, start=1):
        con.execute("INSERT INTO runs (run_id, client, rule_id) VALUES (?,?,?)", (i, None, rule_id))
    con.commit()
    con.close()
    return db


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dupes(db: Path):
    con = sqlite3.connect(db)
    try:
        return tool.duplicate_gstins(con)
    finally:
        con.close()


TWO_SAME = [(2, "KMAP Capital Limited", GSTIN), (3, "KMAP Capital Limited", GSTIN)]


# --- report only -----------------------------------------------------------


def test_dry_run_reports_the_duplicate_and_writes_nothing(tmp_path, capsys):
    db = _db(tmp_path, rows=TWO_SAME, coa=[2], runs=["Test Client"])
    before = _digest(db)

    assert tool.main(["--db", str(db)]) == 0

    out = capsys.readouterr().out
    assert f"GSTIN {GSTIN}" in out
    assert "client_id=2" in out and "client_id=3" in out
    assert "would delete client_id=3" in out
    assert _digest(db) == before, "a report-only run must not touch the database"
    assert _dupes(db)  # still there


def test_no_duplicates_is_a_clean_no_op(tmp_path, capsys):
    db = _db(tmp_path, rows=[(2, "KMAP Capital Limited", GSTIN),
                            (4, "Test 2", None)])
    assert tool.main(["--db", str(db)]) == 0
    assert "No duplicate GSTINs" in capsys.readouterr().out


# --- applying --------------------------------------------------------------


def test_apply_keeps_the_row_that_owns_the_data(tmp_path, capsys):
    db = _db(tmp_path, rows=TWO_SAME, coa=[2], runs=["Test Client"])

    assert tool.main(["--db", str(db), "--apply"]) == 0

    out = capsys.readouterr().out
    assert "deleted client_id=3" in out
    assert "idx_end_clients_gstin_unique present" in out
    assert _dupes(db) == []

    con = sqlite3.connect(db)
    try:
        kept = [r[0] for r in con.execute("SELECT client_id FROM end_clients")]
        # the owner survives, with its data intact
        assert kept == [2]
        assert con.execute("SELECT COUNT(*) FROM chart_of_accounts WHERE client_id=2").fetchone()[0] == 1
        # and the constraint that was blocked can now be enforced
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx ON end_clients(gstin) "
            "WHERE gstin IS NOT NULL AND gstin <> ''"
        )
    finally:
        con.close()

    assert list((tmp_path / "backups").glob("poc_pre_dupgstin_*.db")), "no backup taken"


def test_a_second_apply_is_a_no_op(tmp_path, capsys):
    db = _db(tmp_path, rows=TWO_SAME, coa=[2], runs=["Test Client"])
    tool.main(["--db", str(db), "--apply"])
    capsys.readouterr()
    after_first = _digest(db)

    assert tool.main(["--db", str(db), "--apply"]) == 0
    assert "No duplicate GSTINs" in capsys.readouterr().out
    assert _digest(db) == after_first


def test_neither_row_owning_data_keeps_the_lowest_id(tmp_path, capsys):
    db = _db(tmp_path, rows=TWO_SAME, runs=["Test Client"])
    assert tool.main(["--db", str(db), "--apply"]) == 0
    assert "deleted client_id=3" in capsys.readouterr().out
    con = sqlite3.connect(db)
    try:
        assert [r[0] for r in con.execute("SELECT client_id FROM end_clients")] == [2]
    finally:
        con.close()


# --- refusals --------------------------------------------------------------


def test_refuses_when_both_rows_own_data(tmp_path, capsys):
    """Merging two clients that BOTH hold data is a human decision. Deleting
    either one would orphan real records."""
    db = _db(tmp_path, rows=TWO_SAME, coa=[2, 3])
    before = _digest(db)

    assert tool.main(["--db", str(db), "--apply"]) == 2

    assert "REFUSED" in capsys.readouterr().out
    assert _digest(db) == before, "a refusal must not write anything"
    assert _dupes(db), "nothing deleted"


def test_a_name_keyed_reference_counts(tmp_path, capsys):
    """A pre-migration database keys tables on the client NAME (`runs.client`).
    Deleting its client would orphan that row, so it counts as owning data."""
    db = _db(tmp_path, rows=TWO_SAME, runs=["KMAP Capital Limited"])

    # id 3 has no client_id reference, but the NAME reference is shared by both
    # rows — so both are treated as owning data and the tool refuses.
    assert tool.main(["--db", str(db), "--apply"]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_a_name_reference_makes_that_row_the_owner(tmp_path, capsys):
    """A pre-migration database keys tables on the client NAME (`runs.client`).
    Whichever row the name points at owns data, so the OTHER row is the one that
    gets deleted — even though neither has a `client_id` reference."""
    db = _db(tmp_path, rows=[(2, "Alpha", GSTIN), (3, "Beta", GSTIN)])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO runs (run_id, client) VALUES (9, 'Beta')")
    con.commit()
    con.close()

    assert tool.main(["--db", str(db), "--apply"]) == 0

    out = capsys.readouterr().out
    assert "runs.client by legal_name: 1" in out, "the name column must be scanned"
    assert "deleted client_id=2" in out
    con = sqlite3.connect(db)
    try:
        assert [r[0] for r in con.execute("SELECT client_id FROM end_clients")] == [3]
    finally:
        con.close()


# --- the false-positive guard ---------------------------------------------


def test_unrelated_integer_columns_are_not_references(tmp_path, capsys):
    """Regression guard. The first version of this tool compared the client id
    against EVERY column, so unrelated small integers (`rule_id = 2`,
    `sort_order = 3`) read as references and it refused a safe delete."""
    db = _db(tmp_path, rows=TWO_SAME, rules=[2, 3])
    before = _digest(db)

    assert tool.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out

    assert "would delete client_id=3" in out, out
    assert "rule_id" not in out, "rule_id is not a client reference"
    assert _digest(db) == before


# --- the non-destructive alternative --------------------------------------


@pytest.mark.parametrize("strategy", ["blank-gstin"])
def test_blank_gstin_keeps_both_clients(tmp_path, capsys, strategy):
    """When deleting is not acceptable (both rows hold data), the newer row can
    simply give up the registration — every client survives."""
    db = _db(tmp_path, rows=TWO_SAME, coa=[2, 3])

    assert tool.main(["--db", str(db), "--apply", "--strategy", strategy]) == 0

    assert "cleared GSTIN on client_id=2" in capsys.readouterr().out
    assert _dupes(db) == []
    con = sqlite3.connect(db)
    try:
        rows = dict(con.execute("SELECT client_id, gstin FROM end_clients"))
        assert set(rows) == {2, 3}, "both clients kept"
        assert rows[3] == GSTIN, "the newest row keeps the registration"
        assert rows[2] is None
        # its data is untouched
        assert con.execute("SELECT COUNT(*) FROM chart_of_accounts WHERE client_id=2").fetchone()[0] == 1
    finally:
        con.close()
