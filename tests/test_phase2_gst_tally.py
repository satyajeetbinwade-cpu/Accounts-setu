"""Phase 2 — GST Portal & Tally Integration module.

Pins the preview → confirm workflow, the credential encryption at rest, the
"confirm only a previewing op" guard, and the connection/payload plumbing
that a confirmed push uses. All external clients are stubbed, so nothing
touches the network.

Run:

    venv/bin/python -m pytest tests/test_phase2_gst_tally.py -v
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from src import bootstrap
from src import db as recon_db
from src.clients import service as clients
from src.gst_tally_integration import db as gtdb
from src.gst_tally_integration import service as gt_svc
from src.vault.crypto import decrypt

GSTIN = "27AABCM1234K1Z9"


@pytest.fixture(scope="session")
def _seeded_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("phase2_gst_seed") / "poc.db"
    bootstrap.run_migrations(path)
    return path


@pytest.fixture()
def db_path(_seeded_db, tmp_path):
    dest = tmp_path / "poc.db"
    shutil.copy(_seeded_db, dest)
    return str(dest)


@pytest.fixture()
def client_id(db_path):
    return clients.create_client(
        legal_name="Meridian Fabrics",
        pan="AABCM1234K",
        gstin=GSTIN,
        actor="admin",
        owner_user_id=1,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_init_is_idempotent(db_path):
    gt_svc.init_gst_tally(db_path)
    gt_svc.init_gst_tally(db_path)
    names = {
        r[0]
        for r in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "gst_credentials", "tally_connections", "sync_operations",
        "gst_returns", "tally_data_cache",
    } <= names


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_credentials_encrypted_at_rest_and_hidden(db_path, client_id):
    gt_svc.save_gst_credential(client_id, GSTIN, "portal_user", "s3cret", db_path=db_path)

    raw = sqlite3.connect(db_path).execute(
        "SELECT encrypted_password FROM gst_credentials WHERE client_id=? AND gstin=?",
        (client_id, GSTIN),
    ).fetchone()[0]
    assert raw != "s3cret"
    assert decrypt(raw) == "s3cret"

    listed = gt_svc.list_gst_credentials(client_id, db_path=db_path)
    assert listed and "encrypted_password" not in listed[0]


# ---------------------------------------------------------------------------
# Workflow guards
# ---------------------------------------------------------------------------


def test_confirm_rejects_an_op_that_is_not_previewing(db_path, client_id):
    conn = recon_db.get_connection(db_path)
    try:
        op_id = gtdb.create_sync_op(conn, client_id, "fetch", "tally", "coa")
    finally:
        conn.close()

    with pytest.raises(gt_svc.GstTallyError):
        gt_svc.confirm_sync_operation(op_id, "tester", db_path=db_path)


def test_confirm_missing_op_raises(db_path):
    with pytest.raises(gt_svc.GstTallyError):
        gt_svc.confirm_sync_operation(999999, "tester", db_path=db_path)


def test_cancel_then_confirm_is_blocked(db_path, client_id):
    conn_id = gt_svc.save_tally_connection(
        None, client_id, host="localhost", port=9000, company_name="Co", db_path=db_path,
    )
    op_id = gt_svc.push_reconciliation_to_tally(
        conn_id, client_id, "202608", _entries(), db_path=db_path,
    )
    gt_svc.cancel_sync_operation(op_id, db_path=db_path)
    assert gt_svc.get_sync_operation(op_id, db_path=db_path)["status"] == "cancelled"
    with pytest.raises(gt_svc.GstTallyError):
        gt_svc.confirm_sync_operation(op_id, "tester", db_path=db_path)


def test_fetch_tally_without_connection_raises(db_path, client_id):
    with pytest.raises(gt_svc.GstTallyError):
        gt_svc.fetch_from_tally(123456, client_id, "coa", db_path=db_path)


def test_fetch_gst_without_credentials_raises(db_path, client_id):
    with pytest.raises(gt_svc.GstTallyError):
        gt_svc.fetch_from_gst_portal(client_id, GSTIN, "returns", "082026", db_path=db_path)


# ---------------------------------------------------------------------------
# Preview payloads
# ---------------------------------------------------------------------------


def _entries():
    return [
        {
            "date": "20260801",
            "narration": "ITC adjustment",
            "entries": [
                {"ledger": "ITC Input", "amount": 5000, "type": "Dr"},
                {"ledger": "ITC Output", "amount": 5000, "type": "Cr"},
            ],
        }
    ]


def test_push_to_tally_preview_totals_use_leg_amounts(db_path, client_id):
    conn_id = gt_svc.save_tally_connection(
        None, client_id, host="localhost", port=9000, company_name="Co", db_path=db_path,
    )
    op_id = gt_svc.push_reconciliation_to_tally(
        conn_id, client_id, "202608", _entries(), db_path=db_path,
    )
    op = gt_svc.get_sync_operation(op_id, db_path=db_path)
    details = json.loads(op["details_json"])
    assert op["direction"] == "push"
    assert op["status"] == "previewing"
    assert details["conn_id"] == conn_id
    assert details["total_debit"] == 5000
    assert details["total_credit"] == 5000


def test_gst_push_retains_payload(db_path, client_id):
    gt_svc.save_gst_credential(client_id, GSTIN, "portal_user", "s3cret", db_path=db_path)
    payload = {"gstin": GSTIN, "taxable": 1000}
    op_id = gt_svc.push_to_gst_portal(
        client_id, GSTIN, "GSTR3B", "082026", payload, db_path=db_path,
    )
    details = json.loads(gt_svc.get_sync_operation(op_id, db_path=db_path)["details_json"])
    assert details["data"] == payload


# ---------------------------------------------------------------------------
# Confirm executes the push through the right connection
# ---------------------------------------------------------------------------


class _FakeTally:
    last_host = None
    calls: list = []

    def __init__(self, host="localhost", port=9000, company_name=None):
        _FakeTally.last_host = host

    def push_journal_entry(self, entry):
        _FakeTally.calls.append(entry)
        return True, None


class _FakeGst:
    last_password = None
    last_payload = None

    def __init__(self, gstin, username, password, *, sandbox=True,
                 client_id=None, client_secret=None):
        _FakeGst.last_password = password

    def push_return(self, return_type, period, data):
        _FakeGst.last_payload = data
        return True, {"ok": True}


def test_tally_confirm_executes_and_uses_the_right_connection(db_path, client_id, monkeypatch):
    _FakeTally.last_host = None
    _FakeTally.calls = []
    monkeypatch.setattr(gt_svc, "TallyClient", _FakeTally)

    gt_svc.save_tally_connection(
        None, client_id, host="wrong.example", port=9000, company_name="A", db_path=db_path,
    )
    right = gt_svc.save_tally_connection(
        None, client_id, host="right.example", port=9000, company_name="B", db_path=db_path,
    )
    op_id = gt_svc.push_reconciliation_to_tally(
        right, client_id, "202608", _entries(), db_path=db_path,
    )
    gt_svc.confirm_sync_operation(op_id, "tester", db_path=db_path)

    op = gt_svc.get_sync_operation(op_id, db_path=db_path)
    assert op["status"] == "completed"
    assert op["confirmed_by"] == "tester"
    assert _FakeTally.last_host == "right.example"
    assert len(_FakeTally.calls) == 1


def test_gst_confirm_decrypts_password_and_sends_payload(db_path, client_id, monkeypatch):
    _FakeGst.last_password = None
    _FakeGst.last_payload = None
    monkeypatch.setattr(gt_svc, "GstPortalClient", _FakeGst)

    gt_svc.save_gst_credential(client_id, GSTIN, "portal_user", "s3cret", db_path=db_path)
    payload = {"gstin": GSTIN, "taxable": 1000}
    op_id = gt_svc.push_to_gst_portal(
        client_id, GSTIN, "GSTR3B", "082026", payload, db_path=db_path,
    )
    gt_svc.confirm_sync_operation(op_id, "tester", db_path=db_path)

    assert gt_svc.get_sync_operation(op_id, db_path=db_path)["status"] == "completed"
    assert _FakeGst.last_password == "s3cret"
    assert _FakeGst.last_payload == payload


# ---------------------------------------------------------------------------
# Tally XML safety
# ---------------------------------------------------------------------------


def test_tally_xml_escapes_special_characters():
    import xml.etree.ElementTree as ET

    from src.gst_tally_integration.tally.client import TallyClient

    client = TallyClient()
    captured: dict = {}

    def fake_send(body):
        captured["body"] = body
        return True, "<ENVELOPE></ENVELOPE>"

    client._send_request = fake_send
    ok, err = client.push_journal_entry({
        "date": "20260801",
        "narration": "A & B <x>",
        "voucher_number": "V1",
        "entries": [
            {"ledger": "ITC & Co", "amount": 100, "type": "Dr"},
            {"ledger": "Output <GST>", "amount": 100, "type": "Cr"},
        ],
    })
    assert ok and err is None
    assert "&amp;" in captured["body"] and "&lt;GST&gt;" in captured["body"]
    ET.fromstring(f"<ENVELOPE><BODY>{captured['body']}</BODY></ENVELOPE>")

