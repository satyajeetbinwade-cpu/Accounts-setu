"""Stable result fingerprinting — carries review status across re-runs.

A fingerprint identifies the underlying source record(s), NOT the verdict
the matcher assigned. It must be unaffected by tolerance, fuzzy threshold,
or confidence baseline changes in config — those are exactly what tuning
changes between runs, and the fingerprint must survive them so review
status carried in `review_state` (see src/db.py) still applies.

  GST:  gstin + normalized invoice number + document type
  TDS:  pan + section + period + amount at its stored precision (2dp)

For results with only one side present (Not in Books / Not in Portal /
section-validation findings), fingerprint that side only.

Fingerprints are not guaranteed unique within a run (e.g. duplicate
invoices in books). Collisions are resolved by appending a stable
occurrence index — never by failing.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

# Mirrors gst_matcher._normalize_invoice_number without importing the
# matching module, so fingerprinting has no dependency on matching internals.
def _normalize_invoice_number(raw: Any) -> str:
    s = str(raw).strip().upper()
    s = re.sub(r"[\s/\\-]", "", s)
    s = re.sub(r"(?<!\d)0+(\d)", r"\1", s)
    return s


def _parse_record(record_json: Optional[str]) -> dict[str, Any]:
    if not record_json:
        return {}
    try:
        return json.loads(record_json)
    except (TypeError, ValueError):
        return {}


def _infer_gst_doc_type(record: dict[str, Any]) -> str:
    """Sign-based document-type inference — same rule the matcher uses to
    derive type from books records, kept stable regardless of config."""
    try:
        inv_val = float(record.get("invoice_value", 0) or 0)
    except (TypeError, ValueError):
        inv_val = 0.0
    return "Credit Note" if inv_val < 0 else "Invoice"


def _hash(parts: list[str]) -> str:
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def compute_base_fingerprint(
    result: dict[str, Any], recon_type: str, period: str
) -> str:
    """Compute the identity fingerprint for one result, before collision
    resolution. Depends only on source-record identity."""
    books = _parse_record(result.get("books_record"))
    portal = _parse_record(result.get("portal_record"))
    record = books or portal  # prefer books identity when both present
    side = "books" if books else "portal"

    if recon_type == "GST":
        gstin = str(record.get("gstin", "")).strip().upper()
        norm_inv = _normalize_invoice_number(record.get("invoice_number", ""))
        doc_type = _infer_gst_doc_type(record)
        return _hash(["GST", side, gstin, norm_inv, doc_type])

    if recon_type == "TDS":
        pan = str(record.get("pan", "")).strip().upper()
        section = str(record.get("section", "")).strip()
        amount = record.get("tax_deducted", record.get("amount_paid_credited", 0))
        try:
            amount_str = f"{float(amount or 0):.2f}"
        except (TypeError, ValueError):
            amount_str = "0.00"
        return _hash(["TDS", side, pan, section, period, amount_str])

    if recon_type == "OTHER":
        # 2C — the six "Other" sources share a generic reference/date/amount
        # identity. The source_key (bank / vendor_ledger / form26as /
        # opening_balances / loan_sheet / salary) is part of the identity so
        # the same reference in two different 2C sources never collides.
        source_key = str(record.get("source_type", "")).strip().lower()
        reference = str(record.get("reference", record.get("invoice_number", ""))).strip().upper()
        amount = record.get("amount", record.get("invoice_value", 0))
        try:
            amount_str = f"{float(amount or 0):.2f}"
        except (TypeError, ValueError):
            amount_str = "0.00"
        return _hash(["OTHER", side, source_key, reference, period, amount_str])

    raise ValueError(f"Unknown recon_type for fingerprinting: {recon_type!r}")


def assign_fingerprints(
    results: list[dict[str, Any]], recon_type: str, period: str
) -> None:
    """Mutate each result dict in place, adding a 'fingerprint' key.

    Collisions (same base fingerprint appearing more than once within this
    run — e.g. duplicate invoices in books) are resolved deterministically
    by appending a stable occurrence index in encounter order.
    """
    seen_counts: dict[str, int] = {}
    for r in results:
        base = compute_base_fingerprint(r, recon_type, period)
        occurrence = seen_counts.get(base, 0)
        seen_counts[base] = occurrence + 1
        r["fingerprint"] = base if occurrence == 0 else f"{base}#{occurrence + 1}"
