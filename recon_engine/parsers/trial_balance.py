"""Parser for the Tally Trial Balance Excel export.

Layout (standard Tally "Export to Excel" TB):
  rows 0..n   : company header (title, address, contacts) -- skipped
  a header row: "Particulars | <period>"
  a subheader : "        | Opening | Transactions |    | Closing"
  data rows   : Particulars, Opening Balance, Debit, Credit, Closing Balance
  last row    : "Grand Total | ..."

Note: group rows carry balances; child rows have blank cells where the group
sums them. We preserve both -- sibling detection via indentation would be
needed, so we simply keep all rows and label group vs child by blank cells.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..integrity import control_totals_check


class TBRow:
    __slots__ = ("particulars", "opening", "debit", "credit", "closing", "is_group")

    def __init__(self, particulars: str, opening: Decimal, debit: Decimal,
                 credit: Decimal, closing: Decimal, is_group: bool = False):
        self.particulars = particulars
        self.opening = opening
        self.debit = debit
        self.credit = credit
        self.closing = closing
        self.is_group = is_group

    def to_dict(self) -> dict[str, Any]:
        return {
            "particulars": self.particulars,
            "opening": str(self.opening),
            "debit": str(self.debit),
            "credit": str(self.credit),
            "closing": str(self.closing),
            "is_group": self.is_group,
        }


def _to_dec(v) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    return Decimal(str(v).replace(",", ""))


# Groups in this TB (from inspection) -- parent rows whose values are the
# roll-up of their children. Parent rows must be excluded from any leaf sum.
known_groups = {
    "Capital Account",
    "Loans (Liability)", "Unsecured Loans",
    "Current Liabilities", "Duties & Taxes", "Sundry Creditors",
    "Expenses Payable",
    "Fixed Assets", "Investments",
    "Current Assets", "Loans & Advances (Asset)",
    "Sundry Debtors", "Cash-in-Hand", "Bank Accounts",
    "Sales Accounts", "Direct Expenses", "Indirect Incomes", "Indirect Expenses",
    # NOT a group: "Profit & Loss A/c" (leaf ledger, no children)
}


def parse_trial_balance(path: str) -> list[TBRow]:
    """Parse a Tally TB xlsx into a list of TBRows (header rows dropped)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]

    rows: list[TBRow] = []
    started = False
    for raw in ws.iter_rows(values_only=True):
        if not raw or raw[0] is None:
            continue
        p = str(raw[0]).strip()

        # Header zone detection
        if not started:
            if p.lower().startswith("particulars"):
                started = True
            continue  # pre-header address/company lines

        # Every TB data row has a particulars value in column 0. Rows with a
        # blank first cell are sub-header continuation ("Opening | ..." under
        # "Particulars") or empty lines -- skip them.
        if not p:
            continue
        if p.lower() in ("opening", "balance", "transactions", "debit", "credit", "closing"):
            continue

        if p.lower() in ("grand total", "total"):
            rows.append(TBRow(
                particulars="Grand Total",
                opening=_to_dec(raw[1]), debit=_to_dec(raw[2]),
                credit=_to_dec(raw[3]), closing=_to_dec(raw[4]),
                is_group=True,
            ))
            continue

        opening, debit, credit, closing = (raw[1], raw[2], raw[3], raw[4])
        # Skip subtotal rows that are exactly identical to a group (parent).
        # Tally parent group = non-empty non-child row; we keep everything and
        # flag groups by the known set + presence of opening/closing but blank
        # transaction columns in child leaf rows.
        is_group = p in known_groups or (
            opening not in (None, "") and debit in (None, "") and credit in (None, ""))

        rows.append(TBRow(
            particulars=p,
            opening=_to_dec(opening),
            debit=_to_dec(debit),
            credit=_to_dec(credit),
            closing=_to_dec(closing),
            is_group=is_group,
        ))
    return rows


def tb_runs_balance(rows: list[TBRow]) -> dict[str, Any]:
    """F5-style check: TB debits equal credits.

    A Tally TB export contains BOTH group (parent) rows -- whose values are the
    roll-up of their children -- and the individual leaf ledgers. Summing every
    row double-counts. So:
      * the authoritative check is the exported Grand Total row
      * we cross-check by summing only LEAF rows the exporter itself would
        reproduce in the grand total.
    """
    grand = next((r for r in rows if r.particulars == "Grand Total"), None)

    total_dr = sum((r.debit for r in rows if not r.is_group), Decimal("0"))
    total_cr = sum((r.credit for r in rows if not r.is_group), Decimal("0"))

    if grand is not None:
        ok = grand.debit == grand.credit
        return {
            "pass": ok,
            "grand_total_debit": str(grand.debit),
            "grand_total_credit": str(grand.credit),
            "difference": str(abs(grand.debit - grand.credit)),
            "leaf_rows": len([r for r in rows if not r.is_group]),
            "leaf_debit": str(total_dr),
            "leaf_credit": str(total_cr),
            "leaf_reconciles_to_grand":
                abs(total_dr - grand.debit) <= Decimal("0.01") and
                abs(total_cr - grand.credit) <= Decimal("0.01"),
            "note": "Trial Balance debits equal credits" if ok
                    else "TB DOES NOT BALANCE",
        }

    ok = total_dr == total_cr
    return {
        "pass": ok,
        "total_debit": str(total_dr),
        "total_credit": str(total_cr),
        "difference": str(abs(total_dr - total_cr)),
        "note": "Trial Balance debits equal credits (leaf rows)" if ok
                else "TB DOES NOT BALANCE",
    }


def tb_control_check(rows: list[TBRow], expected_grand: Decimal | None = None) -> dict[str, Any]:
    """Completeness: sum of closing balances equals the Grand Total."""
    closing_sum = sum((abs(r.closing) for r in rows), Decimal("0"))
    return {
        "pass": expected_grand is None or abs(closing_sum - expected_grand) <= Decimal("0.01"),
        "closing_summed": str(closing_sum),
        "expected": str(expected_grand) if expected_grand is not None else None,
    }
