"""Demo/CLI showing a GST 2B vs Books reconciliation run end-to-end.

Usage:
    python -m recon_engine.demo              # run with sample data (no LLM)
    SARVAM_API_KEY=... python -m recon_engine.demo   # with Sarvam-105B
    SARVAM_API_KEY=... SETU_LLM_ENABLED=0 python -m recon_engine.demo  # force off
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
from decimal import Decimal

from .config import Settings
from .engine import ReconciliationEngine
from .parsers import parse_gstr2b_json, parse_tally_purchase_rows


SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample_data")


async def load_tally_rows() -> list[dict]:
    rows = []
    with open(os.path.join(SAMPLE, "tally_purchase_sample.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


async def main() -> None:
    settings = Settings()
    if not settings.has_api_key:
        print("[demo] No SARVAM_API_KEY set -- running with LLM disabled "
              "(deterministic only). Set SARVAM_API_KEY to enable Sarvam-105B.\n")

    engine = ReconciliationEngine(settings)
    rows = await load_tally_rows()

    # Control totals (F5 completeness) are normally read from an authoritative
    # source-system summary -- e.g. Tally's voucher count/sum or the GSTN
    # portal's 2B summary block. Here we derive them from the parsed source so
    # the F5 checks genuinely reconcile with the ingested data.
    portal_parsed = parse_gstr2b_json(os.path.join(SAMPLE, "gstr2b_sample.json"))
    tally_parsed = parse_tally_purchase_rows(rows)

    def ctl_sum(invs):
        return sum((inv.taxable_value + inv.tax_amount for inv in invs), Decimal("0"))

    packet = await engine.run(
        client_id="client_001",
        period="2026-08",
        portal_invoices=portal_parsed,
        book_invoices=tally_parsed,
        # (expected_count, expected_sum) from the authoritative source
        portal_ct=(len(portal_parsed), ctl_sum(portal_parsed)),
        tally_ct=(len(tally_parsed), ctl_sum(tally_parsed)),
        output_tax="120000",
    )

    print("=" * 70)
    print(f"SETU GST 2B vs BOOKS -- Client {packet.client_id} period {packet.period}")
    print("=" * 70)
    print("\n[SUMMARY]")
    for k, v in packet.summary.items():
        print(f"  {k:16} {v}")
    print("\n[ITC / GSTR-3B DRAFT]")
    if packet.itc:
        for k, v in packet.itc.to_dict().items():
            if k != "gstr3b_draft":
                print(f"  {k:16} {v}")
        print("  gstr3b_draft:")
        for k, v in packet.itc.gstr3b_draft.items():
            print(f"      {k:18} {v}")
    print("\n[F5 VALIDATION]")
    print("  overall:", "PASS" if packet.validation.get("pass") else "BLOCKED")
    for c in packet.validation.get("checks", []):
        status = "PASS" if c.get("pass") else ("~" if c.get("pass") is None else "FAIL")
        print(f"  - {c.get('name')}: {status}")

    print("\n[EXCEPTIONS]")
    for i, e in enumerate(packet.exceptions, 1):
        portal = e.portal_invoice
        book_no = e.book_invoice.invoice_no if e.book_invoice else "-"
        print(f"  {i:2}. {e.status.value:14} conf={e.confidence.value:6} "
              f"portal={portal.invoice_no:14} book={book_no:12} "
              f"ed={e.edit_distance} dd={e.date_delta}d "
              f"gstin_ok={portal.gstin == (e.book_invoice.gstin if e.book_invoice else '')}")
        for reason in e.reasons:
            print(f"        - {reason}")

    print("\n[DRAFTED JVs]")
    if not packet.drafted_jvs:
        print("  (none)")
    for jv in packet.drafted_jvs:
        print(f"  * {jv.narration}  balanced={jv.is_balanced()}")
        for line in jv.lines:
            print(f"      {line['account']:20} dr={line['dr']:>12} cr={line['cr']:>12}")

    print("\n[AI SUMMARY]")
    print("  ", packet.ai_summary or "(LLM disabled / failed -- deterministic fallback used)")

    # Also emit the machine-readable packet
    out = os.path.join(os.path.dirname(SAMPLE), "output_packet.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(packet.to_dict(), f, indent=2, default=str)
    print(f"\n[OUTPUT] full packet written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
