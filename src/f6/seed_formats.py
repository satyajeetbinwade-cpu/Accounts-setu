"""F6 §5 — Seed format registry.

Four versions seeded at build time, transcribed from the real specimens
in hand (data/PNSarees/2025-04/gstr2b, .../ims via AcmeTextiles,
.../tally) plus the four additional GSTR-2B sheets the user scoped in
before this build (ISD/ISDA, IMPG/IMPGA, IMPGSEZ/IMPGSEZA, ECO/ECOA — see
UNVERIFIED_SHEETS below: no real specimen exists for these yet, so their
parse_config is derived from the documented GSTN template structure,
same merged-header pattern as B2B, and flagged unverified until a real
file confirms it).

Each entry here is a CONFIG record, not code: `parser.py`'s generic
parser reads these. A GSTN template change is then a new config row
(a new FormatVersion), never a code change.

parse_config shape (JSON):
{
  "sheets": {
    "<normalized sheet name>": {
      "role": "line_items" | "summary_ignored" | "out_of_scope" | "deferred",
      "header": {"type": "single", "row": <0-based>} |
                {"type": "pair", "rows": [<parent 0-based>, <child 0-based>]},
      "data_start_row": <0-based>,
      "row_exclusion": {
          "trailing_blank": true,
          "total_row_marker": {"column": "<flattened label>", "contains": "total"}
      },
      "field_rules": [
        {"canonical_field": "...", "kind": "direct"|"aggregate"|"derived"|"unavailable",
         "source_columns": ["<flattened label>", ...], "transform": "..."}
      ],
      "control_total": {"source": "own_total_row" | "summary_sheet:<sheet>",
                         "columns": ["<flattened label>", ...]}
    }
  },
  "date_format": "%d/%m/%Y",
  "unverified": true   # only present when no real specimen confirmed this config yet
}
"""

from __future__ import annotations

from typing import Any

# Sheets scoped in without a real specimen on hand (user decision, this
# build) — parse configs below marked accordingly; §11's acceptance
# criteria only require the four sheets WITH real specimens to actually
# ingest end-to-end.
UNVERIFIED_2B_SHEETS = {"isd", "isda", "impg", "impga", "impgsez", "impgseza"}

# GSTR-2B ITC-summary sheets and the deferred variants (§5.1) — recognised,
# never parsed as line items. "recognised_not_parsed" state.
GSTR2B_DEFERRED_SHEETS = [
    "read me",
    "itc available",
    "itc not available",
    "itc reversal",
    "itc rejected",
    "b2b (itc reversal)",
    "b2ba (itc reversal)",
    "b2b-dnr",
    "b2b-dnra",
    "b2b(rejected)",
    "b2ba(rejected)",
    "b2b-cdnr(rejected)",
    "b2b-cdnra(rejected)",
    "eco(rejected)",
    "ecoa(rejected)",
    "isd(rejected)",
    "isda(rejected)",
]


def _gstr2b_b2b_family_rules(supplier_col: str = "gstin of supplier") -> list[dict[str, Any]]:
    """Shared field_rules for B2B / B2BA / B2B-CDNR / B2B-CDNRA — all four
    share the same row-5/6 merged-header layout (§5.1)."""
    return [
        {"canonical_field": "gstin", "kind": "direct", "source_columns": [supplier_col]},
        {"canonical_field": "party_name", "kind": "direct", "source_columns": ["trade/legal name"]},
        {"canonical_field": "invoice_number", "kind": "direct",
         "source_columns": ["invoice details :: invoice number"]},
        {"canonical_field": "invoice_date", "kind": "derived",
         "source_columns": ["invoice details :: invoice date"], "transform": "parse_date:%d/%m/%Y"},
        {"canonical_field": "invoice_value", "kind": "direct",
         "source_columns": ["invoice details :: invoice value(₹)"]},
        {"canonical_field": "taxable_value", "kind": "direct", "source_columns": ["taxable value (₹)"]},
        {"canonical_field": "igst", "kind": "direct", "source_columns": ["tax amount :: integrated tax(₹)"]},
        {"canonical_field": "cgst", "kind": "direct", "source_columns": ["tax amount :: central tax(₹)"]},
        {"canonical_field": "sgst", "kind": "direct", "source_columns": ["tax amount :: state/ut tax(₹)"]},
        {"canonical_field": "cess", "kind": "direct", "source_columns": ["tax amount :: cess(₹)"]},
        # Retained verbatim per §5.1 — a reconciliation input, not derived.
        {"canonical_field": "itc_eligibility", "kind": "direct", "source_columns": ["itc availability"]},
        {"canonical_field": "reverse_charge", "kind": "derived",
         "source_columns": ["supply attract reverse charge"], "transform": "yes_no_to_bool"},
        {"canonical_field": "gstr1_period", "kind": "derived",
         "source_columns": ["gstr-1/iff/gstr-5 period"], "transform": "parse_period:MMM'YY"},
    ]


def gstr2b_config() -> dict[str, Any]:
    """§5.1 — B2B/B2BA/B2B-CDNR/B2B-CDNRA in scope with real specimens;
    ISD/ISDA/IMPG/IMPGA/IMPGSEZ/IMPGSEZA/ECO/ECOA added per this build's
    explicit scope expansion (user decision) — UNVERIFIED until a real
    specimen confirms the parse config."""
    sheets: dict[str, Any] = {}
    for sheet_key in ("b2b", "b2ba", "b2b-cdnr", "b2b-cdnra"):
        sheets[sheet_key] = {
            "role": "line_items",
            "header": {"type": "pair", "rows": [4, 5]},
            "data_start_row": 6,
            "row_exclusion": {"trailing_blank": True},
            "field_rules": _gstr2b_b2b_family_rules(),
        }
    # Deferred sheets: recognised, never parsed. Includes the ITC summary
    # sheets AND the "(Rejected)"/"(ITC Reversal)" variants (§5.1).
    for sheet_key in GSTR2B_DEFERRED_SHEETS:
        sheets[sheet_key] = {"role": "summary_ignored" if "itc" in sheet_key or sheet_key == "read me"
                              else "deferred"}
    # Newly in-scope sheets (ISD/IMPG/IMPGSEZ/ECO family) — same merged-
    # header pattern as B2B is the best structural assumption available
    # without a specimen; flagged unverified in the version record.
    for sheet_key in ("isd", "isda", "impg", "impga", "impgsez", "impgseza", "eco", "ecoa"):
        sheets[sheet_key] = {
            "role": "line_items",
            "header": {"type": "pair", "rows": [4, 5]},
            "data_start_row": 6,
            "row_exclusion": {"trailing_blank": True},
            "field_rules": _gstr2b_b2b_family_rules(),
            "unverified": True,
        }
    return {
        "sheets": sheets,
        "date_format": "%d/%m/%Y",
        "period_format": "MMM'YY",
    }


def ims_config() -> dict[str, Any]:
    """§5.2 — B2B and B2B-CN (detail) in scope. All other ITC / Import of
    Goods sheets are summary count tables, classified summary_ignored,
    never line items.

    CRITICAL TRAP (§5.2): B2B-CN has TWO tax-amount blocks with identical
    child labels (Integrated/Central/State-UT/Cess) — one under
    "Tax Amount" (supplier-declared), one under "Amount declared by
    taxpayer for ITC reduction". Keying on flattened parent::child label
    (see fingerprint.flatten_two_row_header) resolves them to DISTINCT
    source columns. See tests/test_f6_ims_dual_block.py for the
    regression test this build prompt names explicitly (§11 criterion 6).
    """
    b2b_rules = _gstr2b_b2b_family_rules(supplier_col="gstin of supplier")
    b2b_rules = [r for r in b2b_rules if r["canonical_field"] != "gstr1_period"]

    b2bcn_rules = [
        {"canonical_field": "gstin", "kind": "direct", "source_columns": ["gstin of supplier"]},
        {"canonical_field": "party_name", "kind": "direct", "source_columns": ["trade/legal name"]},
        {"canonical_field": "invoice_number", "kind": "direct", "source_columns": ["credit note number"]},
        {"canonical_field": "invoice_date", "kind": "derived",
         "source_columns": ["credit note date"], "transform": "parse_date:%d-%m-%Y"},
        {"canonical_field": "invoice_value", "kind": "direct", "source_columns": ["credit note value"]},
        {"canonical_field": "taxable_value", "kind": "direct", "source_columns": ["taxable value"]},
        # Supplier-declared block (bare labels — first occurrence).
        {"canonical_field": "igst", "kind": "direct", "source_columns": ["integrated tax"]},
        {"canonical_field": "cgst", "kind": "direct", "source_columns": ["central tax"]},
        {"canonical_field": "sgst", "kind": "direct", "source_columns": ["state/ut tax"]},
        {"canonical_field": "cess", "kind": "direct", "source_columns": ["cess"]},
        # Taxpayer-ITC-reduction block — MUST key on the flattened
        # parent::child label so it never resolves to the same source
        # column as the block above.
        {"canonical_field": "itc_reduction_igst", "kind": "direct",
         "source_columns": ["amount declared by taxpayer for itc reduction :: integrated tax"]},
        {"canonical_field": "itc_reduction_cgst", "kind": "direct",
         "source_columns": ["amount declared by taxpayer for itc reduction :: central tax"]},
        {"canonical_field": "itc_reduction_sgst", "kind": "direct",
         "source_columns": ["amount declared by taxpayer for itc reduction :: state/ut tax"]},
        {"canonical_field": "itc_reduction_cess", "kind": "direct",
         "source_columns": ["amount declared by taxpayer for itc reduction :: cess"]},
        {"canonical_field": "ims_action_state", "kind": "direct", "source_columns": ["status"]},
        {"canonical_field": "gstr1_period", "kind": "derived",
         "source_columns": ["source return period"], "transform": "parse_period:MMM-YY"},
    ]

    return {
        "sheets": {
            "b2b": {
                "role": "line_items",
                "header": {"type": "pair", "rows": [4, 5]},
                "data_start_row": 6,
                "row_exclusion": {"trailing_blank": True},
                "field_rules": b2b_rules,
            },
            "b2b-cn": {
                "role": "line_items",
                "header": {"type": "pair", "rows": [2, 2]},  # single labelled row in this specimen's layout
                "data_start_row": 3,
                "row_exclusion": {"trailing_blank": True},
                "field_rules": b2bcn_rules,
            },
            # Every other ITC/Import-of-Goods sheet: summary count tables,
            # never line items.
            "itc": {"role": "summary_ignored"},
            "import of goods": {"role": "summary_ignored"},
        },
        "date_format": "%d-%m-%Y",
        "period_format": "MMM-YY",
    }


def gstr1_config() -> dict[str, Any]:
    """§5.3 — b2b,sez,de / cdnr / b2cs. Note the literal comma in the
    first sheet name (quoted in config, never split on comma). Single
    header row (unlike 2B/IMS) at row index 3 (0-based), data from row 4.

    Rate-row aggregation (§5.3): GSTR-1 emits one row per tax rate per
    document. Per-document taxable value/tax require grouping by document
    key (gstin+invoice_number) BEFORE comparison — aggregation happens in
    THIS module so Module 2 receives document-level rows; pre-aggregation
    rows are retained for drill-down (the C1-confirmed
    pre_aggregation_drill_down field addition, §12/§13).

    Seeded now per the user's explicit decision, left UNCONSUMED (no
    sales-register reconciliation exists in Phase 2's locked scope yet).
    """
    b2b_rules = [
        {"canonical_field": "gstin", "kind": "direct", "source_columns": ["gstin/uin of recipient"]},
        {"canonical_field": "party_name", "kind": "direct", "source_columns": ["receiver name"]},
        {"canonical_field": "invoice_number", "kind": "direct", "source_columns": ["invoice number"]},
        {"canonical_field": "invoice_date", "kind": "derived",
         "source_columns": ["invoice date"], "transform": "parse_date:%d-%m-%Y"},
        {"canonical_field": "invoice_value", "kind": "direct", "source_columns": ["invoice value"]},
        {"canonical_field": "taxable_value", "kind": "aggregate",
         "source_columns": ["taxable value"], "transform": "group_by_document_key"},
        {"canonical_field": "igst", "kind": "aggregate",
         "source_columns": ["integrated tax amount"], "transform": "group_by_document_key"},
        {"canonical_field": "cgst", "kind": "aggregate",
         "source_columns": ["central tax amount"], "transform": "group_by_document_key"},
        {"canonical_field": "sgst", "kind": "aggregate",
         "source_columns": ["state/ut tax amount"], "transform": "group_by_document_key"},
        {"canonical_field": "cess", "kind": "aggregate",
         "source_columns": ["cess amount"], "transform": "group_by_document_key"},
    ]
    return {
        "sheets": {
            "b2b,sez,de": {
                "role": "line_items",
                "header": {"type": "single", "row": 3},
                "data_start_row": 4,
                "row_exclusion": {"trailing_blank": True},
                "field_rules": b2b_rules,
                # §5.3 empty-sheet handling: zero data rows is a valid
                # non-error outcome, reported as "0 rows" not a failure.
                "allow_empty": True,
            },
            "cdnr": {
                "role": "line_items",
                "header": {"type": "single", "row": 3},
                "data_start_row": 4,
                "row_exclusion": {"trailing_blank": True},
                "field_rules": b2b_rules,
                "allow_empty": True,
            },
            "b2cs": {
                "role": "line_items",
                "header": {"type": "single", "row": 3},
                "data_start_row": 4,
                "row_exclusion": {"trailing_blank": True},
                "field_rules": [
                    {"canonical_field": "party_name", "kind": "unavailable",
                     "source_columns": [], "transform": None},  # B2CS has no identified recipient
                    {"canonical_field": "taxable_value", "kind": "aggregate",
                     "source_columns": ["taxable value"], "transform": "group_by_document_key"},
                    {"canonical_field": "igst", "kind": "aggregate",
                     "source_columns": ["integrated tax amount"], "transform": "group_by_document_key"},
                    {"canonical_field": "cgst", "kind": "aggregate",
                     "source_columns": ["central tax amount"], "transform": "group_by_document_key"},
                    {"canonical_field": "sgst", "kind": "aggregate",
                     "source_columns": ["state/ut tax amount"], "transform": "group_by_document_key"},
                ],
                "allow_empty": True,
            },
        },
        "date_format": "%d-%m-%Y",
        "provisioned_only": True,  # seeded, unconsumed — no sales-register recon exists yet
    }


def purchase_register_tally_config() -> dict[str, Any]:
    """§5.4 — Purchase Register (Tally Bill-wise), CLIENT-SCOPED to
    PNSarees. Title/metadata rows 1-5, blank row 6, header row 7 (0-based
    row index 6), data from row 8 (0-based index 7).

    Mandatory aggregations (§5.4): no single CGST/SGST/IGST column exists
    — each tax head is split across rate buckets, and a single invoice
    can populate two buckets at once. De-duplication is the single most
    error-prone item named in this build — see
    tests/test_f6_purchase_register_dedupe.py for the required per-
    rate-bucket unit test (§11 criterion 7).
    """
    return {
        "sheets": {
            "purchase register (bill-wise)": {
                "role": "line_items",
                "header": {"type": "single", "row": 6},
                "data_start_row": 7,
                "row_exclusion": {
                    "trailing_blank": True,
                    "total_row_marker": {"column": "purc. type", "contains": "total"},
                },
                "field_rules": [
                    {"canonical_field": "invoice_number", "kind": "direct", "source_columns": ["bill no"]},
                    {"canonical_field": "invoice_date", "kind": "derived",
                     "source_columns": ["date"], "transform": "parse_date:%d-%m-%Y"},
                    {"canonical_field": "party_name", "kind": "direct",
                     "source_columns": ["name & address of dealer"]},
                    {"canonical_field": "gstin", "kind": "direct", "source_columns": ["gstin"]},
                    {"canonical_field": "invoice_value", "kind": "direct", "source_columns": ["bill amount"]},
                    # Rate-bucket aggregation, mandatory per §5.4. taxable
                    # value de-duplicated across heads (CGST and SGST
                    # buckets restate the SAME taxable base — summing all
                    # four double-counts it, so only ONE side's buckets
                    # feed taxable_value; see parser.py's dedupe logic).
                    {"canonical_field": "igst", "kind": "aggregate",
                     "source_columns": ["igst tax amt @ 5%", "igst tax amt @ 18%"]},
                    {"canonical_field": "cgst", "kind": "aggregate",
                     "source_columns": ["cgst tax amt @ 2.5%", "cgst tax amt @ 9%"]},
                    {"canonical_field": "sgst", "kind": "aggregate",
                     "source_columns": ["sgst tax amt @ 2.5%", "sgst tax amt @ 9%"]},
                    {"canonical_field": "taxable_value", "kind": "aggregate",
                     "source_columns": [
                         "igst txbl. amt @ 5%", "igst txbl. amt @ 18%",
                         "cgst txbl. amt @ 2.5%", "cgst txbl. amt @ 9%",
                     ],
                     "transform": "dedupe_taxable_base_cgst_sgst_restate"},
                    {"canonical_field": "rounding_adjustment", "kind": "direct",
                     "source_columns": ["other amt."]},
                ],
                "control_total": {"source": "own_total_row",
                                   "columns": ["bill amount", "cgst tax amt @ 2.5%", "cgst tax amt @ 9%",
                                               "sgst tax amt @ 2.5%", "sgst tax amt @ 9%",
                                               "igst tax amt @ 5%", "igst tax amt @ 18%", "other amt."]},
            },
        },
        "date_format": "%d-%m-%Y",
    }


# (source_format_key, label, slot, scope_kind, provisioned_only, description, sort_order, parse_config)
SEED_SOURCE_FORMATS: list[tuple[str, str, str, str, bool, str, int]] = [
    ("gstr2b", "GSTR-2B", "portal", "firm_only", False,
     "GSTN-standardized ITC statement.", 1),
    ("gstr1", "GSTR-1", "portal", "firm_only", True,
     "GSTN-standardized outward supplies return. Seeded, unconsumed — no sales-register recon in scope.", 2),
    ("ims", "IMS export", "portal", "firm_only", False,
     "GSTN Invoice Management System export.", 3),
    ("purchase_register", "Purchase Register / Books", "books", "client_scoped", False,
     "Internal books/purchase export — layout varies per client's accounting setup.", 4),
    ("sales_register", "Sales Register / Books", "books", "client_scoped", True,
     "Provisioned, not seeded — no real specimen or consuming recon yet.", 5),
    ("tds_26as", "Form 26AS / TDS return", "portal", "firm_only", True,
     "Provisioned, not seeded — F6 scope is spreadsheet/delimited GST sources this build.", 6),
]

SEED_VERSIONS: dict[str, Any] = {
    "gstr2b": gstr2b_config,
    "ims": ims_config,
    "gstr1": gstr1_config,
    "purchase_register": purchase_register_tally_config,
}
