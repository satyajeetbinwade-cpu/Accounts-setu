"""Synthetic test documents for the reconciliation engine.

The user's SampleTB-AI.xlsx is a Tally Trial Balance: no supplier invoices, no
voucher numbers, no GSTINs. To exercise the invoice-level 2B-vs-books engine we
generate a coherent, deterministic scenario:

  * GSTR-2B JSON   (portal side) - 9 supplier invoices, 6 suppliers, valid GSTINs
  * Tally purchase register CSV (books side) - 10 voucher rows

The books register intentionally contains realistic discrepancies so the engine
has something to classify:
  - 7 exact / near-exact matches
  - 1 amount difference   (BL/2024/0811: books value 64000 vs portal 65000)
  - 1 not-in-books        (BL/2024/0815 in portal but missing from books)
  - 2 not-in-portal       (DT/0830, ITF-2024-08-115 booked but absent from 2B)
  - 1 ineligible supplier (EP-INV-9033: supplier non-filer -> ITC ineligible)

All figures are SAMPLE figures for engine testing only.
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

# gstin -> supplier name
SUPPLIERS = {
    "29AAFCB5236K1ZL": "Abhishek Enterprises (Bengaluru)",
    "07AAACH7409R1ZK": "Bright Steel Traders (Delhi)",
    "27AABCT1332L1ZV": "CloudNine Tech Services (Gujarat)",
    "24AAACJ7417J1ZY": "Delta Fabrics (Gujarat)",
    "06AABCS5309Q1ZW": "Emerald PackWorks (Maharashtra)",
    "29BGTFS2754Q1ZL": "Fusion IT Solutions (Bengaluru)",
}

# GSTR-2B portal invoices (field names match the GSTN JSON download format)
PORTAL_INVOICES = [
    {"gstin": "29AAFCB5236K1ZL", "invoice_no": "INV-24001",       "date": "2024-08-02", "taxable": 120000, "cgst": 10800,  "sgst": 10800,  "igst": 0,     "status": "active"},
    {"gstin": "29AAFCB5236K1ZL", "invoice_no": "INV-24002 ",      "date": "2024-08-07", "taxable": 45000,  "cgst": 4050,   "sgst": 4050,   "igst": 0,     "status": "active"},
    {"gstin": "07AAACH7409R1ZK", "invoice_no": "BL/2024/0811",    "date": "2024-08-04", "taxable": 65000,  "cgst": 5850,   "sgst": 5850,   "igst": 0,     "status": "active"},
    {"gstin": "07AAACH7409R1ZK", "invoice_no": "BL/2024/0815",    "date": "2024-08-10", "taxable": 28000,  "cgst": 2520,   "sgst": 2520,   "igst": 0,     "status": "active"},
    {"gstin": "27AABCT1332L1ZV", "invoice_no": "CN-8841",         "date": "2024-08-06", "taxable": 95000,  "cgst": 0,      "sgst": 0,      "igst": 17100, "status": "active"},
    {"gstin": "24AAACJ7417J1ZY", "invoice_no": "DT/0821",         "date": "2024-08-12", "taxable": 42000,  "cgst": 3780,   "sgst": 3780,   "igst": 0,     "status": "active"},
    {"gstin": "06AABCS5309Q1ZW", "invoice_no": "EP-INV-9033",     "date": "2024-08-15", "taxable": 50000,  "cgst": 0,      "sgst": 0,      "igst": 9000,  "status": "non_filer"},
    {"gstin": "29BGTFS2754Q1ZL", "invoice_no": "ITF-2024-08-112", "date": "2024-08-18", "taxable": 31000,  "cgst": 2790,   "sgst": 2790,   "igst": 0,     "status": "active"},
    {"gstin": "27AABCT1332L1ZV", "invoice_no": "CN-8850",         "date": "2024-08-21", "taxable": 24000,  "cgst": 0,      "sgst": 0,      "igst": 4320,  "status": "active"},
]

# Phase-2 rules variant: same invoices, but two carry business-rule flags:
#   - INV-24001 : Section 17(5) blocked credit   (ITC never claimable)
#   - CN-8841   : reverse charge / RCM supply    (handled outside regular ITC)
PORTAL_INVOICES_RULES = [
    {**i,
     **({"blocked": True} if i["invoice_no"] == "INV-24001" else {}),
     **({"rcm": True} if i["invoice_no"] == "CN-8841" else {})}
    for i in PORTAL_INVOICES
]


# Tally purchase register voucher rows (books side). Note: BL/2024/0811 is 64000
# in books vs 65000 in portal (amount difference); DT-0821 uses '-' vs portal's
# '/' (fuzzy invoice number); DT/0830 & ITF-2024-08-115 are books-only.
BOOKS_ROWS = [
    {"gstin": "29AAFCB5236K1ZL", "invoice_no": "INV-24001",       "date": "2024-08-02", "taxable": 120000, "tax": 21600},
    {"gstin": "29AAFCB5236K1ZL", "invoice_no": "INV-24002 ",      "date": "2024-08-07", "taxable": 45000,  "tax": 8100},
    {"gstin": "07AAACH7409R1ZK", "invoice_no": "BL/2024/0811",    "date": "2024-08-04", "taxable": 64000,  "tax": 11520},
    {"gstin": "27AABCT1332L1ZV", "invoice_no": "CN-8841",         "date": "2024-08-06", "taxable": 95000,  "tax": 17100},
    {"gstin": "24AAACJ7417J1ZY", "invoice_no": "DT-0821",         "date": "2024-08-13", "taxable": 42000,  "tax": 7560},
    {"gstin": "06AABCS5309Q1ZW", "invoice_no": "EP-INV-9033",     "date": "2024-08-15", "taxable": 50000,  "tax": 9000},
    {"gstin": "29BGTFS2754Q1ZL", "invoice_no": "ITF-2024-08-112", "date": "2024-08-19", "taxable": 31000,  "tax": 5580},
    {"gstin": "27AABCT1332L1ZV", "invoice_no": "CN-8850",         "date": "2024-08-21", "taxable": 24000,  "tax": 4320},
    {"gstin": "24AAACJ7417J1ZY", "invoice_no": "DT/0830",         "date": "2024-08-26", "taxable": 15000,  "tax": 2700},
    {"gstin": "29BGTFS2754Q1ZL", "invoice_no": "ITF-2024-08-115", "date": "2024-08-23", "taxable": 12000,  "tax": 2160},
]


def write_gstr2b_json(path: str | Path,
                      invoices: list | None = None) -> Path:
    """Write a GSTR-2B JSON (docdata.doclist format).

    Two-key flag scheme keeps RCM and §17(5) independent:
      - "rcm": "Y"/"N"         -> reverse_charge flag (NOT blocked)
      - "isBlocked": "true"/"" -> blocked-credit flag
    """
    path = Path(path)
    invoices = PORTAL_INVOICES if invoices is None else invoices
    doclist = []
    for i in invoices:
        doclist.append({
            "supplierGstin": i["gstin"],
            "docnum": i["invoice_no"],
            "docdt": i["date"],
            "taxableValue": i["taxable"],
            "cgst": i["cgst"],
            "sgst": i["sgst"],
            "igst": i["igst"],
            "status": i["status"],
            "rcm": "Y" if i.get("rcm") else "N",
            "isBlocked": "true" if i.get("blocked") else "",
        })
    path.write_text(json.dumps({"docdata": {"doclist": doclist}}, indent=2),
                    encoding="utf-8")
    return path


def write_tally_csv(path: str | Path) -> Path:
    """Write the sample Tally purchase register CSV."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["gstin", "invoice_no", "date", "taxable_value", "tax_amount"])
        for r in BOOKS_ROWS:
            w.writerow([r["gstin"], r["invoice_no"], r["date"],
                        r["taxable"], r["tax"]])
    return path




def write_gstr2b_excel(path: str | Path) -> Path:
    """Write the sample GSTR-2B as a realistic portal Excel export.

    Mirrors the GSTN download layout (B2B sheet, 'GSTIN of Supplier' ...
    'Status' columns). Status uses the portal's R/N convention, which the
    Excel parser maps back to active / non_filer -- keeping parity with the
    JSON fixture.
    """
    path = Path(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "B2B"
def write_gstr2b_excel(path: str | Path,
                       invoices: list | None = None) -> Path:
    """Write a GSTR-2B as a realistic portal Excel export.

    Columns mirror the GSTN download; Status uses R/N, RCM is Y/N and
    ITC Availability is 'Avail'/'Not Avail' -- the parser maps all back to
    engine vocabulary, keeping parity with the JSON fixture.
    """
    path = Path(path)
    invoices = PORTAL_INVOICES if invoices is None else invoices
    wb = Workbook()
    ws = wb.active
    ws.title = "B2B"
    ws.append(["GSTIN of Supplier", "Trade Name", "Invoice Number",
               "Invoice Date", "Invoice Value", "Place Of Supply",
               "Supply Type", "Rate", "Taxable Value", "CGST", "SGST",
               "IGST", "Status", "Reverse Charge", "ITC Availability"])
    for i in invoices:
        total = i["taxable"] + i["cgst"] + i["sgst"] + i["igst"]
        ws.append([
            i["gstin"], SUPPLIERS.get(i["gstin"], ""), i["invoice_no"],
            i["date"], total, i["gstin"][:2], "B2B", "18", i["taxable"],
            i["cgst"], i["sgst"], i["igst"],
            "R" if i["status"] != "non_filer" else "N",
            "Y" if i.get("rcm") else "N",
            "Not Avail" if i.get("blocked") else "Avail",
        ])
    wb.save(path)
    return path


def write_tally_excel(path: str | Path) -> Path:
    """Write the sample Tally purchase register as an Excel export.

    Same 10 vouchers as the CSV fixture; tax split into CGST/SGST columns
    (the parser sums them back to a single tax amount, so parity holds).
    """
    path = Path(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "Tally Purchase"
    ws.append(["Voucher Number", "Voucher Date", "Party Name", "GSTIN",
               "Taxable Value", "CGST", "SGST", "IGST"])
    for r in BOOKS_ROWS:
        cg = sg = Decimal(str(r["tax"])) / 2
        ws.append([
            r["invoice_no"], r["date"], SUPPLIERS.get(r["gstin"], ""),
            r["gstin"], r["taxable"], float(cg), float(sg), 0,
        ])
    wb.save(path)
    return path


def write_gstr2b_rules_json(path: str | Path) -> Path:
    """GSTR-2B JSON with §17(5)/RCM flags (Phase-2 rules fixture)."""
    return write_gstr2b_json(path, PORTAL_INVOICES_RULES)


def write_gstr2b_rules_excel(path: str | Path) -> Path:
    """GSTR-2B Excel with §17(5)/RCM flags (Phase-2 rules fixture)."""
    return write_gstr2b_excel(path, PORTAL_INVOICES_RULES)


def generate(outdir: str | Path = "sample_data") -> dict[str, Path]:
    """Write both sample document sets (JSON/CSV + Excel) into outdir."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    g = write_gstr2b_json(outdir / "gstr2b_sample_full.json")
    t = write_tally_csv(outdir / "tally_purchase_sample_full.csv")
    gx = write_gstr2b_excel(outdir / "gstr2b_sample.xlsx")
    tx = write_tally_excel(outdir / "tally_purchase_sample.xlsx")
    gr = write_gstr2b_rules_json(outdir / "gstr2b_rules.json")
    gxr = write_gstr2b_rules_excel(outdir / "gstr2b_rules.xlsx")
    return {"gstr2b": g, "tally": t, "gstr2b_xlsx": gx, "tally_xlsx": tx,
            "gstr2b_rules": gr, "gstr2b_rules_xlsx": gxr}


if __name__ == "__main__":
    import os
    here = os.path.dirname(__file__)
    outs = generate(os.path.join(here, "..", "sample_data"))
    for k, p in outs.items():
        print(f"{k}: {p}")
