"""Review packet builder (Module 3 + gate packet).

Deterministic JV construction: the engine drafts balanced corrective entries
for "Not in Books" (portal) invoices. The LLM may be asked to write the
narration, but the amounts and balancing come from Tier-A code.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .models import DraftedJV, ITCComputation, MatchResult, MatchStatus, ReviewPacket


# Account mapping (v1 defaults; per-client configurable via client_config)
PURCHASE_ACCOUNT = "Purchase A/c"
CGST_ACCOUNT = "Input CGST"
SGST_ACCOUNT = "Input SGST"
IGST_ACCOUNT = "Input IGST"
SUNDRY_ACCOUNT = "Sundry Creditors A/c"


def _draft_jv_for_portal(portal: MatchResult, voucher_date: date,
                         narration_llm: str = "") -> DraftedJV | None:
    """Draft a corrective JV for a 'Not in Books' portal invoice.

    Uses the 'reverse charge / purchase not booked' treatment by default:
        Dr Purchase A/c   (taxable value)
        Dr Input CGST/SGST/IGST
        Cr Sundry Creditors A/c  (total)
    Always balanced by construction (dr == cr).
    """
    inv = portal.portal_invoice
    lines: list[dict[str, str | Decimal]] = [
        {"account": PURCHASE_ACCOUNT, "dr": inv.taxable_value, "cr": Decimal("0")},
    ]
    if inv.cgst:
        lines.append({"account": CGST_ACCOUNT, "dr": inv.cgst, "cr": Decimal("0")})
    if inv.sgst:
        lines.append({"account": SGST_ACCOUNT, "dr": inv.sgst, "cr": Decimal("0")})
    if inv.igst:
        lines.append({"account": IGST_ACCOUNT, "dr": inv.igst, "cr": Decimal("0")})

    total_dr = sum(Decimal(l["dr"]) for l in lines)
    lines.append({"account": SUNDRY_ACCOUNT, "dr": Decimal("0"), "cr": total_dr})

    narration = narration_llm or (
        f"Purchase booked from GSTR-2B not found in books: supplier {inv.gstin}, "
        f"invoice {inv.invoice_no} dated {inv.date}"
    )
    return DraftedJV(
        exception_ref=inv.invoice_no,
        voucher_date=voucher_date,
        narration=narration,
        lines=lines,
        rationale=(
            f"GSTR-2B shows invoice {inv.invoice_no} which is absent from Tally. "
            f"Suggested entry books the purchase and input tax; verify nature "
            f"(reverse charge / unrecorded purchase) before approving."
        ),
    )


def build_review_packet(
    client_id: str,
    period: str,
    results: list[MatchResult],
    itc: ITCComputation,
    summary: dict,
    validation: dict,
    voucher_date: date | None = None,
    llm_narrations: dict[str, str] | None = None,
) -> ReviewPacket:
    """Assemble the full gate packet with deterministic JV drafts."""
    packet = ReviewPacket(
        client_id=client_id,
        period=period,
        summary=summary,
        exceptions=results,
        itc=itc,
        validation=validation,
    )
    llm_narrations = llm_narrations or {}

    for r in results:
        # Draft a corrective JV only for genuine 'Not in Books' portal rows.
        if r.status != MatchStatus.NOT_IN_BOOKS:
            continue
        jv = _draft_jv_for_portal(
            r, voucher_date or r.portal_invoice.date,
            narration_llm=llm_narrations.get(r.portal_invoice.invoice_no, ""),
        )
        if jv:
            packet.drafted_jvs.append(jv)

    return packet
