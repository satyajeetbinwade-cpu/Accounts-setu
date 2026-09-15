"""Books-side GST Position Analysis from a Tally Trial Balance.

Useful when you don't yet have the GSTR-2B file: this extracts the GST ledger
positions from the Trial Balance so a reviewer can see what the books say the
ITC / output tax / net payable position is -- before matching invoice-level.

This is NOT the invoice-level 2B-vs-books recon (that needs GSTR-2B). It's the
"GST Position" Gate-4 checklist item, computed from a single TB export.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .models import MatchStatus


def analyze_gst_position(tb_rows: list) -> dict[str, Any]:
    """Scan TB rows for GST-related ledgers and compute a position summary.

    Naming conventions recognised (add/fix as needed per client):
      - Input CGST/SGST/IGST [@ rate] -> ITC availed in books
      - Output CGST/SGST/IGST [@ rate] -> output tax booked
      - CGST/SGST/IGST (Claim Next Year) -> unutilised ITC asset
      - TDS Payable / any TDS account -> TDS position
      - Advance Tax -> advance tax paid
    """
    itc_availed = defaultdict(Decimal)      # by component
    output_tax = defaultdict(Decimal)
    claim_next_year = defaultdict(Decimal)
    tds_payable = defaultdict(Decimal)
    extra: list[dict[str, str]] = []

    for r in tb_rows:
        name = r.particulars.strip()
        low = name.lower()
        closing = r.closing
        debit = r.debit
        credit = r.credit

        if any(k in low for k in ("input cgst", "input sgst", "input igst")):
            itc_availed[name] += closing
            if closing == 0:
                itc_availed[name] += debit  # fully set off / utilised
        elif any(k in low for k in ("output cgst", "output sgst", "output igst")):
            output_tax[name] += max(credit, debit) if credit else debit
        elif "claim next year" in low:
            claim_next_year[name] += closing
        elif low.startswith("tds payable") or " tds payable" in low:
            tds_payable[name] += closing
        elif low in ("advance tax", "income tax"):
            extra.append({"ledger": name, "closing": str(closing), "type": "tax_paid"})

    total_itc = sum(itc_availed.values(), Decimal("0"))
    total_output = sum(output_tax.values(), Decimal("0"))
    total_carry = sum(claim_next_year.values(), Decimal("0"))

    return {
        "itc_availed_in_books": {
            k: str(v) for k, v in sorted(itc_availed.items())
        },
        "output_tax_booked": {
            k: str(v) for k, v in sorted(output_tax.items())
        },
        "itc_carry_forward": {
            k: str(v) for k, v in sorted(claim_next_year.items())
        },
        "tds_payable": {k: str(v) for k, v in sorted(tds_payable.items())},
        "other_tax_positions": extra,
        "totals": {
            "itc_availed": str(total_itc),
            "output_tax": str(total_output),
            "itc_carry_forward": str(total_carry),
            "net_output_before_setoff": str(total_output - total_itc),
        },
    }
