"""Phase-1 tests: Excel intake + classification + Sarvam-assisted fallback."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from recon_engine import intake
from recon_engine.agent import auto_reconcile
from recon_engine.config import Settings
from recon_engine.parsers import (
    parse_gstr2b_excel,
    parse_gstr2b_json,
    parse_tally_excel,
)
from recon_engine.sample_scenario import generate as gen_sample_docs

SAMPLES = gen_sample_docs()


# ---------------------------------------------------------------------------
# Excel parser parity (Excel fixtures must match the JSON/CSV fixtures)
# ---------------------------------------------------------------------------
def _norm(inv):
    return (inv.gstin, inv.invoice_no, inv.date.isoformat(),
            str(inv.taxable_value), format(inv.tax_amount, "0.2f"), inv.status)


def test_gstr2b_excel_parity_with_json():
    js = parse_gstr2b_json(SAMPLES["gstr2b"])
    xl = parse_gstr2b_excel(SAMPLES["gstr2b_xlsx"])
    assert len(xl) == 9
    assert {_norm(i) for i in xl} == {_norm(i) for i in js}
    # status R/N mapped back to engine vocabulary
    assert {i.status for i in xl} == {"active", "non_filer"}


def test_tally_excel_parity_with_csv():
    from recon_engine.parsers import parse_tally_purchase_rows
    import csv
    with open(SAMPLES["tally"], encoding="utf-8") as f:
        csv_rows = parse_tally_purchase_rows(list(csv.DictReader(f)))
    xl = parse_tally_excel(SAMPLES["tally_xlsx"])
    assert len(xl) == 10
    assert {_norm(i) for i in xl} == {_norm(i) for i in csv_rows}
    # books total (taxable + tax) preserved
    total = sum((i.taxable_value + i.tax_amount for i in xl), Decimal("0"))
    assert total == Decimal("587640.00")


# ---------------------------------------------------------------------------
# Intake classification (deterministic structural)
# ---------------------------------------------------------------------------
def test_classify_sample_files():
    no_llm = Settings()  # no SARVAM_API_KEY -> deterministic only
    g = intake.classify_file(SAMPLES["gstr2b_xlsx"], settings=no_llm)
    assert g.kind == "gstr2b_excel" and g.source == "portal"
    assert g.confidence == "high"

    t = intake.classify_file(SAMPLES["tally_xlsx"], settings=no_llm)
    assert t.kind == "tally_excel" and t.source == "books"

    j = intake.classify_file(SAMPLES["gstr2b"], settings=no_llm)
    assert j.kind == "gstr2b_json"

    tb = intake.classify_file("sample_data/SampleTB-AI.xlsx", settings=no_llm)
    assert tb.kind == "trial_balance"


def test_client_period_inference_from_filename(tmp_path):
    no_llm = Settings()
    f = tmp_path / "Acme_GSTR2B_2024-08.xlsx"
    f.write_bytes(b"")
    d = intake.classify_file(f, settings=no_llm)
    assert d.client == "acme"
    assert d.period == "2024-08"


def test_scan_folder_and_build_inputs():
    no_llm = Settings()
    scan = intake.scan_folder("sample_data", settings=no_llm)
    kinds = {f.kind for f in scan.files}
    assert {"gstr2b_excel", "gstr2b_json", "tally_excel", "trial_balance"} \
        <= kinds
    run = intake.build_run_inputs(scan)
    assert run["ready"] is True
    assert run["gstr2b_path"].suffix in (".json", ".xlsx")
    assert run["tally_path"].suffix in (".csv", ".xlsx")
    assert run["client_id"]  # client inferred from sample filenames


# ---------------------------------------------------------------------------
# Sarvam-105B assist (ambiguous files, graceful fallback)
# ---------------------------------------------------------------------------
class _FakeKeySettings(Settings):
    """Settings that *look* like a key is configured but never dial out.

    Settings is a frozen dataclass, so the key must be set via
    object.__setattr__ (the property has_api_key then reads it).
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        object.__setattr__(
            self, "sarvan_api_key", "dummy-sarvam-key-for-tests-0000")


class _FakeLLM:
    """Returns a canned classification the intake layer must validate."""

    def __init__(self, payload: dict):
        self._payload = payload

    def available(self):
        return True

    async def chat(self, messages, **kw):
        import json as _json
        return _json.dumps(self._payload)


def test_llm_assist_classifies_ambiguous_file(tmp_path):
    # "doc.xlsx": corrupt workbook (structurally 'unknown') AND no client/period
    # inferable from the name -> intake must ask the LLM assist layer.
    f = tmp_path / "doc.xlsx"
    f.write_bytes(b"not really a workbook")
    fake = _FakeLLM({"document_type": "gstr2b_excel", "client": "acme",
                     "period": "2024-08", "confidence": "medium"})
    d = intake.classify_file(f, settings=_FakeKeySettings(),
                             llm_client=_AsyncWrap(fake))
    # structural detection says 'unknown'; LLM assist upgrades it only after
    # intake VALIDATES the proposal against known document types.
    assert d.kind == "gstr2b_excel"
    assert d.method == "llm_assist"
    assert d.client == "acme"
    assert d.period == "2024-08"


class _AsyncWrap:
    """Wrap the (async) fake LLM so awaiting chat() resolves to its payload."""

    def __init__(self, inner):
        self._inner = inner

    def available(self):
        return self._inner.available()

    async def chat(self, messages, **kw):
        return await self._inner.chat(messages, **kw)


def test_llm_garbage_falls_back_to_deterministic(tmp_path):
    f = tmp_path / "Sheet2.xlsx"
    f.write_bytes(b"garbage data")
    # LLM returns nonsense -> intake must keep the deterministic result
    fake = _AsyncWrap(_FakeLLM({"document_type": "martian_ufo"}))
    d = intake.classify_file(f, settings=_FakeKeySettings(), llm_client=fake)
    assert d.kind == "unknown"
    assert d.method == "needs_review"
    assert any(("unrecognised" in i) or ("llm proposed unknown type" in i)
               for i in d.issues)


# ---------------------------------------------------------------------------
# Agent accepts Excel inputs end-to-end (canonical results preserved)
# ---------------------------------------------------------------------------
def test_agent_with_excel_inputs_matches_json_csv_results():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_xlsx"],
        tally_path=SAMPLES["tally_xlsx"],
        client_id="acme", period="2024-08", output_tax=120000,
    )
    p = res["packet"]
    assert p.summary["matched"] == 7
    assert p.summary["amount_diff"] == 1
    assert p.summary["not_in_books"] == 1
    assert p.summary["not_in_portal"] == 2
    assert p.itc.eligible_itc == Decimal("64260.00")
    assert p.validation.get("pass") is True
    assert res["verification"].get("pass") is True
    assert res["reports"]["html"].exists()
