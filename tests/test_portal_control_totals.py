"""Regression — GSTR-2B ITC-summary control total must not fold
reverse-charge B2B invoices into the forward-charge ("B2B - Invoices
(IMS)") stated total.

A real GSTR-2B's B2B sheet carries BOTH forward-charge and reverse-charge
invoices. The ITC-summary states them separately:

  * "All other ITC - Supplies from registered persons other than reverse
    charge (IMS)" -> "B2B - Invoices (IMS)"   (forward-charge ITC)
  * "Inward Supplies liable for reverse charge" -> "B2B - Invoices"
    (reverse-charge ITC)

Comparing the WHOLE B2B sheet against the forward-charge row over-counts by
exactly the reverse-charge tax and hard-stops a perfectly good file (the
042025_07AAACP9472A1ZJ_GSTR2B_14052025 specimen failed with a ₹4.75 delta on
both CGST and SGST for precisely this reason).

This test builds a minimal two-sheet workbook with one reverse-charge row and
asserts each subset is checked against its own summary row — no hard stop.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from src.f6 import validation as f6validation
from src.ingestion_ai.f6_bridge import parse_portal_export, portal_control_totals

# Summary sheet geometry (0-based), mirroring the real GSTN layout.
_ITC_HEADER_ROW = 5          # "Heading" + tax columns
_ITC_FORWARD_DATA_ROW = 9    # "B2B - Invoices (IMS)"
_ITC_RC_DATA_ROW = 19        # "B2B - Invoices" under "liable for reverse charge"

_FORWARD_CGST = 100.00
_FORWARD_SGST = 100.00
_RC_CGST = 4.75
_RC_SGST = 4.75


def _put(ws, row0: int, col1: int, value) -> None:
    """Write a cell by 0-based row (matching how the reader sees the sheet)."""
    ws.cell(row=row0 + 1, column=col1, value=value)


def _build_itc_available(ws) -> None:
    _put(ws, 0, 1, "FORM GSTR-2B")
    _put(ws, 4, 1, "FORM SUMMARY - ITC Available")

    _put(ws, _ITC_HEADER_ROW, 3, "Heading")
    _put(ws, _ITC_HEADER_ROW, 4, "GSTR-3B table")
    _put(ws, _ITC_HEADER_ROW, 5, "Integrated Tax  (₹)")
    _put(ws, _ITC_HEADER_ROW, 6, "Central Tax (₹)")
    _put(ws, _ITC_HEADER_ROW, 7, "State/UT Tax (₹)")
    _put(ws, _ITC_HEADER_ROW, 8, "Cess  (₹)")

    # Section I — all other ITC (forward charge), then its detail rows.
    _put(ws, 7, 2, "I")
    _put(ws, 7, 3, "All other ITC - Supplies from registered persons other than reverse charge (IMS)")
    _put(ws, 7, 4, "4(A)(5)")
    _put(ws, 7, 6, _FORWARD_CGST)
    _put(ws, 7, 7, _FORWARD_SGST)

    _put(ws, _ITC_FORWARD_DATA_ROW, 2, "Details")
    _put(ws, _ITC_FORWARD_DATA_ROW, 3, "B2B - Invoices (IMS)")
    _put(ws, _ITC_FORWARD_DATA_ROW, 6, _FORWARD_CGST)
    _put(ws, _ITC_FORWARD_DATA_ROW, 7, _FORWARD_SGST)

    _put(ws, 10, 3, "B2B - Debit notes (IMS)")
    _put(ws, 11, 3, "ECO - Documents (IMS)")
    _put(ws, 12, 3, "B2B - Invoices (Amendment) (IMS)")
    _put(ws, 13, 3, "B2B - Debit notes (Amendment) (IMS)")
    _put(ws, 14, 3, "ECO - Documents (Amendment) (IMS)")

    # Section II — ISD (irrelevant detail rows).
    _put(ws, 15, 2, "II")
    _put(ws, 15, 3, "Inward Supplies from ISD")
    _put(ws, 16, 3, "ISD - Invoices")
    _put(ws, 17, 3, "ISD - Invoices (Amendment)")

    # Section III — reverse charge, with its own "B2B - Invoices" detail row.
    _put(ws, _ITC_RC_DATA_ROW, 2, "III")
    _put(ws, _ITC_RC_DATA_ROW, 3, "Inward Supplies liable for reverse charge")
    _put(ws, _ITC_RC_DATA_ROW, 4, "3.1(d)")
    _put(ws, _ITC_RC_DATA_ROW, 6, _RC_CGST)
    _put(ws, _ITC_RC_DATA_ROW, 7, _RC_SGST)

    _put(ws, _ITC_RC_DATA_ROW + 1, 2, "Details")
    _put(ws, _ITC_RC_DATA_ROW + 1, 3, "B2B - Invoices")
    _put(ws, _ITC_RC_DATA_ROW + 1, 6, _RC_CGST)
    _put(ws, _ITC_RC_DATA_ROW + 1, 7, _RC_SGST)


def _build_b2b(ws) -> None:
    parent = [
        "GSTIN of supplier", "Trade/Legal name", "Invoice Details", "", "", "",
        "Place of supply", "Supply Attract Reverse Charge", "Taxable Value (₹)",
        "Tax Amount", "", "", "",
    ]
    child = [
        "", "", "Invoice number", "Invoice type", "Invoice Date", "Invoice Value(₹)",
        "", "", "", "Integrated Tax(₹)", "Central Tax(₹)", "State/UT Tax(₹)", "Cess(₹)",
    ]
    for col, value in enumerate(parent, start=1):
        _put(ws, 4, col, value)
    for col, value in enumerate(child, start=1):
        _put(ws, 5, col, value)

    forward = ["27AAAAA0000A1Z5", "Forward Supplier", "INV-1", "Regular", "01/04/2025",
               "118.00", "27", "No", "100.00", "0", "100.00", "100.00", "0"]
    reverse = ["07AAFCP9264R1ZH", "Reverse Supplier", "INV-2", "Regular", "01/04/2025",
               "9.50", "27", "Yes", "5.00", "0", "4.75", "4.75", "0"]
    for col, value in enumerate(forward, start=1):
        _put(ws, 6, col, value)
    for col, value in enumerate(reverse, start=1):
        _put(ws, 7, col, value)


@pytest.fixture()
def gstr2b_file(tmp_path: Path) -> Path:
    wb = openpyxl.Workbook()
    itc = wb.active
    itc.title = "ITC Available"
    _build_itc_available(itc)
    b2b = wb.create_sheet("B2B")
    _build_b2b(b2b)
    path = tmp_path / "07052025_07AAACP9472A1ZJ_GSTR2B_01042025.xlsx"
    wb.save(path)
    return path


def test_control_totals_split_forward_and_reverse_charge(gstr2b_file: Path):
    from src.f6.parser import parse_file
    from src.f6.seed_formats import gstr2b_config

    parsed = parse_file(str(gstr2b_file), gstr2b_config(), is_excel=True)
    totals = portal_control_totals(gstr2b_file, "gstr2b", parsed.all_rows)
    by_label = {label: (parsed_sum, stated) for parsed_sum, stated, label in totals}

    # Forward-charge B2B is compared to the "(IMS)" row — NOT inflated by the
    # reverse-charge invoice.
    assert by_label["GSTR-2B ITC summary — B2B CGST"] == (_FORWARD_CGST, _FORWARD_CGST)
    assert by_label["GSTR-2B ITC summary — B2B SGST"] == (_FORWARD_SGST, _FORWARD_SGST)

    # Reverse-charge B2B is compared to ITS OWN summary row.
    assert by_label["GSTR-2B ITC summary — B2B reverse charge CGST"] == (_RC_CGST, _RC_CGST)
    assert by_label["GSTR-2B ITC summary — B2B reverse charge SGST"] == (_RC_SGST, _RC_SGST)


def test_no_hard_stop_for_reverse_charge_file(gstr2b_file: Path):
    result = parse_portal_export(gstr2b_file, "gstr2b", gstr2b_file.name)
    assert result is not None
    _df, _mappings, warnings, validation = result

    hard_stops = [v for v in validation if v["result"] == "hard_stop"]
    assert hard_stops == [], f"unexpected hard stops: {hard_stops}"
    assert not any("BLOCKED" in w for w in warnings)


def test_hard_stop_still_fires_when_a_subset_truly_mismatches(gstr2b_file: Path):
    """The split must not blunt the guardrail: a genuine over/under-count in
    the forward-charge subset still hard-stops."""
    check = f6validation.check_control_total(
        100.00, 200.00, label="GSTR-2B ITC summary — B2B CGST",
    )
    assert check.result == "hard_stop"
