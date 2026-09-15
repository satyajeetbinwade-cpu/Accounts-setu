# Demo data for testing the reconciliation engine (real-time, via the UI)

Open http://localhost:8124/clients/<client-id> and upload the pair of files
(GSTR-2B first, Tally purchase register second), set the period, and click **Run**.

Each scenario is engineered so the F5 gate and independent verification PASS,
and the dashboard lights up different tabs. Default match rules:
invoice-no edit tolerance = 2, date window = ±7 days, value tolerance = 1%,
GSTIN must match exactly.

## demo_clean  (client id: `democlean`, period `2024-09`)
A tidy supplier ledger with two genuine open items.
- 9 matched (2 are Medium confidence on purpose:
  `DL/240913` vs portal `DL/240912` - typo, and `MH-8841` booked 12 days late)
- 1 NOT_IN_BOOKS  : `GJ/90833` in GSTR-2B but never booked in Tally
- 1 NOT_IN_PORTAL : `GJ/90908` booked in Tally but missing from GSTR-2B
- Best for the Overview tab, matched invoices, and the GSTR-3B draft.

## demo_exceptions  (client id: `demoexceptions`, period `2024-09`)
A messy books year: needle the Exceptions + Review-queue tabs.
- 2 AMOUNT_DIFF   : `SB/240905` (+4.7%) and `CN/7788` vs `CN/7794`
- 1 GSTIN mismatch: books `CN/7888` under a different GSTIN -> open items both ways
- 1 blocked credit: `SB/240930` isBlocked=true (ITC flagged, §17(5)-style)
- 1 RCM supply    : `HR-552` reverse-charge flagged
- 1 late booking  : `KA/INV-301` posted 18 days after invoice date
- 3 NOT_IN_BOOKS  : `NC/1204`, `GJ/7744`, `HR-558`
- 3 NOT_IN_PORTAL : `MR/00099`, `KA/INV-310`, `CN/7785`

## demo_igst  (client id: `demoigst`, period `2024-10`)
Cross-state spend at 18% IGST - a near-perfect match (8 matched + 1 amount
diff `SB/10011`), plus one blocked-credit invoice `DL/1015`. Best for watching
IGST-only ITC flow and the green matched view.

## demo_excel  (client id: `demoexcel`, period `2024-10`)
Same clean inter-state dataset, but as real portal/Tally Excel exports:
- `gstr2b_portal_b2b.xlsx`  - GSTIN 2B download-style workbook (B2B sheet,
  "ITC Availability" column, Status R)
- `tally_purchase_register.xlsx` - Tally purchase-register export layout
Use it to verify the Excel upload path (allowed: GSTR-2B .json/.xlsx/.xlsm,
Tally .csv/.xlsx/.xlsm).

## Try these variations
- **Loosen matching**: uncheck "Require exact GSTIN" or widen the date window
  to see `demo_exceptions` converge (e.g. `CN/7888` then matches its GSTIN).
- **Materiality gate**: pass e.g. materiality=25000 to demo_exceptions and the
  review queue marks items below cutoff as auto-approvable.
- **Period blank**: field can be left empty - the engine infers the period
  from the invoices.
