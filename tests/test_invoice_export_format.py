"""Regression tests for the F3-B invoice export format (PURCHASE-FORMAT3).

WHY THESE EXIST
---------------
The invoice module originally exported a 16-column "Tally Import" sheet whose
headers were the internal canonical field labels. The client's purchase
register template (``PURCHASE-FORMAT3.xlsx``) is a 14-column sheet with its own
header wording, and it carries BOTH a rate and an amount for each tax head
while a real invoice usually prints only ONE of the two.

These tests lock in the four behaviours that make that work:

1. The exported header row is exactly the PURCHASE-FORMAT3 layout (typos
   corrected) and nothing else.
2. A tax head present as an AMOUNT gets its RATE derived, and a head present
   only as a RATE gets its AMOUNT derived — the export never leaves a
   derivable column blank.
3. The review GATE does not send a derivable tax half to a human, but
   sum-derived fields (total tax / invoice total) and a genuinely absent tax
   head (IGST on an intra-state invoice) still gate.
4. A recorded (immutable) batch re-downloads in the same layout.

Plus one extractor test: a printed "CGST @ 9%: 1,161.00" must read 9 as the
RATE and 1,161.00 as the AMOUNT — not the other way round.

Run:

    venv/bin/python -m pytest tests/test_invoice_export_format.py -v
"""

from __future__ import annotations

import io

import pandas as pd

from src import db as recon_db
from src.invoice_extract import db as idb
from src.invoice_extract import extractor
from src.invoice_extract import service as ie
from src.invoice_extract.seed import CANONICAL_FIELD_KEYS, EXPORT_HEADERS

EXPECTED_HEADERS = [
    "SUPPLIER INV NO",
    "INVOICE DATE",
    "PARTY A/C NAME",
    "PLACE OF SUPPLY",
    "PO Number",
    "Taxable Amount",
    "SGST %",
    "SGST Amount",
    "CGST %",
    "CGST Amount",
    "IGST %",
    "IGST Amount",
    "Total Invoice AMOUNT",
    "Description",
]

# High-confidence fields every fixture starts from — deliberately WITHOUT any
# tax rate, because real invoices print tax AMOUNTS, not rates.
_BASE = {
    "invoice_number": ("SBE/0191", 95),
    "invoice_date": ("12-Sep-2026", 93),
    "vendor_name": ("Shree Balaji Hardware", 95),
    "vendor_gstin": ("27AABCM1234K1Z9", 96),
    "place_of_supply": ("Maharashtra", 90),
    "reference_po": ("PO-7781", 92),
    "hsn_sac": ("7308", 94),
    "item_description": ("MS Pipe", 95),
    "quantity": ("10", 92),
    "rate": ("430", 92),
    "taxable_value": ("12,900.00", 93),
    "total_tax": ("2,322.00", 93),
    "invoice_total": ("15,222.00", 94),
}


def _seed(tmp_path, overrides=None, absent=()):
    """Create a scratch F3-B DB with ONE upload whose fields are the union of
    _BASE and ``overrides``; everything else is stored "not present" (exactly
    what the extractors do for a field they can't read)."""
    db_path = str(tmp_path / "scratch.db")
    ie.init_invoice_extract(db_path)

    values = {**_BASE, **(overrides or {})}
    conn = recon_db.get_connection(db_path)
    upload_id = idb.create_upload(
        conn, client_id=1, filename="inv.xlsx", file_ext="xlsx", source_format="excel",
        extraction_path="structured", batch_id=None, status="needs_review",
        duplicate_warning=False, actor="admin", ai_used=True, ai_model="test-model",
    )
    for key in CANONICAL_FIELD_KEYS:
        if key in absent:
            value, conf, present = None, None, False
        elif key in values:
            value, conf, present = values[key][0], values[key][1], True
        else:
            value, conf, present = None, None, False
        idb.insert_field(conn, upload_id=upload_id, result={
            "field_name": key, "extracted_value": value, "confidence": conf,
            "source_location": "test", "is_present": present,
        })
    conn.close()
    return db_path, upload_id


def _reviewable(db_path, upload_id) -> list[str]:
    return [r["field_name"] for r in ie.get_reviewable_fields(upload_id, db_path=db_path)]


def _export_row(db_path, upload_id, fmt="xlsx") -> tuple[dict, dict]:
    ie.confirm_upload(upload_id, actor="admin", db_path=db_path)
    result = ie.generate_export(upload_ids=[upload_id], actor="admin", fmt=fmt, db_path=db_path)
    if fmt == "csv":
        df = pd.read_csv(io.BytesIO(result["data"]), dtype=str, keep_default_na=False)
    else:
        df = pd.read_excel(io.BytesIO(result["data"]), dtype=str, keep_default_na=False)
    assert list(df.columns) == EXPECTED_HEADERS
    return df.iloc[0].to_dict(), result


# ---------------------------------------------------------------------------
# 1. Header row is exactly the PURCHASE-FORMAT3 layout
# ---------------------------------------------------------------------------


def test_export_headers_match_purchase_format3():
    assert EXPORT_HEADERS == EXPECTED_HEADERS


def test_export_header_order_and_sheet(tmp_path):
    db_path, upload_id = _seed(tmp_path, {"cgst_amount": ("1,161.00", 93),
                                          "sgst_amount": ("1,161.00", 93),
                                          "igst_amount": ("0", 93)})
    row, result = _export_row(db_path, upload_id)
    # Both a RATE and an AMOUNT column exist for every tax head.
    for head in ("SGST", "CGST", "IGST"):
        assert f"{head} %" in row and f"{head} Amount" in row
    # The template's typos are corrected, and its trailing apostrophe is gone.
    assert "Discription" not in row
    assert "Taxable Amount'" not in row
    assert result["format_version"] == "purchase-format3-1.0"
    assert result["filename"].startswith("purchase_register_export_")


# ---------------------------------------------------------------------------
# 2. Rate ↔ amount derivation (both directions)
# ---------------------------------------------------------------------------


def test_amounts_derive_rates(tmp_path):
    """An invoice that prints only the tax AMOUNTS still fills the rate
    columns (9% on 12,900 = 1,161)."""
    db_path, upload_id = _seed(tmp_path, {"cgst_amount": ("1,161.00", 93),
                                          "sgst_amount": ("1,161.00", 93),
                                          "igst_amount": ("0", 93)})
    row, _ = _export_row(db_path, upload_id)
    assert row["Taxable Amount"] == "12,900.00"
    assert row["CGST Amount"] == "1,161.00" and row["CGST %"] == "9"
    assert row["SGST Amount"] == "1,161.00" and row["SGST %"] == "9"
    assert row["IGST Amount"] == "0" and row["IGST %"] == "0"
    assert row["Total Invoice AMOUNT"] == "15,222.00"


def test_rates_derive_amounts(tmp_path):
    """An invoice that prints only the tax RATES still fills the amount
    columns (9% of 12,900 = 1,161.00)."""
    db_path, upload_id = _seed(tmp_path, {"cgst_rate": ("9", 92),
                                          "sgst_rate": ("9", 92),
                                          "igst_rate": ("0", 92)})
    row, _ = _export_row(db_path, upload_id)
    assert row["CGST %"] == "9" and row["CGST Amount"] == "1161.00"
    assert row["SGST %"] == "9" and row["SGST Amount"] == "1161.00"
    assert row["IGST %"] == "0" and row["IGST Amount"] == "0.00"


def test_present_rate_is_used_verbatim_but_normalised(tmp_path):
    """When the document DOES print a rate (even "9%"), it is used as-is —
    minus the '%', because the column header already carries it."""
    db_path, upload_id = _seed(tmp_path, {"cgst_amount": ("1,161.00", 93),
                                          "sgst_amount": ("1,161.00", 93),
                                          "igst_amount": ("0", 93),
                                          "cgst_rate": ("9%", 60)})
    row, _ = _export_row(db_path, upload_id)
    assert row["CGST %"] == "9"
    assert row["CGST Amount"] == "1,161.00"


# ---------------------------------------------------------------------------
# 3. The review gate
# ---------------------------------------------------------------------------


def test_derivable_tax_rates_do_not_gate(tmp_path):
    """A plain amounts-only invoice must not land 3 extra rate fields in the
    review queue — they are derived, not reviewed."""
    db_path, upload_id = _seed(tmp_path, {"cgst_amount": ("1,161.00", 93),
                                          "sgst_amount": ("1,161.00", 93),
                                          "igst_amount": ("0", 93)})
    assert _reviewable(db_path, upload_id) == []


def test_absent_intra_state_igst_still_gates(tmp_path):
    """Neither half of the IGST pair is on an intra-state invoice and neither
    is derivable, so it must still be reviewed (exactly as before)."""
    db_path, upload_id = _seed(tmp_path, {"cgst_amount": ("1,161.00", 93),
                                          "sgst_amount": ("1,161.00", 93)})
    assert _reviewable(db_path, upload_id) == ["igst_amount"]


def test_sum_derived_fields_still_gate(tmp_path):
    """total_tax / invoice_total are derivable sums, but they keep the OLD
    behaviour — a genuinely missing total still needs a human."""
    db_path, upload_id = _seed(
        tmp_path,
        {"cgst_amount": ("1,161.00", 93), "sgst_amount": ("1,161.00", 93), "igst_amount": ("0", 93)},
        absent=("total_tax", "invoice_total"),
    )
    assert set(_reviewable(db_path, upload_id)) == {"total_tax", "invoice_total"}


# ---------------------------------------------------------------------------
# 4. Immutable batch re-download
# ---------------------------------------------------------------------------


def test_recorded_batch_redownloads_in_same_layout(tmp_path):
    db_path, upload_id = _seed(tmp_path, {"cgst_amount": ("1,161.00", 93),
                                          "sgst_amount": ("1,161.00", 93),
                                          "igst_amount": ("0", 93)})
    _row, result = _export_row(db_path, upload_id)
    batch = ie.get_export_batch(result["batch_id"], db_path=db_path)
    again = ie.regenerate_export_bytes(batch["batch_id"], db_path=db_path)
    df = pd.read_excel(io.BytesIO(again), dtype=str, keep_default_na=False)
    assert list(df.columns) == EXPECTED_HEADERS
    assert df.iloc[0]["CGST %"] == "9"
    assert df.iloc[0]["SUPPLIER INV NO"] == "SBE/0191"


# ---------------------------------------------------------------------------
# 5. Deterministic extractor: rate vs amount, not the other way round
# ---------------------------------------------------------------------------


def test_text_extraction_separates_rate_from_amount():
    text = (
        "TAX INVOICE Invoice No.: SBE/0191 Date: 12-Sep-2026 "
        "Vendor: Shree Balaji Hardware GSTIN: 27AABCM1234K1Z9 "
        "Taxable Value: 12,900.00 CGST @ 9%: 1,161.00 SGST @ 9%: 1,161.00 "
        "Total Tax: 2,322.00 Invoice Total: 15,222.00"
    )
    by_name = {r["field_name"]: r for r in extractor._extract_from_text(text)}
    assert by_name["taxable_value"]["extracted_value"] == "12,900.00"
    assert by_name["cgst_rate"]["extracted_value"] == "9"
    assert by_name["cgst_amount"]["extracted_value"] == "1,161.00"
    assert by_name["sgst_rate"]["extracted_value"] == "9"
    assert by_name["sgst_amount"]["extracted_value"] == "1,161.00"
    # IGST is genuinely absent on an intra-state invoice — never invented.
    assert by_name["igst_amount"]["is_present"] is False
    assert by_name["igst_rate"]["is_present"] is False
