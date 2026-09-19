"""Seed data for F3-B — Invoice Extraction & Digitalization.

Additive-upsert only — never resets a value an admin changed, mirrors
auth.seed's _sync_new_permissions and rules.seed.

Two things are seeded here:
  1. The locked canonical field set (matches Tally voucher-import columns
     plus GST-return needs) — the fixed column layout extraction targets
     and the export writes.
  2. The two new AITouchpoint rows this build adds to C3-ext's
     ModelAssignment table (invoice_extraction_visual and
     invoice_extraction_structured). Both are seeded ACTIVE at build time
     — unlike C3-ext's original placeholder rows for modules not yet
     built, this module ships functional AI extraction from day one.
"""

from __future__ import annotations

import sqlite3

# ---------------------------------------------------------------------------
# Locked canonical field set (F3-B build prompt, "Canonical field set").
# (key, label, tally_column, source_hint, section)
#
# ``section`` is the review screen's grouping key — where on a real invoice
# this field lives. It is a CODE constant (like the field set itself), not a
# table: the review screen groups by it and nothing else consumes it.
# ---------------------------------------------------------------------------
SECTION_HEADER = "header"
SECTION_LINE_ITEMS = "line_items"
SECTION_TOTALS = "totals"

# Display order + human labels for the three review-screen groups.
SECTION_ORDER: list[str] = [SECTION_HEADER, SECTION_LINE_ITEMS, SECTION_TOTALS]
SECTION_LABELS: dict[str, str] = {
    SECTION_HEADER: "Header",
    SECTION_LINE_ITEMS: "Line items",
    SECTION_TOTALS: "Totals block",
}

CANONICAL_FIELDS: list[tuple[str, str, str, str, str]] = [
    ("invoice_number", "Invoice Number", "Voucher Number", "page/cell", SECTION_HEADER),
    ("invoice_date", "Invoice Date", "Date", "page/cell", SECTION_HEADER),
    ("vendor_name", "Vendor/Supplier Name", "Party Ledger Name", "page/cell", SECTION_HEADER),
    ("vendor_gstin", "Vendor GSTIN", "Party GSTIN", "page/cell", SECTION_HEADER),
    ("place_of_supply", "Place of Supply", "Place of Supply", "page/cell", SECTION_HEADER),
    ("reference_po", "Reference/PO Number", "Reference", "page/cell", SECTION_HEADER),
    ("hsn_sac", "HSN/SAC Code", "HSN/SAC", "row", SECTION_LINE_ITEMS),
    ("item_description", "Item/Line Description", "Particulars", "row", SECTION_LINE_ITEMS),
    ("quantity", "Quantity", "Quantity", "row", SECTION_LINE_ITEMS),
    ("rate", "Rate", "Rate", "row", SECTION_LINE_ITEMS),
    ("taxable_value", "Taxable Value", "Taxable Value", "row", SECTION_TOTALS),
    ("cgst_amount", "CGST Amount", "CGST", "row", SECTION_TOTALS),
    ("sgst_amount", "SGST Amount", "SGST", "row", SECTION_TOTALS),
    ("igst_amount", "IGST Amount", "IGST", "row", SECTION_TOTALS),
    ("total_tax", "Total Tax", "Total Tax", "row", SECTION_TOTALS),
    ("invoice_total", "Invoice Total", "Voucher Total", "page/cell", SECTION_TOTALS),
]

CANONICAL_FIELD_KEYS: list[str] = [k for k, *_ in CANONICAL_FIELDS]

# key -> (label, tally_column)
FIELD_LABELS: dict[str, str] = {k: label for k, label, _, _, _ in CANONICAL_FIELDS}
FIELD_TALLY_COLUMNS: dict[str, str] = {k: col for k, _, col, _, _ in CANONICAL_FIELDS}
FIELD_SECTIONS: dict[str, str] = {k: section for k, _, _, _, section in CANONICAL_FIELDS}

# ---------------------------------------------------------------------------
# Math-derivable fields (review-screen suggestions only).
#
# key -> (dependency keys, human formula label). A flagged/not-present field
# whose dependencies are ALL auto-accepted (or already resolved) gets its
# input PRE-FILLED with the computed value, clearly labelled as a suggestion.
# It is NEVER auto-confirmed — the reviewer still clicks Resolve, per the
# platform's standing no-silent-posting principle. If any dependency is
# itself flagged/missing, no suggestion is offered (the arithmetic would be
# unreliable) and the field stays a manual entry.
# ---------------------------------------------------------------------------
DERIVATIONS: dict[str, tuple[list[str], str]] = {
    "total_tax": (["cgst_amount", "sgst_amount", "igst_amount"], "CGST + SGST + IGST"),
    "invoice_total": (["taxable_value", "total_tax"], "Taxable Value + Total Tax"),
}

# ---------------------------------------------------------------------------
# The two new C3-ext AITouchpoint rows (seeded ACTIVE). See the build
# prompt's "Recommended Runtime AI Model Assignment".
# (key, label, primary_model, primary_provider, fallback_model,
#  fallback_provider, sort_order)
# ---------------------------------------------------------------------------
SEED_TOUCHPOINTS: list[tuple[str, str, str, str, str, str, int]] = [
    (
        "invoice_extraction_visual",
        "Invoice extraction — visual (images / scanned PDFs)",
        "Claude Sonnet (vision)", "Anthropic",
        "DeepSeek V4.1 Flash", "OpenRouter",
        1,
    ),
    (
        "invoice_extraction_structured",
        "Invoice extraction — structured (native PDF / Excel / Word)",
        "DeepSeek V4.1 Flash", "OpenRouter",
        "DeepSeek V4 Flash", "OpenRouter",
        2,
    ),
]

# Platform-wide default confidence threshold (admin-editable per touchpoint
# in C3-ext, exactly like F3-AI's thresholds — never hardcoded in code).
DEFAULT_CONFIDENCE_THRESHOLD = 90

EXPORT_FORMAT_VERSION = "tally-1.0"


def run_seed(conn: sqlite3.Connection) -> None:
    """Additively upsert the two touchpoint rows only. The canonical field
    set is a code constant (it drives extraction + export deterministically),
    not a table — so there is nothing to seed for it. Existing touchpoint
    rows are left untouched (an admin's edit to a model assignment survives)."""
    for key, label, p_model, p_prov, f_model, f_prov, order in SEED_TOUCHPOINTS:
        conn.execute(
            """
            INSERT INTO invoice_ai_touchpoints
                (touchpoint_key, label, primary_model, primary_provider,
                 fallback_model, fallback_provider, is_active, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(touchpoint_key) DO NOTHING
            """,
            (key, label, p_model, p_prov, f_model, f_prov, order),
        )
    conn.commit()