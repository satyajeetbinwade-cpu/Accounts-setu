"""IMS thin rules layer (Module 2A) - deterministic, conservative.

The Invoice Management System (IMS) requires each 2A/2B record to be tagged
Accept / Reject / Pending before ITC can be claimed. This module derives that
recommendation from the match result + supplier status + §17(5)/RCM flags.

Design rule: Tier-A code only. Conservative defaults -- any uncertainty
(fuzzy match, amount dispute, supplier not filed, not in books) yields
'Pending' so a human decides. Blocked/RCM rows are 'Reject' (never claimable).
Books-only rows ('not in portal') are 'N/A' because the IMS only governs
records present in the portal.

Also provides the 2A materiality tag used by the review queue:
  - 'above' -> requires individual review, never batch-approved (PRD M2 #3)
  - 'below' -> eligible for batch approval
  - 'none'  -> materiality gate not configured
"""
from __future__ import annotations

from .config import MatchRules
from .models import MatchResult, MatchStatus

SUPPLIER_NON_FILER = ("non_filer", "noncompliant", "pending")


def recommend_ims(result: MatchResult) -> str:
    """IMS recommendation for one match result: Accept|Reject|Pending|N/A."""
    if result.status == MatchStatus.NOT_IN_PORTAL:
        return "N/A"                      # IMS governs portal records only

    pinv = result.portal_invoice
    if pinv.blocked_credit:
        return "Reject"                   # §17(5) - credit blocked by law
    if pinv.reverse_charge:
        return "Reject"                   # RCM handled outside regular ITC

    if result.status in (MatchStatus.NOT_IN_BOOKS, MatchStatus.AMOUNT_DIFF):
        return "Pending"                  # book entry / value dispute first

    if result.status == MatchStatus.MATCHED:
        status = (pinv.status or "").lower()
        if status in SUPPLIER_NON_FILER:
            return "Pending"              # supplier must file first
        if result.confidence.value == "high":
            return "Accept"
        return "Pending"                  # fuzzy -> human confirm

    return "Pending"


def materiality_for(result: MatchResult, rules: MatchRules | None) -> str:
    """2A materiality tag vs the configured gate amount."""
    if rules is None or rules.materiality_amount is None:
        return "none"
    amount = result.portal_invoice.taxable_value
    return "above" if amount >= rules.materiality_amount else "below"


def apply_meta(result: MatchResult, rules: MatchRules | None) -> MatchResult:
    """Attach materiality + IMS recommendation to a match result."""
    result.materiality = materiality_for(result, rules)
    result.ims_recommendation = recommend_ims(result)
    return result
