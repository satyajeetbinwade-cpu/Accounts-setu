"""Regression tests for the Test Set 3 (Kavya Apparels) GST QA bug-fix pass.

Encodes the QA-verified expectations for:

  Bug 1  headline "ITC at stake" = SUM over ALL exception buckets, and the
         integrity check compares the DISPLAYED headline against the recount
  Bug 2  blank-GSTIN fuzzy party fallback (OFH/20 → one Matched row)
  Bug 3  Difference column = Portal invoice value − Books invoice value on a
         multi-factor row; ONE shared gross-invoice-value computation
  Bug 4  Invoice Type ingested and compared (OFH/12 → Document Type Mismatch)
  Bug 5  multi-factor rows surface "Unexplained"; the tax-component check is no
         longer disabled by a missing cess column

plus the do-not-regress rules (no duplicate rows for a shared key, no
credit/debit notes leaking into the invoice pool, one side per report row).

Run:

    venv/bin/python -m pytest tests/test_gst_recon_set3.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src import bootstrap
from src.config_loader import load_config
from src.ingestion_ai.normalizer import load_canonical_pair
from src.matching.gst_matcher import match_gst
from src.reconciliation import review

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "set3"
GSTR2B = FIXTURES / "092026_09AAFFK7654G1ZS_GSTR2B_TESTSET3.xlsx"
IMS = FIXTURES / "IMS_09AAFFK7654G1ZS_TESTSET3.xlsx"
BOOKS = FIXTURES / "PurchaseRegister_TESTSET3_KavyaApparels.xlsx"
CLIENT = "Test Client"
PERIOD = "2026-08"
SELECTED = {"tally": BOOKS.name, "gstr2b": GSTR2B.name, "ims": IMS.name}

# Count reconciliation against Set3_GroundTruth (B1 ambiguity folded to Matched,
# exactly as the QA count table does).
EXPECTED_TOTAL = 19
EXPECTED_MATCHED = 13
EXPECTED_AMOUNT_DIFF = 2
EXPECTED_NOT_IN_BOOKS = 2
EXPECTED_NOT_IN_PORTAL = 2


@pytest.fixture(scope="module")
def data_root(tmp_path_factory):
    """Point the engine's DATA_ROOT at a private copy of the three files."""
    root = tmp_path_factory.mktemp("set3_data")
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
def db_path(tmp_path_factory):
    """An isolated DB so a stored result from another test/run can never be
    replayed in place of a fresh parse."""
    path = tmp_path_factory.mktemp("set3_db") / "poc.db"
    bootstrap.init_all(path)
    return path


@pytest.fixture(scope="module")
def loaded(data_root, db_path):
    return load_canonical_pair(
        CLIENT, PERIOD, "GST", SELECTED, client_id=None, db_path=db_path,
    )


@pytest.fixture(scope="module")
def results(loaded):
    books, portal, _files, _caveats, _notes = loaded
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


# --- Bug 1: headline + integrity --------------------------------------------

def test_count_reconciliation(model):
    k = model["kpi"]
    assert k["total_count"] == EXPECTED_TOTAL
    assert k["matched_count"] == EXPECTED_MATCHED
    assert k["amount_difference_count"] == EXPECTED_AMOUNT_DIFF
    assert k["not_in_books_count"] == EXPECTED_NOT_IN_BOOKS
    assert k["not_in_portal_count"] == EXPECTED_NOT_IN_PORTAL


def test_headline_is_sum_of_every_exception_bucket(model):
    k = model["kpi"]
    exception_tax = round(sum(i["tax"] for i in model["items"] if i["bucket"] != "Matched"), 2)
    cause_tax = round(sum(s["tax_value"] for s in model["cause_segments"]), 2)
    # The headline, the row recount and the cause split must be the SAME number.
    assert k["itc_at_stake_tax"] == exception_tax
    assert k["itc_at_stake_tax"] == cause_tax
    # And it must be strictly larger than any single bucket (the old bug bound
    # the headline to the Not-in-Books bucket alone).
    assert k["itc_at_stake_tax"] > k["not_in_books_tax"]


def test_headline_integrity_check_flags_a_divergent_binding(model):
    """The self-check must be REAL: if the headline is re-bound to a single
    bucket (the Run-4 defect) the recount must report a mismatch."""
    assert review.headline_integrity(model)["ties"] is True

    doctored = {**model, "kpi": {**model["kpi"]}}
    doctored["kpi"]["itc_at_stake_tax"] = model["kpi"]["not_in_books_tax"]
    chk = review.headline_integrity(doctored)
    assert chk["ties"] is False
    assert chk["headline"] != chk["recount"]


def test_gross_invoice_value_is_one_shared_computation(model):
    k = model["kpi"]
    expected = round(
        sum((i["portal_value"] or i["books_value"])
            for i in model["items"] if i["bucket"] != "Matched"), 2
    )
    assert k["gross_value"] == expected
    # Never the headline, and never a row's sub-component sum.
    assert k["gross_value"] != k["itc_at_stake_tax"]


# --- Bug 2: blank-GSTIN fuzzy fallback --------------------------------------

def test_blank_gstin_invoice_is_one_matched_row(model):
    rows = _by_ref(model)["OFH/20"]
    assert len(rows) == 1, [r["classification"] for r in rows]
    row = rows[0]
    assert row["classification"] == "Matched"
    assert row["confidence_band"] == "Medium"
    assert row["confidence_score"] == 65


# --- Bug 3 + 5: multi-factor row --------------------------------------------

def test_multifactor_row_is_unexplained_with_full_invoice_gap(model):
    row = _by_ref(model)["BSH/515"][0]
    assert row["classification"] == "Amount Difference"
    assert row["difference_type"] == "Unexplained"
    # Difference column = Portal invoice value − Books invoice value (₹1,180),
    # never the taxable-only sub-component (₹1,000).
    assert row["difference"] == pytest.approx(1180.0, abs=0.01)
    # More than one discrepancy is enumerated in the reason.
    assert "Taxable Value Difference" in row["match_reason"]
    assert "Tax Split Mismatch" in row["match_reason"]


# --- Bug 4: document type mismatch ------------------------------------------

def test_invoice_type_mismatch_surfaces(model):
    row = _by_ref(model)["OFH/12"][0]
    assert row["classification"] == "Amount Difference"
    assert row["difference_type"] == "Document Type Mismatch"


# --- Bug 5: cess no longer disables the tax-component check ------------------

def test_cess_mapping_does_not_disable_tax_component_check(loaded):
    _books, _portal, _files, caveats, _notes = loaded
    codes = {c["code"] for c in caveats}
    assert "check_tax_amounts" not in codes
    assert "check_cess" in codes


# --- Do-not-regress + Set-3 specific residuals ------------------------------

def test_debit_note_is_matched_not_leaked(model):
    """The debit note (S3-F1) is a genuine charge — it must match, and must NOT
    appear as an orphan exception or as an unreconciled credit note."""
    row = _by_ref(model)["DN-DN/City/07"][0]
    assert row["classification"] == "Matched"
    # No credit-note leak either way.
    refs = {str(i["reference"]) for i in model["items"]}
    assert not any(r.upper().startswith("CN") for r in refs)


def test_no_duplicate_rows_for_shared_key(model):
    keys: dict[tuple, int] = {}
    for i in model["items"]:
        keys[(i["gstin"], str(i["reference"]))] = keys.get((i["gstin"], str(i["reference"])), 0) + 1
    assert all(n == 1 for n in keys.values()), {k: n for k, n in keys.items() if n > 1}


def test_eligible_credit_uses_portal_side_tax(db_path):
    """SG/301 books tax 3,241.80 vs portal 3,240.00 — the eligible-credit
    figure must read the SAME (portal) side the review model and report use,
    or it drifts from the matched rows by the ₹1.80 books-side rounding."""
    import json
    import sqlite3

    from src.module2 import service as m2

    conn = sqlite3.connect(db_path)
    try:
        fig = m2._compute_eligible_credit(
            conn, client_id=1, run_id=1, period=PERIOD,
            results=[{
                "classification": "Matched",
                "books_record": json.dumps({"total_tax": 3241.80, "gstin": "09AABCS8888H1Z1"}),
                "portal_record": json.dumps({"total_tax": 3240.00, "gstin": "09AABCS8888H1Z1"}),
            }],
        )
    finally:
        conn.close()
    assert fig["matched_itc"] == pytest.approx(3240.00, abs=0.01)
    assert fig["eligible_credit"] == pytest.approx(3240.00, abs=0.01)


# --- End-to-end report path -------------------------------------------------

def test_report_headline_and_difference_type_render(data_root, db_path):
    from src import runner
    from src.reconciliation import report_export as ex

    rid = runner.execute_run(
        CLIENT, PERIOD, "GST", None, load_config(),
        db_path=db_path, selected_files=SELECTED, actor="test",
    )
    data = ex.build_export_data(rid, db_path=db_path)
    k = data["kpi"]
    # Headline == Σ exception tax (the report and the screen must agree).
    assert k["itc_at_stake_tax"] == pytest.approx(23223.60, abs=0.01)
    rows = {str(i["reference"]): i for i in data["invoices"]}
    assert rows["BSH/515"]["difference_type"] == "Unexplained"
    assert rows["OFH/12"]["difference_type"] == "Document Type Mismatch"
    # Invoice value is single-side (portal-first) and ties out.
    for ref in ("BSH/515", "OFH/12"):
        r = rows[ref]
        assert round(r["taxable_value"] + r["total_tax"], 2) == r["invoice_value"], ref
    # The debit note is reconciled as an invoice, so it is NOT in the separately
    # reported credit-note list.
    assert not any("DN" in str(n["reference"]).upper() for n in data["credit_notes"])
