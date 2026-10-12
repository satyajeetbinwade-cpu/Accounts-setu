"""Tests for the model-comparison scoring command (``triage score``).

Stubbed only — no network, no live model calls, and never the real database for
writing. Every run happens against a temp COPY of a scratch source database.

Run:

    venv/bin/python -m pytest tests/test_triage_score_compare.py -v
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from src import db as recon_db
from src.ai_models import providers as prov, service as ai_models
from src.invoice_extract import service as ie
from src.invoice_extract import triage
from src.vault import service as vault


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _mk_pdf(path: Path, pages: int = 2) -> Path:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), f"Page {i + 1}")
        page.insert_text((72, 120), "=" * 40)
        page.insert_text((72, 300), "DOC 1234")
    path.write_bytes(doc.tobytes())
    doc.close()
    return path


def _seed_catalogue(db_path: str, model_id: str, *, modality: str, price: float = 1.0) -> None:
    from datetime import datetime, timezone

    conn = recon_db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_model_catalog "
            "(model_id, name, context_length, prompt_price, completion_price, modality, fetched_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?)",
            (model_id, model_id, price, price, modality, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def source_db(tmp_path):
    """A scratch 'app' database (migrations applied, vault ready, one client)."""
    from src import bootstrap
    from src.clients import service as clients

    path = str(tmp_path / "source.db")
    bootstrap.run_migrations(db_path=path, data_root=tmp_path / "srcdata")
    vault.init_vault(path)
    clients.create_client(legal_name="Scratch Client", pan="AAAAA0000A",
                          actor="test", db_path=path)
    return path


@pytest.fixture()
def stub_page_model(monkeypatch):
    """Stub the ONE triage model seam. Returns the call log + a knob for usage."""
    state = {"calls": [], "usage": {"input_tokens": 10, "output_tokens": 5}}

    def fake(png, *, page, total, cfg, db_path, run_id, actor, upload_id=None):
        state["calls"].append(page)
        return (
            {"rotation_deg": 0, "page_is_blank": False,
             "documents": [{"category": "tax_invoice", "label": f"p{page}",
                            "boxes": [{"x": 0.1, "y": 0.1, "w": 0.6, "h": 0.6}]}]},
            {"provider": "openrouter", "model": "stub/model", "latency_ms": 1,
             "usage": dict(state["usage"]), "fallback_used": False},
        )

    monkeypatch.setattr(triage, "_call_page_model", fake)
    return state


@pytest.fixture()
def stub_keys(monkeypatch):
    """Report every provider as configured, and never decrypt a real key."""
    monkeypatch.setattr(
        ai_models, "provider_key_configured", lambda provider, *, db_path=None: (True, "vault")
    )
    monkeypatch.setattr(
        ai_models, "resolve_provider_secret",
        lambda provider, *, actor, purpose, db_path=None: "sk-STUB-NEVER-PRINTED",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _triage_row(db_path: str) -> tuple:
    conn = recon_db.get_connection(db_path)
    try:
        return tuple(conn.execute(
            "SELECT primary_provider, primary_model, fallback_provider, fallback_model "
            "FROM ai_model_assignments WHERE touchpoint_key = 'invoice_triage'"
        ).fetchone())
    finally:
        conn.close()


def _run(tmp_path, source_db, pdf, **kw):
    kw.setdefault("out_dir", tmp_path / "report")
    kw.setdefault("expected_path", tmp_path / "no-such-expected.json")
    return triage.score(str(pdf), db_path=source_db, **kw)


# ===========================================================================
# 1. Flags change ONLY the copy; the source database is byte-identical
# ===========================================================================


def test_flags_change_only_the_copy_and_source_db_is_byte_identical(
    tmp_path, source_db, stub_page_model, stub_keys
):
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    before_row = _triage_row(source_db)
    before_sha = _sha(Path(source_db))

    real_db = Path(recon_db.DB_PATH)
    real_sha = _sha(real_db) if real_db.exists() else None

    summary = _run(
        tmp_path, source_db, pdf,
        primary=["openrouter:qwen/qwen3.7-flash"],
        fallback=["openrouter:mistralai/ministral-3b-2512"],
    )

    # The SOURCE database is untouched — same bytes, same assignment.
    assert _sha(Path(source_db)) == before_sha
    assert _triage_row(source_db) == before_row
    assert summary["source_db"] == str(source_db)

    # And the REAL app database was never opened for writing either.
    if real_sha is not None:
        assert _sha(real_db) == real_sha

    # The copy really did receive the assignment (visible in the report).
    report = Path(summary["report_path"]).read_text()
    assert "openrouter:qwen/qwen3.7-flash" in report
    assert "openrouter:mistralai/ministral-3b-2512" in report
    # A stub key must never leak into the report.
    assert "sk-STUB-NEVER-PRINTED" not in report


def test_report_prints_the_resolved_assignment_at_the_start(
    tmp_path, source_db, stub_page_model, stub_keys
):
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    summary = _run(tmp_path, source_db, pdf,
                   primary=["openrouter:qwen/qwen3.7-flash"],
                   fallback=["openrouter:mistralai/ministral-3b-2512"])
    report = Path(summary["report_path"]).read_text()
    head = report[: report.index("## Comparison")]
    assert "Resolved assignment" in head
    assert "qwen/qwen3.7-flash" in head
    # key availability is stated, and only as yes/no
    assert "key available" in report
    assert re.search(r"\|\s*openrouter\s*\|\s*yes\s*\|", report)


# ===========================================================================
# 2. Missing key stops with a clear message
# ===========================================================================


def test_missing_key_stops_with_a_clear_message(tmp_path, source_db, stub_page_model, monkeypatch):
    monkeypatch.setattr(
        ai_models, "provider_key_configured", lambda provider, *, db_path=None: (False, None)
    )
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    with pytest.raises(ie.InvoiceExtractError) as exc:
        _run(tmp_path, source_db, pdf, primary=["openrouter:qwen/qwen3.7-flash"],
             fallback=["openrouter:mistralai/ministral-3b-2512"])
    msg = str(exc.value)
    assert "Missing API key" in msg
    assert "openrouter" in msg
    # No model call was attempted.
    assert stub_page_model["calls"] == []


# ===========================================================================
# 3. A non-vision model is refused unless --force-model
# ===========================================================================


def test_non_vision_model_refused_without_force(tmp_path, source_db, stub_page_model, stub_keys):
    _seed_catalogue(source_db, "acme/text-only", modality="text->text")
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    with pytest.raises(ie.InvoiceExtractError) as exc:
        _run(tmp_path, source_db, pdf,
             primary=["openrouter:acme/text-only"],
             fallback=["openrouter:acme/vision-ok"])
    assert "image-capability check failed" in str(exc.value)
    assert "acme/text-only" in str(exc.value)
    assert stub_page_model["calls"] == []


def test_forced_non_vision_model_runs_and_says_so(tmp_path, source_db, stub_page_model, stub_keys):
    _seed_catalogue(source_db, "acme/text-only", modality="text->text")
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    summary = _run(tmp_path, source_db, pdf, force_model=True,
                   primary=["openrouter:acme/text-only"],
                   fallback=["openrouter:acme/vision-ok"])
    report = Path(summary["report_path"]).read_text()
    assert "`--force-model`: **used**" in report
    assert "--force-model" in report and "acme/text-only" in report
    assert stub_page_model["calls"], "the run should have happened"


# ===========================================================================
# 5. An empty provider catalogue skips the check and relies on the image test
# ===========================================================================


def test_empty_provider_catalogue_skips_check_and_uses_live_image_test(
    tmp_path, source_db, stub_page_model, stub_keys, monkeypatch
):
    """A DeepSeek-style provider has no catalogue in the copy: the id is passed
    through verbatim and the LIVE image test is what checks it."""
    image_calls = []

    def fake_provider(spec, api_key, **kw):
        if kw.get("images"):
            image_calls.append((spec.key, kw.get("model")))
            return "SETU", 9
        return "ok", 9

    monkeypatch.setattr(prov, "call_provider", fake_provider)
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    summary = _run(tmp_path, source_db, pdf,
                   primary=["deepseek:deepseek-flash"],
                   fallback=["openrouter:qwen/qwen3.7-flash"])
    assert image_calls, "an image must be sent to the leg with no catalogue"
    assert image_calls[0][0] == "deepseek"          # id passed to that provider verbatim
    assert image_calls[0][1] == "deepseek-flash"
    report = Path(summary["report_path"]).read_text()
    assert "catalogue image check SKIPPED" in report
    assert "Live image test (primary leg): **passed**" in report


# ===========================================================================
# 3. One combined report, one row per candidate
# ===========================================================================


def test_combined_report_has_one_row_and_section_per_candidate(
    tmp_path, source_db, stub_page_model, stub_keys
):
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    summary = _run(tmp_path, source_db, pdf, candidates=[
        {"name": "alpha", "primary": "openrouter:acme/alpha", "fallback": "openrouter:acme/alpha-fb"},
        {"name": "beta", "primary": "openrouter:acme/beta", "fallback": "openrouter:acme/beta-fb"},
    ])
    report = Path(summary["report_path"]).read_text()
    comparison = report[report.index("## Comparison"): report.index("## Candidate: alpha")]

    rows = [ln for ln in comparison.splitlines() if ln.startswith("| alpha ") or ln.startswith("| beta ")]
    assert len(rows) == 2, "one comparison row per candidate"
    assert report.count("## Candidate: ") == 2
    assert "## Candidate: alpha" in report and "## Candidate: beta" in report
    assert len(summary["candidates"]) == 2
    # one report file only
    assert len(list((tmp_path / "report").glob("*.md"))) == 1
    # annotated PNGs are per candidate and kept OUT of the markdown body
    assert "(tmp_path" not in report and "![" not in report
    assert len(list((tmp_path / "report" / "alpha").glob("page_*.png"))) == 2
    assert len(list((tmp_path / "report" / "beta").glob("page_*.png"))) == 2


def test_repeat_runs_the_candidate_more_than_once(tmp_path, source_db, stub_page_model, stub_keys):
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    summary = _run(tmp_path, source_db, pdf, repeat=2,
                   primary=["openrouter:acme/one"], fallback=["openrouter:acme/two"])
    assert summary["candidates"][0]["runs"] == 2
    assert len(stub_page_model["calls"]) >= 4  # 2 pages × 2 repeats


# ===========================================================================
# 6. Cost cap
# ===========================================================================


def test_cost_cap_stops_a_candidate_that_exceeds_it(
    tmp_path, source_db, stub_page_model, stub_keys
):
    # A stub whose tokens cost far more than the cap (price per 1M == 1,000,000).
    _seed_catalogue(source_db, "stub/model", modality="text+image->text", price=1_000_000.0)
    stub_page_model["usage"] = {"input_tokens": 100, "output_tokens": 100}
    pdf = _mk_pdf(tmp_path / "bundle.pdf")

    summary = _run(tmp_path, source_db, pdf, repeat=3, max_cost_usd=1.00,
                   primary=["openrouter:acme/one"], fallback=["openrouter:acme/two"])
    cand = summary["candidates"][0]
    assert cand["cost_cap_reached"] is True
    assert cand["runs"] == 1, "the remaining repeats must not run once the cap is exceeded"

    report = Path(summary["report_path"]).read_text()
    assert "cost cap reached" in report
    assert "was stopped" in report


def test_under_the_cap_all_repeats_run(tmp_path, source_db, stub_page_model, stub_keys):
    _seed_catalogue(source_db, "stub/model", modality="text+image->text", price=0.0001)
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    summary = _run(tmp_path, source_db, pdf, repeat=2, max_cost_usd=1.00,
                   primary=["openrouter:acme/one"], fallback=["openrouter:acme/two"])
    assert summary["candidates"][0]["cost_cap_reached"] is False
    assert summary["candidates"][0]["runs"] == 2


# ===========================================================================
# Candidate parsing
# ===========================================================================


def test_parse_leg_keeps_openrouter_variant_suffix():
    assert triage._parse_leg("openrouter:google/gemma-3-4b-it:free") == (
        "openrouter", "google/gemma-3-4b-it:free")
    assert triage._parse_leg("DeepSeek:deepseek-flash") == ("deepseek", "deepseek-flash")
    with pytest.raises(ie.InvoiceExtractError):
        triage._parse_leg("openrouter")


def test_repeatable_primary_becomes_one_candidate_each():
    plan = triage._candidates_from_args(
        primary=["openrouter:a/one", "openrouter:b/two"],
        fallback=["openrouter:a/one-fb"],
        candidates_file=None, candidates=None,
    )
    assert [c["name"] for c in plan] == ["cli-1", "cli-2"]
    assert plan[0]["primary"] == ("openrouter", "a/one")
    assert plan[0]["fallback"] == ("openrouter", "a/one-fb")
    assert plan[1]["fallback"] is None  # falls back to the copy's current leg


def test_single_primary_keeps_the_plain_name():
    plan = triage._candidates_from_args(
        primary=["openrouter:a/one"], fallback=None, candidates_file=None, candidates=None,
    )
    assert len(plan) == 1 and plan[0]["name"] == "cli"


def test_candidates_file_is_read(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps([
        {"name": "cheap", "primary": "deepseek:deepseek-flash", "fallback": "openrouter:qwen/qwen3.7-flash"},
        {"name": "strong", "primary": "openrouter:anthropic/claude-sonnet-4.5", "fallback": None},
    ]))
    plan = triage._candidates_from_args(primary=None, fallback=None,
                                        candidates_file=str(path), candidates=None)
    assert [c["name"] for c in plan] == ["cheap", "strong"]
    assert plan[0]["primary"] == ("deepseek", "deepseek-flash")
    assert plan[1]["fallback"] is None


def test_candidates_file_and_flags_together_is_refused(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text("[]")
    with pytest.raises(ie.InvoiceExtractError):
        triage._candidates_from_args(primary=["openrouter:a/b"], fallback=None,
                                     candidates_file=str(path), candidates=None)


def test_no_legs_uses_the_copy_as_is():
    plan = triage._candidates_from_args(primary=None, fallback=None,
                                        candidates_file=None, candidates=None)
    assert plan == [{"name": "default", "primary": None, "fallback": None}]


def test_more_fallbacks_than_primaries_is_refused():
    with pytest.raises(ie.InvoiceExtractError):
        triage._candidates_from_args(
            primary=["openrouter:a/one", "openrouter:b/two"],
            fallback=["openrouter:f/1", "openrouter:f/2", "openrouter:f/3"],
            candidates_file=None, candidates=None,
        )


def test_missing_fixture_pdf_is_reported(tmp_path):
    with pytest.raises(ie.InvoiceExtractError) as exc:
        triage.score(str(tmp_path / "nope.pdf"), out_dir=tmp_path)
    assert "Fixture PDF not found" in str(exc.value)


def test_missing_database_is_reported(tmp_path):
    pdf = _mk_pdf(tmp_path / "bundle.pdf")
    with pytest.raises(ie.InvoiceExtractError) as exc:
        triage.score(str(pdf), out_dir=tmp_path, db_path=str(tmp_path / "nope.db"))
    assert "No database at" in str(exc.value)
