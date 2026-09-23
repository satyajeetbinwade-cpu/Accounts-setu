"""F6 → ingestion_ai bridge — the deterministic, zero-AI parse path for
GSTN portal exports (GSTR-2B, IMS).

WHY THIS EXISTS
---------------
`normalize_source_file()` maps a file by asking a model to match its columns
onto the canonical schema. That works for arbitrary client exports, but a
GSTN portal export (GSTR-2B, IMS) has a KNOWN, fixed layout — a two-row
merged header with documented sheet names and column labels. F6 already
models those layouts precisely (`src/f6/seed_formats.py`) and parses them
deterministically (`src/f6/parser.py`), with zero model calls.

This bridge lets the reconciliation path use that precise parser for the
source types it covers, instead of the generic model mapper. It is the
"known format → deterministic parse" fast path (F6's Path A) applied to the
reconcile flow.

It deliberately does NOT go through `f6.service.ingest_file()`: that entry
point fingerprints the file and requires a REGISTERED format version to
reach Path A, so a real file with no registered fingerprint would fall to
Path B (an AI proposal + human confirmation). Here we already KNOW the
source type from the upload slot, so we call the parser directly with the
matching config — no fingerprint gate, no AI, no confirmation round-trip.

Falls back cleanly: if the parser yields no rows (an unexpected layout, a
summary-only file), the caller continues with the generic model path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.ingestion import GST_CANONICAL_FIELDS

# Source types this bridge covers, mapped to the F6 config factory.
_BRIDGE_CONFIGS = {
    "gstr2b": ("src.f6.seed_formats", "gstr2b_config"),
    "ims": ("src.f6.seed_formats", "ims_config"),
}

# F6 emits a few fields that aren't part of the canonical GST schema but ARE
# consumed downstream (Module 2's eligible-credit reads itc_eligibility;
# reverse-charge is tracked separately). Keep them on the frame.
_EXTRA_FIELDS = ["itc_eligibility", "reverse_charge", "gstr1_period", "ims_action_state"]

_STRUCTURAL = {"source_type", "source_file", "original_row", "total_tax"}


def supports(source_type: str) -> bool:
    """Whether this bridge has a deterministic parser for the source type."""
    return source_type in _BRIDGE_CONFIGS


def _load_config(source_type: str) -> Optional[dict[str, Any]]:
    module_name, factory = _BRIDGE_CONFIGS[source_type]
    try:
        import importlib

        mod = importlib.import_module(module_name)
        return getattr(mod, factory)()
    except Exception:  # noqa: BLE001
        return None


def parse_portal_export(
    path: Path, source_type: str, filename: str,
) -> Optional[tuple[pd.DataFrame, list[dict[str, Any]], list[str]]]:
    """Parse a GSTN portal export deterministically.

    Returns (canonical_df, field_mappings, warnings) or None when the bridge
    doesn't cover this source type or the parser produced no rows (so the
    caller falls back to the generic model path).

    `field_mappings` is a list of {canonical_field, source_column, confidence,
    reason, required} dicts describing what the deterministic parser mapped —
    the same shape the review UI renders, so a deterministic parse is shown
    to the user exactly like a model mapping (with a rule-sourced reason).
    """
    if not supports(source_type):
        return None

    config = _load_config(source_type)
    if config is None:
        return None

    try:
        from src.f6.parser import parse_file

        is_excel = path.suffix.lower() in (".xlsx", ".xls")
        parsed = parse_file(str(path), config, is_excel=is_excel)
    except Exception:  # noqa: BLE001
        return None

    rows = parsed.all_rows
    if not rows:
        return None

    warnings: list[str] = []
    if parsed.recognised_not_parsed:
        warnings.append(
            "Recognised but not parsed (summary/deferred sheets): "
            + ", ".join(sorted(set(parsed.recognised_not_parsed)))
            + "."
        )
    for sr in parsed.sheet_results:
        if sr.role == "line_items" and sr.row_count_excluded:
            warnings.append(
                f"Sheet {sr.sheet_name!r}: {sr.row_count_excluded} row(s) excluded "
                "(blank/total rows)."
            )

    canonical_fields = [f for f in GST_CANONICAL_FIELDS if f not in _STRUCTURAL]
    frame_fields = canonical_fields + [f for f in _EXTRA_FIELDS if f not in canonical_fields]

    records: list[dict[str, Any]] = []
    for row in rows:
        rec: dict[str, Any] = {}
        for f in frame_fields:
            rec[f] = row.get(f)
        rec["source_type"] = source_type
        rec["source_file"] = filename
        rec["original_row"] = json.dumps(
            {str(k): (None if v is None else str(v)) for k, v in row.items()}
        )
        records.append(rec)

    df = pd.DataFrame(records)

    # Coerce numerics/dates the same way the generic path does, so the
    # engine sees an identical frame shape regardless of which path ran.
    from src.ingestion import (
        GST_DATE_FIELDS,
        GST_NUMERIC_FIELDS,
        GST_UPPER_FIELDS,
        _coerce_numeric,
        _normalize_dates_flexible,
    )

    for f in GST_UPPER_FIELDS:
        if f in df.columns:
            df[f] = df[f].astype(str).str.upper().str.strip().replace({"NAN": "", "NONE": ""})
    for f in GST_DATE_FIELDS:
        if f in df.columns and df[f].notna().any():
            df[f] = _normalize_dates_flexible(df[f], field=f, context=f"{source_type}/{filename}")
    for f in GST_NUMERIC_FIELDS:
        if f in df.columns:
            coerced, _affected = _coerce_numeric(df[f])
            df[f] = coerced
    if "rounding_adjustment" in df.columns:
        df["rounding_adjustment"] = df["rounding_adjustment"].fillna(0.0)
    df["total_tax"] = df["cgst"] + df["sgst"] + df["igst"] + df["cess"]

    # Drop rows with no usable identity (mirrors apply_mapping's rule).
    identity_cols = [c for c in ("gstin", "invoice_number") if c in df.columns]
    if identity_cols:
        has_identity = df[identity_cols].apply(
            lambda r: any(str(v).strip() not in ("", "nan", "None", "0", "0.0") for v in r), axis=1
        )
        dropped = int((~has_identity).sum())
        if dropped:
            df = df.loc[has_identity]
            warnings.append(f"{dropped} row(s) dropped — no usable GSTIN/invoice number.")

    if df.empty:
        return None

    mappings = _mappings_from_config(config, source_type)
    return df.reset_index(drop=True), mappings, warnings


def _mappings_from_config(config: dict[str, Any], source_type: str) -> list[dict[str, Any]]:
    """Describe the deterministic mapping for the review UI.

    Every canonical field the config's line_items sheets map gets a
    rule-sourced entry (confidence None = rule, not AI — the UI renders it
    as a solid rule badge, never an AI percentage). Fields the config
    doesn't cover are listed as unmapped so nothing is silently absent.
    """
    from src.ingestion_ai.normalizer import required_fields_for

    required = set(required_fields_for(source_type))
    mapped: dict[str, str] = {}
    for sheet_cfg in config.get("sheets", {}).values():
        if sheet_cfg.get("role") != "line_items":
            continue
        for rule in sheet_cfg.get("field_rules", []):
            field = rule.get("canonical_field")
            cols = rule.get("source_columns") or []
            if field and cols and field not in mapped:
                mapped[field] = cols[0]

    out: list[dict[str, Any]] = []
    for f in [x for x in GST_CANONICAL_FIELDS if x not in _STRUCTURAL]:
        # rounding_adjustment is a residual that defaults to 0 when absent —
        # a portal export never carries it, so listing it as "unmapped" would
        # wrongly read as a gap needing review.
        if f == "rounding_adjustment":
            continue
        col = mapped.get(f)
        out.append({
            "canonical_field": f,
            "source_column": col,
            "confidence": None,
            "reason": (
                f"Deterministic GSTR-2B/IMS layout — mapped from {col!r}."
                if col else "Not present in this portal export's layout."
            ),
            "required": f in required,
        })
    return out
