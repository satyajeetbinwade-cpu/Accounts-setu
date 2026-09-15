"""Disagreement analysis and adjudication.

Each disagreement carries both verdicts, both values, engine confidence,
full match_reason, alignment tier, and a cause grouping.

Three possible causes for every disagreement: the engine is wrong, the
baseline is wrong, or they describe the same thing differently. The
adjudication mechanism lets the firm record which, then re-import those
decisions to produce adjudicated metrics alongside raw ones.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


# Cause categories — determinable from match_reason and record content
CAUSE_CATEGORIES = [
    "tolerance_boundary",   # amount difference near the tolerance edge
    "timing_difference",    # period/quarter mismatch
    "fuzzy_match",          # name similarity used to match
    "aggregation",          # group/aggregate vs individual
    "ambiguity",            # multiple candidates
    "normalization",        # invoice number / identifier formatting
    "ingestion",            # data quality or mapping
    "unknown",              # genuinely indeterminate — the most important signal
]

ADJUDICATION_VALUES = {"engine correct", "baseline correct", "both defensible", "unresolved"}


def build_disagreements(
    aligned_df: pd.DataFrame,
    recon_type: str,
) -> pd.DataFrame:
    """Build a per-disagreement record from the aligned subset.

    Returns a DataFrame with one row per disagreement, carrying both verdicts,
    values, confidence, match_reason, alignment tier, and cause group.
    """
    if aligned_df.empty:
        return pd.DataFrame()

    df = aligned_df.copy()
    # Filter to disagreements only
    disagree_mask = df["engine_classification"] != df["baseline_baseline_verdict"]
    ddf = df[disagree_mask].copy().reset_index(drop=True)

    if ddf.empty:
        return pd.DataFrame()

    # Build output columns
    records: list[dict[str, Any]] = []
    for _, row in ddf.iterrows():
        rec: dict[str, Any] = {
            "engine_verdict": row.get("engine_classification"),
            "baseline_verdict": row.get("baseline_baseline_verdict"),
            "engine_difference_type": row.get("engine_difference_type"),
            "baseline_difference_type": row.get("baseline_baseline_difference_type"),
            "engine_confidence_score": _safe_float(row.get("engine_confidence_score")),
            "engine_confidence_band": row.get("engine_confidence_band"),
            "match_reason": row.get("engine_match_reason"),
            "alignment_tier": row.get("alignment_tier"),
            "engine_fingerprint": row.get("engine_fingerprint"),
        }

        # Values from both sides
        rec["engine_value"] = _engine_value(row)
        rec["baseline_value"] = _baseline_value(row)

        # Identity fields
        if recon_type.upper() == "GST":
            rec["gstin"] = _safe_str(row.get("engine_books_record", {}).get("gstin") or
                                      row.get("baseline_gstin"))
            rec["invoice_number"] = _safe_str(row.get("engine_books_record", {}).get("invoice_number") or
                                               row.get("baseline_invoice_number"))
            rec["party_name"] = _safe_str(row.get("engine_books_record", {}).get("party_name") or
                                           row.get("baseline_party_name"))
        else:
            rec["pan"] = _safe_str(row.get("engine_books_record", {}).get("pan") or
                                    row.get("baseline_pan"))
            rec["deductee_name"] = _safe_str(row.get("engine_books_record", {}).get("deductee_name") or
                                              row.get("baseline_deductee_name"))
            rec["section"] = _safe_str(row.get("engine_books_record", {}).get("section") or
                                        row.get("baseline_section"))

        # Cause grouping
        rec["cause_group"] = _infer_cause(row, recon_type)

        # Adjudication placeholder
        rec["adjudication"] = ""

        records.append(rec)

    return pd.DataFrame(records)


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_str(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _engine_value(row: pd.Series) -> float:
    for side in ("engine_books_record", "engine_portal_record"):
        rec = row.get(side)
        if isinstance(rec, dict):
            for key in ("invoice_value", "amount_paid_credited", "tax_deducted"):
                if key in rec and rec[key] is not None:
                    try:
                        return abs(float(rec[key]))
                    except (TypeError, ValueError):
                        continue
    return 0.0


def _baseline_value(row: pd.Series) -> float:
    for col in ("baseline_invoice_value", "baseline_taxable_value",
                "baseline_amount_paid_credited", "baseline_tax_deducted"):
        v = row.get(col)
        if v is not None:
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                continue
    return 0.0


def _infer_cause(row: pd.Series, recon_type: str) -> str:
    """Infer a cause category from match_reason and record content.

    Genuinely indeterminate cases stay 'unknown' — a large unknown group
    is itself the most important signal.
    """
    reason = str(row.get("engine_match_reason") or "").lower()

    if "ambig" in reason:
        return "ambiguity"
    if "aggregat" in reason or "group" in reason:
        return "aggregation"
    if "timing" in reason or "cross-period" in reason or "cross-quarter" in reason or "adjacent" in reason:
        return "timing_difference"
    if "fuzzy" in reason or "name" in reason and "similar" in reason:
        return "fuzzy_match"
    if "rounding" in reason or "tolerance" in reason:
        return "tolerance_boundary"
    if "normali" in reason or "format" in reason:
        return "normalization"
    if "ingest" in reason or "coerced" in reason or "missing" in reason:
        return "ingestion"

    return "unknown"


def cause_summary(disagreements_df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Counts and total values by cause group."""
    if disagreements_df.empty:
        return {}
    summary: dict[str, dict[str, Any]] = {}
    for cause, group in disagreements_df.groupby("cause_group"):
        summary[str(cause)] = {
            "count": len(group),
            "total_engine_value": round(float(group["engine_value"].sum()), 2),
            "total_baseline_value": round(float(group["baseline_value"].sum()), 2),
        }
    return summary


# ---------------------------------------------------------------------------
# Adjudication import/export
# ---------------------------------------------------------------------------

def export_for_adjudication(
    disagreements_df: pd.DataFrame, output_path: str | Path
) -> Path:
    """Write disagreements to Excel with an adjudication column for firm review.

    The adjudication column is left blank — the firm fills in one of:
    'engine correct', 'baseline correct', 'both defensible', 'unresolved'.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    disagreements_df.to_excel(output_path, index=False, sheet_name="Disagreements")
    return output_path


def import_adjudicated(adjudicated_path: str | Path) -> pd.DataFrame:
    """Re-import an adjudicated disagreement file.

    Validates that the adjudication column contains only valid values.
    """
    path = Path(adjudicated_path)
    if not path.exists():
        raise FileNotFoundError(f"Adjudicated file not found: {path}")

    df = pd.read_excel(path, sheet_name="Disagreements", dtype=str)

    if "adjudication" not in df.columns:
        raise ValueError("Adjudicated file is missing 'adjudication' column")

    # Validate adjudication values
    df["adjudication"] = df["adjudication"].fillna("").str.strip().str.lower()
    non_blank = df[df["adjudication"] != ""]
    invalid = set(non_blank["adjudication"].unique()) - ADJUDICATION_VALUES - {""}
    if invalid:
        raise ValueError(
            f"Invalid adjudication values: {sorted(invalid)}. "
            f"Valid values: {sorted(ADJUDICATION_VALUES)}"
        )

    return df


def compute_adjudicated_metrics(
    disagreements_df: pd.DataFrame,
    adjudicated_df: pd.DataFrame,
) -> dict[str, Any]:
    """Compute adjudicated false-positive/negative counts.

    After adjudication, a disagreement where the firm says 'engine correct'
    is no longer a real false positive — the baseline was wrong.
    """
    if disagreements_df.empty or adjudicated_df.empty:
        return {
            "adjudicated_count": 0,
            "unadjudicated_count": len(disagreements_df),
            "fp_after_adjudication": 0,
            "fn_after_adjudication": 0,
            "both_defensible_count": 0,
        }

    # Merge adjudication decisions back
    merged = disagreements_df.copy()
    if "engine_fingerprint" in merged.columns and "engine_fingerprint" in adjudicated_df.columns:
        adj_map = dict(zip(
            adjudicated_df["engine_fingerprint"],
            adjudicated_df["adjudication"]
        ))
        merged["adjudication"] = merged["engine_fingerprint"].map(adj_map).fillna("")
    else:
        # Fall back to positional if no fingerprint
        merged["adjudication"] = adjudicated_df["adjudication"].values[:len(merged)]

    adjudicated_mask = merged["adjudication"] != ""
    adjudicated_count = int(adjudicated_mask.sum())
    unadjudicated_count = len(merged) - adjudicated_count

    # After adjudication: if firm says "engine correct", the FP is removed
    # If firm says "baseline correct", the FP stands
    fp_mask = (merged["engine_verdict"] == "Matched") & (merged["baseline_verdict"] != "Matched")
    fn_mask = (merged["baseline_verdict"] == "Matched") & (merged["engine_verdict"] != "Matched")

    fp_after = fp_mask & (merged["adjudication"] != "engine correct") & (merged["adjudication"] != "both defensible")
    fn_after = fn_mask & (merged["adjudication"] != "engine correct") & (merged["adjudication"] != "both defensible")

    return {
        "adjudicated_count": adjudicated_count,
        "unadjudicated_count": unadjudicated_count,
        "unadjudicated_share": unadjudicated_count / len(merged) if len(merged) else 0.0,
        "fp_after_adjudication": int(fp_after.sum()),
        "fn_after_adjudication": int(fn_after.sum()),
        "both_defensible_count": int((merged["adjudication"] == "both defensible").sum()),
    }
