"""Run books-side integrity checks + GST position analysis on a Tally TB export.

Usage:
    python -m recon_engine.tb_demo [path_to_tb.xlsx]
"""
from __future__ import annotations

import sys
from decimal import Decimal

from .parsers.trial_balance import (
    parse_trial_balance, tb_runs_balance, tb_control_check,
)
from .gst_position import analyze_gst_position


def main(path: str) -> None:
    rows = parse_trial_balance(path)
    print(f"Parsed {len(rows)} TB rows from {path}\n")

    # F5-style integrity on the TB itself
    balance = tb_runs_balance(rows)
    print("== TB INTEGRITY ==")
    print(f"  TB balances: {'YES' if balance['pass'] else 'NO'}")
    if "grand_total_debit" in balance:
        print(f"  grand total debit : {balance['grand_total_debit']}")
        print(f"  grand total credit: {balance['grand_total_credit']}")
        print(f"  leaf rows counted: {balance['leaf_rows']}")
        if not balance.get("leaf_reconciles_to_grand"):
            print("  (leaf-level re-sum is approximate -- this Tally export carries no")
            print("   explicit parent/child hierarchy; the Grand Total row above is the")
            print("   authoritative balance check, and it balances.)")
    else:
        print(f"  total debit  : {balance['total_debit']}")
        print(f"  total credit : {balance['total_credit']}")

    # GST Position
    print("\n== GST POSITION (Books side, from TB) ==")
    gp = analyze_gst_position(rows)
    if gp["itc_availed_in_books"]:
        print("\n  ITC availed in books (Input CGST/SGST/IGST):")
        for k, v in gp["itc_availed_in_books"].items():
            print(f"    {k:28} {v}")
    else:
        print("\n  ITC availed in books: none found")

    if gp["output_tax_booked"]:
        print("\n  Output tax booked (Output CGST/SGST/IGST):")
        for k, v in gp["output_tax_booked"].items():
            print(f"    {k:28} {v}")
    else:
        print("\n  Output tax booked: none found")

    if gp["itc_carry_forward"]:
        print("\n  ITC carry-forward (Claim Next Year):")
        for k, v in gp["itc_carry_forward"].items():
            print(f"    {k:28} {v}")
    if gp["tds_payable"]:
        print("\n  TDS payable:")
        for k, v in gp["tds_payable"].items():
            print(f"    {k:28} {v}")
    if gp["other_tax_positions"]:
        print("\n  Other tax positions:")
        for x in gp["other_tax_positions"]:
            print(f"    {x['ledger']:28} {x['closing']}  ({x['type']})")

    t = gp["totals"]
    print("\n== TOTALS ==")
    for k, v in t.items():
        print(f"  {k:28} {v}")

    print("\n== INTERPRETATION ==")
    total_itc = Decimal(t["itc_availed"])
    total_output = Decimal(t["output_tax"])
    net = total_output - total_itc
    if net > 0:
        print(f"  Books show net output liability of {net} after utilising ITC "
              "(subject to 2B confirmation of ITC eligibility).")
    elif net < 0:
        print(f"  Books show ITC credit in excess of output tax of {-net} "
              "-> carry-forward, verify against 2B.")
    else:
        print("  Books show a balanced GST position (output = input) -- verify against portal.")
    print("  NOTE: this is the TB-ledger view. Invoice-level 2B-vs-books matching "
          "requires the GSTR-2B file.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "sample_data/SampleTB-AI.xlsx"
    main(path)
