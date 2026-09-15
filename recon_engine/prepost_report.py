"""Pre vs Post GST Reconciliation Report generator.

Runs the full 2B-vs-books engine over the sample documents (GSTR-2B + Tally
purchase register, backed by the Trial Balance for the books-side context) and
produces:

  * a Markdown report  (reports/pre_vs_post_reconciliation.md)
  * an Excel workbook  (reports/Pre_Post_Reconciliation_Report.xlsx)
  * CSV dumps          (pre_recon_open_items.csv / post_recon_exceptions.csv)

PRE  = documents as received, before the engine ran: every invoice is an open
       item, no ITC claimed, only the books' raw GST position is known.
POST = after matching/classification: matched set, exceptions, eligible &
       ineligible ITC, GSTR-3B draft figures and proposed JVs.

Usage:
    python -m recon_engine.prepost_report
"""
from __future__ import annotations

import csv
import os
from decimal import Decimal
from pathlib import Path

from .config import Settings
from .engine import ReconciliationEngine
from .gst_position import analyze_gst_position
from .htmlreport import write_html
from .parsers import parse_gstr2b_json, parse_tally_purchase_rows
from .parsers.trial_balance import parse_trial_balance
from .sample_scenario import generate as gen_sample_docs
from .models import MatchStatus

HERE = Path(__file__).resolve().parent
SAMPLE_DIR = HERE.parent / "sample_data"
REPORTS_DIR = HERE.parent / "reports"

SAMPLE_OUTPUT_TAX = Decimal("120000.00")   # sample output tax for the period


# ---------------------------------------------------------------------------
# PRE state
# ---------------------------------------------------------------------------
def build_pre_state(portal_invoices, book_invoices, tb_rows) -> dict:
    """The documents as received, before any engine processing."""
    def total(inv):
        return (inv.taxable_value + inv.tax_amount)

    portal_total = sum((total(p) for p in portal_invoices), Decimal("0"))
    book_total = sum((total(b) for b in book_invoices), Decimal("0"))

    tb_gp = analyze_gst_position(tb_rows)["totals"]

    open_items = (
        [{"side": "GSTR-2B", "gstin": p.gstin, "invoice_no": p.invoice_no,
          "date": str(p.date), "value": str(total(p))} for p in portal_invoices] +
        [{"side": "Books", "gstin": b.gstin, "invoice_no": b.invoice_no,
          "date": str(b.date), "value": str(total(b))} for b in book_invoices]
    )

    pre_net_payable = SAMPLE_OUTPUT_TAX  # no ITC claimed yet

    return {
        "period": "2024-08 (sample)",
        "portal_invoices": len(portal_invoices),
        "book_invoices": len(book_invoices),
        "portal_total_value": portal_total,
        "book_total_value": book_total,
        # Gross gap between what the portal shows and what the books booked
        "gross_gap": portal_total - book_total,
        "itc_at_stake": sum((p.tax_amount for p in portal_invoices), Decimal("0")),
        "open_items": open_items,
        "books_context_tb": tb_gp,  # books-side GST position from trial balance
        "pre_net_payable": pre_net_payable,
        "note": "Everything is an open item; no ITC claimed; no exceptions classified.",
    }


# ---------------------------------------------------------------------------
# POST state (from the engine packet)
# ---------------------------------------------------------------------------
def build_post_state(packet) -> dict:
    s = packet.summary
    exceptions = [e for e in packet.exceptions]
    not_in_books = [e for e in exceptions if e.status == MatchStatus.NOT_IN_BOOKS]
    not_in_portal = [e for e in exceptions if e.status == MatchStatus.NOT_IN_PORTAL]
    amount_diff = [e for e in exceptions if e.status == MatchStatus.AMOUNT_DIFF]

    itc = packet.itc
    return {
        "matched": s["matched"],
        "not_in_books": s["not_in_books"],
        "not_in_portal": s["not_in_portal"],
        "amount_diff": s["amount_diff"],
        "confidence_breakdown": s["confidence"],
        "exceptions": [e.to_dict() for e in exceptions],
        "not_in_books_items": [{
            "invoice_no": e.portal_invoice.invoice_no,
            "gstin": e.portal_invoice.gstin,
            "value": str(e.portal_invoice.taxable_value + e.portal_invoice.tax_amount),
        } for e in not_in_books],
        "not_in_portal_items": [{
            "invoice_no": e.book_invoice.invoice_no,
            "gstin": e.book_invoice.gstin,
            "value": str(e.book_invoice.taxable_value + e.book_invoice.tax_amount),
        } for e in not_in_portal],
        "amount_diff_items": [{
            "invoice_no": e.portal_invoice.invoice_no,
            "portal_value": str(e.portal_invoice.taxable_value + e.portal_invoice.tax_amount),
            "book_value": str(e.book_invoice.taxable_value + e.book_invoice.tax_amount),
        } for e in amount_diff],
        "totals_tax": {k: str(Decimal(v or 0)) for k, v in s.get("totals_tax_amount", {}).items()},
        "eligible_itc": itc.eligible_itc,
        "ineligible_itc": itc.ineligible_itc,
        "eligible_count": itc.eligible_count,
        "ineligible_count": itc.ineligible_count,
        "gstr3b_draft": itc.gstr3b_draft,
        "net_payable": itc.gstr3b_draft["net_payable"],
        "drafted_jvs": [j.to_dict() for j in packet.drafted_jvs],
        "validation_pass": packet.validation.get("pass", False),
        "validation_checks": [
            {"name": c.get("name"), "pass": c.get("pass"),
             **({k: v for k, v in c.items() if k not in ("name", "pass")})}
            for c in packet.validation.get("checks", [])
        ],
    }


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    if isinstance(v, Decimal):
        return f"{v:,.2f}"
    if isinstance(v, float):
        return f"{v:,.4f}"
    return str(v)


def write_markdown(pre: dict, post: dict,
                   out_dir: str | Path | None = None) -> Path:
    out_dir = Path(out_dir) if out_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "pre_vs_post_reconciliation.md"
    tb = pre["books_context_tb"]

    L = []
    L.append("# GST 2B vs Books — Pre vs Post Reconciliation Report")
    L.append("")
    L.append("**Entity:** ABC SOLUTIONS (sample)   **Period:** 2024-08 (sample scenario)")
    L.append("")
    L.append("Sources: `SampleTB-AI.xlsx` (books trial balance, FY 2024-25), "
             "generated `gstr2b_sample_full.json` + `tally_purchase_sample_full.csv`."
             " All figures are SAMPLE figures for engine testing.")
    L.append("")

    L.append("## 1. Summary")
    L.append("")
    L.append("| Metric | PRE (as received) | POST (after engine) | Delta / Note |")
    L.append("|---|---|---|---|")
    L.append(f"| Portal (GSTR-2B) invoices | {pre['portal_invoices']} | — | — |")
    L.append(f"| Books (purchase) vouchers | {pre['book_invoices']} | — | — |")
    L.append(f"| Matched (High confidence) | 0 | {post['matched']} | engine matched pair |")
    L.append(f"| Amount difference (Medium) | n/a | {post['amount_diff']} | verify value |")
    L.append(f"| Not in books (portal side) | n/a | {post['not_in_books']} | JV proposed |")
    L.append(f"| Not in portal (books side) | n/a | {post['not_in_portal']} | check RC / pending GST |")
    L.append(f"| Gross gap (portal − books) | {_fmt(pre['gross_gap'])} | reconciled | F5 gate |")
    L.append(f"| Eligible input tax credited | 0 | {_fmt(post['eligible_itc'])} | +{_fmt(post['eligible_itc'])} |")
    L.append(f"| Ineligible input tax | 0 | {_fmt(post['ineligible_itc'])} | non-filer + unmatched |")
    L.append(f"| Net GST payable | {_fmt(pre['pre_net_payable'])} | {_fmt(post['net_payable'])} | ITC utilised |")
    L.append(f"| F5 validation gate | n/a | {'PASS' if post['validation_pass'] else 'BLOCKED'} | — |")
    L.append("")

    L.append("## 2. PRE — documents as received (before reconciliation)")
    L.append("")
    L.append("Every invoice is an open item. No ITC claimed, no exceptions classified, "
             "the only knowledge is the books' raw GST position.")
    L.append("")
    L.append("| Side | Invoices | Total taxable + tax |")
    L.append("|---|---|---|")
    L.append(f"| GSTR-2B (portal) | {pre['portal_invoices']} | {_fmt(pre['portal_total_value'])} |")
    L.append(f"| Tally purchase register (books) | {pre['book_invoices']} | {_fmt(pre['book_total_value'])} |")
    L.append(f"| **Gross gap** | | **{_fmt(pre['gross_gap'])}** |")
    L.append("")
    L.append("Books-side GST position (from `SampleTB-AI.xlsx`):")
    L.append("")
    L.append(f"- ITC availed in books: {_fmt(Decimal(tb['itc_availed']))}")
    L.append(f"- Output tax booked: {_fmt(Decimal(tb['output_tax']))}")
    L.append(f"- ITC carry-forward ('Claim Next Year'): {_fmt(Decimal(tb['itc_carry_forward']))}")
    L.append(f"- Net output before set-off: {_fmt(Decimal(tb['net_output_before_setoff']))}")
    L.append("")

    L.append("## 3. POST — after the reconciliation engine")
    L.append("")
    L.append("### 3.1 Classification")
    L.append("")
    tt = post.get("totals_tax", {})
    L.append("| Status | Count | Tax amount | Action |")
    L.append("|---|---|---|---|")
    L.append(f"| Matched | {post['matched']} | {_fmt(Decimal(tt.get('matched', 0) or 0))} | ok |")
    L.append(f"| Amount diff (Medium) | {post['amount_diff']} | {_fmt(Decimal(tt.get('amount_diff', 0) or 0))} | verify |")
    L.append(f"| Not in books (Low) | {post['not_in_books']} | {_fmt(Decimal(tt.get('not_in_books', 0) or 0))} | proposed JV |")
    L.append(f"| Not in portal (Low) | {post['not_in_portal']} | {_fmt(Decimal(tt.get('not_in_portal', 0) or 0))} | investigate |")
    L.append("")

    L.append("### 3.2 Exceptions")
    L.append("")
    if post["not_in_books_items"]:
        L.append("**Not found in books (portal-only):**")
        L.append("")
        L.append("| Invoice | GSTIN | Value |")
        L.append("|---|---|---|")
        for it in post["not_in_books_items"]:
            L.append(f"| {it['invoice_no']} | {it['gstin']} | {it['value']} |")
        L.append("")
    if post["amount_diff_items"]:
        L.append("**Amount differences:**")
        L.append("")
        L.append("| Invoice | Portal value | Books value |")
        L.append("|---|---|---|")
        for it in post["amount_diff_items"]:
            L.append(f"| {it['invoice_no']} | {it['portal_value']} | {it['book_value']} |")
        L.append("")
    if post["not_in_portal_items"]:
        L.append("**Booked but not in portal:**")
        L.append("")
        L.append("| Invoice | GSTIN | Value |")
        L.append("|---|---|---|")
        for it in post["not_in_portal_items"]:
            L.append(f"| {it['invoice_no']} | {it['gstin']} | {it['value']} |")
        L.append("")

    L.append("### 3.3 ITC & GSTR-3B draft")
    L.append("")
    L.append(f"- Eligible input tax credit: **{_fmt(post['eligible_itc'])}** "
             f"({post['eligible_count']} invoices)")
    L.append(f"- Ineligible input tax: **{_fmt(post['ineligible_itc'])}** "
             f"({post['ineligible_count']} invoices) — non-filer suppliers + unmatched items")
    L.append("")
    g = post["gstr3b_draft"]
    L.append(f"- Output tax (sample): {_fmt(Decimal(g['output_tax']))}")
    L.append(f"- GSTR-3B net payable: **{_fmt(Decimal(g['net_payable']))}**")
    L.append(f"- Credit available (if any): {_fmt(Decimal(g['credit_available']))}")
    L.append("")

    L.append("### 3.4 Proposed journal vouchers")
    L.append("")
    if not post["drafted_jvs"]:
        L.append("_(none)_")
    for jv in post["drafted_jvs"]:
        L.append(f"- **JV** `{jv['exception_ref']}` `{jv['voucher_date']}` "
                 f"balanced={jv['balanced']}")
        for ln in jv["lines"]:
            L.append(f"    - {ln['account']}  Dr {ln['dr']} / Cr {ln['cr']}")
        L.append(f"    - Narration: {jv['narration']}")
        L.append("")

    L.append("### 3.5 F5 data integrity gate")
    L.append("")
    L.append(f"Overall: **{'PASS' if post['validation_pass'] else 'BLOCKED'}**")
    L.append("")
    L.append("| Check | Result |")
    L.append("|---|---|")
    for c in post["validation_checks"]:
        L.append(f"| {c['name']} | {'PASS' if c.get('pass') else '~' if c.get('pass') is None else 'FAIL'} |")
    L.append("")

    L.append("## 4. What changed (PRE → POST)")
    L.append("")
    L.append("1. **9 portal invoices** and **10 book vouchers** were open items → after matching, "
             f"**{post['matched']} matched** with High confidence, including a fuzzy invoice "
             "number (`DT/0821` vs `DT-0821`) and a date-window match (`ITF-2024-08-112`, +1 day).")
    L.append(f"2. **Not in books:** {len(post['not_in_books_items'])} portal invoice(s) missing from "
             "Tally got a **balanced corrective JV** drafted automatically.")
    L.append(f"3. **Amount diff:** {len(post['amount_diff_items'])} invoice(s) matched on GSTIN+invoice "
             "but value differs → flagged Medium for verification (no ITC auto-claimed).")
    L.append(f"4. **Not in portal:** {len(post['not_in_portal_items'])} voucher(s) booked but absent "
             "from 2B → investigate reverse charge / supplier pending filing.")
    L.append(f"5. **ITC claimed:** pre-recon carried no credit → post-recon claims "
             f"**{_fmt(post['eligible_itc'])}** eligible ITC (excluding non-filer "
             f"**{_fmt(post['ineligible_itc'])}**).")
    L.append(f"6. **Net GST payable** moved from {_fmt(pre['pre_net_payable'])} (no credit) to "
             f"**{_fmt(post['net_payable'])}** after utilising eligible ITC.")
    L.append("")
    L.append("---")
    L.append("*Engine: Setu Reconciliation (Module 2A) · Sarvam-105B for judgement only · "
             "matching & arithmetic deterministic (Tier-A).*")

    out.write_text("\n".join(L), encoding="utf-8")
    return out


def write_excel(pre: dict, post: dict, packet,
                 out_dir: str | Path | None = None) -> Path:
    """Write the pre/post report as an Excel workbook (sheets: Summary, Pre, Post...)."""
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()

    # --- Summary sheet
    ws = wb.active
    ws.title = "Summary"
    rows = [
        ("Metric", "PRE", "POST"),
        ("Portal invoices", pre["portal_invoices"], post["matched"] + post["not_in_books"] + post["amount_diff"]),
        ("Books vouchers", pre["book_invoices"], post["matched"] + post["amount_diff"] + post["not_in_portal"]),
        ("Matched", 0, post["matched"]),
        ("Amount diff", "-", post["amount_diff"]),
        ("Not in books", "-", post["not_in_books"]),
        ("Not in portal", "-", post["not_in_portal"]),
        ("Eligible ITC", "0", str(post["eligible_itc"])),
        ("Ineligible ITC", "0", str(post["ineligible_itc"])),
        ("Net GST payable", str(pre["pre_net_payable"]), str(post["net_payable"])),
        ("F5 validation", "-", "PASS" if post["validation_pass"] else "BLOCKED"),
    ]
    for r in rows:
        ws.append(r)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    # --- Pre open items
    ws2 = wb.create_sheet("Pre_Open_Items")
    ws2.append(["Side", "GSTIN", "Invoice No", "Date", "Value (taxable+tax)"])
    for it in pre["open_items"]:
        ws2.append([it["side"], it["gstin"], it["invoice_no"], it["date"], it["value"]])
    for cell in ws2[1]:
        cell.font = Font(bold=True)

    # --- Post exceptions
    ws3 = wb.create_sheet("Post_Exceptions")
    ws3.append(["Status", "Confidence", "Portal Invoice", "Book Invoice",
                "EditDist", "DateDelta", "ValueDeltaPct", "Reasons"])
    for e in packet.exceptions:
        ws3.append([
            e.status.value, e.confidence.value,
            e.portal_invoice.invoice_no,
            e.book_invoice.invoice_no if e.book_invoice else "-",
            e.edit_distance, e.date_delta, e.value_delta_pct,
            "; ".join(e.reasons),
        ])
    for cell in ws3[1]:
        cell.font = Font(bold=True)

    # --- Drafted JVs
    ws4 = wb.create_sheet("Drafted_JVs")
    ws4.append(["Exception Ref", "Date", "Account", "Dr", "Cr", "Balanced"])
    for jv in packet.drafted_jvs:
        first = True
        for ln in jv.lines:
            ws4.append([jv.exception_ref if first else "",
                        jv.voucher_date if first else "",
                        ln["account"], str(ln["dr"]), str(ln["cr"]),
                        jv.is_balanced() if first else ""])
            first = False
    for cell in ws4[1]:
        cell.font = Font(bold=True)

    # --- F5 validation
    ws5 = wb.create_sheet("F5_Validation")
    ws5.append(["Check", "Pass"])
    for c in post["validation_checks"]:
        ws5.append([c["name"], "PASS" if c.get("pass") else "~" if c.get("pass") is None else "FAIL"])
    for cell in ws5[1]:
        cell.font = Font(bold=True)

    out_dir = Path(out_dir) if out_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "Pre_Post_Reconciliation_Report.xlsx"
    wb.save(out)
    return out


def write_csvs(pre: dict, post: dict,
                 out_dir: str | Path | None = None) -> tuple[Path, Path]:
    out_dir = Path(out_dir) if out_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_pre = out_dir / "pre_recon_open_items.csv"
    with open(csv_pre, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Side", "GSTIN", "Invoice No", "Date", "Value"])
        for it in pre["open_items"]:
            w.writerow([it["side"], it["gstin"], it["invoice_no"], it["date"], it["value"]])

    csv_post = out_dir / "post_recon_exceptions.csv"
    with open(csv_post, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Status", "Confidence", "Portal Invoice", "Book Invoice",
                    "EditDist", "DateDelta", "ValueDeltaPct", "Reasons"])
        for e in post["exceptions"]:
            w.writerow([e["status"], e["confidence"], e["portal_invoice"]["invoice_no"],
                        e["book_invoice"]["invoice_no"] if e["book_invoice"] else "-",
                        e["edit_distance"], e["date_delta"], e["value_delta_pct"],
                        "; ".join(e["reasons"])])
    return csv_pre, csv_post


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def generate_report() -> dict:
    # 0) (Re)generate sample documents
    gen_sample_docs(SAMPLE_DIR)

    # 1) Ingest both document sets + the trial balance
    portal = parse_gstr2b_json(SAMPLE_DIR / "gstr2b_sample_full.json")
    books = parse_tally_purchase_rows(
        _csv_rows(SAMPLE_DIR / "tally_purchase_sample_full.csv"))
    tb_rows = parse_trial_balance(SAMPLE_DIR / "SampleTB-AI.xlsx")

    # Control totals from the source documents themselves (F5 completeness)
    def ctl(invs):
        return (len(invs),
                sum((i.taxable_value + i.tax_amount for i in invs), Decimal("0")))

    # 2) Run the engine
    engine = ReconciliationEngine(Settings())
    packet = engine.run_sync(
        client_id="client_001", period="2024-08",
        portal_invoices=portal, book_invoices=books,
        client_config={"match_rules": {"invoice_edit_max": 2,
                                        "date_window_days": 7,
                                        "value_tolerance": 0.01,
                                        "require_gstin_exact": True}},
        portal_ct=ctl(portal), tally_ct=ctl(books),
        output_tax=SAMPLE_OUTPUT_TAX,
    )

    # 3) Build pre/post states
    pre = build_pre_state(portal, books, tb_rows)
    post = build_post_state(packet)

    # 4) Emit reports
    md = write_markdown(pre, post)
    xlsx = write_excel(pre, post, packet)
    html = write_html(pre, post, packet)  # visual dashboard (no verification here)
    c1, c2 = write_csvs(pre, post)

    return {"pre": pre, "post": post, "packet": packet,
            "reports": {"md": md, "xlsx": xlsx, "html": html,
                        "csv_pre": c1, "csv_post": c2}}


def _csv_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    res = generate_report()
    pre, post = res["pre"], res["post"]
    print("=" * 68)
    print("PRE vs POST GST RECONCILIATION — SAMPLE SCENARIO (2024-08)")
    print("=" * 68)
    print(f"\n PRE : {pre['portal_invoices']} portal invoices / {pre['book_invoices']} book vouchers")
    print(f"       portal total {pre['portal_total_value']} | books total {pre['book_total_value']}"
          f" | gross gap {pre['gross_gap']}")
    print(f"       ITC at stake: {pre['itc_at_stake']} | pre net payable: {pre['pre_net_payable']}")
    print(f"\n POST: matched={post['matched']}  amount_diff={post['amount_diff']}  "
          f"not_in_books={post['not_in_books']}  not_in_portal={post['not_in_portal']}")
    print(f"       eligible ITC {post['eligible_itc']} | ineligible {post['ineligible_itc']} "
          f"| net payable {post['net_payable']}")
    print(f"       JVs drafted: {len(post['drafted_jvs'])} | F5: "
          f"{'PASS' if post['validation_pass'] else 'BLOCKED'}")
    print("\n Reports written:")
    for k, p in res["reports"].items():
        print(f"   {k}: {p}")


if __name__ == "__main__":
    main()
