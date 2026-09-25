"""Golden end-to-end regression for the PNSarees Aug-2026 GST reconciliation.

Reproduces the real client dataset (Run 4) end to end: books ingestion,
portal ingestion, matching, value-at-risk semantics and run notes. Every
figure below is the independently-reconciled golden value, so a regression
in any layer is caught immediately.

Run:

    venv/bin/python -m pytest tests/test_pnsarees_golden.py -v
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.config_loader import load_config
from src.f6.books_register import parse_books_register
from src.ingestion_ai.normalizer import load_canonical_pair
from src.matching.gst_matcher import match_gst

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pnsarees_aug2026"
BOOKS = FIXTURES / "PurchaseRegister(Bill-wise) (1).xlsx"
GSTR2B = FIXTURES / "082026_07AAACP9472A1ZJ_GSTR2B_15092026 (1).xlsx"
IMS = FIXTURES / "IMS_07AAACP9472A1ZJ_14092026_1 (1).xlsx"
PERIOD = "2026-08"

# --- Golden books totals (§1.1) --------------------------------------------
BOOKS_TOTALS = {
    "invoice_value": Decimal("821221.00"),
    "taxable_value": Decimal("722707.00"),
    "cgst": Decimal("47154.43"),
    "sgst": Decimal("47154.43"),
    "igst": Decimal("4204.70"),
    "rounding_adjustment": Decimal("0.44"),
}
BOOKS_TAX_TOTAL = Decimal("98513.56")

# --- Golden portal totals ---------------------------------------------------
PORTAL_TOTALS = {
    "taxable_value": Decimal("733518.24"),
    "invoice_value": Decimal("833978.26"),
}
PORTAL_TAX_TOTAL = Decimal("100459.48")

# --- Golden Not-in-Books items ---------------------------------------------
NOT_IN_BOOKS = {
    "HF2707I005456843": Decimal("706.82"),
    "27071C0000155202": Decimal("23.60"),
    "7406PC6080067461": Decimal("226.84"),
    "27": Decimal("11800.00"),
}
RUN_GROSS_VALUE = Decimal("12757.26")
RUN_ITC_AT_RISK = Decimal("1946.02")


def _sum(frame, column: str) -> Decimal:
    vals = [x for x in frame[column].tolist() if x is not None]
    return sum(vals, Decimal("0.00"))


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def books_parsed():
    result = parse_books_register(str(BOOKS), "tally", BOOKS.name)
    assert result is not None
    return result


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """Load the canonical pair through the RUN path (not the parser directly),
    so the test exercises exactly what the engine consumes."""
    root = tmp_path_factory.mktemp("pnsarees")
    client = "Test Client"
    for source_type, src in (("tally", BOOKS), ("gstr2b", GSTR2B), ("ims", IMS)):
        dest = root / client / PERIOD / source_type
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest / src.name)

    import src.data_paths as data_paths

    original = data_paths.DATA_ROOT
    data_paths.DATA_ROOT = root
    try:
        selected = {"tally": BOOKS.name, "gstr2b": GSTR2B.name, "ims": IMS.name}
        books, portal, source_files, caveats, run_notes = load_canonical_pair(
            client, PERIOD, "GST", selected, client_id=None,
        )
        yield books, portal, source_files, caveats, run_notes
    finally:
        data_paths.DATA_ROOT = original


# --- Books ingestion (D1) ---------------------------------------------------

def test_books_ingestion_totals(books_parsed):
    """35 documents with the golden totals — the rate-bucket aggregation."""
    assert books_parsed.row_count_parsed == 35
    for column, expected in BOOKS_TOTALS.items():
        assert _sum(books_parsed.frame, column) == expected, column
    tax = (BOOKS_TOTALS["igst"] + BOOKS_TOTALS["cgst"] + BOOKS_TOTALS["sgst"])
    assert tax == BOOKS_TAX_TOTAL


def test_books_rate_bucket_bases_and_tax(books_parsed):
    """Each rate bucket's base and tax, read from the source columns."""
    src = books_parsed.source_rows

    def col_sum(col: str) -> Decimal:
        total = Decimal("0.00")
        for row in src:
            v = row.get(col)
            if v in (None, ""):
                continue
            total += Decimal(str(v).strip())
        return total

    assert col_sum("IGST Txbl. Amt @ 5%") == Decimal("4570.00")
    assert col_sum("IGST Tax Amt @ 5%") == Decimal("228.50")
    assert col_sum("IGST Txbl. Amt @ 18%") == Decimal("22090.00")
    assert col_sum("IGST Tax Amt @ 18%") == Decimal("3976.20")
    assert col_sum("CGST Txbl. Amt @ 2.5%") == Decimal("238307.00")
    assert col_sum("CGST Tax Amt @ 2.5%") == Decimal("5957.83")
    assert col_sum("CGST Txbl. Amt @ 9%") == Decimal("457740.00")
    assert col_sum("CGST Tax Amt @ 9%") == Decimal("41196.60")


def test_books_spot_rows(books_parsed):
    """7953, T15158 and 4284 — the rows the defect report names."""
    by_bill = {str(r["invoice_number"]): r for _, r in books_parsed.frame.iterrows()}

    r7953 = by_bill["7953"]
    assert r7953["taxable_value"] == Decimal("22090.00")
    assert r7953["igst"] == Decimal("3976.20")
    assert r7953["rounding_adjustment"] == Decimal("-0.20")
    assert r7953["invoice_value"] == Decimal("26066.00")

    t15158 = by_bill["T15158"]
    assert t15158["taxable_value"] == Decimal("32685.00")
    assert t15158["cgst"] == Decimal("2549.70")
    assert t15158["sgst"] == Decimal("2549.70")

    r4284 = by_bill["4284"]
    assert r4284["taxable_value"] == Decimal("33670.00")
    assert r4284["cgst"] == Decimal("1621.77")


def test_books_run_path_matches_parser(books_parsed, pair):
    """The RUN path (stored mapping re-derivation) must produce the SAME
    figures as the deterministic parser — this is the D1 regression."""
    books, _portal, _sf, _cav, _notes = pair
    assert len(books) == 35
    assert round(float(books["taxable_value"].astype(float).sum()), 2) == 722707.00
    assert round(float(books["cgst"].astype(float).sum()), 2) == 47154.43
    assert round(float(books["sgst"].astype(float).sum()), 2) == 47154.43
    assert round(float(books["igst"].astype(float).sum()), 2) == 4204.70
    assert round(float(books["rounding_adjustment"].astype(float).sum()), 2) == 0.44
    assert round(float(books["invoice_value"].astype(float).sum()), 2) == 821221.00


# --- Portal ingestion -------------------------------------------------------

def test_portal_totals(pair):
    _books, portal, _sf, _cav, _notes = pair
    assert len(portal) == 39
    assert round(float(portal["taxable_value"].astype(float).sum()), 2) == 733518.24
    tax = (
        portal["cgst"].astype(float) + portal["sgst"].astype(float)
        + portal["igst"].astype(float) + portal["cess"].astype(float)
    ).sum()
    assert round(float(tax), 2) == 100459.48
    assert round(float(portal["invoice_value"].astype(float).sum()), 2) == 833978.26


# --- Matching (D3, D4, D6) --------------------------------------------------

@pytest.fixture(scope="module")
def results(pair, config):
    books, portal, _sf, _cav, _notes = pair
    return match_gst(books, portal, config["gst"])


def test_matching_counts(results):
    from collections import Counter

    counts = Counter(r["classification"] for r in results)
    assert counts["Matched"] == 35
    assert counts["Amount Difference"] == 0
    assert counts["Not in Portal"] == 0
    assert counts["Not in Books"] == 4


def test_all_matched_are_high_confidence(results):
    matched = [r for r in results if r["classification"] == "Matched"]
    assert len(matched) == 35
    assert all(r["confidence_band"] == "High" for r in matched), [
        (r["confidence_band"], r["match_reason"][:80]) for r in matched
        if r["confidence_band"] != "High"
    ]


def test_not_in_books_items(results):
    import json

    nib = [r for r in results if r["classification"] == "Not in Books"]
    assert len(nib) == 4
    by_inv = {}
    for r in nib:
        rec = json.loads(r["portal_record"])
        by_inv[str(rec["invoice_number"])] = r
    assert set(by_inv) == set(NOT_IN_BOOKS)
    for inv, expected in NOT_IN_BOOKS.items():
        assert Decimal(str(by_inv[inv]["gross_value"])) == expected, inv


def test_run_aggregates_value_at_risk(results):
    gross = sum(Decimal(str(r["gross_value"])) for r in results)
    itc = sum(Decimal(str(r["itc_at_risk"])) for r in results)
    assert gross == RUN_GROSS_VALUE
    assert itc == RUN_ITC_AT_RISK


def test_t14781_and_t15076_not_ambiguous(results):
    """Both ₹8,349 invoices pair to their OWN portal record at High confidence."""
    import json

    matched = [r for r in results if r["classification"] == "Matched"]
    by_books_inv = {}
    for r in matched:
        rec = json.loads(r["books_record"])
        by_books_inv[str(rec["invoice_number"])] = r

    for inv in ("T14781", "T15076"):
        r = by_books_inv[inv]
        assert r["confidence_band"] == "High", r["match_reason"]
        assert "Ambiguous" not in r["match_reason"]
        portal_rec = json.loads(r["portal_record"])
        assert str(portal_rec["invoice_number"]) == f"{inv}/26-27"


def test_every_result_has_plain_language_reason(results):
    for r in results:
        reason = (r.get("match_reason") or "").strip()
        assert reason, r["classification"]
        assert len(reason) > 20


# --- Run notes (D7) ---------------------------------------------------------

def test_run_notes_declare_credit_notes_and_ims(pair):
    _books, _portal, _sf, _cav, notes = pair
    codes = {n["code"] for n in notes}
    assert "credit_notes_not_reconciled" in codes
    credit = next(n for n in notes if n["code"] == "credit_notes_not_reconciled")
    assert credit["count"] == 12
    assert "12 credit note" in credit["title"]


# --- Negative tests (D2) ----------------------------------------------------

def test_broken_books_mapping_hard_stops(tmp_path):
    """Removing the bucket mapping must hard-stop with the tax-arithmetic
    failure and create no match results."""
    from src.f6 import validation as v

    # Simulate the broken parse: taxable and tax all zero, invoice value real.
    rows = []
    for _, r in parse_books_register(str(BOOKS), "tally", BOOKS.name).frame.iterrows():
        rows.append({
            "taxable_value": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0,
            "rounding_adjustment": float(r["rounding_adjustment"] or 0),
            "invoice_value": float(r["invoice_value"] or 0),
        })
    outcome = v.run_all_checks(
        rows=rows, row_count_read=len(rows), row_count_parsed=len(rows),
        exclusion_reasons=[], required_fields=[],
    )
    assert outcome.hard_stopped
    tax_check = next(c for c in outcome.results if c.check == "tax_arithmetic")
    assert tax_check.result == "hard_stop"
    assert "35/35" in tax_check.detail


def test_books_control_total_altered_hard_stops(books_parsed):
    """A control total that disagrees with the file's own Total row must
    hard-stop naming the delta."""
    from src.f6 import validation as v

    parsed_sum = 821221.00
    stated = 821221.00 + 500.0
    check = v.check_control_total(parsed_sum, stated, label="file's own Total row")
    assert check.result == "hard_stop"
    assert "500.00" in check.detail


def test_broken_books_mapping_blocks_the_run(tmp_path, monkeypatch):
    """Defence in depth: a stored books mapping with the rate buckets removed
    must make Module 2 REFUSE to start — no partial results.

    The stored mapping is written directly (as an older/broken build would
    have), then the run path is asked to load the pair. It must raise
    IngestionBlockedError naming the tax-arithmetic failure.
    """
    import src.data_paths as data_paths
    from src.ingestion_ai import db as idb
    from src.ingestion_ai.normalizer import (
        IngestionBlockedError,
        load_canonical_pair,
        upload_key,
    )

    root = tmp_path / "data"
    client = "Test Client"
    for source_type, src in (("tally", BOOKS), ("gstr2b", GSTR2B), ("ims", IMS)):
        dest = root / client / PERIOD / source_type
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest / src.name)
    monkeypatch.setattr(data_paths, "DATA_ROOT", root)

    db_path = tmp_path / "poc.db"
    from src.ingestion_ai.schema import init_ingestion_ai_schema

    conn = idb.connect(db_path)
    init_ingestion_ai_schema(conn)
    # A mapping that maps identity + invoice value but NOT the tax buckets —
    # exactly the broken parse the defect report describes.
    broken_mapping = [
        {"canonical_field": "gstin", "source_column": "GSTIN", "confidence": None,
         "reason": "broken", "required": True},
        {"canonical_field": "invoice_number", "source_column": "Bill No", "confidence": None,
         "reason": "broken", "required": True},
        {"canonical_field": "invoice_date", "source_column": "Date", "confidence": None,
         "reason": "broken", "required": True},
        {"canonical_field": "invoice_value", "source_column": "Bill Amount", "confidence": None,
         "reason": "broken", "required": True},
        {"canonical_field": "rounding_adjustment", "source_column": "Other Amt.", "confidence": None,
         "reason": "broken", "required": False},
    ]
    idb.upsert_ingestion_result(
        conn,
        upload_key=upload_key(client, PERIOD, "tally", BOOKS.name),
        client_ref=client, client_id=None, period=PERIOD, source_type="tally",
        filename=BOOKS.name, status="partial", header_row=6, sheet_name="Sheet1",
        sheet_ambiguous=False, headers=[], mapping=broken_mapping, classification=None,
        unmapped_required=[], warnings=[], row_count_in=36, row_count_out=35,
        model_used=None, llm_cached=False, actor="test",
        validation=[{
            "check": "tax_arithmetic", "result": "hard_stop",
            "detail": "35/35 rows (100%) fail tax arithmetic — exceeds the 20% escalation threshold.",
            "affected_rows": list(range(35)),
        }],
    )
    conn.close()

    selected = {"tally": BOOKS.name, "gstr2b": GSTR2B.name, "ims": IMS.name}
    with pytest.raises(IngestionBlockedError) as exc:
        load_canonical_pair(client, PERIOD, "GST", selected, db_path=db_path)
    message = str(exc.value)
    assert "failed validation" in message
    assert "tax_arithmetic" in message
    assert "First contributing rows" in message


def test_ambiguity_note_reports_chosen_score_above_runner_up():
    """The ambiguity note must show the CHOSEN score, never the penalised one
    (which would read as the chosen candidate scoring below the runner-up)."""
    import pandas as pd

    from src.matching.gst_matcher import _resolve_ambiguity

    config = load_config()["gst"]
    candidates = [
        (98.0, pd.Series({"gstin": "07AAAAA0000A1Z5", "invoice_number": "A"}), ""),
        (96.0, pd.Series({"gstin": "07AAAAA0000A1Z5", "invoice_number": "B"}), ""),
    ]
    _chosen, final_score, note, forced = _resolve_ambiguity(candidates, config)
    assert forced is True
    assert "98 vs runner-up 96" in note
    # The penalty is applied to the returned score, not to the reported one.
    assert final_score == 90.0

