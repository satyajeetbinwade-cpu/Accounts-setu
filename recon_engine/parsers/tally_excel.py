"""Parser for Tally purchase-register Excel exports.

Header-tolerant: accepts the common export spellings (Voucher Number / Date /
Party Name / GSTIN / Taxable Value / CGST / SGST / IGST, or a single
"Tax Amount" column). Rows are emitted as dicts compatible with
parse_tally_purchase_rows, which owns the Invoice construction.

Deterministic Tier-A parsing -- no LLM.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ..models import Invoice
from ..normalizer import norm_amount
from .excel_common import iter_rows, load_workbook, locate_header
from .gstr2b import parse_tally_purchase_rows

TALLY_ALIASES: dict[str, tuple[str, ...]] = {
    "voucher_no": ("voucher number", "voucher no", "vch no", "voucher"),
    "invoice_no": ("invoice number", "invoice no", "inv no", "bill no",
                   "bill number"),
    "date": ("voucher date", "date", "invoice date", "entry date",
             "posting date"),
    "party": ("party name", "supplier name", "party", "supplier", "ledger"),
    "gstin": ("gstin", "party gstin", "supplier gstin", "gstin no",
              "gst number"),
    "taxable": ("taxable value", "taxable_amount", "taxable amount",
                "taxable", "value"),
    "tax": ("tax amount", "gst amount", "tax", "gst"),
    "cgst": ("cgst", "c gst", "cgst amount"),
    "sgst": ("sgst", "s gst", "sgst amount"),
    "igst": ("igst", "i gst", "igst amount"),
    "rcm": ("reverse charge", "rcm"),
    "blocked": ("blocked credit", "itc blocked", "blocked"),
}


def _iso(v) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


def _row_tax(raw: dict) -> Decimal:
    """Total tax for a row: explicit Tax Amount, else CGST+SGST+IGST."""
    tax = raw.get("tax")
    if tax not in (None, ""):
        return norm_amount(tax)
    total = Decimal("0")
    for k in ("cgst", "sgst", "igst"):
        v = raw.get(k)
        if v not in (None, ""):
            total += norm_amount(v)
    return total


def parse_tally_excel(path: str | Path) -> list[Invoice]:
    """Parse a Tally purchase-register Excel export into book invoices."""
    wb = load_workbook(path)
    try:
        header_row, col_map = locate_header(wb.active, TALLY_ALIASES)
        if header_row is None or "taxable" not in col_map:
            return []
        rows: list[dict] = []
        for raw in iter_rows(wb.active, header_row, col_map):
            inv_no = raw.get("invoice_no") or raw.get("voucher_no")
            if inv_no is None or str(inv_no).strip() == "":
                continue
            rows.append({
                "gstin": raw.get("gstin") or "",
                "invoice_no": inv_no,
                "date": _iso(raw.get("date") or ""),
                "taxable": raw.get("taxable") or "0",
                "tax": str(_row_tax(raw)),
                "party": raw.get("party") or "",
                "rcm": raw.get("rcm"),
                "blocked": raw.get("blocked"),
            })
        return parse_tally_purchase_rows(rows)
    finally:
        wb.close()
