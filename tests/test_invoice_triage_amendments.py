"""Tests for the Phase-1 triage AMENDMENTS (background runs, gateway limits,
full call logging, vision capability guard).

Stubbed only — no live model calls.

Run:

    venv/bin/python -m pytest tests/test_invoice_triage_amendments.py -v
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import db as recon_db
from src.ai_models import gateway, providers as prov, service as ams
from src.invoice_extract import db as idb
from src.invoice_extract import service as ie
from src.invoice_extract import triage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_pdf(pages: int = 3) -> bytes:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 72), f"Page {i + 1}")
        p.insert_text((72, 120), "=" * 40)
        p.insert_text((72, 300), "DOC 1234")
    data = doc.tobytes()
    doc.close()
    return data


def _fresh(tmp_path, *, pages=3, name="ai.db") -> tuple[str, int]:
    db_path = str(tmp_path / name)
    ie.init_invoice_extract(db_path)
    conn = recon_db.get_connection(db_path)
    try:
        uid = idb.create_upload(
            conn, client_id=1, filename="b.pdf", file_ext="pdf", source_format="pdf",
            extraction_path="structured", batch_id=None, status="extracted",
            duplicate_warning=False, actor="t", file_bytes=_mk_pdf(pages),
        )
    finally:
        conn.close()
    return db_path, uid


def _ai_db(tmp_path, name="ai.db") -> str:
    db_path = str(tmp_path / name)
    ams.init_ai_models(db_path)
    from src.settings import service as settings

    settings.init_settings(db_path)
    return db_path


def _ok_meta(model="m", fallback=False):
    return {"provider": "p", "model": model, "latency_ms": 2, "usage": {}, "fallback_used": fallback}


def _one_doc(label="INV"):
    return {"rotation_deg": 0, "page_is_blank": False,
            "documents": [{"category": "tax_invoice", "label": label,
                           "boxes": [{"x": 0.1, "y": 0.1, "w": 0.6, "h": 0.6}]}]}


# ===========================================================================
# A — background job
# ===========================================================================


def test_run_returns_at_once_and_worker_finishes_later(monkeypatch, tmp_path):
    db_path, uid = _fresh(tmp_path)
    gate = threading.Event()
    reached = threading.Event()

    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        reached.set()
        gate.wait(timeout=5)          # block until the test releases it
        return _one_doc(f"p{page}"), _ok_meta()

    monkeypatch.setattr(triage, "_call_page_model", fake)

    started = time.monotonic()
    queued = triage.run_triage(uid, db_path=db_path, include_disabled=True)
    elapsed = time.monotonic() - started

    # The handler returned immediately, while the worker is still blocked.
    assert queued["status"] == "queued"
    assert elapsed < 1.0
    assert reached.wait(timeout=5)
    assert triage.get_run(int(queued["run_id"]), db_path=db_path)["status"] in ("running", "queued")

    gate.set()
    run = triage.wait_for_run(int(queued["run_id"]), db_path=db_path, timeout=10)
    assert run["status"] == "complete"
    assert len(triage.get_triage(uid, db_path=db_path)["documents"]) == 3


def test_running_run_is_interrupted_at_boot_and_resumes_only_unfinished(monkeypatch, tmp_path):
    db_path, uid = _fresh(tmp_path, pages=3)
    with recon_db.get_connection(db_path) as conn:
        run_id = conn.execute(
            "INSERT INTO invoice_triage_runs (upload_id, status, pages_total, pages_ok, pages_failed,"
            " settings_json, started_at) VALUES (?, 'running', 3, 1, 0, '{}', '2026-01-01')",
            (uid,),
        ).lastrowid
        # page 1 finished before the crash; pages 2-3 never recorded
        conn.execute(
            "INSERT INTO invoice_triage_pages (run_id, page, status, content_hash) VALUES (?, 1, 'ok', 'x')",
            (run_id,),
        )
        conn.commit()

    marked = triage.init_triage(db_path)
    assert marked == 1
    assert triage.get_run(run_id, db_path=db_path)["status"] == "interrupted"

    called: list[int] = []

    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        called.append(page)
        return _one_doc(f"p{page}"), _ok_meta()

    monkeypatch.setattr(triage, "_call_page_model", fake)

    res = triage.resume_triage(run_id, db_path=db_path)
    assert res["resume_pages"] == 2          # only 2 and 3
    triage.wait_for_run(run_id, db_path=db_path, timeout=10)
    assert sorted(called) == [2, 3]          # page 1 was NOT re-run (pages run concurrently)
    assert 1 not in called
    assert triage.get_run(run_id, db_path=db_path)["status"] == "complete"


def test_only_one_run_executes_at_a_time(tmp_path, monkeypatch):
    db_path, uid = _fresh(tmp_path, pages=2)
    order: list[str] = []
    started = threading.Event()

    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        order.append(f"start-{run_id}-{page}")
        started.set()
        time.sleep(0.15)
        return _one_doc(f"r{run_id}p{page}"), _ok_meta()

    monkeypatch.setattr(triage, "_call_page_model", fake)

    first = triage.run_triage(uid, db_path=db_path, include_disabled=True)
    started.wait(timeout=5)
    second = triage.run_triage(uid, db_path=db_path, include_disabled=True, force_supersede=True)

    # The second run's row is queued until the single worker is free.
    assert triage.get_run(int(second["run_id"]), db_path=db_path)["status"] == "queued"
    triage.wait_for_run(int(first["run_id"]), db_path=db_path, timeout=10)
    triage.wait_for_run(int(second["run_id"]), db_path=db_path, timeout=10)


# ===========================================================================
# B — gateway limits
# ===========================================================================


def test_vision_semaphore_caps_concurrency(monkeypatch, tmp_path):
    db_path = _ai_db(tmp_path, "sem.db")
    from src.settings import service as settings

    settings.set_setting_int("ai.vision.max_concurrency", 2, actor="t", db_path=db_path)
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk")

    lock = threading.Lock()
    active = {"n": 0, "max": 0}

    def fake_call_provider(spec, api_key, **kw):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        with lock:
            active["n"] -= 1
        return '{"ok": true}', 3

    monkeypatch.setattr(prov, "call_provider", fake_call_provider)

    def one():
        return gateway.call_touchpoint_text(
            "invoice_triage", "s", "u", images=[("image/png", b"x")], db_path=db_path,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: one(), range(6)))

    assert active["max"] == 2      # never more than the configured limit


def test_429_retries_same_leg_then_falls_back(monkeypatch, tmp_path):
    db_path = _ai_db(tmp_path, "retry.db")
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk")
    legs = ams.effective_leg("invoice_triage", db_path=db_path)
    primary_model = legs["primary"]["model"]
    fallback_model = legs["fallback"]["model"]

    calls: list[str] = []

    def fake_call_provider(spec, api_key, **kw):
        model = kw.get("model") or ""
        calls.append(model)
        if model == primary_model:
            raise prov.ProviderError("rate_limit|HTTP 429 slow down")
        return '{"ok": true}', 4

    monkeypatch.setattr(prov, "call_provider", fake_call_provider)

    from src.ingestion_ai import llm

    parsed, meta = gateway.call_touchpoint_json(
        "invoice_triage", "s", "u",
        retry_policy={"attempts": 3, "base_delay": 0.0, "max_delay": 0.0, "jitter": 0.0},
        db_path=db_path,
    )
    assert parsed == {"ok": True}
    assert meta["fallback_used"] is True
    # primary tried 3x on the SAME leg, THEN the fallback once
    assert calls == [primary_model, primary_model, primary_model, fallback_model]

    rows = ams.list_ai_call_log(db_path=db_path)
    outcomes = [r["outcome"] for r in rows]
    assert outcomes.count("transport_error") == 3
    assert "fallback_used" in outcomes
    assert [r["attempt"] for r in rows if r["outcome"] == "transport_error"] == [3, 2, 1]
    assert ams.list_fallback_events(db_path=db_path)[0]["resolved_model"] == fallback_model


def test_no_retry_policy_keeps_one_attempt_per_leg(monkeypatch, tmp_path):
    db_path = _ai_db(tmp_path, "noretry.db")
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk")
    legs = ams.effective_leg("invoice_triage", db_path=db_path)
    primary_model, fallback_model = legs["primary"]["model"], legs["fallback"]["model"]

    calls: list[str] = []

    def fake_call_provider(spec, api_key, **kw):
        calls.append(kw.get("model") or "")
        if kw.get("model") == primary_model:
            raise prov.ProviderError("rate_limit|HTTP 429")
        return '{"ok": true}', 4

    monkeypatch.setattr(prov, "call_provider", fake_call_provider)

    gateway.call_touchpoint_json("invoice_triage", "s", "u", db_path=db_path)   # no policy
    assert calls == [primary_model, fallback_model]     # ONE attempt on the primary


def test_triage_records_fallback_pages_and_model_per_page(monkeypatch, tmp_path):
    db_path, uid = _fresh(tmp_path, pages=3)

    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        return _one_doc(f"p{page}"), _ok_meta(model=f"m{page}", fallback=(page == 2))

    monkeypatch.setattr(triage, "_call_page_model", fake)
    queued = triage.run_triage(uid, db_path=db_path, include_disabled=True)
    run = triage.wait_for_run(int(queued["run_id"]), db_path=db_path, timeout=10)

    assert run["fallback_pages"] == 1
    pages = {p["page"]: p for p in triage.get_triage(uid, db_path=db_path)["pages"]}
    assert pages[2]["fallback_used"] is True
    assert pages[1]["model"] == "m1"


# ===========================================================================
# C — every call logged, including failures
# ===========================================================================


def test_billed_unparseable_reply_logs_parse_error_with_tokens(monkeypatch, tmp_path):
    db_path = _ai_db(tmp_path, "parse.db")
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk")
    monkeypatch.setattr(
        prov, "_http_post_json",
        lambda url, headers, payload, *, timeout: (
            '{"choices":[{"message":{"content":"sorry, not json"}}],'
            '"usage":{"prompt_tokens":21,"completion_tokens":9}}', 5),
    )

    from src.ingestion_ai.llm import LLMError

    with pytest.raises(LLMError):
        gateway.call_touchpoint_json("invoice_triage", "s", "u", db_path=db_path)

    rows = ams.list_ai_call_log(db_path=db_path)
    assert len(rows) >= 1
    row = rows[0]
    assert row["outcome"] == "parse_error"
    assert row["input_tokens"] == 21 and row["output_tokens"] == 9    # billed, and recorded


def test_successful_call_is_logged_with_outcome_and_attempt(monkeypatch, tmp_path):
    db_path = _ai_db(tmp_path, "ok.db")
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk")
    monkeypatch.setattr(prov, "call_provider", lambda spec, api_key, **kw: ('{"ok": true}', 2))

    gateway.call_touchpoint_json("invoice_triage", "s", "u", upload_id=7, db_path=db_path)
    row = ams.list_ai_call_log(db_path=db_path)[0]
    assert row["outcome"] == "success"
    assert row["attempt"] == 1
    assert row["upload_id"] == 7
    assert row["touchpoint_key"] == "invoice_triage"


def test_re_extract_preserves_history_in_ai_call_log(tmp_path, monkeypatch):
    db_path = _ai_db(tmp_path, "history.db")
    ie.init_invoice_extract(db_path)
    conn = recon_db.get_connection(db_path)
    try:
        uid = idb.create_upload(
            conn, client_id=1, filename="x.xlsx", file_ext="xlsx", source_format="excel",
            extraction_path="structured", batch_id=None, status="needs_review",
            duplicate_warning=False, actor="t", file_bytes=b"bytes", ai_used=True,
            ai_model="old-model", ai_latency_ms=1111,
        )
    finally:
        conn.close()

    captured: dict = {}

    def fake_upload_id_forward(**kw):
        captured.update(kw)
        return (
            [{"field_name": "invoice_number", "extracted_value": "9", "confidence": 99,
              "source_location": "t", "is_present": True}],
            "structured",
            {"ai_used": True, "model_id": "new-model", "latency_ms": 2222, "ai_error": None,
             "touchpoint": "invoice_extraction_structured"},
        )

    monkeypatch.setattr("src.invoice_extract.ai_extractor.extract_with_ai", fake_upload_id_forward)
    res = ie.re_extract_upload(uid, actor="t", db_path=db_path)

    # The re-extract attributed the call to the upload, so the log holds history.
    assert captured["upload_id"] == uid
    assert res["extraction_touchpoint"] == "invoice_extraction_structured"
    assert ie.get_upload(uid, db_path=db_path)["extraction_touchpoint"] == "invoice_extraction_structured"


# ===========================================================================
# D — vision capability guard
# ===========================================================================


def test_capability_guard_refuses_non_vision_model(monkeypatch, tmp_path):
    db_path = _ai_db(tmp_path, "vision.db")
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk")
    legs = ams.effective_leg("invoice_triage", db_path=db_path)

    # Tell the catalogue BOTH legs' models are TEXT-ONLY.
    conn = recon_db.get_connection(db_path)
    try:
        for leg in (legs["primary"], legs["fallback"]):
            conn.execute(
                "INSERT OR REPLACE INTO ai_provider_models (provider_key, model_id, modality, source) "
                "VALUES (?, ?, 'text->text', 'static')",
                (leg["provider"], leg["model"]),
            )
        conn.commit()
    finally:
        conn.close()

    def must_not_be_called(spec, api_key, **kw):
        raise AssertionError("a text-only model must not be sent images")

    monkeypatch.setattr(prov, "call_provider", must_not_be_called)

    with pytest.raises(gateway.TouchpointUnavailableError) as exc:
        gateway.call_touchpoint_text(
            "invoice_triage", "s", "u", images=[("image/png", b"x")], db_path=db_path,
        )
    assert "invoice_triage" in str(exc.value)
    assert legs["primary"]["model"] in str(exc.value)
    rows = ams.list_ai_call_log(db_path=db_path)
    assert [r["outcome"] for r in rows].count("rejected") == 2      # both legs refused


def test_capability_guard_allows_unknown_modality(monkeypatch, tmp_path):
    """An unknown modality is NOT refused (we only refuse what we KNOW is text)."""
    db_path = _ai_db(tmp_path, "vision2.db")
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk")
    monkeypatch.setattr(prov, "call_provider", lambda spec, api_key, **kw: ('{"ok": true}', 2))
    text, meta = gateway.call_touchpoint_text(
        "invoice_triage", "s", "u", images=[("image/png", b"x")], db_path=db_path,
    )
    assert text == '{"ok": true}'


def test_supports_images_helper():
    assert prov.supports_images("text+image->text") is True
    assert prov.supports_images("text->text") is False
    assert prov.supports_images("image->text") is True
    assert prov.supports_images(None) is None
    assert prov.supports_images("") is None
