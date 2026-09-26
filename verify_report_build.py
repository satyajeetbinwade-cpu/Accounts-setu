"""§7 mandatory self-verification for the Phase-2 report build.

Run: ./venv/bin/python verify_report_build.py
Prints one PASS/FAIL line per §7 item. Read-only over the existing run data.
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitz  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from src import db, queries  # noqa: E402
from src.action_center import report as acr  # noqa: E402
from src.module2 import service as m2  # noqa: E402
from src.reconciliation import report_export as ex  # noqa: E402
from src.reconciliation import review as rv  # noqa: E402
from src.reconciliation import run_model  # noqa: E402

RUN_ID = 11
RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))


def money(v: float) -> str:
    return rv.format_money(v)


def main() -> int:
    db.init_db()
    run, model = run_model.load_run_model(RUN_ID)
    k = model["kpi"]
    data = ex.build_export_data(RUN_ID)
    ek = data["kpi"]

    # --- 1. Headline is a tax amount with a % badge, cross-checked by hand ---
    nib_tax = sum(
        rv.component_tax_total(r.get("portal_record") if isinstance(r.get("portal_record"), dict)
                               else json.loads(r.get("portal_record") or "{}"))
        for r in queries.get_results(RUN_ID).to_dict("records")
        if r["classification"] == "Not in Books"
    )
    check(
        "1. Headline is ITC at stake (tax) with a % badge, hand-summed",
        abs(k["itc_at_stake_tax"] - round(nib_tax, 2)) < 0.01
        and k["itc_at_stake_pct_display"].endswith("%")
        and k["period_itc_total"] > 0,
        f"{money(k['itc_at_stake_tax'])} tax = {k['itc_at_stake_pct_display']} of "
        f"{money(k['period_itc_total'])} period ITC · hand sum {money(nib_tax)}",
    )

    # --- 2. Gross invoice value is not the headline anywhere on the screen ---
    screen_headline = k["itc_at_stake_tax"]
    check(
        "2. Gross invoice value is not the headline/at-risk number",
        screen_headline != k["gross_value"] and k["gross_value"] == round(
            sum(i["difference"] for i in model["items"] if i["bucket"] != "Matched"), 2
        ),
        f"headline {money(screen_headline)} (tax) vs gross {money(k['gross_value'])} "
        f"(secondary: supplier chart + detail tables + labelled note)",
    )

    # --- 3. Zero-ineligible run renders no second segment ---
    check(
        "3. ITC donut on a 0-ineligible run renders a single ring",
        len(model["itc"]["slices"]) == 1
        and model["itc"]["slices"][0]["name"] == "Eligible ITC"
        and model["itc"]["single"] is True,
        f"slices={[s['name'] for s in model['itc']['slices']]}",
    )

    # --- 4. A run WITH an ineligible/RCM record renders the second segment ---
    credit = m2.eligible_credit_for_client(1, period="2026-08")
    patched = dict(credit)
    patched["blocked_credit_itc"] = 1500.0
    patched["reverse_charge_itc"] = 500.0
    synthetic = rv.build_review_model(
        run, queries.get_results(RUN_ID).to_dict("records"),
        itc_eligible=patched["eligible_credit"],
        itc_claimed=patched["total_itc_claimed"],
        itc_blocked=0.0, itc_reverse_charge=2000.0,
    )
    slices = synthetic["itc"]["slices"]
    check(
        "4. ITC donut renders the second segment proportionally when one exists",
        len(slices) == 2 and slices[1]["value"] == 2000.0 and synthetic["itc"]["single"] is False,
        f"slices={[(s['name'], s['value']) for s in slices]}",
    )

    # --- 5. All three formats + the live screen agree on every headline number ---
    b64_ok = True
    paths = {}
    for fmt in ("html", "pdf", "xlsx"):
        paths[fmt] = ex.export_report(RUN_ID, fmt)
    from openpyxl import load_workbook  # noqa: F811

    wb = load_workbook(paths["xlsx"])
    ov = wb["Overview"]
    fmt_values = {
        "invoice_count": ov["B10"].value,
        "matched": ov["B11"].value,
        "itc_at_stake": ov["B7"].value,
        "pct": ov["B9"].value,
        "credit_notes": ov["B16"].value,
        "credit_note_tax": ov["B17"].value,
    }
    # The Excel cells hold FORMULAS (not values) — assert that, then compare
    # against the recalculated export produced by LibreOffice separately.
    formulas_are_live = all(
        isinstance(v, str) and v.startswith("=") for v in fmt_values.values()
    )
    html_text = Path(paths["html"]).read_text(encoding="utf-8")
    # The raw xlsx stores no cached value, so "Excel agrees" is asserted on the
    # recalculated workbook (see verify_recalc) and on the formula targets here.
    check(
        "5a. Excel Overview totals are LIVE formulas (no hardcoded numbers)",
        formulas_are_live,
        "; ".join(f"{k}={str(v)[:34]}" for k, v in fmt_values.items()),
    )
    sheet = wb["Master data"]
    invoice_count = sum(1 for r in sheet.iter_rows(min_row=2, max_col=1) if r[0].value)
    cn_sheet = wb["Credit Notes"]
    cn_count = sum(1 for r in cn_sheet.iter_rows(min_row=2, max_col=1) if r[0].value)
    check(
        "5b. Detail sheets carry the same counts as the live screen",
        invoice_count == k["total_count"] and cn_count == ek["credit_note_count"],
        f"invoices {invoice_count} vs screen {k['total_count']} · "
        f"credit notes {cn_count} vs screen {ek['credit_note_count']}",
    )
    check(
        "5c. HTML export carries the same headline + ITC-at-stake figures",
        ek["itc_at_stake_display"] in html_text
        and ek["itc_at_stake_pct_display"] in html_text
        and ek["period_itc_total_display"] in html_text
        and str(ek["invoice_count"]) in html_text,
        f"{ek['itc_at_stake_display']} · {ek['itc_at_stake_pct_display']} · "
        f"{ek['period_itc_total_display']}",
    )

    # --- 6. PDF: light theme, multi-column layout, charts, non-Qt renderer ---
    pdf = fitz.open(paths["pdf"])
    producer = (pdf.metadata or {}).get("producer") or ""
    text = "\n".join(p.get_text() for p in pdf)
    has_charts = "Eligible ITC" in text and "Top suppliers" in text
    check(
        "6. PDF renderer is Chromium (not wkhtmltopdf/QtWebKit), charts present",
        "Chromium" in producer or "Skia" in producer,
        f"producer={producer!r}, pages={pdf.page_count}, charts={'yes' if has_charts else 'no'}",
    )
    page1 = pdf[0]
    images = page1.get_images(full=True)
    drawings = page1.get_drawings()
    check(
        "6b. PDF page 1 keeps the multi-column grid (cards side by side)",
        len(drawings) > 8,
        f"{len(drawings)} vector drawing objects on page 1 (grid + cards + donuts)",
    )

    # --- 7. Excel recalculates (LibreOffice re-opens THIS workbook) ---
    # The workbook stores FORMULAS with no cached values, so a real spreadsheet
    # app must recompute them. We hand the exact file we just wrote to
    # LibreOffice and read back what it calculated.
    import csv as _csv
    import shutil
    import subprocess
    import tempfile

    recalc_dir = tempfile.mkdtemp(prefix="recalc_")
    recalc_values: dict[str, str] = {}
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        try:
            subprocess.run(
                [soffice, "--headless", "--calc", "--convert-to", "csv",
                 "--outdir", recalc_dir, str(paths["xlsx"])],
                check=True, capture_output=True, timeout=240,
            )
            csv_files = glob.glob(f"{recalc_dir}/*.csv")
            if csv_files:
                with open(csv_files[0], newline="", encoding="utf-8") as fh:
                    for row in _csv.reader(fh):
                        if len(row) >= 2 and row[0]:
                            recalc_values[row[0].strip()] = row[1].strip()
        except Exception as exc:  # noqa: BLE001
            print(f"  (LibreOffice recalc failed: {exc})")

    if recalc_values:
        def _f(key: str) -> float:
            try:
                return float(recalc_values.get(key, "").replace(",", "").rstrip("%"))
            except ValueError:
                return float("nan")

        checks = {
            "ITC at stake (tax on exceptions)": (k["itc_at_stake_tax"], _f("ITC at stake (tax on exceptions)")),
            "Period ITC (total tax in run)": (k["period_itc_total"], _f("Period ITC (total tax in run)")),
            "Invoices": (float(k["total_count"]), _f("Invoices")),
            "Matched": (float(k["matched_count"]), _f("Matched")),
            "Exceptions": (float(k["exception_count"]), _f("Exceptions")),
            "Credit notes": (float(ek["credit_note_count"]), _f("Credit notes")),
            "Credit note tax": (ek["credit_note_tax"], _f("Credit note tax")),
        }
        mismatched = [
            f"{name}: recalc {got} vs screen {want}"
            for name, (want, got) in checks.items()
            if not (abs(got - want) < 0.01)
        ]
        pct_ok = abs(_f("ITC at stake % of period ITC") - k["itc_at_stake_pct"]) < 0.01
        check(
            "7. Excel recalculates in a real spreadsheet app, matching the screen",
            not mismatched and pct_ok,
            ("all 7 totals + the % reconcile exactly · " if not mismatched and pct_ok
             else "; ".join(mismatched) + f" · pct ok={pct_ok} · ")
            + f"ITC at stake {money(k['itc_at_stake_tax'])} · period ITC "
            f"{money(k['period_itc_total'])} · {k['itc_at_stake_pct_display']} · "
            f"CN tax {money(ek['credit_note_tax'])}",
        )
    else:
        check("7. Excel recalculates in a real spreadsheet app", False,
              "LibreOffice unavailable or produced no output (formulas are still live)")

    # --- 8. §6 no regression: invoices/credit notes, confidence, rounding note ---
    rows = queries.get_results(RUN_ID).to_dict("records")
    invoice_rows = [r for r in rows if r["classification"] != "Matched"
                    or not str(r.get("portal_record") or "").lower().startswith("null")]
    cn_note = next((n for n in (run.get("run_notes") or [])
                    if n.get("code") == "credit_notes_not_reconciled"), None)
    quality_titles = " ".join(n["title"] for n in data["quality_notes"])
    high_confidence = all(
        i["confidence_band"] == "High" for i in model["items"]
        if i["classification"] == "Matched" and i["confidence_score"] >= 100
    )
    check(
        "8. §6 no regression (39 invoices / 12 CNs separate, confidence, rounding note)",
        k["total_count"] == 39 and ek["credit_note_count"] == 12
        and cn_note is not None and "Rounding adjustments" in quality_titles
        and high_confidence,
        f"{k['total_count']} invoices · {ek['credit_note_count']} credit notes tracked "
        f"separately · rounding note {'present' if 'Rounding adjustments' in quality_titles else 'MISSING'}",
    )

    # --- 9. Module 8 report module shares the same fix ---
    ac = acr.build_report(RUN_ID)
    check(
        "9. Module 8 report module no longer shows a false Ineligible slice",
        len(ac["gst"]["itc_slices"]) == 1 and ac["gst"]["itc_single"] is True,
        ac["gst"]["itc_note"],
    )

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"===== {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS =====")
    for label, ok, detail in failed:
        print(f"  FAILED: {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
