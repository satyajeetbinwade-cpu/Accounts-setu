"""Tests for the Invoice Extraction Phase-1 triage foundation.

No live model calls anywhere: the ONE external seam (``triage._call_page_model``)
is stubbed, exactly as the task requires.

Run:

    venv/bin/python -m pytest tests/test_invoice_triage.py -v
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from src import db as recon_db
from src.invoice_extract import db as idb
from src.auth import access
from src.invoice_extract import service as ie
from src.invoice_extract import triage
from src.invoice_extract.triage_schema import ensure_triage_schema

FIXTURE_ENV = "SETU_TRIAGE_FIXTURE_PDF"
EXPECTED = Path(__file__).resolve().parent / "fixtures" / "invoice_extract" / "batch_apr2026_expected.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_pdf(pages: int = 3) -> bytes:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 72), f"Page {i + 1} content")
        p.insert_text((72, 120), "=" * 40)
        p.insert_text((72, 400), "SOME DOCUMENT TEXT 1234")
    data = doc.tobytes()
    doc.close()
    return data


def _fresh_upload(tmp_path, *, pages=3, name="bundle.pdf", db_name="poc.db") -> tuple[str, int]:
    db_path = str(tmp_path / db_name)
    ie.init_invoice_extract(db_path)
    conn = recon_db.get_connection(db_path)
    try:
        upload_id = idb.create_upload(
            conn, client_id=1, filename=name, file_ext="pdf", source_format="pdf",
            extraction_path="structured", batch_id=None, status="extracted",
            duplicate_warning=False, actor="t", file_bytes=_mk_pdf(pages),
        )
    finally:
        conn.close()
    return db_path, upload_id


def _stub(monkeypatch, responses: dict[int, dict], *, usage=None, fail_pages=()):
    """Stub the model seam. ``fail_pages`` pages raise; empty dict = invalid reply."""
    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        if page in fail_pages:
            raise RuntimeError("simulated provider failure")
        return responses[page], {
            "provider": "test", "model": "test-model", "latency_ms": 3,
            "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 5},
        }
    monkeypatch.setattr(triage, "_call_page_model", fake)


def _wait(run_id: int, db_path: str, timeout: float = 15.0) -> dict:
    return triage.wait_for_run(run_id, db_path=db_path, timeout=timeout)


def _run_and_wait(upload_id: int, db_path: str, **kw) -> dict:
    """Queue a run and block until it finishes — returns the RUN row."""
    queued = triage.run_triage(upload_id, db_path=db_path, include_disabled=True, **kw)
    return _wait(int(queued["run_id"]), db_path)


def _page_outcome(page: int, documents: list[dict], **kw) -> triage.PageOutcome:
    return triage.PageOutcome(
        page=page, status=kw.pop("status", "ok"),
        result={"rotation_deg": 0, "page_is_blank": bool(kw.pop("blank", False)), "documents": documents},
        **kw,
    )


def _doc(category="tax_invoice", label="X", boxes=None, cp=False, cn=False, flags=None):
    return {
        "category": category, "label": label, "continues_previous": cp, "continues_next": cn,
        "flags": flags or [], "boxes": boxes or [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}],
    }


# ===========================================================================
# Stitcher (pure — no DB)
# ===========================================================================


def test_stitch_multi_page_document_attaches_continuation():
    pages = [
        _page_outcome(1, [_doc(label="INV-1", cn=True)]),
        _page_outcome(2, [_doc(label="INV-1", cp=True)]),
    ]
    docs, unassigned = triage._stitch(pages)
    assert len(docs) == 1
    assert sorted({r["page"] for r in docs[0].regions}) == [1, 2]
    assert unassigned == []


def test_stitch_three_regions_on_one_page_become_three_documents():
    pages = [_page_outcome(1, [
        _doc(category="not_a_voucher", label="docket", boxes=[{"x": 0.09, "y": 0.03, "w": 0.86, "h": 0.26}]),
        _doc(category="no_gst_expense", label="slip", boxes=[{"x": 0.01, "y": 0.31, "w": 0.5, "h": 0.64}]),
        _doc(category="no_gst_expense", label="memo", boxes=[{"x": 0.54, "y": 0.31, "w": 0.42, "h": 0.49}]),
    ])]
    docs, unassigned = triage._stitch(pages)
    assert [d.ordinal for d in docs] == [1, 2, 3]
    assert all(len(d.regions) == 1 for d in docs)
    assert unassigned == []


def test_stitch_orphan_continuation_is_flagged_not_dropped():
    # A continuation on page 1 has no previous page to attach to -> orphan.
    pages = [_page_outcome(1, [_doc(label="Z", cp=True)])]
    docs, unassigned = triage._stitch(pages)
    assert len(docs) == 1
    assert "orphan_continuation" in docs[0].flags


def test_stitch_banner_only_continuation_attaches_when_marked():
    pages = [
        _page_outcome(1, [_doc(label="INV-1", cn=True)]),
        _page_outcome(2, []),
    ]
    # mark page 2 as a continuation with no documents of its own
    pages[1].result["continues_previous"] = True
    docs, unassigned = triage._stitch(pages)
    assert len(docs) == 1
    assert 2 in {r["page"] for r in docs[0].regions}   # covered by a full-page region
    assert unassigned == []


def test_stitch_banner_only_without_continuation_is_unassigned():
    pages = [_page_outcome(1, []), ]
    docs, unassigned = triage._stitch(pages)
    assert docs == []
    assert unassigned == [{"page": 1, "reason": "no_document_detected"}]


def test_stitch_category_disagreement_flags_but_keeps_first():
    pages = [
        _page_outcome(1, [_doc(category="tax_invoice", label="INV-1", cn=True)]),
        _page_outcome(2, [_doc(category="supporting", label="INV-1", cp=True)]),
    ]
    docs, _ = triage._stitch(pages)
    assert docs[0].category == "tax_invoice"
    assert "category_disagrees" in docs[0].flags


def test_stitch_failed_page_is_unassigned_with_reason():
    pages = [_page_outcome(1, [], status="failed", error="boom")]
    docs, unassigned = triage._stitch(pages)
    assert docs == []
    assert unassigned == [{"page": 1, "reason": "page_failed"}]


def test_stitch_blank_page_is_unassigned_blank():
    pages = [_page_outcome(1, [], blank=True)]
    docs, unassigned = triage._stitch(pages)
    assert unassigned == [{"page": 1, "reason": "blank"}]


def test_stitch_coverage_invariant_on_random_inputs():
    rng = random.Random(1234)
    for _ in range(200):
        n = rng.randint(1, 8)
        pages = []
        for p in range(1, n + 1):
            status = rng.choice(["ok", "ok", "ok", "failed"])
            docs = []
            if status == "ok":
                for _ in range(rng.randint(0, 3)):
                    docs.append(_doc(
                        category=rng.choice(list(triage.CATEGORIES)),
                        label=rng.choice(["A", "B", "C", ""]),
                        cp=rng.random() < 0.4, cn=rng.random() < 0.3,
                    ))
            pages.append(_page_outcome(p, docs, status=status, blank=(not docs and rng.random() < 0.3)))
        docs, unassigned = triage._stitch(pages)  # asserts coverage internally
        covered = {r["page"] for d in docs for r in d.regions}
        assert covered | {u["page"] for u in unassigned} == set(range(1, n + 1))


# ---------------------------------------------------------------------------
# A COPY of an earlier page: the model's flag AND support, never one alone
# ---------------------------------------------------------------------------


def test_stitch_duplicate_copy_with_hash_support_is_excluded_not_attached():
    """A copy the hash detector also sees becomes an EXCLUDED document: it is
    neither appended to the page it copies nor counted as extractable."""
    pages = [
        _page_outcome(1, [_doc(label="INV-1", cn=True)]),
        # The copy is labelled differently (a re-scan reads differently), so the
        # hash is the only support here.
        _page_outcome(2, [_doc(label="Airtel Wi-Fi Monthly Statement", cp=True,
                               flags=[triage.DUPLICATE_PAGE_FLAG])]),
    ]
    pages[1].duplicate_of_page = 1              # the hash detector's own flag
    docs, unassigned = triage._stitch(pages)
    assert sorted({r["page"] for r in docs[0].regions}) == [1]     # NOT attached
    assert len(docs) == 2
    assert docs[1].excluded_reason.startswith("duplicate of page 1")
    assert "hash" in docs[1].excluded_reason
    assert triage.DUPLICATE_PAGE_FLAG in docs[1].flags
    assert unassigned == []


def test_stitch_duplicate_copy_with_a_matching_label_is_excluded():
    """The same label on an earlier page is the other accepted support."""
    pages = [
        _page_outcome(1, [_doc(label="HF27071000796346", cn=True)]),
        _page_outcome(2, [_doc(label="HF27071000796346", cp=True,
                               flags=[triage.DUPLICATE_PAGE_FLAG])]),
    ]
    # No hash support at all — the label match carries the decision.
    docs, unassigned = triage._stitch(pages)
    assert len(docs) == 2
    assert docs[1].excluded_reason.startswith("duplicate of page 1")
    assert "label" in docs[1].excluded_reason
    # The category is NOT rewritten — the document is excluded, which is what
    # keeps it out of extraction and out of the counts.
    assert docs[1].category == "tax_invoice"
    assert unassigned == []


def test_stitch_hash_match_alone_is_not_a_duplicate():
    """Pages 2 and 3 of the real bundle: two DIFFERENT invoices the unreliable
    64-bit average hash calls near-identical. Nothing may act on that alone."""
    pages = [
        _page_outcome(2, [_doc(label="KMAP/2026-27/003")]),
        _page_outcome(3, [_doc(label="KMAP/2026-27/002")]),
    ]
    pages[1].duplicate_of_page = 2
    docs, _ = triage._stitch(pages)
    assert len(docs) == 2
    assert all(d.excluded_reason is None for d in docs)
    assert sorted({r["page"] for r in docs[1].regions}) == [3]
    assert docs[1].last_page == 3


def test_stitch_model_flag_alone_is_not_a_duplicate():
    """A copy claim with nothing behind it changes nothing: it stays a
    continuation, exactly as it did before this rule existed."""
    pages = [
        _page_outcome(1, [_doc(label="INV-1", cn=True)]),
        _page_outcome(2, [_doc(label="a completely different label", cp=True,
                               flags=[triage.DUPLICATE_PAGE_FLAG])]),
    ]
    # No hash flag, no matching label.
    docs, _ = triage._stitch(pages)
    assert len(docs) == 1
    assert sorted({r["page"] for r in docs[0].regions}) == [1, 2]


def test_stitch_duplicate_copy_is_recorded_once_and_covers_its_page(tmp_path):
    """An unlabelled copy claim still goes through the coverage invariant."""
    pages = [
        _page_outcome(1, [_doc(label="INV-1", cn=True)]),
        _page_outcome(2, [_doc(label="INV-1", cp=True,
                               flags=[triage.DUPLICATE_PAGE_FLAG])]),
        _page_outcome(3, [_doc(label="INV-2")]),
    ]
    docs, unassigned = triage._stitch(pages)       # asserts coverage internally
    assert [d.ordinal for d in docs] == [1, 2, 3]
    assert sorted({r["page"] for r in docs[1].regions}) == [2]   # the copy owns page 2
    assert docs[2].label == "INV-2" and docs[2].excluded_reason is None
    assert unassigned == []


def test_normalizer_keeps_the_duplicate_page_flag():
    """Flags are not whitelisted, but the pipeline must never lose this one."""
    rendered = triage.RenderedPage(page=1, png=b"", px_w=1000, px_h=1000, text_layer_chars=0, content_hash="0")
    _, _, docs = triage._normalize_page(
        {"documents": [{"category": "tax_invoice", "label": "INV-1",
                        "flags": [triage.DUPLICATE_PAGE_FLAG],
                        "boxes": [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}]}]},
        rendered,
    )
    assert triage.DUPLICATE_PAGE_FLAG in docs[0].flags


def test_prompt_states_the_statement_rule_and_the_duplicate_copy_rule():
    """Contract for the two rules added for the page 23 / 30-31 / 7-9 findings."""
    prompt = triage._SYSTEM_PROMPT
    # (i) statements are never invoices, whatever they list.
    assert "Statements, ledgers and passbooks" in prompt
    assert "NEVER tax_invoice/no_gst_expense" in prompt
    assert "even with charges or GST" in prompt
    assert "are supporting" in prompt
    # (ii) a copy of an earlier page is a flagged document, never a continuation.
    assert triage.DUPLICATE_PAGE_FLAG in prompt
    assert "COPY" in prompt
    assert "continues_previous=false" in prompt
    # The prompt must stay inside its length budget: it was 2012 characters
    # before these two rules, and the ceiling is +15%.
    assert len(prompt) <= 2314, f"system prompt grew to {len(prompt)} chars"


# ===========================================================================
# Box conversion + validation
# ===========================================================================


def test_canonical_box_fraction_form():
    assert triage._canonical_box({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, 1000, 1000) == (0.1, 0.2, 0.3, 0.4)


def test_canonical_box_grid_0_1000_form():
    box = triage._canonical_box({"x": 100, "y": 200, "w": 300, "h": 400}, 1000, 1000)
    assert box == (0.1, 0.2, 0.3, 0.4)


def test_canonical_box_list_corners_form():
    assert triage._canonical_box([0.1, 0.2, 0.4, 0.6], 1000, 1000) == (0.1, 0.2, 0.3, 0.4)


def test_canonical_box_rejects_zero_size_and_out_of_range():
    assert triage._canonical_box({"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.5}, 1000, 1000) is None
    assert triage._canonical_box({"x": -0.5, "y": 0.0, "w": 0.0001, "h": 0.5}, 1000, 1000) is None
    assert triage._canonical_box("nonsense", 1000, 1000) is None


def test_normalize_page_rejects_empty_non_blank_reply():
    rendered = triage.RenderedPage(page=1, png=b"", px_w=1000, px_h=1000, text_layer_chars=0, content_hash="0")
    with pytest.raises(triage._InvalidPageResponse):
        triage._normalize_page({"page_is_blank": False, "documents": []}, rendered)


def test_normalize_page_rejects_unknown_category_and_bad_box():
    rendered = triage.RenderedPage(page=1, png=b"", px_w=1000, px_h=1000, text_layer_chars=0, content_hash="0")
    with pytest.raises(triage._InvalidPageResponse):
        triage._normalize_page({"documents": [{"category": "banana", "boxes": [{"x": 0, "y": 0, "w": 1, "h": 1}]}]}, rendered)
    with pytest.raises(triage._InvalidPageResponse):
        triage._normalize_page({"documents": [{"category": "tax_invoice", "boxes": [{"x": 0.1, "y": 0.1, "w": 0, "h": 0.5}]}]}, rendered)


def test_normalize_page_accepts_blank_and_synonym():
    rendered = triage.RenderedPage(page=1, png=b"", px_w=1000, px_h=1000, text_layer_chars=0, content_hash="0")
    rot, blank, docs = triage._normalize_page({"page_is_blank": True, "documents": []}, rendered)
    assert blank is True and docs == []
    _, _, docs2 = triage._normalize_page({"documents": [{"category": "Tax Invoice", "boxes": [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}]}]}, rendered)
    assert docs2[0].category == "tax_invoice"


# ===========================================================================
# Rotation re-ask + duplicate hash
# ===========================================================================


def test_rotation_triggers_one_re_ask(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=1)
    calls = {"n": 0}

    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"rotation_deg": 90, "page_is_blank": False,
                     "documents": [_doc(category="supporting")]},
                    {"provider": "p", "model": "m", "latency_ms": 1, "usage": {}})
        return ({"rotation_deg": 0, "page_is_blank": False,
                 "documents": [_doc(category="supporting")]},
                {"provider": "p", "model": "m", "latency_ms": 1, "usage": {}})
    monkeypatch.setattr(triage, "_call_page_model", fake)

    _run_and_wait(uid, db_path)
    assert calls["n"] == 2
    page = triage.get_triage(uid, db_path=db_path)["pages"][0]
    assert page["rotation_deg"] == 90 and page["status"] == "ok"


def test_duplicate_hash_flags_earlier_page(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=2)
    # Force identical content hashes by stubbing the hash to a constant.
    monkeypatch.setattr(triage, "_average_hash", lambda grey: "00000000000000ff")
    _stub(monkeypatch, {
        1: {"rotation_deg": 0, "page_is_blank": False, "documents": [_doc()]},
        2: {"rotation_deg": 0, "page_is_blank": False, "documents": [_doc()]},
    })
    _run_and_wait(uid, db_path)
    pages = {p["page"]: p for p in triage.get_triage(uid, db_path=db_path)["pages"]}
    assert pages[2]["duplicate_of_page"] == 1
    assert pages[1]["duplicate_of_page"] is None


def test_hamming_helper():
    assert triage._hamming_hex("00", "00") == 0
    assert triage._hamming_hex("0f", "00") == 4
    assert triage._hamming_hex("", "00") == 999


def test_duplicate_copy_is_stored_excluded_with_a_decision_log_line(monkeypatch, tmp_path):
    """End to end: a flagged copy of an earlier page is persisted EXCLUDED, with
    its reason on the row and one decision line in the existing audit table."""
    db_path, uid = _fresh_upload(tmp_path, pages=2)
    _stub(monkeypatch, {
        1: {"rotation_deg": 0, "page_is_blank": False,
            "documents": [_doc(label="INV-1", cn=True)]},
        2: {"rotation_deg": 0, "page_is_blank": False,
            "documents": [_doc(label="INV-1", cp=True, flags=[triage.DUPLICATE_PAGE_FLAG])]},
    })
    _run_and_wait(uid, db_path)

    conn = recon_db.get_connection(db_path)
    try:
        docs = conn.execute(
            "SELECT ordinal, category, excluded, excluded_reason, flags_json FROM invoice_documents "
            "WHERE upload_id = ? ORDER BY ordinal", (uid,)).fetchall()
        log = conn.execute(
            "SELECT action, after_json FROM document_region_log "
            "WHERE action = 'page_duplicate_excluded'").fetchall()
    finally:
        conn.close()

    assert len(docs) == 2
    assert docs[0][2] == 0 and docs[0][3] is None          # the invoice itself is fine
    assert docs[1][2] == 1                                  # the copy: excluded
    assert docs[1][3].startswith("duplicate of page 1")
    assert triage.DUPLICATE_PAGE_FLAG in docs[1][4]
    assert len(log) == 1
    payload = json.loads(log[0][1])
    assert payload["page"] == 2
    assert payload["reason"].startswith("duplicate of page 1")


# ===========================================================================
# Runs: partial / retry / disabled / max_pages
# ===========================================================================


def test_failed_page_makes_run_partial_then_retry_fixes_it(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=3)
    responses = {p: {"rotation_deg": 0, "page_is_blank": False, "documents": [_doc(label=f"doc{p}")]} for p in (1, 2, 3)}

    _stub(monkeypatch, responses, fail_pages=(2,))
    result = _run_and_wait(uid, db_path)
    assert result["status"] == "partial"
    assert result["pages_failed"] == 1
    assert {"page": 2, "reason": "page_failed"} in triage.get_triage(uid, db_path=db_path)["unassigned"]

    _stub(monkeypatch, responses)
    retry = triage.retry_failed_pages(result["run_id"], db_path=db_path)
    assert retry["status"] == "complete"
    assert retry["retried"] == 1
    assert retry["triage"]["unassigned"] == []
    assert len(retry["triage"]["documents"]) == 3


def test_invalid_and_empty_replies_count_as_failures(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=2)
    _stub(monkeypatch, {
        1: {"documents": [{"category": "tax_invoice", "boxes": [{"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.5}]}]},
        2: {"page_is_blank": False, "documents": []},
    })
    result = _run_and_wait(uid, db_path)
    assert result["status"] == "failed"       # every page failed
    assert result["pages_ok"] == 0


def test_max_pages_refuses_never_truncates(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=4)
    import dataclasses

    base = triage.get_config(db_path=db_path)
    monkeypatch.setattr(triage, "get_config", lambda *, db_path=None: dataclasses.replace(base, max_pages=2))
    with pytest.raises(ie.InvoiceExtractError) as exc:
        triage.run_triage(uid, db_path=db_path, include_disabled=True)
    assert "never silently skips" in str(exc.value)


def test_triage_disabled_by_default(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=1)
    assert triage.get_config(db_path=db_path).enabled is False
    with pytest.raises(ie.InvoiceExtractError):
        triage.run_triage(uid, db_path=db_path)   # no include_disabled
    # and nothing was written
    assert triage.get_triage(uid, db_path=db_path)["run"] is None


def test_normal_upload_is_unaffected_and_records_coverage(monkeypatch, tmp_path):
    db_path = str(tmp_path / "poc.db")
    ie.init_invoice_extract(db_path)

    monkeypatch.setattr(
        "src.invoice_extract.ai_extractor.extract_with_ai",
        lambda **kw: ([{"field_name": k, "extracted_value": "1", "confidence": 99,
                        "source_location": "t", "is_present": True} for k in ("invoice_number",)],
                      "structured", {"ai_used": True, "model_id": "m", "latency_ms": 1, "ai_error": None}),
    )
    res = ie.upload_invoice(client_id=1, filename="x.pdf", file_bytes=_mk_pdf(3), batch_id=None,
                            actor="t", db_path=db_path)
    assert "coverage" in res
    assert res["coverage"]["pages_total"] == 3
    # A normal upload never runs triage.
    assert ie.get_upload(
        res["upload_id"], viewer=access.SYSTEM_VIEWER, db_path=db_path
    )["pages_total"] == 3
    conn = recon_db.get_connection(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM invoice_triage_runs").fetchone()[0] == 0
    finally:
        conn.close()


# ===========================================================================
# Re-triage supersede + protection
# ===========================================================================


def test_retriage_supersedes_old_documents(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=1)
    _stub(monkeypatch, {1: {"documents": [_doc(label="first")]}})
    _run_and_wait(uid, db_path)
    _stub(monkeypatch, {1: {"documents": [_doc(label="second"), _doc(label="third")]}})
    _run_and_wait(uid, db_path, force_supersede=True)

    docs = triage.get_triage(uid, db_path=db_path)["documents"]
    assert {d["label"] for d in docs} == {"second", "third"}   # only the latest run's docs are active
    conn = recon_db.get_connection(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM invoice_documents").fetchone()[0]
    finally:
        conn.close()
    assert total == 3  # history kept


def test_retriage_refuses_when_user_edited(monkeypatch, tmp_path):
    db_path, uid = _fresh_upload(tmp_path, pages=1)
    _stub(monkeypatch, {1: {"documents": [_doc(label="first")]}})
    _run_and_wait(uid, db_path)
    doc_id = triage.get_triage(uid, db_path=db_path)["documents"][0]["document_id"]

    conn = recon_db.get_connection(db_path)
    try:
        conn.execute("UPDATE invoice_documents SET category_source = 'user' WHERE document_id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ie.InvoiceExtractError) as exc:
        triage.run_triage(uid, db_path=db_path, include_disabled=True)
    assert "Refusing unless forced" in str(exc.value)

    # forced still works
    _run_and_wait(uid, db_path, force_supersede=True)


# ===========================================================================
# Migration idempotency
# ===========================================================================


def test_migration_is_idempotent_and_leaves_uploads_untouched(tmp_path):
    from src.migrations import framework
    from src.migrations.m0002_invoice_triage import MIGRATION

    db_path = tmp_path / "poc.db"
    ie.init_invoice_extract(str(db_path))
    conn = recon_db.get_connection(str(db_path))
    try:
        upload_id = idb.create_upload(
            conn, client_id=1, filename="keep.pdf", file_ext="pdf", source_format="pdf",
            extraction_path="structured", batch_id=None, status="extracted",
            duplicate_warning=False, actor="t", file_bytes=b"x",
        )
        before = conn.execute("SELECT COUNT(*) FROM invoice_uploads").fetchone()[0]

        ran = framework.run_migrations(conn, [MIGRATION], verbose=False)
        assert ran == [2]
        after = conn.execute("SELECT COUNT(*) FROM invoice_uploads").fetchone()[0]
        assert after == before
        assert idb.get_upload(conn, upload_id)["filename"] == "keep.pdf"
        for table in ("invoice_triage_runs", "invoice_documents", "document_regions", "ai_call_log"):
            assert framework.table_exists(conn, table)

        # A second run is a no-op.
        assert framework.run_migrations(conn, [MIGRATION]) == []
    finally:
        conn.close()


# ===========================================================================
# Token / cost accounting
# ===========================================================================


def _assign_triage_leg(db_path, *, provider="openrouter", model="acme/triage-model"):
    """Assign the triage touchpoint a model for this test.

    The seed deliberately leaves a page-reading touchpoint UNASSIGNED when the
    catalogue cannot price a vision model, so a test that actually calls the
    gateway has to say which model it is calling.
    """
    from src.ai_models import service as ams

    ams.set_assignment_legs(
        "invoice_triage", primary_provider=provider, primary_model=model,
        fallback_provider=provider, fallback_model=f"{model}-fallback",
        actor="test", db_path=db_path,
    )


def test_usage_logged_with_nulls_when_provider_reports_none(monkeypatch, tmp_path):
    from src.ai_models import service as ams, gateway, providers as prov

    db_path = str(tmp_path / "ai.db")
    ams.init_ai_models(db_path)
    _assign_triage_leg(db_path)

    def fake_call_provider(spec, api_key, *, model, system, user, images=None, max_tokens=0,
                           temperature=0.0, timeout=120, usage_sink=None):
        return '{"ok": true}', 7      # no usage block at all
    monkeypatch.setattr(prov, "call_provider", fake_call_provider)
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk-test")

    gateway.call_touchpoint_json("invoice_triage", "s", "u", db_path=db_path)
    rows = ams.list_ai_call_log(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] is None and rows[0]["output_tokens"] is None
    assert rows[0]["cost_usd"] is None


def test_usage_captured_through_the_real_provider_adapter(monkeypatch, tmp_path):
    from src.ai_models import service as ams, gateway, providers as prov

    db_path = str(tmp_path / "ai4.db")
    ams.init_ai_models(db_path)
    _assign_triage_leg(db_path)
    monkeypatch.setattr(
        prov, "_http_post_json",
        lambda url, headers, payload, *, timeout: (
            '{"choices":[{"message":{"content":"{\\"ok\\": true}"}}],'
            '"usage":{"prompt_tokens":12,"completion_tokens":3}}', 9),
    )
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk-test")

    text, meta = gateway.call_touchpoint_json("invoice_triage", "s", "u", db_path=db_path)
    assert meta["usage"] == {"input_tokens": 12, "output_tokens": 3}
    rows = ams.list_ai_call_log(db_path=db_path)
    assert rows[0]["input_tokens"] == 12 and rows[0]["output_tokens"] == 3


def test_logging_failure_never_breaks_a_call(monkeypatch, tmp_path):
    from src.ai_models import service as ams, gateway, providers as prov

    db_path = str(tmp_path / "ai2.db")
    ams.init_ai_models(db_path)
    _assign_triage_leg(db_path)
    monkeypatch.setattr(prov, "call_provider",
                        lambda spec, api_key, **k: ('{"ok": true}', 3))
    monkeypatch.setattr(ams, "resolve_provider_secret", lambda *a, **k: "sk-test")
    monkeypatch.setattr(ams, "log_ai_call", lambda **k: (_ for _ in ()).throw(RuntimeError("db down")))

    text, meta = gateway.call_touchpoint_json("invoice_triage", "s", "u", db_path=db_path)
    assert text == {"ok": True}


def test_extract_usage_handles_both_conventions():
    from src.ai_models import providers as prov

    spec = prov.PROVIDER_SPECS["openrouter"]
    assert prov.extract_usage(spec, '{"usage":{"prompt_tokens":10,"completion_tokens":4}}') == {
        "input_tokens": 10, "output_tokens": 4}
    anth = prov.PROVIDER_SPECS["anthropic"]
    assert prov.extract_usage(anth, '{"usage":{"input_tokens":7,"output_tokens":2}}') == {
        "input_tokens": 7, "output_tokens": 2}
    assert prov.extract_usage(spec, "not json") == {}


def test_triage_touchpoint_seeded_with_same_models_as_visual(tmp_path):
    from src.ai_models import service as ams

    db_path = str(tmp_path / "ai3.db")
    ams.init_ai_models(db_path)
    triage_leg = ams.effective_leg("invoice_triage", db_path=db_path)
    visual_leg = ams.effective_leg("invoice_extraction_visual", db_path=db_path)
    assert triage_leg["primary"]["model"] == visual_leg["primary"]["model"]
    assert triage_leg["fallback"]["model"] == visual_leg["fallback"]["model"]


# ===========================================================================
# Legacy honesty (Step 3)
# ===========================================================================


def test_partial_read_warns_and_requires_acknowledgement(tmp_path):
    db_path = str(tmp_path / "poc.db")
    ie.init_invoice_extract(db_path)
    conn = recon_db.get_connection(db_path)
    try:
        upload_id = idb.create_upload(
            conn, client_id=1, filename="big.pdf", file_ext="pdf", source_format="pdf",
            extraction_path="visual", batch_id=None, status="needs_review",
            duplicate_warning=False, actor="t", pages_examined=5, pages_total=31,
        )
    finally:
        conn.close()

    cov = ie.upload_coverage(upload_id, db_path=db_path)
    assert cov["incomplete"] is True
    assert ie.coverage_warning(upload_id, db_path=db_path) == "Only pages 1-5 of 31 were read."

    with pytest.raises(ie.InvoiceExtractError) as exc:
        ie.confirm_upload(upload_id, actor="t", db_path=db_path)
    assert "tick the acknowledgement" in str(exc.value)

    ie.confirm_upload(upload_id, actor="t", acknowledge_coverage=True, db_path=db_path)
    assert ie.get_upload(
        upload_id, viewer=access.SYSTEM_VIEWER, db_path=db_path
    )["status"] == "confirmed"


def test_full_read_needs_no_acknowledgement(tmp_path):
    db_path = str(tmp_path / "poc.db")
    ie.init_invoice_extract(db_path)
    conn = recon_db.get_connection(db_path)
    try:
        upload_id = idb.create_upload(
            conn, client_id=1, filename="ok.pdf", file_ext="pdf", source_format="pdf",
            extraction_path="structured", batch_id=None, status="extracted",
            duplicate_warning=False, actor="t", pages_examined=3, pages_total=3,
        )
    finally:
        conn.close()
    assert ie.upload_coverage(upload_id, db_path=db_path)["incomplete"] is False
    assert ie.coverage_warning(upload_id, db_path=db_path) == ""
    ie.confirm_upload(upload_id, actor="t", db_path=db_path)
    assert ie.get_upload(
        upload_id, viewer=access.SYSTEM_VIEWER, db_path=db_path
    )["status"] == "confirmed"


# ===========================================================================
# Scoring comparison (no live calls)
# ===========================================================================


def test_iou_and_compare():
    assert triage._iou([0, 0, 1, 1], [0, 0, 1, 1]) == 1.0
    assert triage._iou([0, 0, 1, 1], [2, 2, 3, 3]) == 0.0

    triage_result = {
        "pages": [{"page": 1, "rotation_deg": 0, "duplicate_of_page": None},
                  {"page": 2, "rotation_deg": 0, "duplicate_of_page": 1}],
        "unassigned": [{"page": 2, "reason": "blank"}],
        "documents": [
            {"ordinal": 1, "category": "not_a_voucher", "label": "docket",
             "regions": [{"page": 1, "x": 0.1, "y": 0.03, "w": 0.84, "h": 0.26}]},
        ],
    }
    expected = triage._Expected(data={
        "documents_total": 1,
        "documents": [{"ordinal": 1, "category": "not_a_voucher", "pages": [1],
                       "acceptable": ["not_a_voucher"]}],
        "page1_boxes": [{"label": "docket", "box": [0.09, 0.03, 0.95, 0.29]}],
        "duplicate_pages": {"2": 1},
        "rotated_pages": [],
    }, path=Path("x"))
    analysis = triage._compare(triage_result, {"input_tokens": 10, "output_tokens": 5, "cost_usd": None, "status": "complete"}, expected)
    assert analysis["failed"] == 0
    iou_check = next(c for c in analysis["checks"] if "page-1" in c["name"])
    assert iou_check["pass"] is True


# ===========================================================================
# Fixture gating
# ===========================================================================


def test_expected_fixture_is_provisional_and_well_formed():
    data = json.loads(EXPECTED.read_text())
    assert data["verified"] is False
    assert data["pages_total"] == 31
    assert data["documents_total"] == 21
    assert len(data["documents"]) == 21
    assert data["duplicate_pages"] == {"29": 28}
    assert sum(1 for d in data["documents"] if d.get("excluded")) == 1
    # No personal data beyond short labels: labels are short strings.
    assert all(len(str(d.get("label", ""))) <= 60 for d in data["documents"])


def test_fixture_pdf_is_optional_and_gitignored():
    import os

    path = os.environ.get(FIXTURE_ENV)
    if not path:
        pytest.skip(f"{FIXTURE_ENV} not set — the real client bundle is never committed")
    assert Path(path).exists()


# ===========================================================================
# Schema module creates everything (fresh-DB path used by tests + init_*)
# ===========================================================================


def test_ensure_triage_schema_creates_all_tables(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    try:
        conn.executescript("CREATE TABLE invoice_uploads (upload_id INTEGER PRIMARY KEY);")
        conn.executescript("CREATE TABLE extracted_invoice_fields (field_id INTEGER PRIMARY KEY);")
        ensure_triage_schema(conn)
        ensure_triage_schema(conn)  # idempotent
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("invoice_triage_runs", "invoice_triage_pages", "invoice_documents",
                  "document_regions", "document_region_log"):
            assert t in names
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invoice_uploads)")}
        assert {"pages_examined", "pages_total", "text_truncated"} <= cols
        assert "document_id" in {r[1] for r in conn.execute("PRAGMA table_info(extracted_invoice_fields)")}
    finally:
        conn.close()
