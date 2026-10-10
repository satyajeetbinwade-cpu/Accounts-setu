"""Phase 2 — Communications module (WhatsApp / email follow-ups).

Pins the non-network behaviour of the module so a regression is caught
without sending anything: schema + template seeding, ``{{var}}`` rendering,
provider CRUD persistence (the update path must actually commit), the send
flow with a stubbed provider, the no-provider failure path, and per-client
follow-up preferences.

Run:

    venv/bin/python -m pytest tests/test_phase2_communications.py -v
"""

from __future__ import annotations

import shutil

import pytest

from src import bootstrap
from src.clients import service as clients
from src.communications import service as comms
from src.communications.models import FollowUpRecipient, FollowUpRequest
from src.communications.templates import render_subject, render_template


@pytest.fixture(scope="session")
def _seeded_db(tmp_path_factory):
    """One fully-seeded database per session — booting every module's init_*
    is slow, and every test only needs a pristine copy of the result."""
    path = tmp_path_factory.mktemp("phase2_comms_seed") / "poc.db"
    bootstrap.run_migrations(path)
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


@pytest.fixture()
def client_id(db_path):
    return clients.create_client(
        legal_name="Meridian Fabrics",
        pan="AABCM1234K",
        gstin="27AABCM1234K1Z9",
        actor="admin",
        db_path=db_path,
    )


def _request(client_id, **overrides):
    recipient = FollowUpRecipient(
        client_id=client_id,
        client_name="Meridian Fabrics",
        email="accounts@meridian.example",
        phone="+919876543210",
        contact_name="Priya",
    )
    payload = dict(
        recipient=recipient,
        channel="email",
        template_name="missing_invoice",
        context={
            "period": "2026-08",
            "recon_type": "GST",
            "missing_count": "3",
            "missing_details": "INV-1, INV-2, INV-3",
        },
    )
    payload.update(overrides)
    return FollowUpRequest(**payload)


# ---------------------------------------------------------------------------
# Schema + templates
# ---------------------------------------------------------------------------


def test_init_is_idempotent_and_seeds_templates(db_path):
    comms.init_communications(db_path)  # second call must not error
    names = {t["name"] for t in comms.list_templates(db_path=db_path)}
    assert {"missing_invoice", "missing_entries", "followup_reminder", "custom"} <= names


def test_render_substitutes_known_and_keeps_unknown():
    out = render_template("Hi {{name}} — {{missing}}", {"name": "Priya"})
    assert out == "Hi Priya — {{missing}}"


def test_render_subject_handles_none():
    assert render_subject(None, {}) is None
    assert render_subject("{{period}}", {"period": "2026-08"}) == "2026-08"


# ---------------------------------------------------------------------------
# Provider CRUD — the update path must persist
# ---------------------------------------------------------------------------


def test_provider_update_persists(db_path):
    pid = comms.save_provider(
        None,
        channel="email",
        provider_type="smtp",
        label="first",
        config={"host": "smtp.example.com", "port": 587},
        db_path=db_path,
    )
    updated = comms.save_provider(
        pid,
        channel="email",
        provider_type="smtp",
        label="second",
        config={"host": "smtp.example.com", "port": 2525},
        db_path=db_path,
    )
    assert updated == pid
    row = next(p for p in comms.list_providers(db_path=db_path) if p["provider_id"] == pid)
    assert row["label"] == "second"
    assert '"port": 2525' in row["config_json"]


# ---------------------------------------------------------------------------
# Send flow
# ---------------------------------------------------------------------------


def test_send_followup_logs_sent_with_stub(db_path, client_id, monkeypatch):
    comms.save_provider(
        None, channel="email", provider_type="smtp",
        config={"host": "smtp.example.com"}, db_path=db_path,
    )

    captured = {}

    def fake_send_email(config, *, to_email, to_name, subject, body_text):
        captured.update(to_email=to_email, subject=subject, body=body_text)
        return True, "msg-42"

    monkeypatch.setattr(comms, "send_email", fake_send_email)

    log_id = comms.send_followup(_request(client_id), db_path=db_path)
    logs = comms.list_logs(client_id=client_id, db_path=db_path)
    entry = next(e for e in logs if e["log_id"] == log_id)

    assert entry["status"] == "sent"
    assert entry["provider_ref"] == "msg-42"
    assert captured["to_email"] == "accounts@meridian.example"
    assert "2026-08" in captured["subject"]
    assert "INV-1" in captured["body"]


def test_send_followup_without_provider_marks_failed(db_path, client_id):
    log_id = comms.send_followup(_request(client_id), db_path=db_path)
    entry = next(
        e for e in comms.list_logs(client_id=client_id, db_path=db_path)
        if e["log_id"] == log_id
    )
    assert entry["status"] == "failed"
    assert "provider" in (entry["error_message"] or "").lower()


def test_send_followup_requires_contact_info(db_path, client_id):
    request = _request(
        client_id,
        recipient=FollowUpRecipient(client_id=client_id, client_name="Meridian Fabrics"),
    )
    with pytest.raises(comms.CommunicationError):
        comms.send_followup(request, db_path=db_path)


def test_send_followup_whatsapp_routing(db_path, client_id, monkeypatch):
    comms.save_provider(
        None, channel="whatsapp", provider_type="twilio",
        config={"account_sid": "AC", "auth_token": "tok"}, db_path=db_path,
    )
    monkeypatch.setattr(comms, "send_whatsapp", lambda *a, **k: (True, "SM123"))

    log_id = comms.send_followup(_request(client_id, channel="whatsapp"), db_path=db_path)
    entry = next(
        e for e in comms.list_logs(client_id=client_id, db_path=db_path)
        if e["log_id"] == log_id
    )
    assert entry["channel"] == "whatsapp"
    assert entry["status"] == "sent"


# ---------------------------------------------------------------------------
# Per-client preferences
# ---------------------------------------------------------------------------


def test_followup_prefs_upsert_is_single_row(db_path, client_id):
    first = comms.save_followup_pref(
        client_id, email="a@x.com", preferred_channel="email", db_path=db_path,
    )
    second = comms.save_followup_pref(
        client_id, email="b@x.com", phone="+91", preferred_channel="both", db_path=db_path,
    )
    assert first == second
    prefs = comms.get_client_followup_prefs(client_id, db_path=db_path)
    assert len(prefs) == 1
    assert prefs[0]["email"] == "b@x.com"
    assert prefs[0]["preferred_channel"] == "both"
