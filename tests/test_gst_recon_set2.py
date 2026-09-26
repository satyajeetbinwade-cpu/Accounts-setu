"""Golden regression for the Test Set 2 GST bug-fix pass (Run-13 findings).

Encodes the QA-verified expectations for:

  F1 duplicate-books join   (S2-09 OT/85 booked twice)
  F2 credit-note isolation  (CN-SF/CN/118, CN-RTM/CN/29)
  F3 one side per report row (Invoice Value is portal-side)
  F4 pass-based confidence  (independent of value agreement)
  F5 difference_type surfaced in the HTML report

plus the count reconciliation from the QA Findings tab:

    19 rows · 11 Matched · 8 exceptions · Rs 74,897.82 Period ITC

Run:

    venv/bin/python -m pytest tests/test_gst_recon_set2.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.config_loader import load_config
from src.ingestion_ai.normalizer import load_canonical_pair
from src.matching.gst_matcher import match_gst
from src.reconciliation import review

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "set2"
GSTR2B = FIXTURES / "092026_07AAECM4521F1Z8_GSTR2B_TESTSET2.xlsx"
IMS = FIXTURES / "IMS_07AAECM4521F1Z8_TESTSET2.xlsx"
BOOKS = FIXTURES / "PurchaseRegister_TESTSET2_MeridianTextiles.xlsx"
CLIENT = "Test Client"
PERIOD = "2026-08"
SELECTED = {"tally": BOOKS.name, "gstr2b": GSTR2B.name, "ims": IMS.name}

# Count reconciliation ("Ground truth (expected)").
EXPECTED_TOTAL = 19
EXPECTED_MATCHED = 11
EXPECTED_EXCEPTIONS = 8
EXPECTED_PERIOD_ITC = 74897.82


@pytest.fixture(scope="module")
def data_root(tmp_path_factory):
    """Point the engine's DATA_ROOT at a private copy of the three files."""
    root = tmp_path_factory.mktemp("set2_data")
    for source_type, src in (("gstr2b", GSTR2B), ("ims", IMS), ("tally", BOOKS)):
        dest = root / CLIENT / PERIOD / source_type
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest / src.name)

    import src.data_paths as data_paths

    original = data_paths.DATA_ROOT
    data_paths.DATA_ROOT = root
    try:
        yield root
    finally:
        data_paths.DATA_ROOT = original


@pytest.fixture(scope="module")
def results(data_root):
    books, portal, _files, _caveats, _notes = load_canonical_pair(
        CLIENT, PERIOD, "GST", SELECTED, client_id=None,
    )
    return match_gst(books, portal, load_config()["gst"])


@pytest.fixture(scope="module")
def model(results):
    return review.build_review_model(
        {"recon_type": "GST", "client": CLIENT, "period": PERIOD}, results
    )


def _by_ref(model):
    out: dict[str, list] = {}
    for item in model["items"]:
        out.setdefault(str(item["reference"]), []).append(item)
    return out


# --- Count reconciliation ----------------------------------------------------

def test_count_reconciliation(model):
    k = model["kpi"]
    assert k["total_count"] == EXPECTED_TOTAL
    assert k["matched_count"] == EXPECTED_MATCHED
    assert k["exception_count"] == EXPECTED_EXCEPTIONS
    assert k["period_itc_total"] == EXPECTED_PERIOD_ITC


# --- F1 duplicate-books join (S2-09) ----------------------------------------

def test_duplicate_books_is_one_match_plus_one_residual(model):
    """OT/85 booked twice must yield exactly 2 rows — 1 Matched + 1 residual —
    never the old 2 bogus 'Amount Difference' rows + matched + residual = 4."""
    rows = _by_ref(model)["OT/85"]
    assert len(rows) == 2, [r["classification"] for r in rows]
    assert sorted(r["classification"] for r in rows) == ["Matched", "Not in Portal"]

    matched = next(r for r in rows if r["classification"] == "Matched")
    surplus = next(r for r in rows if r["classification"] == "Not in Portal")
    assert matched["difference_type"] == ""
    assert matched["confidence_band"] == "High"
    assert surplus["difference_type"] == "Duplicate in Books"

    # No spurious paired comparison carries a zero value delta.
    assert not any(
        r["classification"] == "Amount Difference" and r["difference_type"] == "Duplicate in Books"
        for r in model["items"]
    )


# --- F2 credit-note isolation ------------------------------------------------

def test_credit_notes_absent_from_invoice_pool(model):
    refs = {str(i["reference"]) for i in model["items"]}
    assert not any(r.startswith("CN-") for r in refs), sorted(refs)
    assert "CN-SF/CN/118" not in refs
    assert "CN-RTM/CN/29" not in refs


# --- F4 pass-based identity confidence --------------------------------------

@pytest.mark.parametrize(
    "reference,classification,diff_type,band",
    [
        # Exact GSTIN+invoice matches with only a value gap stay High.
        ("RTM/158", "Amount Difference", "Taxable Value Difference", "High"),
        ("SF-2305", "Amount Difference", "Tax Amount Difference", "High"),
        ("OT/77", "Amount Difference", "Tax Split Mismatch", "High"),
        ("RTM/162", "Amount Difference", "Tax Amount Difference", "High"),
        ("RTM/145", "Matched", "", "High"),
        ("DTS/998877", "Matched", "", "High"),
        # A leading-zero invoice-number variant is NOT an exact match.
        ("VE-234", "Matched", "", "Medium"),
        # Fuzzy-name match vs a structurally invalid GSTIN — distinct bands.
        ("OT/91", "Matched", "", "Medium"),
        ("BST/501", "Matched", "", "Low"),
        # No counterpart at all.
        ("GL/551", "Not in Portal", "", "Low"),
        ("VE-250", "Not in Portal", "", "Low"),
        ("PPS/912", "Not in Books", "", "Low"),
    ],
)
def test_per_scenario_classification_and_confidence(
    model, reference, classification, diff_type, band
):
    rows = _by_ref(model)[reference]
    assert len(rows) == 1, reference
    row = rows[0]
    assert row["classification"] == classification
    assert row["difference_type"] == diff_type
    assert row["confidence_band"] == band


def test_confidence_is_independent_of_value_gap(model):
    """A large value gap must not drag an exact-key match below High, and an
    invalid-GSTIN structure must not coincide with the fuzzy baseline."""
    rows = _by_ref(model)
    assert rows["RTM/158"][0]["confidence_score"] >= 92
    assert rows["SF-2305"][0]["confidence_score"] >= 92
    assert rows["OT/91"][0]["confidence_score"] == 65
    assert rows["BST/501"][0]["confidence_score"] < rows["OT/91"][0]["confidence_score"]


# --- F3 / F5 report rendering ------------------------------------------------

def test_report_row_value_single_side_and_difference_type(
    data_root, tmp_path
):
    from src import db, runner
    from src.reconciliation import report_export as ex

    db_path = tmp_path / "poc.db"
    rid = runner.execute_run(
        CLIENT, PERIOD, "GST", None, load_config(),
        db_path=db_path, selected_files=SELECTED, actor="test",
    )
    data = ex.build_export_data(rid, db_path=db_path)
    rows = {str(i["reference"]): i for i in data["invoices"]}

    # F3 — Invoice Value is the SAME side as the tax columns (portal-first):
    # the rounding case must read the portal total, not the books one.
    rounding = rows["DTS/998877"]
    assert rounding["invoice_value"] == 706.82
    assert round(rounding["taxable_value"] + rounding["total_tax"], 2) == rounding["invoice_value"]
    for ref in ("RTM/158", "SF-2305"):
        r = rows[ref]
        assert round(r["taxable_value"] + r["total_tax"], 2) == r["invoice_value"], ref

    # F5 — difference_type is carried in the data AND rendered as a column.
    assert rows["RTM/158"]["difference_type"] == "Taxable Value Difference"
    html = ex.render_html(data)
    assert "<th>Difference type</th>" in html
    assert "Taxable Value Difference" in html
    assert "Duplicate in Books" in html
