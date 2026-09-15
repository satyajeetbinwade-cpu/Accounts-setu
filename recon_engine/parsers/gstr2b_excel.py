"""Parser for GSTR-2B Excel exports (GSTN portal download).

The portal's GSTR-2B download produces an .xlsx workbook with invoice-level
sheets (B2B, B2BA, ISD, ...). This parser reads every sheet whose name
suggests invoices (B2B / B2BA / doclist / invoices), merges the rows and
deduplicates identical invoices (same gstin + invoice no + date + value).

Header rows are located token-wise (see excel_common), so column spellings
may vary between portal versions. The Status column is normalised to the
engine's vocabulary: "R"/"filed" -> active, "N"/"not filed" -> non_filer.

All parsing is deterministic Tier-A code.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from ..normalizer import make_invoice
from ..models import Invoice
from .excel_common import iter_rows, load_workbook, locate_header

B2B_ALIASES: dict[str, tuple[str, ...]] = {
    "gstin": ("gstin of supplier", "supplier gstin", "gstin", "gstin no",
              "supplier_gstin", "gst no"),
    "invoice_no": ("invoice number", "invoice no", "inv no", "inv number",
                   "docnum", "invoice_number"),
    "invoice_date": ("invoice date", "inv date", "doc date", "docdt", "date"),
    "invoice_value": ("invoice value", "inv value", "total value",
                      "invoice amount"),
    "taxable": ("taxable value", "taxable_value", "taxable amount", "txval",
                "assessable value"),
    "cgst": ("cgst", "c gst", "cgst amount"),
    "sgst": ("sgst", "s gst", "sgst amount"),
    "igst": ("igst", "i gst", "igst amount"),
    "status": ("status", "supplier status", "filing status", "return status"),
    "rcm": ("reverse charge", "supply attract reverse charge", "rcm"),
    "blocked": ("itc blocked", "blocked credit", "itc availability",
                "section 17(5)", "17(5)", "blocked"),
}

# GSTR-2B Status column: "R" = supplier's return filed, "N" = not filed.
_STATUS_MAP = {
    "r": "active", "filed": "active", "y": "active", "yes": "active",
    "n": "non_filer", "not filed": "non_filer", "nf": "non_filer",
    "no": "non_filer", "": "",
}

_INVOICE_SHEET_RE = re.compile(r"(^|[_ \-])(b2b|b2ba|doclist|invoices?|cdnr)"
                               r"|(^|[_ \-])invoice", re.IGNORECASE)


def _date_iso(v) -> str | date:
    """Excel date cells arrive as datetime/date objects; strings pass through."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return str(v).strip()


def _parse_sheet(ws) -> list[Invoice]:
    header_row, col_map = locate_header(ws, B2B_ALIASES)
    if header_row is None or "invoice_no" not in col_map:
        return []
    out: list[Invoice] = []
    for row in iter_rows(ws, header_row, col_map):
        inv_no = row.get("invoice_no")
        if inv_no is None or str(inv_no).strip() == "":
            continue
        status_raw = str(row.get("status") or "").strip().lower()
        status = _STATUS_MAP.get(status_raw, status_raw)
        taxable = row.get("taxable")
        if taxable in (None, ""):
            # Some exports only carry the gross Invoice Value column.
            taxable = row.get("invoice_value") or "0"
        try:
            out.append(make_invoice(
                gstin=row.get("gstin") or "",
                invoice_no=inv_no,
                date=_date_iso(row.get("invoice_date") or ""),
                taxable_value=taxable,
                cgst=row.get("cgst") or "0",
                sgst=row.get("sgst") or "0",
                igst=row.get("igst") or "0",
                source="portal",
                status=status,
                reverse_charge=row.get("rcm"),
                blocked_credit=row.get("blocked"),
                reference=str(inv_no).strip(),
            ))
        except Exception:
            # skip malformed rows; F5 flags date/GSTIN issues later
            continue
    return out


def parse_gstr2b_excel(path: str | Path) -> list[Invoice]:
    """Parse a GSTR-2B Excel export into normalised portal invoices."""
    wb = load_workbook(path)
    try:
        invoices: list[Invoice] = []
        seen: set[tuple] = set()
        for ws in wb.worksheets:
            if not _INVOICE_SHEET_RE.search(ws.title):
                continue
            for inv in _parse_sheet(ws):
                key = (inv.gstin, inv.invoice_no, inv.date, inv.taxable_value)
                if key in seen:
                    continue
                seen.add(key)
                invoices.append(inv)
        return invoices
    finally:
        wb.close()
