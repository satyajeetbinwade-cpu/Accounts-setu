"""One-off runner: regenerate the interactive HTML report with the
Sarvam-105B judgement layer (AI summary, verifier, JV narrations).

Mirrors agent.auto_reconcile but drives the ASYNC engine.run path so the
LLM layer actually fires (auto_reconcile uses run_sync = deterministic only).

Usage:
    .venv/bin/python run_sarvam_report.py <client_id> <period> <gstr2b> <tally> <out_html>
"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

from recon_engine.agent import _load_portal, _load_books, _mtime_sources
from recon_engine.config import Settings
from recon_engine.engine import ReconciliationEngine
from recon_engine.htmlreport import write_html
from recon_engine.prepost_report import (
    build_post_state, build_pre_state,
    write_csvs, write_excel, write_markdown,
)
from recon_engine.review_queue import ReviewQueue
from recon_engine.verification import verify_packet


async def main() -> None:
    client_id, period, g2b, tally, out_html = sys.argv[1:6]
    output_tax = sys.argv[6] if len(sys.argv) > 6 else "0"
    out = Path(out_html)
    out.parent.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    portal = _load_portal(Path(g2b))
    books = _load_books(Path(tally))

    def ctl(invs):
        return (len(invs),
                sum((i.taxable_value + i.tax_amount for i in invs), Decimal("0")))

    engine = ReconciliationEngine(settings)
    print(f"[sarvam] available={engine.sarvam.available()} model={settings.model}")

    packet = await engine.run(
        client_id=client_id, period=period,
        portal_invoices=portal, book_invoices=books,
        portal_ct=ctl(portal), tally_ct=ctl(books), output_tax=output_tax,
    )

    pre = build_pre_state(portal, books, [])
    post = build_post_state(packet)

    write_markdown(pre, post, out_dir=out.parent)
    write_excel(pre, post, packet, out_dir=out.parent)
    write_csvs(pre, post, out_dir=out.parent)

    verification = verify_packet(packet, portal_path=Path(g2b), books_path=Path(tally),
                                 report_md=out.parent / "pre_vs_post_reconciliation.md")

    review = ReviewQueue.from_packet(packet)
    review.save(out.parent / "review_queue.json")

    rpt = write_html(pre, post, packet, verification=verification,
                     out_path=out,
                     sources=_mtime_sources(Path(g2b), Path(tally), None),
                     review=review)

    print("=" * 70)
    print(f"REPORT WRITTEN : {rpt}")
    print(f"AI SUMMARY     : {packet.ai_summary!r}")
    print(f"LLM JV NARRATIONS : {len(packet.drafted_jvs)}")
    for jv in packet.drafted_jvs:
        print(f"   - {jv.narration}")
    print(f"VERIFICATION   : {'PASS' if verification.get('pass') else 'FAIL'}")
    print(f"F5 GATE        : {'PASS' if packet.validation.get('pass') else 'BLOCKED'}")
    print(f"LLM USED       : {settings.llm_enabled and settings.has_api_key}")


if __name__ == "__main__":
    asyncio.run(main())
