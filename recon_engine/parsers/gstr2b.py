"""Parsers for GSTR-2B JSON export and Tally purchase register rows.

These are ingest adapters: they read raw external data and emit `Invoice`
objects via the normalisation layer. All parsing is deterministic (Tier-A).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from ..normalizer import make_invoice, norm_gstin, norm_invoice_no, norm_date_str
from ..models import Invoice


# ---------------------------------------------------------------------------
# GSTR-2B JSON
# ---------------------------------------------------------------------------
def _extract(rec: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        v = rec.get(k)
        if v not in (None, ""):
            return v
    return ""


def iter_2b_invoices(data: dict[str, Any]):
    """Yield supplier-invoice dicts from a GSTR-2B JSON structure.

    Handles both:
      - { "docdata": { "doclist": [ { "docdt", "docnum", "supplierGstin", ... } ] } }
      - { "b2b": [ { "inv": [ {...} ] } ] }
    """
    # Latest-ish format used by GSTN offline tools / downloads
    for table in (data.get("docdata"), data.get("b2b"), data.get("invoices")):
        if not table:
            continue
        if isinstance(table, dict):
            for lst in (table.get("doclist"), table.get("docs")):
                if lst:
                    yield from lst
        elif isinstance(table, list):
            for entry in table:
                if isinstance(entry, dict) and "inv" in entry:
                    yield from entry["inv"]
                elif isinstance(entry, dict):
                    yield entry


def parse_gstr2b_json(path: str | Path) -> list[Invoice]:
    """Parse a GSTR-2B JSON file into normalised invoices."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    invoices: list[Invoice] = []
    for raw in iter_2b_invoices(data):
        gstin = norm_gstin(_extract(raw, ("supplierGstin", "supplier_gstin", "gstin")))
        inv_no = norm_invoice_no(_extract(raw, ("docnum", "invoiceNo", "invoice_no", "num")))
        date_raw = _extract(raw, ("docdt", "invoiceDate", "invoice_date", "dt"))
        taxable = _extract(raw, ("taxableValue", "taxable_value", "txval", "value"))
        cgst = _extract(raw, ("cgst", "cGst"))
        sgst = _extract(raw, ("sgst", "sGst"))
        igst = _extract(raw, ("igst", "iGst"))
        status = _extract(raw, ("status", "supplierStatus", "is2b"))
        # Phase-2 flags: RCM supply + §17(5) blocked credit / ITC availability
        rcm = _extract(raw, ("rcm", "isReverseCharge", "reverseCharge"))
        blocked = _extract(raw, ("isBlocked", "blocked", "blockedCredit",
                                 "itcAvailability", "itcAvail"))
        if not inv_no:
            continue
        try:
            invoices.append(make_invoice(
                gstin=gstin, invoice_no=inv_no, date=date_raw,
                taxable_value=taxable or "0", cgst=cgst or "0",
                sgst=sgst or "0", igst=igst or "0",
                source="portal", status=status,
                reverse_charge=rcm, blocked_credit=blocked,
                reference=inv_no,
            ))
        except Exception:
            continue
    return invoices


# ---------------------------------------------------------------------------
# Tally purchase register (ODBC rows)
# ---------------------------------------------------------------------------
def parse_tally_purchase_rows(rows: Iterable[Any]) -> list[Invoice]:
    """Convert Tally ODBC purchase register rows into invoices.

    Accepts a list of row objects/tuples/dicts. Dict keys may use the Tally
    ODBC aliases ($Name, $Date, $PartyName, $VoucherNumber, $GSTIN,
    $TaxableValue, $TaxAmount) or plain snake_case.
    """
    invoices: list[Invoice] = []
    for row in rows:
        if hasattr(row, "_asdict"):
            row: dict[str, Any] = row._asdict()
        if isinstance(row, dict):
            gstin = norm_gstin(row.get("$GSTIN") or row.get("gstin") or "")
            inv_no = norm_invoice_no(row.get("$VoucherNumber")
                                     or row.get("invoice_no") or row.get("voucher_no") or "")
            date_raw = row.get("$Date") or row.get("date") or row.get("invoice_date")
            taxable = (row.get("$TaxableValue") or row.get("taxable_value")
                       or row.get("taxable") or "0")
            tax = (row.get("$TaxAmount") or row.get("tax_amount")
                   or row.get("tax") or "0")
            rcm = row.get("rcm") or row.get("reverse_charge")
            blocked = row.get("blocked") or row.get("blocked_credit")
            ref = row.get("$VoucherNumber") or row.get("voucher_no") or ""
        else:
            # tuple-row fallback: (gstin, invoice_no, date, taxable, tax)
            vals = list(row) + [None] * 6
            gstin = norm_gstin(vals[0] or "")
            inv_no = norm_invoice_no(vals[1] or "")
            date_raw = vals[2] or ""
            taxable = vals[3] if vals[3] is not None else "0"
            tax = vals[4] if vals[4] is not None else "0"
            ref = vals[1] or ""

        if not inv_no or not date_raw:
            continue
        try:
            invoices.append(make_invoice(
                gstin=gstin, invoice_no=inv_no, date=str(date_raw),
                taxable_value=taxable, cgst=Decimal("0"), sgst=Decimal("0"),
                igst=Decimal(tax),
                source="tally", reverse_charge=rcm, blocked_credit=blocked,
                reference=str(ref),
            ))
        except Exception:
            continue
    return invoices
