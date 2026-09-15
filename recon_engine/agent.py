"""Automation agent: run the whole GST 2B vs Books reconciliation from files.

Given input documents, the agent:
  1. ingests + normalises the GSTR-2B JSON and Tally purchase register
  2. runs the rule-based engine (match, classify, ITC, F5 gate)
  3. independently verifies the results (recon_engine.verification)
  4. generates the pre-vs-post reconciliation report (md / xlsx / csv)
  5. returns a ready-to-review packet + file paths

Usage (CLI):
    python -m recon_engine.agent \
        --gstr2b sample_data/gstr2b_sample_full.json \
        --tally  sample_data/tally_purchase_sample_full.csv \
        [--tb sample_data/SampleTB-AI.xlsx] \
        [--period 2024-08] [--client acme] [--output-tax 120000]

Programmatic:
    from recon_engine.agent import auto_reconcile
    result = auto_reconcile("2b.json", "tally.csv", output_tax="120000")
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import ReconciliationEngine
from .prepost_report import (
    build_post_state,
    build_pre_state,
    write_csvs,
    write_excel,
    write_markdown,
    REPORTS_DIR,
)
from .review_queue import ReviewQueue
from .htmlreport import write_html
from .sample_scenario import generate as gen_sample_docs
from .verification import (
    independent_totals,
    verify_packet,
    load_raw_books as _raw_books,
    load_raw_portal as _raw_portal,
)
from .parsers import (
    parse_gstr2b_excel,
    parse_gstr2b_json,
    parse_tally_excel,
    parse_tally_purchase_rows,
)
from .parsers.trial_balance import parse_trial_balance

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def auto_reconcile(
    gstr2b_path: str | Path,
    tally_path: str | Path,
    tb_path: str | Path | None = None,
    client_id: str = "client_001",
    period: str = "",
    output_tax: str | Decimal = "0",
    client_config: dict | None = None,
    settings: Settings | None = None,
    reports_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Full automated reconciliation from input files -> review packet + reports.

    Returns a dict with: packet, pre-state, post-state, verification, reports.
    reports_dir overrides the report output folder (default: reports/);
    the API and batch runner use per-client/per-period packs.
    """
    settings = settings or Settings()
    gstr2b_path = Path(gstr2b_path)
    tally_path = Path(tally_path)

    if not gstr2b_path.exists():
        raise FileNotFoundError(f"GSTR-2B file not found: {gstr2b_path}")
    if not tally_path.exists():
        raise FileNotFoundError(f"Tally file not found: {tally_path}")

    # ---- 1. Ingest (format auto-detected: json/csv/excel) ------------------------
    portal = _load_portal(gstr2b_path)
    books = _load_books(tally_path)

    if not period:
        period = _infer_period(portal)

    # Control totals straight from the source systems (F5 completeness).
    def ctl(invs):
        return (len(invs),
                sum((i.taxable_value + i.tax_amount for i in invs), Decimal("0")))

    # ---- 2. Engine -----------------------------------------------------------------
    engine = ReconciliationEngine(settings)
    packet = engine.run_sync(
        client_id=client_id, period=period,
        portal_invoices=portal, book_invoices=books,
        client_config=client_config,
        portal_ct=ctl(portal), tally_ct=ctl(books),
        output_tax=output_tax,
    )

    # ---- 4. Pre/post states + reports ---------------------------------------------
    tb_rows = parse_trial_balance(tb_path) if tb_path and Path(tb_path).exists() else []
    pre = build_pre_state(portal, books, tb_rows)
    post = build_post_state(packet)

    out_dir = Path(reports_dir) if reports_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    reports = {
        "md": write_markdown(pre, post, out_dir=out_dir),
        "xlsx": write_excel(pre, post, packet, out_dir=out_dir),
        "csv_pre": None, "csv_post": None,
    }
    reports["csv_pre"], reports["csv_post"] = write_csvs(pre, post, out_dir=out_dir)

    # ---- 3. Independent verification ----------------------------------------------
    # Forward the ACTUAL input files so verification recomputes from the same
    # raw data (not the canonical sample defaults). Loaders dispatch on
    # extension: json/csv (GSTN) and xlsx/xlsm (portal/Tally Excel). The report
    # check points at the Markdown emitted for THIS run.
    verification = verify_packet(packet,
                                 portal_path=gstr2b_path,
                                 books_path=tally_path,
                                 report_md=out_dir / "pre_vs_post_reconciliation.md",
                                 match_rules=client_config)

    # ---- 5. Review queue (Phase-3 Module-3 workflow) -----------------------
    review = ReviewQueue.from_packet(packet)
    review_path = out_dir / "review_queue.json"
    review.save(review_path)
    reports["review_queue"] = review_path

    # HTML dashboard gets the queue so the Review-queue tab is populated.
    reports["html"] = write_html(
        pre, post, packet, verification=verification,
        out_path=out_dir / "gst_2b_reconciliation_report.html",
        sources=_mtime_sources(gstr2b_path, tally_path, tb_path),
        review=review)

    return {
        "packet": packet,
        "pre": pre,
        "post": post,
        "verification": verification,
        "review": review,
        "reports": reports,
        "llm_used": settings.llm_enabled and settings.has_api_key,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_portal(path: str | Path):
    """GSTR-2B input: JSON export or portal Excel export."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        return parse_gstr2b_json(p)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return parse_gstr2b_excel(p)
    raise ValueError(f"unsupported GSTR-2B format: {p.suffix} "
                     "(use .json or .xlsx)")


def _load_books(path: str | Path):
    """Tally purchase register: CSV rows or Excel export."""
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return parse_tally_excel(p)
    if p.suffix.lower() == ".csv":
        with open(p, encoding="utf-8") as f:
            return parse_tally_purchase_rows(list(csv.DictReader(f)))
    raise ValueError(f"unsupported Tally register format: {p.suffix} "
                     "(use .csv or .xlsx)")


def _infer_period(portal) -> str:
    dates = [i.date for i in portal if i.date]
    if not dates:
        return "unknown"
    d = max(dates)
    return f"{d.year}-{d.month:02d}"


def _mtime_sources(g2b: Path, tally: Path, tb: Path | None) -> dict[str, str]:
    """Per-source data-freshness timestamps for the M7 dashboard."""
    from datetime import datetime as _dt
    def ts(p: Path) -> str:
        return _dt.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    out = {"portal (GSTR-2B)": ts(g2b), "books (Tally)": ts(tally)}
    if tb:
        tbp = Path(tb)
        if tbp.exists():
            out["trial balance"] = ts(tbp)
    return out


def _fmt(v) -> str:
    if isinstance(v, Decimal):
        return f"{v:,.2f}"
    return str(v)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Automated GST 2B vs Books reconciliation agent")
    ap.add_argument("--gstr2b", required=True, help="GSTR-2B input: JSON export or portal Excel (.xlsx)")
    ap.add_argument("--tally", required=True, help="Tally purchase register: CSV or Excel (.xlsx)")
    ap.add_argument("--tb", default=None, help="Optional trial balance xlsx (books context)")
    ap.add_argument("--client", default="client_001")
    ap.add_argument("--period", default="", help="e.g. 2024-08 (infers if blank)")
    ap.add_argument("--output-tax", default="0", help="Output GST recorded for the period")
    ap.add_argument("--materiality", default=None,
                    help="2A materiality gate amount (exceptions >= this need "
                         "individual review; enables batch approval below it)")
    args = ap.parse_args(argv)

    client_config = None
    if args.materiality is not None:
        client_config = {"match_rules": {"materiality_amount": args.materiality}}

    try:
        res = auto_reconcile(
            gstr2b_path=args.gstr2b,
            tally_path=args.tally,
            tb_path=args.tb,
            client_id=args.client,
            period=args.period,
            output_tax=args.output_tax,
            client_config=client_config,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    packet = res["packet"]
    s = packet.summary
    v = res["verification"]
    print("=" * 72)
    print(f"AUTOMATED RECONCILIATION AGENT  |  client={args.client}  period={packet.period}")
    print("=" * 72)
    print(f"  GSTR-2B invoices: {s['total_portal_invoices']}   "
          f"Tally vouchers: {s['total_book_invoices']}")
    print(f"  matched={s['matched']}  amount_diff={s['amount_diff']}  "
          f"not_in_books={s['not_in_books']}  not_in_portal={s['not_in_portal']}")
    print(f"  eligible ITC: {_fmt(packet.itc.eligible_itc)}  "
          f"ineligible: {_fmt(packet.itc.ineligible_itc)}  "
          f"net payable: {_fmt(packet.itc.gstr3b_draft['net_payable'])}")
    print(f"  F5 gate: {'PASS' if packet.validation.get('pass') else 'BLOCKED'}  "
          f"verification: {'PASS' if v.get('pass') else 'FAIL'}")
    if res.get("llm_used"):
        print(f"  LLM (Sarvam-105B): enabled (judgement layer)")
        if packet.ai_summary:
            print(f"  AI summary: {packet.ai_summary}")
    else:
        print("  LLM (Sarvam-105B): none (no API key / disabled) - deterministic only")
    print("-" * 72)
    print("  Reports:")
    for k, p in res["reports"].items():
        print(f"    {k}: {p}")
    print("=" * 72)
    return 0 if (packet.validation.get("pass") and v.get("pass")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
