"""Adapter: Desktop real-world files -> engine inputs (portal JSON + books CSV).

Files:
  Portal : 042025_...GSTR2B...xlsx  (B2B sheet -> portal; B2B-CDNR -> credit notes)
           IMS_B2B_...csv            (cross-check only)
  Books  : Purchase details.xlsx     (purchase vouchers; NO amount columns)
           Credit Note.xlsx          (same register + Debit Note Register = credit notes)
"""
import csv, json, re
from datetime import datetime, date
from pathlib import Path
import openpyxl

HOME = Path.home() / "Desktop" / "Accounts setu dump"
G2B = HOME / "042025_07AAACP9472A1ZJ_GSTR2B_14052025 (1).xlsx"
IMS = HOME / "IMS_B2B_07AAACP9472A1ZJ_19052025 (1).csv"
PUR = HOME / "Purchase details.xlsx"
CN  = HOME / "Credit Note.xlsx"
OUT_DIR = Path("real_data"); OUT_DIR.mkdir(exist_ok=True)

def to_num(v):
    if v is None: return ""
    s = str(v).strip().replace(",", "")
    if s in ("", "-"): return ""
    try: return str(Decimal(s))
    except Exception: return s
from decimal import Decimal

def core_inv(raw: str) -> str:
    """Normalise invoice ref to its irreducible core for cross-file matching.
    'RL/16/25-26'->'16'  'JSIT-00023/25-26'->'23'  'T36/25-26'->'T36'
    '25-26/059'->'59'    'G/61/25-26'->'61'        '26071C0000002683'->same
    """
    if raw is None: return ""
    s = str(raw).strip().upper()
    # drop trailing period ('/25-26') or date ('/04-2025') segment if present
    if re.search(r"/(\d{2})-(\d{2,4})$", s):
        s = s.rsplit("/", 1)[0]
    s = re.sub(r"^(\d{2})-(\d{2})/", "", s)          # strip leading period
    seg = s.rsplit("/", 1)[-1].strip()
    # collapse runs of zeros used as padding (JSIT-00023 -> 23)
    seg = re.sub(r"(\D|^)0+(?=\d)", r"\1", seg)
    return seg

def back_to_date(v):
    if isinstance(v, datetime): return v.date()
    if isinstance(v, date): return v
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: pass
    return None

def iso(v):
    d = back_to_date(v)
    return d.isoformat() if d is not None else str(v).strip()

# ---------------- PORTAL: GSTR-2B workbook ----------------
wb = openpyxl.load_workbook(G2B, data_only=True)

def sheet_rows(sheet, skip_until_text):
    ws = wb[sheet]
    rows = []
    header_idx = None
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        vals = list(row)
        if header_idx is None and vals and str(vals[0] or "").strip() == skip_until_text:
            header_idx = i
            continue
        if header_idx is not None and any(v not in (None, "") for v in vals):
            rows.append((i, vals))
    return rows

import re as _re
def _valid_iso(x):
    return _re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso(x)) is not None

portal = []
for sheet in ("B2B",):
    for rno, v in sheet_rows(sheet, "GSTIN of supplier"):
        gstin, inv, typ, d, val = (v[0], v[2], v[3], v[4], v[5])
        taxable, igst, cgst, sgst, cess, st = v[8], v[9], v[10], v[11], v[12], v[13]
        if not inv or not _valid_iso(d):    # skip r6 sub-header row
            continue
        portal.append({
            "supplierGstin": str(gstin or "").strip(),
            "docnum": core_inv(inv), "docdt": iso(d),
            "taxableValue": to_num(taxable), "igst": to_num(igst), "cgst": to_num(cgst),
            "sgst": to_num(sgst), "cess": to_num(cess), "sourceReturn": str(st or ""),
            "ref": str(inv)})

# B2B-CDNR (credit/debit notes from portal) - optional third portal bucket
cdnr = []
ws = wb["B2B-CDNR"]
start = None
for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
    vals = list(row)
    if str(vals[0] or "").strip().startswith("GSTIN of supplier"): start = i; continue
    if start and any(x not in (None, "") for x in vals):
        if vals[2] and _valid_iso(vals[5]):    # CDNR: Note date is col F (idx 5)
            # CDNR layout (official GSTN): note number@2, note date@5, taxable@9,
            # IGST@10, CGST@11, SGST@12, Cess@13  -- NOTE: shifted +1 vs B2B sheet
            cdnr.append({"supplierGstin": str(vals[0] or "").strip(), "docnum": core_inv(vals[2]),
                         "docdt": iso(vals[5]), "taxableValue": to_num(vals[9]),
                         "igst": to_num(vals[10]), "cgst": to_num(vals[11]), "sgst": to_num(vals[12]),
                         "cess": to_num(vals[13]), "ref": str(vals[2])})

print(f"[portal] B2B={len(portal)} B2B-CDNR={len(cdnr)}")

# IMS cross-check
with open(IMS, encoding="utf-8-sig") as f:
    ims = [r for r in list(csv.reader(f))[3:] if r and r[0]]
print(f"[portal] IMS cross-check rows={len(ims)} (first gstin '{ims[0][0]}' inv '{ims[0][2]}')")

# ---------------- BOOKS ----------------
def read_register(path, header_row=4):
    ws = openpyxl.load_workbook(path, data_only=True)["Purchase Register"]
    out = []
    for r in range(header_row + 1, ws.max_row + 1):
        v = [ws.cell(row=r, column=c).value for c in range(1, 15)]
        if not v[1] or str(v[1]).strip() in ("", "Grand Total"): continue
        out.append(v)
    return out

books = []             # purchases (no amounts in source; portal join provides values)
for v in read_register(PUR):
    books.append({"date": iso(v[0]), "gstin": str(v[8] or "").strip(),
                  "invoice_no": core_inv(v[6]), "ref": str(v[6]), "voucher_no": str(v[5]),
                  "supplier": str(v[1])})

# Credit notes (Debit Note Register) - real amounts
cn_rows = []
ws2 = openpyxl.load_workbook(CN, data_only=True)["Debit Note Register"]
for r in range(5, ws2.max_row + 1):
    v = [ws2.cell(row=r, column=c).value for c in range(1, 21)]
    if not v[1] or str(v[1]).strip() in ("", "Grand Total"): continue
    if str(v[4] or "").strip() != "Debit Note": continue
    gross = v[12] or 0
    cgst = (v[15] or 0) + (v[17] or 0)
    sgst = (v[16] or 0) + (v[18] or 0)
    taxable = gross - cgst - sgst
    cn_rows.append({"date": iso(v[0]), "gstin": str(v[8] or "").strip(),
                    "invoice_no": core_inv(re.sub(r"^CREDIT NOTE NO\.\s*", "", str(v[11] or ""))),
                    "ref": str(v[11]), "voucher_no": str(v[5]), "supplier": str(v[1]),
                    "taxable": str(taxable), "cgst": str(cgst), "sgst": str(sgst)})
print(f"[books] purchases={len(books)} credit_notes={len(cn_rows)}")

# join portal values into books by (gstin, docnum)
pindex = {(p["supplierGstin"], p["docnum"]): p for p in portal}
for b in books:
    p = pindex.get((b["gstin"], b["invoice_no"]))
    if p:
        b.update({"taxable": p["taxableValue"], "cgst": p["cgst"], "sgst": p["sgst"], "igst": p["igst"]})
    else:
        b.update({"taxable": "0", "cgst": "0", "sgst": "0", "igst": "0"})
    b["note"] = "amounts joined from GSTR-2B (register has no amount columns)" if p else "no portal ref (0 amount)"

# ---------------- WRITE engine inputs ----------------
doclist = portal + cdnr
json.dump({"docdata": {"doclist": doclist}},
          open(OUT_DIR / "portal.json", "w"), indent=1)
cols = ["date", "gstin", "invoice_no", "taxable", "cgst", "sgst", "igst",
        "tax_amount", "voucher_no", "ref", "supplier", "note"]
def _row(r):
    row = {k: r.get(k, "") for k in cols}
    # verifier's independent reader needs an explicit total-tax column
    try:
        row["tax_amount"] = str(Decimal(r.get("cgst") or 0) + Decimal(r.get("sgst") or 0)
                                + Decimal(r.get("igst") or 0))
    except Exception:
        row["tax_amount"] = ""
    return row
with open(OUT_DIR / "books.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for b in books: w.writerow(_row(b))
    for c in cn_rows: w.writerow(_row(c))
json.dump({"purchases": len(books), "credit_notes": len(cn_rows), "portal": len(portal), "cdnr": len(cdnr)},
          open(OUT_DIR / "counts.json", "w"), indent=1)
print("[out] real_data/portal.json + real_data/books.csv written")
print("portal total:", len(doclist), "| books total:", len(books) + len(cn_rows))
