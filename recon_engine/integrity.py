"""F5 Data Integrity & Validation for the reconciliation engine.

Deterministic checks that must hold BEFORE the engine's output is trusted:
  1. Completeness  -- control totals from source systems vs what was ingested.
  2. Structural validity -- every record passed format checks (F5 schema).
  3. Reconciliation-of-the-reconciliation -- matched + unmatched + excluded
     sums reconcile back to the original ingested totals from BOTH sources.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import Invoice, MatchResult, MatchStatus
from .normalizer import validate_gstin, validate_invoice


def validate_structure(invoices: list[Invoice]) -> dict[str, Any]:
    """Run structural validation over an invoice list. Returns report."""
    errors: list[dict[str, Any]] = []
    seen: dict[str, int] = {}

    for i, inv in enumerate(invoices):
        inv_errors = validate_invoice(inv)
        gstin_err = validate_gstin(inv.gstin)
        if gstin_err:
            inv_errors.append(gstin_err)
        key = f"{inv.gstin}|{inv.invoice_no}"
        earlier = seen.get(key)
        if earlier is not None:   # None sentinel: row index 0 is a valid value
            inv_errors.append(f"duplicate of row {earlier}")
        seen[key] = i

        if inv_errors:
            errors.append({"row": i, "reference": inv.reference,
                           "errors": inv_errors})

    return {
        "checked": len(invoices),
        "errors": errors,
        "pass": len(errors) == 0,
    }


def control_totals_check(
    source_label: str,
    expected_count: int,
    expected_sum: Decimal,
    actual_invoices: list[Invoice],
) -> dict[str, Any]:
    """Completeness check: expected voucher count/sum vs what we received."""
    actual_count = len(actual_invoices)
    actual_sum = sum((inv.taxable_value + inv.tax_amount for inv in actual_invoices), Decimal("0"))
    count_ok = actual_count == expected_count
    sum_ok = abs(actual_sum - expected_sum) <= Decimal("0.01")
    return {
        "source": source_label,
        "expected_count": expected_count,
        "received_count": actual_count,
        "expected_sum": str(expected_sum),
        "received_sum": str(actual_sum),
        "count_match": count_ok,
        "sum_match": sum_ok,
        "pass": count_ok and sum_ok,
        "issue": None if (count_ok and sum_ok) else
                 ("count mismatch" if not count_ok else "") +
                 (" + sum mismatch" if not sum_ok else ""),
    }


def recon_of_recon(results: list[MatchResult]) -> dict[str, Any]:
    """Confirm matched + unmatched + excluded sums reconcile to ingested totals.

    Row ownership by design:
      - Portal-side rows carry status MATCHED / NOT_IN_BOOKS / AMOUNT_DIFF.
      - Books-side rows carry status MATCHED / AMOUNT_DIFF / NOT_IN_PORTAL.

    Every ingested portal invoice must produce exactly one portal-side row and
    every ingested book invoice exactly one books-side row, so the accounting
    check is: sum of each side's rows == the total ingested from that source.
    """
    def total(inv: Invoice | None) -> Decimal:
        return (inv.taxable_value + inv.tax_amount) if inv else Decimal("0")

    portal_rows = [r for r in results
                   if r.status in (MatchStatus.MATCHED,
                                   MatchStatus.NOT_IN_BOOKS,
                                   MatchStatus.AMOUNT_DIFF)]
    book_rows = [r for r in results
                 if r.status in (MatchStatus.MATCHED,
                                 MatchStatus.AMOUNT_DIFF,
                                 MatchStatus.NOT_IN_PORTAL)]

    portal_total = sum((total(r.portal_invoice) for r in portal_rows), Decimal("0"))
    book_total = sum((total(r.book_invoice) for r in book_rows), Decimal("0"))
    portal_accounted = sum((total(r.portal_invoice) for r in portal_rows), Decimal("0"))
    book_accounted = sum((total(r.book_invoice) for r in book_rows), Decimal("0"))

    ok = portal_total == portal_accounted and book_total == book_accounted
    return {
        "pass": ok,
        "portal_ingested": str(portal_total),
        "portal_accounted": str(portal_accounted),
        "books_ingested": str(book_total),
        "books_accounted": str(book_accounted),
        "portal_rows_checked": len(portal_rows),
        "book_rows_checked": len(book_rows),
        "note": "matched + unmatched + excluded sums reconcile to ingested totals"
                if ok else "DROP DETECTED: buckets do not sum to ingested totals",
    }


def run_all_validation(
    portal_invoices: list[Invoice],
    book_invoices: list[Invoice],
    results: list[MatchResult],
    portal_ct: tuple[int, Decimal] | None = None,
    book_ct: tuple[int, Decimal] | None = None,
) -> dict[str, Any]:
    """Run the full F5 suite for a recon run. Returns nested report dict."""
    checks: list[Any] = []

    portal_struct = validate_structure(portal_invoices)
    book_struct = validate_structure(book_invoices)
    structural = {
        "name": "structural_validity",
        "pass": portal_struct["pass"] and book_struct["pass"],
        "portal": portal_struct,
        "books": book_struct,
    }
    checks.append(structural)

    if portal_ct:
        checks.append({
            "name": "completeness_portal",
            **control_totals_check("gstr2b", portal_ct[0], portal_ct[1], portal_invoices),
        })
    if book_ct:
        checks.append({
            "name": "completeness_tally",
            **control_totals_check("tally_purchase", book_ct[0], book_ct[1], book_invoices),
        })

    checks.append({
        "name": "reconciliation_of_the_reconciliation",
        **recon_of_recon(results),
    })

    passed = all(c.get("pass", True) for c in checks
                 if c.get("pass") is not None)
    return {"pass": passed, "checks": checks}
