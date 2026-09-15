"""Presentation-only helpers: labels, currency formatting, colour mapping.

Colour carries meaning in exactly three places in this app — classification,
confidence band, and review state — and is always paired with text via
st.badge labels, never colour alone.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pandas as pd

CLASSIFICATION_COLOR = {
    "Matched": "green",
    "Amount Difference": "orange",
    "Not in Books": "red",
    "Not in Portal": "red",
}

BAND_COLOR = {
    "High": "green",
    "Medium": "orange",
    "Low": "red",
}

# Review state colours: reviewed / unreviewed / stale must all read
# distinctly from each other.
REVIEW_STATE_COLOR = {
    "reviewed": "green",
    "unreviewed": "gray",
    "stale": "violet",
}

IDENTITY_FIELDS_GST = ["party_name", "gstin", "invoice_number", "invoice_date", "invoice_value"]
IDENTITY_FIELDS_TDS = ["deductee_name", "pan", "section", "amount_paid_credited", "tax_deducted"]


def money(value: Optional[float]) -> str:
    if value is None:
        return "\u20b90.00"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "\u20b90.00"
    return f"\u20b9{v:,.2f}"


def run_label(run_row: dict[str, Any], summary: Optional[dict[str, Any]] = None) -> str:
    """Label a run for the run selector: timestamp + headline counts."""
    ts = run_row.get("run_timestamp", "")
    ts_short = ts.replace("T", " ").split(".")[0].split("+")[0] if ts else "?"
    label = f"Run {run_row['run_id']} \u2014 {ts_short}"
    if summary is not None:
        total = summary.get("total_results", 0)
        by_cls = summary.get("by_classification", {})
        exceptions = total - by_cls.get("Matched", 0)
        label += f" \u2014 {total} results, {exceptions} exceptions"
    return label


def identity_fields_for(recon_type: str) -> list[str]:
    return IDENTITY_FIELDS_GST if recon_type == "GST" else IDENTITY_FIELDS_TDS


def record_identity_line(record: Any, recon_type: str) -> str:
    """A compact one-line identity summary for a books/portal record."""
    if not isinstance(record, dict):
        return "\u2014 (no record on this side)"
    fields = identity_fields_for(recon_type)
    parts = []
    for f in fields:
        v = record.get(f)
        if v is None or v == "":
            continue
        if f in ("invoice_value", "amount_paid_credited", "tax_deducted"):
            v = money(v)
        parts.append(str(v))
    return " \u00b7 ".join(parts) if parts else "\u2014"


def review_state_of(row: pd.Series) -> str:
    if bool(row.get("review_stale")):
        return "stale"
    if bool(row.get("reviewed")):
        return "reviewed"
    return "unreviewed"


def diff_fields(books: Any, portal: Any) -> set[str]:
    """Keys present in either record whose values differ, for visual
    distinction when showing both records side by side."""
    b = books if isinstance(books, dict) else {}
    p = portal if isinstance(portal, dict) else {}
    keys = set(b.keys()) | set(p.keys())
    keys.discard("original_row")
    differing = set()
    for k in keys:
        bv, pv = b.get(k), p.get(k)
        if isinstance(bv, (int, float)) and isinstance(pv, (int, float)):
            if abs(float(bv) - float(pv)) > 1e-6:
                differing.add(k)
        elif bv != pv:
            differing.add(k)
    return differing


def pretty_original_row(record: Any) -> str:
    if not isinstance(record, dict):
        return ""
    raw = record.get("original_row")
    if not raw:
        return ""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)
