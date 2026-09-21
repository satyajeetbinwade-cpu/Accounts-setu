"""Parsers/normalisers for official-file formats.

Handles the four file-types accountants actually drop in:
  * official GSTN GSTR-2B workbook  (.xlsx, multi-sheet: B2B / B2B-CDNR ...)
  * IMS B2B download                (.csv)
  * Tally purchase register          (.xlsx "Purchase Register" / .csv)
  * Tally credit-note register       (.xlsx "Debit Note Register" - credit notes
                                      are booked as Debit Note vouchers in Tally)

All functions are deterministic Tier-A code; they convert source files into
the engine's canonical input rows (see write_engine_inputs).
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    import openpyxl
except Exception:  # pragma: no cover - tests run with openpyxl installed
    openpyxl = None


# ---------------------------------------------------------------- helpers

def to_num(v) -> str:
    """Cell -> decimal string ('' for empty)."""
    if v is None:
        return ""
    s = str(v).strip().replace(",", "")
    if s in ("", "-"):
        return ""
    try:
        return str(Decimal(s))
    except (InvalidOperation, ValueError):
        return ""


def _iso(v) -> str:
    """Cell -> ISO date string, or raw string when not a date."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if not s:
        return ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


_DD_MM_YYYY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PERIOD_TAIL = re.compile(r"/(\d{2})-(\d{2,4})$")
_PERIOD_HEAD = re.compile(r"^(\d{2})-(\d{2})/")
_ZERO_PAD = re.compile(r"(\D|^)0+(?=\d)")


def core_inv(raw) -> str:
    """Reduce an invoice/note reference to its cross-file matching core.

    'RL/16/25-26'   -> '16'        'JSISC25-26-00142' -> '142'
    'JSIT-00023/25-26' -> '23'     'T36/25-26'  -> 'T36'
    '25-26/059'     -> '59'        'CD-12/04-2025' -> 'CD-12'
    'G/61/25-26'    -> '61'        '26071C0000002683' -> unchanged
    """
    if raw is None:
        return ""
    s = str(raw).strip().upper()
    if _PERIOD_TAIL.search(s):            # drop trailing period/date segment
        s = s.rsplit("/", 1)[0]
    s = _PERIOD_HEAD.sub("", s)
    seg = s.rsplit("/", 1)[-1].strip()
    # 'JSIT-00023' -> '23'  |  'T36' stays 'T36'  |  '25-26/059' -> '59'
    parts = seg.rsplit("-", 1)
    if len(parts) == 2 and re.fullmatch(r"0*\d+", parts[1]):
        return parts[1].lstrip("0") or "0"
    if re.fullmatch(r"0*\d+", seg):
        return seg.lstrip("0") or "0"
    return seg


_ISODATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _rows_after_header(ws, start_text: str):
    """Yield (rownum, values) rows that follow a header cell containing start_text."""
    started = False
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        vals = list(row)
        if not started:
            if str(vals[0] or "").strip() == start_text:
                started = True
            continue
        if any(x not in (None, "") for x in vals):
            yield i, vals


# ------------------------------------------------------------ GSTR-2B xlsx

def parse_gstr2b_workbook(path: str | Path, include_cdnr: bool = True) -> list[dict]:
    """Official GSTN GSTR-2B workbook -> portal invoice rows.

    'B2B' rows: gstin@0, invoice no@2, date@4, taxable@8, IGST@9, CGST@10,
                SGST@11, Cess@12, period@13.
    'B2B-CDNR' rows: same but shifted one column right (date@5, taxable@9 ...).
    """
    if openpyxl is None:
        raise RuntimeError("openpyxl required for GSTR-2B Excel parsing")
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        out: list[dict] = []
        for sheet, start, off in (("B2B", "GSTIN of supplier", 0),
                                  ("B2B-CDNR", "GSTIN of supplier", 1) if include_cdnr
                                  else ("B2B-CDNR", "GSTIN of supplier", 0)):
            if sheet not in wb.sheetnames:
                continue
            for _r, v in _rows_after_header(wb[sheet], start):
                inv = v[2]
                if not inv or not _ISODATE.match(_iso(v[4 + off])):
                    continue          # skips the second (sub-) header row
                out.append({
                    "supplierGstin": str(v[0] or "").strip(),
                    "docnum": core_inv(inv),
                    "docdt": _iso(v[4 + off]),
                    "taxableValue": to_num(v[8 + off]),
                    "igst": to_num(v[9 + off]),
                    "cgst": to_num(v[10 + off]),
                    "sgst": to_num(v[11 + off]),
                    "cess": to_num(v[12 + off]),
                    "ref": str(inv).strip(),
                    "source": "GSTR-2B " + sheet,
                })
        return out
    finally:
        wb.close()


# --------------------------------------------------------------- IMS csv

def parse_ims_csv(path: str | Path) -> list[dict]:
    """IMS B2B periodic download (GSTN IMS) -> portal rows."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    hdr = None
    for i, r in enumerate(rows):
        if r and str(r[0]).strip().lower() == "gstin of supplier":
            hdr = i
            break
    if hdr is None:
        raise ValueError("IMS CSV: 'GSTIN of Supplier' header not found")
    out = []
    for r in rows[hdr + 1:]:
        if not r or not r[0].strip():
            continue
        out.append({
            "supplierGstin": str(r[0]).strip(),
            "docnum": core_inv(r[2]),
            "docdt": _iso(r[4]),
            "taxableValue": to_num(r[8]),
            "igst": to_num(r[9]),
            "cgst": to_num(r[10]),
            "sgst": to_num(r[11]),
            "cess": to_num(r[12]),
            "ref": str(r[2]).strip(),
            "source": "IMS B2B",
        })
    return out


# ------------------------------------------------------- Tally registers

def _tally_register_rows(path: str | Path, sheet_name: str):
    if openpyxl is None:
        raise RuntimeError("openpyxl required for Tally register parsing")
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        # Try exact match first
        ws = None
        if sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            # Try case-insensitive match for common variants
            for sn in wb.sheetnames:
                if sn.lower() == sheet_name.lower():
                    ws = wb[sn]
                    break
        
        if ws is None:
            # Fallback: if only one sheet exists, use it
            if len(wb.sheetnames) == 1:
                ws = wb[wb.sheetnames[0]]
            else:
                # Otherwise list available sheets
                available = ", ".join(wb.sheetnames)
                raise KeyError(f"Worksheet '{sheet_name}' does not exist. Available sheets: {available}")
        
        out = []
        for r in range(5, (ws.max_row or 0) + 1):
            v = [ws.cell(row=r, column=c).value for c in range(1, 21)]
            if not v[1] or str(v[1]).strip() in ("", "Grand Total"):
                continue
            out.append(v)
        return out
    finally:
        wb.close()


def parse_tally_purchase_register(path: str | Path) -> list[dict]:
    """Tally 'Purchase Register' sheet -> book voucher rows (no amounts;
    the ledger columns are omitted in many exports)."""
    out = []
    for v in _tally_register_rows(path, "Purchase Register"):
        out.append({
            "date": _iso(v[0]),
            "gstin": str(v[8] or "").strip(),
            "invoice_no": core_inv(v[6]),
            "taxable": "",
            "cgst": "0", "sgst": "0", "igst": "0",
            "voucher_no": str(v[5]),
            "ref": str(v[6]),
            "supplier": str(v[1]),
        })
    return out


def parse_tally_credit_notes(path: str | Path) -> list[dict]:
    """Tally 'Debit Note Register' (credit notes) rows with real amounts.

    Gross@12, Pur-Lehanga@13, Pur-Sarees@14, Unclaimed CGST/SGST 2.5%@15/16,
    Unclaimed CGST/SGST 6%@17/18. Taxable = Gross - CGST - SGST.
    """
    out = []
    for v in _tally_register_rows(path, "Debit Note Register"):
        if str(v[4] or "").strip() != "Debit Note":
            continue
        gross = Decimal(to_num(v[12]) or "0")
        cgst = Decimal(to_num(v[15]) or "0") + Decimal(to_num(v[17]) or "0")
        sgst = Decimal(to_num(v[16]) or "0") + Decimal(to_num(v[18]) or "0")
        taxable = gross - cgst - sgst
        note_no = re.sub(r"^CREDIT NOTE NO\.\s*", "", str(v[11] or "")).strip()
        out.append({
            "date": _iso(v[0]),
            "gstin": str(v[8] or "").strip(),
            "invoice_no": core_inv(note_no),
            "taxable": str(taxable),
            "cgst": str(cgst), "sgst": str(sgst), "igst": "0",
            "voucher_no": str(v[5]),
            "ref": note_no,
            "supplier": str(v[1]),
        })
    return out


# ------------------------------------------------ engine input assembly

_BOOK_CSV_COLS = ["date", "gstin", "invoice_no", "taxable", "cgst", "sgst",
                  "igst", "tax_amount", "voucher_no", "ref", "supplier", "note"]


def join_portal_values(book_rows: list[dict], portal_rows: list[dict]) -> list[dict]:
    """Books registers often carry no amount columns; join the portal values
    by (GSTIN, core invoice no) so the matcher has something numeric."""
    idx = {(p["supplierGstin"], p["docnum"]): p for p in portal_rows}
    for b in book_rows:
        p = idx.get((b["gstin"], b["invoice_no"]))
        if p:
            b.update(taxable=p["taxableValue"], cgst=p["cgst"],
                     sgst=p["sgst"], igst=p["igst"])
            b["note"] = "amounts joined from GSTR-2B (register has no amount columns)"
        else:
            b.update(taxable="0", cgst="0", sgst="0", igst="0")
            b["note"] = "no portal ref (0 amount)"
    return book_rows


def write_engine_inputs(portal_rows: list[dict], book_rows: list[dict],
                        out_dir: str | Path) -> tuple[Path, Path]:
    """Write canonical engine inputs (portal JSON + books CSV)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "portal.json").write_text(
        json.dumps({"docdata": {"doclist": portal_rows}}, indent=1), encoding="utf-8")

    def row(r):
        out = {k: r.get(k, "") for k in _BOOK_CSV_COLS}
        try:
            out["tax_amount"] = str(Decimal(r.get("cgst") or 0)
                                    + Decimal(r.get("sgst") or 0)
                                    + Decimal(r.get("igst") or 0))
        except Exception:
            out["tax_amount"] = ""
        return out

    with open(out_dir / "books.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_BOOK_CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in book_rows:
            w.writerow(row(r))
    return out_dir / "portal.json", out_dir / "books.csv"
