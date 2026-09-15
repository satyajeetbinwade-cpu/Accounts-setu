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
# (key, label, tally_column, source_hint)
# ---------------------------------------------------------------------------
CANONICAL_FIELDS: list[tuple[str, str, str, str]] = [
    ("invoice_number", "Invoice Number", "Voucher Number", "page/cell"),
    ("invoice_date", "Invoice Date", "Date", "page/cell"),
    ("vendor_name", "Vendor/Supplier Name", "Party Ledger Name", "page/cell"),
    ("vendor_gstin", "Vendor GSTIN", "Party GSTIN", "page/cell"),
    ("hsn_sac", "HSN/SAC Code", "HSN/SAC", "row"),
    ("item_description", "Item/Line Description", "Particulars", "row"),
    ("quantity", "Quantity", "Quantity", "row"),
    ("rate", "Rate", "Rate", "row"),
    ("taxable_value", "Taxable Value", "Taxable Value", "row"),
    ("cgst_amount", "CGST Amount", "CGST", "row"),
    ("sgst_amount", "SGST Amount", "SGST", "row"),
    ("igst_amount", "IGST Amount", "IGST", "row"),
    ("total_tax", "Total Tax", "Total Tax", "row"),
    ("invoice_total", "Invoice Total", "Voucher Total", "page/cell"),
    ("place_of_supply", "Place of Supply", "Place of Supply", "page/cell"),
    ("reference_po", "Reference/PO Number", "Reference", "page/cell"),
]

CANONICAL_FIELD_KEYS: list[str] = [k for k, *_ in CANONICAL_FIELDS]

# key -> (label, tally_column)
FIELD_LABELS: dict[str, str] = {k: label for k, label, _, _ in CANONICAL_FIELDS}
FIELD_TALLY_COLUMNS: dict[str, str] = {k: col for k, _, col, _ in CANONICAL_FIELDS}

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