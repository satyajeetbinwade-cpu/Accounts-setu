"""Deterministic ITC eligibility & GSTR-3B draft figures (Module 2A).

All calculation is Tier-A code. The LLM never does arithmetic.

Phase-2 rules added:
  - §17(5) blocked-credit rows are NEVER eligible, even when matched.
  - Reverse-charge (RCM) rows are NEVER eligible in the regular stream
    (RCM ITC, if claimable, is booked separately).
  - Live credit position is exposed on the packet (eligible now / blocked /
    awaiting supplier / disputed / unbooked) feeding Module 7.
"""
from __future__ import annotations

from decimal import Decimal

from .models import ITCComputation, MatchResult, MatchStatus


# Supplier statuses per GSTR-2B ("status" field on portal rows)
SUPPLIER_COMPLIANT = ("active", "compliant", "registered")
SUPPLIER_NON_FILER = ("non_filer", "noncompliant", "pending")


def compute_itc(
    results: list[MatchResult],
    output_tax: Decimal = Decimal("0"),
) -> ITCComputation:
    """Compute eligible/ineligible ITC from match results and draft GSTR-3B.

    Eligibility (v1 defaults + Phase-2 §17(5)/RCM):
      - Eligible   = matched, compliant supplier, NOT RCM, NOT blocked.
      - Ineligible = matched-but-non-filer, RCM, §17(5)-blocked, or
                     unmatched/disputed portal invoices.

    GSTR-3B draft: output tax minus eligible ITC = net payable.
    itc_position breaks the total into live buckets for review (Module 7).
    """
    comp = ITCComputation()
    pos = {
        "eligible_now": Decimal("0"),
        "blocked": Decimal("0"),            # §17(5) + RCM (never claimable)
        "awaiting_supplier": Decimal("0"),  # matched, supplier not filed
        "disputed": Decimal("0"),           # amount difference
        "unbooked": Decimal("0"),           # in portal, missing from books
    }
    for r in results:
        p = r.portal_invoice
        if r.status in (MatchStatus.NOT_IN_BOOKS, MatchStatus.AMOUNT_DIFF):
            comp.ineligible_itc += p.tax_amount
            comp.ineligible_count += 1
            pos["unbooked" if r.status == MatchStatus.NOT_IN_BOOKS
               else "disputed"] += p.tax_amount
            continue

        if r.status != MatchStatus.MATCHED:
            continue  # NOT_IN_PORTAL rows have no portal tax to classify

        if p.reverse_charge or p.blocked_credit:
            # §17(5) / RCM: matched but never claimable in the regular stream
            comp.ineligible_itc += p.tax_amount
            comp.ineligible_count += 1
            pos["blocked"] += p.tax_amount
            continue

        status = (p.status or "").lower()
        if status in SUPPLIER_NON_FILER:
            comp.ineligible_itc += p.tax_amount
            comp.ineligible_count += 1
            pos["awaiting_supplier"] += p.tax_amount
        else:
            comp.eligible_itc += p.tax_amount
            comp.eligible_count += 1
            pos["eligible_now"] += p.tax_amount

    net_payable = output_tax - comp.eligible_itc
    comp.itc_position = pos
    comp.gstr3b_draft = {
        "output_tax": output_tax,
        "eligible_itc": comp.eligible_itc,
        "ineligible_itc": comp.ineligible_itc,
        "net_payable": net_payable if net_payable >= 0 else Decimal("0"),
        "credit_available": -net_payable if net_payable < 0 else Decimal("0"),
    }
    return comp


def build_summary(results: list[MatchResult]) -> dict:
    """Aggregate summary metrics for the review packet."""
    counts = {s.value: 0 for s in MatchStatus}
    tax_amt = {s.value: Decimal("0") for s in MatchStatus}
    conf_count = {"high": 0, "medium": 0, "low": 0}

    for r in results:
        counts[r.status.value] += 1
        tax_amt[r.status.value] += r.portal_invoice.tax_amount
        conf_count[r.confidence.value] += 1

    return {
        "total_portal_invoices": sum(counts[s.value] for s in (
            MatchStatus.MATCHED, MatchStatus.NOT_IN_BOOKS, MatchStatus.AMOUNT_DIFF)),
        "total_book_invoices": sum(counts[s.value] for s in (
            MatchStatus.MATCHED, MatchStatus.AMOUNT_DIFF, MatchStatus.NOT_IN_PORTAL)),
        "matched": counts[MatchStatus.MATCHED.value],
        "not_in_books": counts[MatchStatus.NOT_IN_BOOKS.value],
        "not_in_portal": counts[MatchStatus.NOT_IN_PORTAL.value],
        "amount_diff": counts[MatchStatus.AMOUNT_DIFF.value],
        "totals_tax_amount": {
            "matched": str(tax_amt[MatchStatus.MATCHED.value]),
            "not_in_books": str(tax_amt[MatchStatus.NOT_IN_BOOKS.value]),
            "not_in_portal": str(tax_amt[MatchStatus.NOT_IN_PORTAL.value]),
            "amount_diff": str(tax_amt[MatchStatus.AMOUNT_DIFF.value]),
        },
        "confidence": conf_count,
    }
