"""Regression tests for the upload experience fixes.

WHY THESE EXIST
---------------
Three separate defects were reported on the Reconcile Stage-2 upload card
(and, for one of them, on the other upload screens too):

1. Choosing a file showed no selection state and clicking Upload gave no
   progress — unlike Smart Ingestion / Invoice Extraction, which stage the
   file as a chip and advance a per-file progress row.
2. Once uploaded, a file kept a "done" tick in the strip ABOVE the Upload
   button instead of moving to the module's own uploaded list.
3. The upload slot had to be chosen by hand even though the file itself says
   what it is.

The fixes are shared, so these tests pin the shared pieces:

* ``src.ingestion_ai.detect`` — the deterministic, zero-model slot detector.
* ``setu.state.shared_upload`` — the staged-file helpers every screen uses,
  plus ``drop_completed`` which removes finished rows from the upload strip.

Run:

    venv/bin/python -m pytest tests/test_upload_slot_detect.py -v
"""

from __future__ import annotations

import pytest

from src.ingestion_ai import detect
from setu.state import shared_upload as su


# ---------------------------------------------------------------------------
# 1. Slot detection — filename signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("082026_07AAACP9472A1ZJ_GSTR2B_15092026 (1).xlsx", "gstr2b"),
        ("gstr2b_2026-08_export.csv", "gstr2b"),
        ("GSTR-2B_July.xlsx", "gstr2b"),
        ("IMS_B2B-CN_07AAACP9472A1ZJ_19052025.xlsx", "ims"),
        ("ims_v3_final_action_19jul.csv", "ims"),
        ("Credit Note.xlsx", "credit_notes"),
        ("Debit Note Register Aug.xlsx", "credit_notes"),
        ("PurchaseRegister(Bill-wise).xlsx", "tally"),
        ("purchase_register_apr2025.csv", "tally"),
        ("form26as_v1_partial_early.csv", "form26as"),
        ("tds_return_challan_v1_filed_15jul.csv", "tds"),
        ("bank_statement_jun.csv", "bank"),
    ],
)
def test_detect_from_filename(filename, expected):
    assert detect.detect_source_type(filename, [])[0] == expected


def test_detect_returns_none_when_the_name_says_nothing():
    """An uninformative name with no corroborating headers must NOT guess."""
    assert detect.detect_source_type("export.xlsx", ["Col1", "Col2"])[0] is None


# ---------------------------------------------------------------------------
# 2. Slot detection — header signal (the filename is uninformative)
# ---------------------------------------------------------------------------


def test_detect_from_headers_only():
    headers = [
        "GSTIN of supplier",
        "Invoice Details :: Invoice number",
        "Tax Amount :: Integrated Tax(₹)",
        "ITC Availability",
        "Supply attract reverse charge",
    ]
    key, reason = detect.detect_source_type("Sheet1.xlsx", headers)
    assert key == "gstr2b"
    assert "headers" in reason


def test_detect_ims_from_headers_only():
    headers = [
        "GSTIN of supplier",
        "Status",
        "Amount declared by taxpayer for ITC reduction :: Central tax",
    ]
    assert detect.detect_source_type("Sheet1.xlsx", headers)[0] == "ims"


def test_one_generic_header_is_not_enough():
    """A single shared header ("Date") must not trigger a confident guess."""
    assert detect.detect_source_type("Sheet1.xlsx", ["Date"])[0] is None


# ---------------------------------------------------------------------------
# 3. allowed filter + ambiguity
# ---------------------------------------------------------------------------


def test_allowed_restricts_the_candidates():
    """A Credit Note is not a slot on a TDS run, so it must not be offered."""
    allowed = {"tally", "form26as", "tds"}
    assert detect.detect_source_type("Credit Note.xlsx", [], allowed=allowed)[0] != "credit_notes"


def test_detection_is_stable_for_the_real_gstr2b_name():
    key, reason = detect.detect_source_type(
        "042025_07AAACP9472A1ZJ_GSTR2B_14052025 (1).xlsx", []
    )
    assert key == "gstr2b"
    assert "GSTR2B" in reason or "gstr2b" in reason


def test_unreadable_path_yields_no_guess(tmp_path):
    assert detect.detect_source_type_from_path("x.xlsx", str(tmp_path / "missing.xlsx")) == (None, "")


def test_detect_from_a_real_file_on_disk(tmp_path):
    """End-to-end: read the header row from an actual CSV and classify it."""
    import pandas as pd

    path = tmp_path / "some_export.csv"
    pd.DataFrame(
        {
            "Bill No": ["A-1"],
            "Date": ["01/04/2025"],
            "Name & Address of Dealer": ["Acme"],
            "GSTIN": ["07AAACP9472A1ZJ"],
            "Bill Amount": [1000],
        }
    ).to_csv(path, index=False)
    key, _reason = detect.detect_source_type_from_path(path.name, str(path))
    assert key == "tally"


# ---------------------------------------------------------------------------
# 4. Shared staging helpers
# ---------------------------------------------------------------------------


def test_build_pending_persists_bytes_and_labels():
    pf = su.build_pending("data.csv", b"a,b\n1,2\n")
    try:
        assert pf is not None
        assert pf.filename == "data.csv"
        assert pf.ext == "csv"
        assert pf.key == "data.csv:8"
        assert pf.size_label == "8 B"
        assert su.read_staged_bytes(pf.stage_path) == b"a,b\n1,2\n"
    finally:
        if pf is not None:
            su.discard_staged(pf.stage_path)
    assert not su.read_staged_bytes(pf.stage_path)


def test_build_pending_ignores_an_empty_read():
    assert su.build_pending("empty.csv", b"") is None


def test_drop_completed_removes_finished_rows_but_keeps_failures():
    rows = [
        su.UploadProgressRow(key="a", filename="a.csv", stage="done", pct=100, label="Uploaded"),
        su.UploadProgressRow(key="b", filename="b.csv", stage="needs_review", pct=0, label="Routed"),
        su.UploadProgressRow(key="c", filename="c.csv", stage="failed", pct=0, label="Boom"),
        su.UploadProgressRow(key="d", filename="d.csv", stage="uploading", pct=60, label=""),
    ]
    kept = su.drop_completed(rows)
    assert [r.key for r in kept] == ["c", "d"]


def test_advance_stage_updates_one_row_in_place():
    rows = [
        su.UploadProgressRow(key="a", filename="a.csv", stage="queued", pct=0, label="Queued"),
        su.UploadProgressRow(key="b", filename="b.csv", stage="queued", pct=0, label="Queued"),
    ]
    out = su.advance_stage(rows, "a", "uploading", pct=60, label="")
    assert out[0].stage == "uploading" and out[0].pct == 60
    assert out[1].stage == "queued"


def test_human_size_units():
    assert su.human_size(512) == "512 B"
    assert su.human_size(2048) == "2 KB"
    assert su.human_size(2 * 1024 * 1024) == "2.0 MB"
