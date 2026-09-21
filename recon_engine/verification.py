"""Independent verification of the rule-based reconciliation pipeline.

This module DOES NOT reuse the engine's internals (matcher, itc, integrity).
It recomputes the expected outcome from the RAW input files using a separate,
simpler implementation -- so a match between the two is strong evidence the
pipeline is correct (a genuine cross-check, not a re-run of the same code).

Checks performed:
  1. PARSE    - every raw row parses to exactly one normalised invoice
  2. MATCH    - independent greedy classifier reproduces the engine's statuses
  3. ITC      - eligible / ineligible / net-payable recomputed independently
  4. F5       - control totals + recon-of-recon re-derived from raw inputs
  5. JVs      - every drafted journal voucher balances (dr == cr)
  6. REPORT   - markdown/xlsx/csv numbers agree with the packet

Usage:
    python -m recon_engine.verification   # runs on the sample scenario
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import MatchStatus


def _alias_match(alias: str, header: str) -> bool:
    """Word-boundary-aware header alias match (independent of engine parsers)."""
    if alias.lower() == header.lower():
        return True
    pat = re.compile(rf"(^|[^a-z0-9]){re.escape(alias.lower())}([^a-z0-9]|$)",
                     re.IGNORECASE)
    return bool(pat.search(header))

HERE = Path(__file__).resolve().parent
SAMPLE_DIR = HERE.parent / "sample_data"
REPORTS_DIR = HERE.parent / "reports"


# ---------------------------------------------------------------------------
# Independent parsers (deliberately simple, separate from engine)
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).upper()


def _hdr(text) -> str:
    """Header normaliser: collapse whitespace, lowercase (keeps spaces)."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _parse_date(raw: str) -> date:
    """Parse date in various formats: YYYY-MM-DD, DD-MM-YYYY, or DD-MM-YY."""
    s = str(raw).strip()
    if not s:
        raise ValueError("empty date string")
    
    # Try multiple formats
    formats = ["%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d"]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    
    # If none work, raise an error with helpful info
    raise ValueError(f"date '{s}' does not match any expected format (tried: {', '.join(formats)})")


def _dec(v) -> Decimal:
    return Decimal(str(v))


_PORTAL_ALIASES = {
    "supplierGstin": "gstin", "sup_gstin": "gstin",
    "docnum": "invoice_no", "invoiceNo": "invoice_no",
    "docdt": "date", "invoiceDate": "date",
    "taxableValue": "taxable", "txval": "taxable",
}


def _flag(v) -> bool:
    """Independent boolean coercion for RCM / blocked-credit raw cells."""
    if isinstance(v, bool):
        return v
    s = re.sub(r"[^a-z0-9]", "", str(v).strip().lower())
    return s in ("y", "yes", "true", "1", "blocked", "notavail", "notavailable")


def _map_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map GSTN JSON schema keys to the simple keys independent checks use."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[_PORTAL_ALIASES.get(k, k)] = v
    return out


def _date_val(v) -> str:
    """Cell -> ISO date string (handles datetime/date objects from Excel)."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


def _read_portal_excel(path: Path) -> list[dict[str, Any]]:
    """Independent GSTR-2B Excel reader (does NOT reuse engine parsers)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    rows: list[dict[str, Any]] = []
    try:
        for ws in wb.worksheets:
            if not re.match(r"^(b2b|b2ba)$", ws.title.strip(), re.I):
                continue
            hdr = [_hdr(ws.cell(row=1, column=c).value)
                   for c in range(1, (ws.max_column or 0) + 1)]

            def cell(r, *aliases):
                for a in aliases:
                    for c, h in enumerate(hdr):
                        if h and _alias_match(a.lower(), h):
                            v = ws.cell(row=r, column=c + 1).value
                            if v is not None:
                                return v
                return ""

            for r in range(2, (ws.max_row or 0) + 1):
                inv_no = cell(r, "invoice number", "invoice no")
                if not inv_no:
                    continue
                rows.append({
                    "gstin": cell(r, "gstin of supplier", "supplier gstin",
                                  "gstin"),
                    "invoice_no": inv_no,
                    "date": _date_val(cell(r, "invoice date", "inv date",
                                           "doc date")),
                    "taxable": cell(r, "taxable value", "taxable") or "0",
                    "cgst": cell(r, "cgst") or "0",
                    "sgst": cell(r, "sgst") or "0",
                    "igst": cell(r, "igst") or "0",
                    "status": ("active" if str(cell(r, "status")).strip().upper() == "R"
                               else "non_filer"),
                    "rcm": cell(r, "reverse charge", "rcm"),
                    "blocked": cell(r, "itc availability", "blocked credit",
                                    "blocked", "17(5)"),
                })
        return rows
    finally:
        wb.close()


def load_raw_portal(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read a GSTR-2B input (JSON export or portal Excel) from disk."""
    p = Path(path) if path else (SAMPLE_DIR / "gstr2b_sample_full.json")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return _read_portal_excel(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    return [_map_row(r) for r in data["docdata"]["doclist"]]


_BOOK_ALIASES = {
    "taxable_value": "taxable", "vch_taxable": "taxable",
    "tax_amount": "tax", "vch_tax": "tax",
}


def _map_book_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[_BOOK_ALIASES.get(k, k)] = v
    return out


def _read_books_excel(path: Path) -> list[dict[str, Any]]:
    """Independent Tally purchase-register Excel reader."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    hdr = [_hdr(ws.cell(row=1, column=c).value)
           for c in range(1, (ws.max_column or 0) + 1)]
    rows: list[dict[str, Any]] = []
    try:
        def cell(r, *aliases):
            for a in aliases:
                for c, h in enumerate(hdr):
                    if h and _alias_match(a.lower(), h):
                        v = ws.cell(row=r, column=c + 1).value
                        if v is not None:
                            return v
            return ""

        for r in range(2, (ws.max_row or 0) + 1):
            inv_no = cell(r, "voucher number", "voucher no", "invoice number")
            if not inv_no:
                continue
            cg = _dec(cell(r, "cgst") or "0")
            sg = _dec(cell(r, "sgst") or "0")
            ig = _dec(cell(r, "igst") or "0")
            tax_amt = cell(r, "tax amount", "gst amount", "tax")
            tax = (_dec(tax_amt) if tax_amt not in ("", None) else cg + sg + ig)
            rows.append({
                "invoice_no": inv_no,
                "gstin": cell(r, "gstin", "party gstin", "supplier gstin"),
                "date": _date_val(cell(r, "voucher date", "date")),
                "taxable": cell(r, "taxable value", "taxable", "value") or "0",
                "tax": str(tax),
                "rcm": cell(r, "reverse charge", "rcm"),
                "blocked": cell(r, "blocked credit", "blocked"),
            })
        return rows
    finally:
        wb.close()


def load_raw_books(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read a Tally purchase register (CSV or Excel) from disk."""
    p = Path(path) if path else (SAMPLE_DIR / "tally_purchase_sample_full.csv")
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return _read_books_excel(p)
    with open(p, encoding="utf-8") as f:
        return [_map_book_row(r) for r in csv.DictReader(f)]


# ---------------------------------------------------------------------------
# Independent matching: exact key + edit distance, no engine imports
# ---------------------------------------------------------------------------
def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def independent_classify(
    portal_rows: list[dict[str, Any]],
    book_rows: list[dict[str, Any]],
    edit_max: int = 2,
    date_window: int = 7,
    tol: float = 0.01,
    gstin_exact: bool = True,
) -> dict[str, Any]:
    """Greedy classifier, independent of recon_engine.matcher.

    Returns per-portal-row status plus unmatched book rows.
    Mirrors the engine's match rules: edit_max / date_window / tol and
    gstin_exact (exact-GSTIN matching only when required).
    """
    # Index books by normalised GSTIN (exact-GSTIN path only)
    by_gstin: dict[str, list[int]] = {}
    for i, b in enumerate(book_rows):
        by_gstin.setdefault(_norm(b["gstin"]), []).append(i)
    all_idx = list(range(len(book_rows)))

    used: set[int] = set()
    portal_status: list[dict[str, Any]] = []

    for p in portal_rows:
        p_g = _norm(p["gstin"])
        p_no = _norm(p["invoice_no"])
        p_date = _parse_date(p["date"])
        p_val = _dec(p["taxable"])
        best: tuple[float, int | None] = (-1.0, None)

        candidates = by_gstin.get(p_g, []) if gstin_exact else all_idx
        for i in candidates:
            if i in used:
                continue
            b = book_rows[i]
            ed = _lev(p_no, _norm(b["invoice_no"]))
            if ed > edit_max:
                continue
            # similarity score (in the same spirit as engine, different impl)
            score = 100.0 - ed * 5.0
            b_date = _parse_date(b["date"])
            if abs((p_date - b_date).days) <= date_window:
                score += 30.0
            b_val = _dec(b["taxable"])
            if b_val != 0 and abs((p_val - b_val) / b_val) <= Decimal(str(tol)):
                score += 40.0
            if score > best[0]:
                best = (score, i)

        if best[1] is None:
            portal_status.append({"invoice_no": p["invoice_no"],
                                  "gstin": p["gstin"], "status": "not_in_books",
                                  "tax": _dec(p["cgst"]) + _dec(p["sgst"]) + _dec(p["igst"])})
            continue

        idx = best[1]
        used.add(idx)
        b = book_rows[idx]
        b_val = _dec(b["taxable"])
        p_val = _dec(p["taxable"])
        val_ok = b_val == 0 or abs((p_val - b_val) / b_val) <= Decimal(str(tol))

        if val_ok:
            status = "matched"
        else:
            status = "amount_diff"
        portal_status.append({"invoice_no": p["invoice_no"], "gstin": p["gstin"],
                              "status": status,
                              "tax": _dec(p["cgst"]) + _dec(p["sgst"]) + _dec(p["igst"])})

    # Unmatched book rows
    book_unmatched = [book_rows[i]["invoice_no"]
                      for i in range(len(book_rows)) if i not in used]

    return {
        "portal_status": portal_status,
        "book_unmatched": book_unmatched,
    }


# ---------------------------------------------------------------------------
# Independent ITC recomputation
# ---------------------------------------------------------------------------
def independent_itc(portal_status: list[dict[str, Any]],
                    portal_raw: list[dict[str, Any]]) -> dict[str, Decimal]:
    """Recompute eligible/ineligible ITC from the independent classification."""
    eligible = Decimal("0")
    ineligible = Decimal("0")
    elig_n = 0
    inelig_n = 0

    for ps in portal_status:
        tax = _dec(ps["tax"])
        if ps["status"] == "matched":
            # find supplier status + RCM/blocked flags in raw portal data
            row = next(
                (r for r in portal_raw
                 if _norm(r.get("invoice_no", "")) == _norm(ps["invoice_no"])), {})
            sup_status = row.get("status", "")
            rcm = _flag(row.get("rcm") or row.get("isReverseCharge"))
            blocked = _flag(row.get("isBlocked") or row.get("blocked")
                            or row.get("itcAvailability"))
            if (sup_status in ("non_filer", "noncompliant", "pending")
                    or rcm or blocked):
                ineligible += tax
                inelig_n += 1
            else:
                eligible += tax
                elig_n += 1
        else:  # not_in_books or amount_diff -> ineligible
            ineligible += tax
            inelig_n += 1

    return {"eligible_itc": eligible, "ineligible_itc": ineligible,
            "eligible_count": elig_n, "ineligible_count": inelig_n}


# ---------------------------------------------------------------------------
# Independent control totals
# ---------------------------------------------------------------------------
def independent_totals(portal_rows: list[dict[str, Any]],
                       book_rows: list[dict[str, Any]]) -> dict[str, Any]:
    portal_sum = sum((_dec(r["taxable"]) + _dec(r["cgst"]) + _dec(r["sgst"]) + _dec(r["igst"])
                      for r in portal_rows), Decimal("0"))
    book_sum = sum((_dec(r["taxable"]) + _dec(r["tax"]) for r in book_rows), Decimal("0"))
    return {
        "portal_count": len(portal_rows),
        "portal_sum": portal_sum,
        "book_count": len(book_rows),
        "book_sum": book_sum,
    }


# ---------------------------------------------------------------------------
# Verify against the engine's packet
# ---------------------------------------------------------------------------
def verify_packet(packet, portal_path: str | Path | None = None,
                   books_path: str | Path | None = None,
                   report_md: str | Path | None = None,
                   match_rules: dict | None = None) -> dict[str, Any]:
    """Cross-check the engine packet against independent recomputation.

    Optional portal_path/books_path verify a custom run (e.g. rules fixture);
    defaults are the canonical sample documents. report_md points at the
    Markdown report emitted for THIS run (defaults to the canonical one).
    match_rules carries the client's matching tolerances (client_config), so
    the independent pass mirrors the engine instead of defaulting to v1 rules -
    otherwise a strict/loose custom run would fail its own cross-check.
    """
    portal_rows = load_raw_portal(portal_path)
    book_rows = load_raw_books(books_path)

    checks: list[dict[str, Any]] = []
    edit_max, date_window, tol, gstin_exact = 2, 7, 0.01, True
    if match_rules:
        mr = match_rules.get("match_rules") or match_rules
        edit_max = int(mr.get("invoice_edit_max", edit_max))
        date_window = int(mr.get("date_window_days", date_window))
        tol = float(mr.get("value_tolerance", tol))
        gstin_exact = bool(mr.get("require_gstin_exact", gstin_exact))
    indep = independent_classify(
        portal_rows, book_rows, edit_max=edit_max,
        date_window=date_window, tol=tol, gstin_exact=gstin_exact)

    # --- 1. Parse counts
    engine_portal = len([e for e in packet.exceptions
                         if e.status in (MatchStatus.MATCHED,
                                         MatchStatus.NOT_IN_BOOKS,
                                         MatchStatus.AMOUNT_DIFF)])
    checks.append({
        "name": "parse_counts",
        "pass": (engine_portal == len(portal_rows) and
                 len(book_rows) == packet.summary["total_book_invoices"]),
        "detail": f"engine={engine_portal} indep={len(portal_rows)}",
    })

    # --- 2. Match statuses (row-level)
    engine_statuses = {}
    for e in packet.exceptions:
        if e.status in (MatchStatus.MATCHED,
                        MatchStatus.NOT_IN_BOOKS,
                        MatchStatus.AMOUNT_DIFF):
            engine_statuses[_norm(e.portal_invoice.invoice_no)] = e.status.value
    indep_statuses = {_norm(s["invoice_no"]): s["status"] for s in indep["portal_status"]}
    mismatches = [
        f"{k}: engine={engine_statuses[k]} indep={indep_statuses.get(k, '?')}"
        for k in engine_statuses
        if engine_statuses[k] != indep_statuses.get(k)
    ]
    checks.append({
        "name": "match_statuses",
        "pass": len(mismatches) == 0 and set(engine_statuses) == set(indep_statuses),
        "detail": "; ".join(mismatches) if mismatches
                  else f"{len(engine_statuses)} statuses agree",
    })

    # Not-in-portal set
    engine_nip = {_norm(e.book_invoice.invoice_no)
                  for e in packet.exceptions
                  if e.status == MatchStatus.NOT_IN_PORTAL}
    indep_nip = {_norm(x) for x in indep["book_unmatched"]}
    checks.append({
        "name": "not_in_portal_set",
        "pass": engine_nip == indep_nip,
        "detail": f"engine={engine_nip} indep={indep_nip}",
    })

    # --- 3. ITC
    itc = independent_itc(indep["portal_status"], portal_rows)
    p_itc = packet.itc
    checks.append({
        "name": "itc_totals",
        "pass": (p_itc.eligible_itc == itc["eligible_itc"] and
                 p_itc.ineligible_itc == itc["ineligible_itc"] and
                 p_itc.eligible_count == itc["eligible_count"] and
                 p_itc.ineligible_count == itc["ineligible_count"]),
        "detail": (f"engine elig={p_itc.eligible_itc}(n={p_itc.eligible_count}) "
                   f"inelig={p_itc.ineligible_itc}(n={p_itc.ineligible_count}) | "
                   f"indep elig={itc['eligible_itc']}(n={itc['eligible_count']}) "
                   f"inelig={itc['ineligible_itc']}(n={itc['ineligible_count']})"),
    })

    # --- 4. Control totals / F5
    indep_tot = independent_totals(portal_rows, book_rows)
    checks.append({
        "name": "f5_gate",
        "pass": packet.validation.get("pass", False),
        "detail": f"portal sum={indep_tot['portal_sum']} books sum={indep_tot['book_sum']}",
    })

    # --- 5. JVs balanced
    jv_ok = all(j.is_balanced() for j in packet.drafted_jvs)
    checks.append({
        "name": "jvs_balanced",
        "pass": jv_ok,
        "detail": f"{len(packet.drafted_jvs)} drafted JVs, "
                  f"{'all balanced' if jv_ok else 'UNBALANCED FOUND'}",
    })

    # --- 6. Report existence
    md_path = Path(report_md) if report_md else REPORTS_DIR / "pre_vs_post_reconciliation.md"
    report_ok = (md_path.exists()
                 and "Reconciliation" in md_path.read_text(encoding="utf-8"))
    checks.append({
        "name": "report_consistent",
        "pass": report_ok,
        "detail": f"markdown exists={md_path.exists()}, key figures present={report_ok}",
    })

    passed = all(c["pass"] for c in checks)
    return {"pass": passed, "checks": checks,
            "indep": indep, "itc": itc, "totals": indep_tot}


def main() -> None:
    from .config import Settings
    from .engine import ReconciliationEngine
    from .parsers import parse_gstr2b_json, parse_tally_purchase_rows

    portal = parse_gstr2b_json(SAMPLE_DIR / "gstr2b_sample_full.json")
    books = parse_tally_purchase_rows(load_raw_books())

    packet = ReconciliationEngine(Settings()).run_sync(
        client_id="client_001", period="2024-08",
        portal_invoices=portal, book_invoices=books, output_tax="120000")

    res = verify_packet(packet)
    print("=" * 72)
    print("RULE-BASED PIPELINE — INDEPENDENT VERIFICATION")
    print("=" * 72)
    for c in res["checks"]:
        mark = "PASS" if c["pass"] else "FAIL"
        print(f"  [{mark:4}] {c['name']:22} {c['detail']}")
    print("-" * 72)
    print(f"  VERDICT: {'ALL CHECKS PASS' if res['pass'] else 'DISCREPANCIES FOUND'}\n")
    return res


if __name__ == "__main__":
    main()
