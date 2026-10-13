"""Tests for the review screen's backend services. NO network.

``triage._call_page_model`` and ``extract._call_extract_model`` are the only two
external seams, and both are stubbed. Every database is a scratch DB created by
``bootstrap.run_migrations`` in a temp folder — the real ``db/poc.db`` and the
real ``data/`` tree are never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import bootstrap
from src.ai_models import defaults as ai_defaults
from src.ai_models import service as ai_models
from src.invoice_extract import confidence as confidence_mod
from src.invoice_extract import crops as crops_mod
from src.invoice_extract import doc_actions, extract as ex, pipeline, review_api
from src.invoice_extract import rules as rules_mod, triage as triage_mod
from src.invoice_extract.service import InvoiceExtractError

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """A scratch app DB with every schema, seed and migration applied."""
    path = str(tmp_path / "review.db")
    bootstrap.run_migrations(db_path=path, data_root=tmp_path / "data")
    return path


def _enable(db: str) -> None:
    """Turn on both flags: the v2 pipeline AND triage."""
    from src.settings import service as settings

    settings.set_setting_str(ex.KEY_ENABLED, "true", actor="test", db_path=db)
    settings.set_setting_str(triage_mod.KEY_ENABLED, "true", actor="test", db_path=db)


def _mk_pdf(path: Path, pages: int = 1) -> Path:
    """A synthetic page with ENOUGH ink that triage's blank guard keeps it.

    The guard is deterministic (fewer than 0.1% dark pixels is blank), so a
    single small text line is deliberately not enough here.
    """
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), f"TAX INVOICE Page {i + 1}", fontsize=18)
        page.insert_text((72, 110), "invoice_total 1,180.00")
        page.insert_text((72, 140), "issuer 07AAECK9054D1ZK", fontsize=12)
        page.draw_rect(fitz.Rect(60, 60, 520, 200), fill=(0.1, 0.1, 0.1))
    path.write_bytes(doc.tobytes())
    doc.close()
    return path


def _seed_upload(db: str, *, documents: int = 1, category: str = "tax_invoice",
                 pdf_bytes: bytes | None = None) -> tuple[int, list[int]]:
    """One upload, N documents (one region each), and the file bytes."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_uploads (client_id, filename, file_ext, source_format, "
            "extraction_path, status, duplicate_warning, created_at, created_by) "
            "VALUES (1, 'bundle.pdf', 'pdf', 'pdf', 'structured', 'extracted', 0, 'now', 'test')"
        )
        upload_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        if pdf_bytes is not None:
            conn.execute("UPDATE invoice_uploads SET file_bytes = ? WHERE upload_id = ?",
                         (pdf_bytes, upload_id))
        conn.execute(
            "INSERT INTO invoice_triage_runs (upload_id, status, pages_total, pages_ok, "
            "started_at, finished_at) VALUES (?, 'complete', 1, 1, 'now', 'now')",
            (upload_id,),
        )
        run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        doc_ids: list[int] = []
        for ordinal in range(1, documents + 1):
            conn.execute(
                "INSERT INTO invoice_documents (upload_id, run_id, ordinal, category, label, "
                "excluded, is_active, created_at) VALUES (?, ?, ?, ?, ?, 0, 1, 'now')",
                (upload_id, run_id, ordinal, category, f"doc{ordinal}"),
            )
            document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO document_regions (document_id, page, x, y, w, h, order_in_doc, "
                "source, is_active) VALUES (?, 1, 0.05, 0.05, 0.85, 0.40, 0, 'triage', 1)",
                (document_id,),
            )
            doc_ids.append(document_id)
        conn.execute(
            "INSERT INTO invoice_triage_pages (run_id, page, status, rotation_deg, "
            "is_blank, text_layer_chars, attempts, fallback_used) "
            "VALUES (?, 1, 'ok', 90, 0, 40, 1, 0)", (run_id,),
        )
        conn.commit()
        return upload_id, doc_ids
    finally:
        conn.close()


def _add_extraction(db: str, document_id: int, *, status: str = "complete") -> int:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_doc_extractions (document_id, status, is_active) "
            "VALUES (?, ?, 1)", (document_id, status),
        )
        extraction_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        return extraction_id
    finally:
        conn.close()


def _add_field(db: str, extraction_id: int, document_id: int, key: str,
               value: str | None = None) -> int:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_doc_fields (extraction_id, document_id, field_key, status, "
            "value_a, value_b, reads_agree, is_handwritten, in_region) "
            "VALUES (?, ?, ?, ?, ?, ?, 'exact', 0, 1)",
            (extraction_id, document_id, key, "read" if value else "not_printed", value, value),
        )
        field_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        return field_id
    finally:
        conn.close()


def _set_upload_status(db: str, upload_id: int, status: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE invoice_uploads SET status = ? WHERE upload_id = ?",
                     (status, upload_id))
        conn.commit()
    finally:
        conn.close()


def _log_actions(db: str, document_id: int) -> list[str]:
    return [row["action"] for row in doc_actions.list_document_log(document_id, db_path=db)]


# ===========================================================================
# 1. Document actions
# ===========================================================================


def test_set_category_logs_the_change_and_marks_the_extraction_for_a_run(db):
    _enable(db)
    upload_id, (document_id,) = _seed_upload(db)
    extraction_id = _add_extraction(db, document_id)

    result = doc_actions.set_category(
        document_id, "no_gst_expense", actor="reviewer", reason="no GST charged", db_path=db,
    )
    assert result["category_source"] == "user"
    assert result["needs_a_run"] is True

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT category, category_source, excluded FROM invoice_documents "
            "WHERE document_id = ?", (document_id,),
        ).fetchone()
        assert row == ("no_gst_expense", "user", 0)
        status = conn.execute(
            "SELECT status FROM invoice_doc_extractions WHERE extraction_id = ?",
            (extraction_id,),
        ).fetchone()[0]
        # Marked as needing a run — and NOT started: no worker was asked to run.
        assert status == "queued"
    finally:
        conn.close()

    log = doc_actions.list_document_log(document_id, db_path=db)
    assert log[0]["action"] == "set_category"
    assert log[0]["actor"] == "reviewer"
    assert log[0]["reason"] == "no GST charged"
    assert "no_gst_expense" in log[0]["after_json"]
    assert "tax_invoice" in log[0]["before_json"]


def test_set_category_refuses_a_bad_category_and_a_missing_reason(db):
    _enable(db)
    _upload_id, (document_id,) = _seed_upload(db)

    with pytest.raises(InvoiceExtractError, match="is not a category"):
        doc_actions.set_category(document_id, "nice_try", actor="r", reason="x", db_path=db)
    with pytest.raises(InvoiceExtractError, match="needs a reason"):
        doc_actions.set_category(document_id, "ticket", actor="r", reason="  ", db_path=db)
    # Neither attempt wrote anything.
    assert doc_actions.list_document_log(document_id, db_path=db) == []


def test_not_a_voucher_excludes_but_leaving_it_does_not_re_include(db):
    _enable(db)
    _upload_id, (document_id,) = _seed_upload(db)

    doc_actions.set_category(document_id, "not_a_voucher", actor="r",
                             reason="a courier docket", db_path=db)
    assert doc_actions.exclude_document is not None      # module surface intact
    conn = sqlite3.connect(db)
    try:
        excluded, reason = conn.execute(
            "SELECT excluded, excluded_reason FROM invoice_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    finally:
        conn.close()
    assert excluded == 1 and "courier docket" in reason

    # Moving OUT of the excluded category is NOT an un-exclusion.
    doc_actions.set_category(document_id, "tax_invoice", actor="r",
                             reason="actually a tax invoice", db_path=db)
    conn = sqlite3.connect(db)
    try:
        excluded = conn.execute(
            "SELECT excluded FROM invoice_documents WHERE document_id = ?", (document_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert excluded == 1, "a category change must not silently re-include a document"

    # Only include_document does that.
    result = doc_actions.include_document(document_id, actor="r", db_path=db)
    assert result["excluded"] is False and result["changed"] is True


def test_exclude_and_include_log_and_say_what_inclusion_does_not_do(db):
    _enable(db)
    _upload_id, (document_id,) = _seed_upload(db, category="supporting")

    with pytest.raises(InvoiceExtractError, match="needs a reason"):
        doc_actions.exclude_document(document_id, actor="r", reason="", db_path=db)

    doc_actions.exclude_document(document_id, actor="r", reason="not ours", db_path=db)
    result = doc_actions.include_document(document_id, actor="r", db_path=db)
    # A non-extractable category is allowed back into the review, and the result
    # says plainly that nothing will be extracted from it.
    assert result["changed"] is True
    assert result["note"] and "not extracted" in result["note"]

    assert _log_actions(db, document_id)[:2] == ["included", "excluded"]


def test_every_document_action_refuses_on_an_exported_upload(db):
    _enable(db)
    upload_id, (document_id,) = _seed_upload(db)
    _set_upload_status(db, upload_id, "exported")

    with pytest.raises(InvoiceExtractError, match="immutable export batch"):
        doc_actions.set_category(document_id, "ticket", actor="r", reason="x", db_path=db)
    with pytest.raises(InvoiceExtractError, match="immutable export batch"):
        doc_actions.exclude_document(document_id, actor="r", reason="x", db_path=db)
    with pytest.raises(InvoiceExtractError, match="immutable export batch"):
        doc_actions.include_document(document_id, actor="r", db_path=db)
    with pytest.raises(InvoiceExtractError, match="immutable export batch"):
        doc_actions.mark_reviewed(document_id, actor="r", db_path=db)

    assert doc_actions.list_document_log(document_id, db_path=db) == []


def test_the_actions_refuse_when_the_v2_flag_is_off(db):
    """A DB with everything seeded but the flag OFF."""
    _upload_id, (document_id,) = _seed_upload(db)
    with pytest.raises(InvoiceExtractError, match="invoice_extract.v2.enabled"):
        doc_actions.set_category(document_id, "ticket", actor="r", reason="x", db_path=db)
    with pytest.raises(InvoiceExtractError, match="invoice_extract.v2.enabled"):
        review_api.get_upload_progress(1, db_path=db)


# ===========================================================================
# 2. Reviewed
# ===========================================================================


def test_mark_reviewed_refuses_while_the_review_model_asks_a_question(db):
    _enable(db)
    _upload_id, (document_id,) = _seed_upload(db)
    extraction_id = _add_extraction(db, document_id)
    # A missing invoice_total is a required field on a tax invoice.
    _add_field(db, extraction_id, document_id, "invoice_date", "1-Apr-26")
    rules_mod.run_rules(extraction_id, actor="test", db_path=db)

    model = rules_mod.get_review_model(document_id, db_path=db)
    assert model["needs_you"], "the fixture must produce something to decide"

    with pytest.raises(InvoiceExtractError, match="needing a decision"):
        doc_actions.mark_reviewed(document_id, actor="r", db_path=db)
    assert doc_actions.review_status(document_id, db_path=db) is None


def test_mark_reviewed_snapshots_the_formula_and_clears_on_a_correction(db):
    _enable(db)
    _upload_id, (document_id,) = _seed_upload(db, category="supporting")
    extraction_id = _add_extraction(db, document_id)
    field_id = _add_field(db, extraction_id, document_id, "invoice_date", "1-Apr-26")

    # A supporting document asks nothing, so it can be signed off.
    marked = doc_actions.mark_reviewed(document_id, actor="reviewer", note="checked",
                                       db_path=db)
    assert marked["reviewed_by"] == "reviewer"
    assert marked["reviewed_threshold"] == 90.0
    assert marked["reviewed_formula_version"]
    assert doc_actions.is_reviewed(document_id, db_path=db) is True

    # A NEW CORRECTION un-signs it, and says so in the log.
    confidence_mod.record_correction(field_id, "confirmed_as_is", actor="reviewer", db_path=db)
    assert doc_actions.review_status(document_id, db_path=db) is None
    log = doc_actions.list_document_log(document_id, db_path=db)
    cleared = [row for row in log if row["action"] == "review_cleared"]
    assert cleared and cleared[0]["field_key"] == "invoice_date"
    # The human reason names the correction; the machine-readable trigger is in
    # the before payload, so a report can count clears by cause.
    assert "correction on invoice_date" in (cleared[0]["reason"] or "")
    assert "field_correction" in cleared[0]["before_json"]

    # And it can be signed off again, then withdrawn by hand.
    doc_actions.mark_reviewed(document_id, actor="reviewer", db_path=db)
    doc_actions.unmark_reviewed(document_id, actor="reviewer", reason="found a problem",
                                db_path=db)
    assert doc_actions.is_reviewed(document_id, db_path=db) is False
    assert _log_actions(db, document_id).count("reviewed") == 2
    assert _log_actions(db, document_id).count("review_cleared") == 2


def test_a_re_extraction_clears_a_standing_review(db, monkeypatch):
    _enable(db)
    pdf = _mk_pdf(Path(db).parent / "b.pdf")
    upload_id, (document_id,) = _seed_upload(db, pdf_bytes=pdf.read_bytes())
    _add_extraction(db, document_id)
    # The review row is inserted directly: this test is about the CLEARING path,
    # and a tax invoice cannot pass the review gate without a settled direction.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_document_review (document_id, reviewed_by, reviewed_at, "
            "reviewed_formula_version, reviewed_threshold) VALUES (?, 'reviewer', 'now', 'v1', 90)",
            (document_id,),
        )
        conn.commit()
    finally:
        conn.close()
    assert doc_actions.is_reviewed(document_id, db_path=db) is True

    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])
    monkeypatch.setattr(ex, "_call_extract_model", lambda **kw: ({"fields": []}, {}))
    ex.start_extraction(upload_id, actor="test", db_path=db, force=True)
    ex.wait_for_extraction(upload_id, db_path=db, timeout=60)

    assert doc_actions.is_reviewed(document_id, db_path=db) is False
    cleared = [row for row in doc_actions.list_document_log(document_id, db_path=db)
               if row["action"] == "review_cleared"]
    assert cleared and cleared[0]["reason"] == "the document was extracted again"
    assert "re_extraction" in cleared[0]["before_json"]


# ===========================================================================
# 3. Progress
# ===========================================================================


def test_get_upload_progress_combines_the_three_sources(db):
    _enable(db)
    upload_id, doc_ids = _seed_upload(db, documents=2)
    _add_extraction(db, doc_ids[0])
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE invoice_triage_runs SET pages_total = 31, pages_ok = 29, "
                     "pages_failed = 2, fallback_pages = 3 WHERE upload_id = ?", (upload_id,))
        conn.commit()
    finally:
        conn.close()

    progress = review_api.get_upload_progress(upload_id, db_path=db)
    assert progress["triage"]["status"] == "complete"
    assert progress["triage"]["pages_total"] == 31
    assert progress["triage"]["pages_ok"] == 29
    assert progress["triage"]["pages_failed"] == 2
    assert progress["triage"]["fallback_pages"] == 3
    assert progress["extraction"]["total"] == 1
    assert progress["review"]["documents"] == 2
    assert progress["review"]["to_review"] == 2
    assert progress["review"]["reviewed"] == 0
    assert progress["review"]["excluded"] == 0
    # An extraction exists but the rules have not run, so both documents are
    # still questions.
    assert progress["review"]["with_needs_you"] == 2


def test_get_upload_progress_counts_the_fallback_pages_and_reviewed(db):
    _enable(db)
    upload_id, (document_id,) = _seed_upload(db, category="supporting")
    doc_actions.mark_reviewed(document_id, actor="r", db_path=db)

    progress = review_api.get_upload_progress(upload_id, db_path=db)
    assert progress["review"]["documents"] == 1
    assert progress["review"]["to_review"] == 0     # supporting is not extracted
    assert progress["triage"]["fallback_pages"] == 0


# ===========================================================================
# 4. Page images
# ===========================================================================


def test_page_image_applies_the_triage_rotation_and_is_cached(db, monkeypatch):
    _enable(db)
    pdf = _mk_pdf(Path(db).parent / "rot.pdf")
    _seed_upload(db, pdf_bytes=pdf.read_bytes())

    calls = {"n": 0}
    real = crops_mod.render_upright_page

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(crops_mod, "render_upright_page", counted)

    first = review_api.get_page_image(1, 1, db_path=db)
    assert first[:4] == b"\x89PNG"
    assert calls["n"] == 1
    # The page row says 90 degrees, so the image handed back is the ROTATED one:
    # taller than wide would mean the rotation was ignored (the source page is
    # portrait, so a 90-degree upright page is landscape).
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(first)) as img:
        assert img.size[0] > img.size[1], "the rotation from the triage run was not applied"

    # Second call: identical bytes, and NOT re-rendered.
    second = review_api.get_page_image(1, 1, db_path=db)
    assert second == first
    assert calls["n"] == 1, "a cached page must never be rendered again"

    # A different dpi is a different cache entry.
    review_api.get_page_image(1, 1, dpi=200, db_path=db)
    assert calls["n"] == 2

    assert review_api.clear_page_cache(upload_id=1) >= 2


def test_page_info_reports_upright_dimensions_and_duplicates(db):
    _enable(db)
    pdf = _mk_pdf(Path(db).parent / "info.pdf")
    _seed_upload(db, pdf_bytes=pdf.read_bytes())

    info = review_api.get_page_info(1, db_path=db)
    assert len(info) == 1
    page = info[0]
    assert page["page"] == 1
    assert page["rotation_deg"] == 90
    # A portrait page turned 90 degrees upright is LANDSCAPE.
    assert page["width"] > page["height"]
    assert page["duplicate_of_page"] is None


def test_page_numbers_and_cache_keys_are_guarded(db):
    _enable(db)
    pdf = _mk_pdf(Path(db).parent / "guard.pdf")
    _seed_upload(db, pdf_bytes=pdf.read_bytes())

    for bad in ("../2", "1;rm -rf /", "two", "", 0, -3, 999999, True, None):
        with pytest.raises(InvoiceExtractError, match="not a page number|out of range"):
            review_api.get_page_image(1, bad, db_path=db)

    # "2" IS accepted (a form value arrives as a string).
    assert review_api.get_page_image(1, "1", db_path=db)[:4] == b"\x89PNG"

    # And the cache path itself refuses anything that is not a plain key.
    from src import data_paths

    for bad_key in ("../../etc/passwd", "a/b", "", "/abs", "x" * 130):
        with pytest.raises(ValueError):
            data_paths.page_cache_path(bad_key)


def test_the_page_cache_is_a_reserved_folder_under_the_data_root(db):
    _enable(db)
    from src import data_paths

    pdf = _mk_pdf(Path(db).parent / "cache.pdf")
    _seed_upload(db, pdf_bytes=pdf.read_bytes())
    review_api.get_page_image(1, 1, db_path=db)

    directory = data_paths.page_cache_dir()
    assert directory.parent == data_paths.DATA_ROOT
    assert directory.name.startswith("_")
    # A reserved folder is never reported as a stray client folder.
    assert directory.name not in data_paths.list_unmapped_folders()
    review_api.clear_page_cache(upload_id=1)


# ===========================================================================
# 5. The pipeline
# ===========================================================================


def _stub_triage_page(monkeypatch, *, category="tax_invoice", calls=None):
    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        if calls is not None:
            calls.append(page)
        return (
            {
                "rotation_deg": 0,
                "page_is_blank": False,
                "documents": [{
                    "category": category,
                    "label": f"doc on page {page}",
                    "box": {"x": 0.05, "y": 0.05, "w": 0.85, "h": 0.40},
                    "evidence": "TAX INVOICE",
                }],
            },
            {"provider": "openrouter", "model": "stub", "input_tokens": 1, "output_tokens": 1},
        )

    monkeypatch.setattr(triage_mod, "_call_page_model", fake)


def test_pipeline_chains_triage_into_extraction(db, monkeypatch):
    _enable(db)
    _seed_upload(db, documents=0, pdf_bytes=_mk_pdf(Path(db).parent / "chain.pdf").read_bytes())
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])
    _stub_triage_page(monkeypatch)
    monkeypatch.setattr(
        ex, "_call_extract_model",
        lambda **kw: ({"fields": [{"key": "invoice_total", "value": "1,180.00",
                                   "handwritten": False, "region": 1,
                                   "box": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.05}}]}, {}),
    )

    started = pipeline.start_v2_pipeline(1, actor="test", include_disabled=True, db_path=db)
    assert started["chained"] is True
    assert pipeline.pending_chains().get(started["run_id"]) is not None

    # The chain runs on the triage worker, so the extraction starts by itself.
    triage_mod.wait_for_run(started["run_id"], db_path=db, timeout=120)
    # ...and it is the CHAIN's persisted state that says when the extraction has
    # started and finished — waiting on the run row alone is a race (the run row
    # is finalised before the chain hands over).
    chain_end = pipeline.wait_for_chain(started["run_id"], db_path=db, timeout=120)
    assert chain_end in ("complete", "failed"), f"chain ended in {chain_end!r}"
    ex.wait_for_extraction(1, db_path=db, timeout=120)

    info = ex.get_extraction(1, db_path=db)
    assert info["extractions"], "the pipeline did not chain into an extraction"
    assert info["extractions"][0]["status"] in ("complete", "partial")
    # The chain entry is consumed, not left behind.
    assert pipeline.pending_chains() == {}


def test_the_chain_is_not_left_behind_when_nothing_is_extractable(db, monkeypatch):
    _enable(db)
    _seed_upload(db, documents=0, pdf_bytes=_mk_pdf(Path(db).parent / "none.pdf").read_bytes())
    _stub_triage_page(monkeypatch, category="ticket")

    started = pipeline.start_v2_pipeline(1, actor="test", include_disabled=True, db_path=db)
    triage_mod.wait_for_run(started["run_id"], db_path=db, timeout=120)
    # The run itself is fine; the chain simply had nothing to extract.
    assert pipeline.pending_chains() == {}
    assert not ex.get_extraction(1, db_path=db)["extractions"]


def test_start_v2_pipeline_respects_the_flag(db):
    _seed_upload(db, pdf_bytes=_mk_pdf(Path(db).parent / "off.pdf").read_bytes())
    with pytest.raises(InvoiceExtractError, match="invoice_extract.v2.enabled"):
        pipeline.start_v2_pipeline(1, actor="test", db_path=db)


def test_run_chain_starts_the_extraction_and_reports_the_cap(db, monkeypatch):
    """The chain's own contract, called directly so its answer is observable.

    It must HAND the cost cap on (the job enforces it), not bypass it, and it
    must consume itself so a second call cannot start the pass twice.
    """
    _enable(db)
    pdf = _mk_pdf(Path(db).parent / "capchain.pdf")
    upload_id, _doc_ids = _seed_upload(db, pdf_bytes=pdf.read_bytes())
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])
    monkeypatch.setattr(ex, "_call_extract_model", lambda **kw: ({"fields": []}, {}))

    pipeline.register_chain(0, upload_id, actor="t", db_path=db)
    out = pipeline.run_chain(0, actor="t", db_path=db)
    assert out["started"] is True
    assert out["documents"] == 1
    # The cap is REPORTED and handed to the job — never raised or ignored here.
    assert out["extraction"]["max_cost_usd"] == 2.0
    assert out["extraction"]["queued"] == 1
    ex.wait_for_extraction(upload_id, db_path=db, timeout=60)

    # Consumed: a second call is a no-op, so the pass cannot be started twice.
    assert pipeline.run_chain(0, actor="t", db_path=db) is None


# ===========================================================================
# 6. Aliases + direction
# ===========================================================================


def test_confirm_direction_returns_the_individual_refusal_as_a_reason(db):
    _enable(db)
    _upload_id, (document_id,) = _seed_upload(db)

    # No recipient name at all: remembering it must be refused, not silently
    # skipped, and the reason must come back as data.
    result = review_api.confirm_direction(
        document_id, "purchase", actor="r", remember_alias=True, db_path=db,
    )
    assert result["ok"] is False
    assert result["reason"]
    assert result["alias"] is None

    # The plain confirmation still works (no alias involved).
    plain = review_api.confirm_direction(document_id, "purchase", actor="r", db_path=db)
    assert plain["ok"] is True and plain["direction"] == "purchase"


def test_confirm_direction_refuses_an_individuals_name_with_a_reason(db):
    _enable(db)
    upload_id, (person_doc,) = _seed_upload(db)
    person_extraction = _add_extraction(db, person_doc)
    _add_field(db, person_extraction, person_doc, "recipient_name", "Ramesh Kumar")

    # The reason is available WITHOUT trying, so a screen can disable the control
    # and say why.
    reason = review_api.alias_refusal_reason(person_doc, db_path=db)
    assert reason and "individual" in reason.lower()

    result = review_api.confirm_direction(
        person_doc, "purchase", actor="r", remember_alias=True, db_path=db,
    )
    assert result["ok"] is False
    assert result["reason"] == reason
    assert result["alias"] is None
    assert review_api.list_aliases(1, db_path=db) == []       # nothing written

    # A business name IS allowed, so the refusal is about the name, not about
    # refusing everything.
    _upload2, (company_doc,) = _seed_upload(db)
    company_extraction = _add_extraction(db, company_doc)
    _add_field(db, company_extraction, company_doc, "recipient_name", "Kavya Textiles Pvt Ltd")
    assert review_api.alias_refusal_reason(company_doc, db_path=db) is None
    ok = review_api.confirm_direction(
        company_doc, "purchase", actor="r", remember_alias=True, db_path=db,
    )
    assert ok["ok"] is True
    assert ok["alias"] and ok["alias"]["created"] is True
    assert len(review_api.list_aliases(1, db_path=db)) == 1


def test_list_aliases_is_empty_for_a_client_without_any(db):
    _enable(db)
    assert review_api.list_aliases(1, db_path=db) == []


# ===========================================================================
# 7. The seed's cheap vision defaults
# ===========================================================================


def test_a_fresh_database_leaves_the_vision_touchpoints_unassigned(db):
    """The catalogue cannot price a non-Anthropic, non-flagship image model on a
    fresh DB, so the seed must NOT guess one."""
    _enable(db)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT touchpoint_key, primary_model, fallback_model FROM ai_model_assignments "
            "WHERE touchpoint_key IN (?, ?, ?)" ,
            ai_defaults.VISION_SEED_TOUCHPOINTS,
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    for _key, primary, fallback in rows:
        assert primary is None and fallback is None

    # And the pre-flight says so rather than quietly picking something.
    _rows, problems = ex.provider_preflight(db_path=db)
    assert any("no vision model assigned" in p for p in problems)


def test_the_chooser_picks_the_cheapest_and_keeps_read_b_separate():
    """The chooser itself, on a catalogue it CAN decide from."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE ai_provider_models (provider_key TEXT, model_id TEXT, "
            "prompt_price REAL, completion_price REAL, modality TEXT)"
        )
        conn.executemany(
            "INSERT INTO ai_provider_models VALUES (?, ?, ?, ?, ?)",
            [
                # Anthropic + a flagship: excluded by policy, however cheap.
                ("openrouter", "anthropic/claude-sonnet-4", 0.01, 0.01, "text+image->text"),
                ("openrouter", "google/gemini-2.5-pro", 0.02, 0.02, "text+image->text"),
                ("openrouter", "acme/vision-cheap", 0.10, 0.10, "text+image->text"),
                ("openrouter", "acme/vision-mid", 0.50, 0.50, "text+image->text"),
                ("openrouter", "acme/text-only", 0.01, 0.01, "text->text"),
                ("deepseek", "acme/other-cheap", 0.20, 0.20, "text+image->text"),
                # Unpriced: cannot be compared, so it is not a candidate.
                ("deepseek", "acme/unpriced", None, None, "text+image->text"),
            ],
        )
        chosen = ai_defaults.choose_vision_defaults(conn)
    finally:
        conn.close()

    read_a = chosen["invoice_extraction_visual"]["primary"]
    read_b = chosen["invoice_extraction_verify"]["primary"]
    assert read_a == {"provider": "openrouter", "model": "acme/vision-cheap"}
    # Read B is on a DIFFERENT provider than read A's primary.
    assert read_b == {"provider": "deepseek", "model": "acme/other-cheap"}
    # The fallback is on a different provider from its own primary.
    assert chosen["invoice_extraction_visual"]["fallback"]["provider"] != read_a["provider"]
    # Triage starts on the same model as read A.
    assert chosen["invoice_triage"]["primary"] == read_a


def test_the_seed_never_overwrites_an_existing_assignment(db):
    """A re-seed must not touch an admin's choice (the additive-upsert rule)."""
    ai_models.set_assignment_legs(
        "invoice_extraction_visual", primary_provider="deepseek", primary_model="deepseek-chat",
        fallback_provider="openrouter", fallback_model="qwen/qwen-2.5-72b-instruct",
        actor="admin", db_path=db, allow_non_vision_override=True,
    )
    # Re-run exactly what a boot runs.
    ai_models.init_ai_models(db)

    assignment = ai_models.get_assignment("invoice_extraction_visual", db_path=db)
    assert assignment["primary_model"] == "deepseek-chat"
    assert assignment["primary_provider"] == "deepseek"
