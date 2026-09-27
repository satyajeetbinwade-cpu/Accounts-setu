"""F2 — the flat EndClient model (27-Sep-2026 revision).

Pins the revision's acceptance criteria at the service/data layer:

* one GST registration = one client — a single `gstin` column, no
  `gstin_branches` table, no branch dimension anywhere in the returned rows;
* at least one of PAN or TAN required before save;
* the primary contact block (email/phone/address) required before save;
* PAN/TAN/GSTIN edits still need Manager+ AND a captured reason;
* a GSTIN can't be cleared or changed while open reconciliation work
  references the client;
* an existing branch-based database is folded onto the flat model and the
  `gstin_branches` table is dropped.

Run:

    venv/bin/python -m pytest tests/test_clients_model.py -v
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from src import bootstrap
from src import db as recon_db
from src.clients import service as clients


@pytest.fixture(scope="session")
def _seeded_db(tmp_path_factory):
    """One fully-seeded database per session — booting every module's init_*
    is slow, and every test only needs a pristine copy of the result."""
    path = tmp_path_factory.mktemp("clients_seed") / "poc.db"
    bootstrap.init_all(path)
    return path


@pytest.fixture()
def db_path(_seeded_db, tmp_path):
    dest = tmp_path / "poc.db"
    shutil.copy(_seeded_db, dest)
    for suffix in ("-wal", "-shm"):
        extra = _seeded_db.with_name(_seeded_db.name + suffix)
        if extra.exists():
            shutil.copy(extra, dest.with_name(dest.name + suffix))
    return str(dest)


def _valid(**overrides):
    base = dict(
        legal_name="Meridian Fabrics",
        pan="AABCM1234K",
        gstin="27AABCM1234K1Z9",
        primary_contact_email="accounts@meridian.example",
        primary_contact_phone="+91 22 4000 0000",
        primary_contact_address="Mumbai, Maharashtra",
        actor="admin",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Creation rules
# ---------------------------------------------------------------------------


def test_create_requires_legal_name(db_path):
    with pytest.raises(clients.ClientError, match="Legal name"):
        clients.create_client(**{**_valid(), "legal_name": "   "}, db_path=db_path)


def test_create_requires_pan_or_tan(db_path):
    with pytest.raises(clients.ClientError, match="At least one of PAN or TAN"):
        clients.create_client(**{**_valid(), "pan": ""}, db_path=db_path)


def test_create_accepts_tan_only(db_path):
    """Either identity alone is enough — PAN is not privileged."""
    cid = clients.create_client(**{**_valid(), "pan": "", "tan": "MUMA12345B"}, db_path=db_path)
    row = clients.get_client(cid, db_path=db_path)
    assert row["pan"] is None
    assert row["tan"] == "MUMA12345B"


@pytest.mark.parametrize(
    "field,label",
    [
        ("primary_contact_email", "Primary contact email"),
        ("primary_contact_phone", "Primary contact phone"),
        ("primary_contact_address", "Primary contact address"),
    ],
)
def test_create_requires_the_primary_contact_block(db_path, field, label):
    with pytest.raises(clients.ClientError, match=label):
        clients.create_client(**{**_valid(), field: ""}, db_path=db_path)


def test_create_stores_exactly_one_gstin(db_path):
    cid = clients.create_client(**{**_valid(), "gstin": " 27AABCM1234K1Z9 "}, db_path=db_path)
    row = clients.get_client(cid, db_path=db_path)
    assert row["gstin"] == "27AABCM1234K1Z9"  # trimmed
    assert not isinstance(row["gstin"], (list, tuple))


def test_list_clients_carries_no_branch_dimension(db_path):
    clients.create_client(**_valid(), db_path=db_path)
    row = clients.list_clients(db_path=db_path)[0]
    assert "branch_count" not in row
    assert "primary_gstin" not in row
    assert row["gstin"] == "27AABCM1234K1Z9"


# ---------------------------------------------------------------------------
# Editing rules
# ---------------------------------------------------------------------------


def test_identity_edit_requires_a_reason(db_path):
    cid = clients.create_client(**_valid(), db_path=db_path)
    with pytest.raises(clients.ClientError, match="reason is required"):
        clients.update_client_field(cid, "gstin", "27AABCM9999K1Z5", actor="admin", db_path=db_path)


def test_identity_edit_succeeds_with_a_reason_and_records_history(db_path):
    cid = clients.create_client(**_valid(), db_path=db_path)
    clients.update_client_field(
        cid, "tan", "MUMA12345B", actor="admin", reason="TAN missing at onboarding", db_path=db_path
    )
    assert clients.get_client(cid, db_path=db_path)["tan"] == "MUMA12345B"

    entries = clients.list_edit_log(cid, db_path=db_path)
    assert [e["field"] for e in entries] == ["tan"]
    assert entries[0]["reason"] == "TAN missing at onboarding"


def test_required_contact_field_cannot_be_blanked(db_path):
    cid = clients.create_client(**_valid(), db_path=db_path)
    with pytest.raises(clients.ClientError, match="can't be blank"):
        clients.update_client_field(cid, "primary_contact_email", "", actor="admin", db_path=db_path)


def test_cannot_clear_the_only_identity(db_path):
    cid = clients.create_client(**{**_valid(), "tan": ""}, db_path=db_path)
    with pytest.raises(clients.ClientError, match="At least one of PAN or TAN"):
        clients.update_client_field(cid, "pan", "", actor="admin", db_path=db_path)


def test_details_save_validates_the_whole_record(db_path):
    """A legacy record with no contact block can't be saved as-is — the
    service reports it rather than silently persisting a broken client."""
    cid = clients.create_client(**_valid(), db_path=db_path)
    with pytest.raises(clients.ClientError, match="Primary contact"):
        clients.update_client_details(
            cid,
            actor="admin",
            legal_name="Meridian Fabrics",
            assigned_team="Team A",
            primary_contact_email="",
            primary_contact_phone="",
            primary_contact_address="",
            db_path=db_path,
        )


def test_details_save_updates_every_field(db_path):
    cid = clients.create_client(**_valid(), db_path=db_path)
    clients.update_client_details(
        cid,
        actor="admin",
        legal_name="Meridian Fabrics Pvt Ltd",
        assigned_team="Team A",
        primary_contact_email="ap@meridian.example",
        primary_contact_phone="+91 22 4000 1111",
        primary_contact_address="Pune, Maharashtra",
        db_path=db_path,
    )
    row = clients.get_client(cid, db_path=db_path)
    assert row["legal_name"] == "Meridian Fabrics Pvt Ltd"
    assert row["assigned_team"] == "Team A"
    assert row["primary_contact_address"] == "Pune, Maharashtra"
    # An unrestricted save must never touch the identity fields.
    assert row["pan"] == "AABCM1234K"
    assert row["gstin"] == "27AABCM1234K1Z9"


# ---------------------------------------------------------------------------
# The GSTIN business rule
# ---------------------------------------------------------------------------


def _add_open_exception(db_path, client_id: int) -> None:
    conn = recon_db.get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO reconciliation_exceptions
                (client_id, sub_type, recon_type, item_type, classification, created_at)
            VALUES (?, '2A', 'GST', 'reconciliation_exception', 'Amount Difference', '2026-09-27T00:00:00Z')
            """,
            (client_id,),
        )
        conn.commit()
    finally:
        conn.close()


def test_gstin_content_and_change_allowed_with_no_open_work(db_path):
    cid = clients.create_client(**_valid(), db_path=db_path)
    assert clients.can_change_gstin(cid, db_path=db_path) == (True, None)


def test_gstin_blocked_while_open_recon_work_exists(db_path):
    cid = clients.create_client(**_valid(), db_path=db_path)
    _add_open_exception(db_path, cid)

    allowed, reason = clients.can_change_gstin(cid, db_path=db_path)
    assert allowed is False
    assert "open reconciliation work" in reason

    with pytest.raises(clients.ClientError, match="open reconciliation work"):
        clients.update_client_field(
            cid, "gstin", "27AABCM9999K1Z5", actor="admin", reason="wrong GSTIN", db_path=db_path
        )
    # A NON-gstin identity edit is unaffected by the gate.
    clients.update_client_field(cid, "tan", "MUMA12345B", actor="admin", reason="adding TAN", db_path=db_path)
    assert clients.get_client(cid, db_path=db_path)["tan"] == "MUMA12345B"


def test_resolved_exceptions_do_not_block(db_path):
    cid = clients.create_client(**_valid(), db_path=db_path)
    _add_open_exception(db_path, cid)
    conn = recon_db.get_connection(db_path)
    try:
        conn.execute("UPDATE reconciliation_exceptions SET status = 'resolved'")
        conn.commit()
    finally:
        conn.close()
    assert clients.can_change_gstin(cid, db_path=db_path)[0] is True


# ---------------------------------------------------------------------------
# Migration off the branch model
# ---------------------------------------------------------------------------


_LEGACY_DDL = """
CREATE TABLE end_clients (
    client_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    legal_name     TEXT    NOT NULL,
    pan            TEXT,
    assigned_team  TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    created_by     TEXT
);
CREATE TABLE gstin_branches (
    branch_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL,
    gstin          TEXT    NOT NULL,
    branch_name    TEXT,
    address        TEXT,
    state          TEXT,
    status         TEXT    NOT NULL DEFAULT 'Not Started',
    is_primary     INTEGER NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL
);
"""


def test_legacy_branch_data_is_folded_into_the_client(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_DDL)
    conn.execute(
        "INSERT INTO end_clients (legal_name, pan, assigned_team, is_active, created_at, created_by) "
        "VALUES ('Test Client', NULL, NULL, 1, '2026-01-01T00:00:00Z', 'admin')"
    )
    conn.execute(
        "INSERT INTO gstin_branches (client_id, gstin, address, state, is_primary, is_active, created_at) "
        "VALUES (1, '07AAACP9472A1ZJ', 'Registered office', 'Delhi', 1, 1, '2026-01-01T00:00:00Z')"
    )
    # A second, non-primary registration must NOT win the fold.
    conn.execute(
        "INSERT INTO gstin_branches (client_id, gstin, address, state, is_primary, is_active, created_at) "
        "VALUES (1, '27AABCM1234K1Z9', 'Warehouse', 'Maharashtra', 0, 1, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    clients.init_clients(str(path))

    conn = recon_db.get_connection(str(path))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(end_clients)")}
        assert {"tan", "gstin", "primary_contact_email", "primary_contact_phone", "primary_contact_address"} <= cols

        row = conn.execute("SELECT gstin, primary_contact_address FROM end_clients").fetchone()
        assert row[0] == "07AAACP9472A1ZJ"
        assert row[1] == "Registered office, Delhi"

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "gstin_branches" not in tables
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    bootstrap.init_all(path)
    bootstrap.init_all(path)  # must not raise
    assert clients.create_client(**_valid(legal_name="Once Only"), db_path=str(path)) > 0


# ---------------------------------------------------------------------------
# Downstream consumer
# ---------------------------------------------------------------------------


def test_report_header_gstin_reads_the_client_column(db_path):
    from src.reconciliation import run_model

    cid = clients.create_client(**_valid(), db_path=db_path)
    assert run_model.client_gstin_for(cid, db_path=db_path) == "27AABCM1234K1Z9"
    assert run_model.client_gstin_for(None, db_path=db_path) == ""
