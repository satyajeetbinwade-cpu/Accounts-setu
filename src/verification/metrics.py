"""Metrics — computed over the aligned subset only.

False positives and false negatives are reported separately, never combined
into a single accuracy number as the headline figure. Every false positive
is enumerated individually because each is a case to be understood.

Confidence calibration is reported by band and by decile. The question is
whether High genuinely means more reliable than Low.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _value_of_engine(row: pd.Series) -> float:
    """Extract rupee value from the engine side of an aligned pair."""
    for col_prefix in ("engine_books_record", "engine_portal_record"):
        rec = row.get(col_prefix)
        if isinstance(rec, dict):
            for key in ("invoice_value", "amount_paid_credited", "tax_deducted"):
                if key in rec and rec[key] is not None:
                    try:
                        return abs(float(rec[key]))
                    except (TypeError, ValueError):
                        continue
    return 0.0


def _value_of_baseline(row: pd.Series) -> float:
    """Extract value from baseline side."""
    for col in ("baseline_invoice_value", "baseline_taxable_value",
                "baseline_amount_paid_credited", "baseline_tax_deducted"):
        v = row.get(col)
        if v is not None:
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                continue
    return 0.0


def compute_metrics(aligned_df: pd.DataFrame, recon_type: str) -> dict[str, Any]:
    """Compute all verification metrics over the aligned subset.

    Returns a dict with confusion_matrix, false_positives, false_negatives,
    per_classification, calibration, difference_type_accuracy, etc.
    """
    if aligned_df.empty:
        return _empty_metrics()

    df = aligned_df.copy()
    df["_engine_cls"] = df["engine_classification"]
    df["_baseline_cls"] = df["baseline_baseline_verdict"]
    df["_value"] = df.apply(
        lambda r: _value_of_engine(r) or _value_of_baseline(r), axis=1
    )
    df["_engine_band"] = df.get("engine_confidence_band", pd.Series("Low", index=df.index))
    df["_engine_score"] = df.get("engine_confidence_score", pd.Series(0.0, index=df.index)).apply(_safe_float)
    df["_agree"] = df["_engine_cls"] == df["_baseline_cls"]

    result: dict[str, Any] = {}

    # --- Confusion matrix ---
    classifications = ["Matched", "Not in Books", "Not in Portal", "Amount Difference"]
    cm_counts = pd.crosstab(
        df["_baseline_cls"], df["_engine_cls"],
        margins=True, margins_name="Total"
    ).reindex(index=classifications + ["Total"], columns=classifications + ["Total"], fill_value=0)

    cm_values = pd.crosstab(
        df["_baseline_cls"], df["_engine_cls"],
        values=df["_value"], aggfunc="sum",
        margins=True, margins_name="Total"
    ).reindex(index=classifications + ["Total"], columns=classifications + ["Total"], fill_value=0.0).round(2)

    result["confusion_matrix_counts"] = cm_counts
    result["confusion_matrix_values"] = cm_values

    # --- False positives: engine said Matched, baseline said otherwise ---
    fp_mask = (df["_engine_cls"] == "Matched") & (df["_baseline_cls"] != "Matched")
    fp_df = df[fp_mask].copy()
    engine_matched_count = int((df["_engine_cls"] == "Matched").sum())
    engine_matched_value = float(df.loc[df["_engine_cls"] == "Matched", "_value"].sum())

    result["false_positives"] = fp_df
    result["false_positive_count"] = len(fp_df)
    result["false_positive_value"] = float(fp_df["_value"].sum())
    result["false_positive_rate"] = (
        len(fp_df) / engine_matched_count if engine_matched_count else 0.0
    )
    result["false_positive_value_rate"] = (
        float(fp_df["_value"].sum()) / engine_matched_value if engine_matched_value else 0.0
    )

    # --- False negatives: baseline said Matched, engine said otherwise ---
    fn_mask = (df["_baseline_cls"] == "Matched") & (df["_engine_cls"] != "Matched")
    fn_df = df[fn_mask].copy()
    baseline_matched_count = int((df["_baseline_cls"] == "Matched").sum())

    result["false_negatives"] = fn_df
    result["false_negative_count"] = len(fn_df)
    result["false_negative_value"] = float(fn_df["_value"].sum())
    result["false_negative_rate"] = (
        len(fn_df) / baseline_matched_count if baseline_matched_count else 0.0
    )

    # --- Per-classification precision/recall ---
    per_cls: dict[str, dict[str, Any]] = {}
    for cls in classifications:
        engine_has = df["_engine_cls"] == cls
        baseline_has = df["_baseline_cls"] == cls
        tp = int((engine_has & baseline_has).sum())
        fp_cls = int((engine_has & ~baseline_has).sum())
        fn_cls = int((~engine_has & baseline_has).sum())
        precision = tp / (tp + fp_cls) if (tp + fp_cls) else 0.0
        recall = tp / (tp + fn_cls) if (tp + fn_cls) else 0.0
        per_cls[cls] = {
            "true_positives": tp,
            "false_positives": fp_cls,
            "false_negatives": fn_cls,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
        }
    result["per_classification"] = per_cls

    # --- Difference type accuracy ---
    diff_type_acc = _compute_difference_type_accuracy(df)
    result["difference_type_accuracy"] = diff_type_acc

    # --- Confidence calibration ---
    calibration = _compute_calibration(df)
    result["calibration"] = calibration

    # --- Agreement rate ---
    result["overall_agreement_count"] = int(df["_agree"].sum())
    result["overall_agreement_rate"] = float(df["_agree"].mean()) if len(df) else 0.0
    result["total_aligned"] = len(df)

    return result


def _compute_difference_type_accuracy(df: pd.DataFrame) -> dict[str, Any]:
    """Where both sides identified a difference, did the engine characterise
    it the same way?"""
    engine_dt_col = "engine_difference_type"
    baseline_dt_col = "baseline_baseline_difference_type"

    if engine_dt_col not in df.columns or baseline_dt_col not in df.columns:
        return {"available": False}

    both_diff = df[
        (df["_engine_cls"] != "Matched")
        & (df["_baseline_cls"] != "Matched")
        & df[engine_dt_col].notna()
        & df[baseline_dt_col].notna()
        & (df[engine_dt_col].astype(str) != "None")
        & (df[baseline_dt_col].astype(str) != "None")
    ].copy()

    if both_diff.empty:
        return {"available": True, "total": 0, "by_type": {}}

    both_diff["_dt_match"] = both_diff[engine_dt_col].astype(str) == both_diff[baseline_dt_col].astype(str)
    total = len(both_diff)
    agree = int(both_diff["_dt_match"].sum())

    # Per important sub-type
    important_types = ["Tax Split Mismatch", "Short Deduction", "Timing Difference"]
    by_type: dict[str, dict] = {}
    for dt in important_types:
        subset = both_diff[
            (both_diff[engine_dt_col] == dt) | (both_diff[baseline_dt_col] == dt)
        ]
        if not subset.empty:
            by_type[dt] = {
                "count": len(subset),
                "agree": int(subset["_dt_match"].sum()),
                "rate": round(subset["_dt_match"].mean(), 4),
            }

    return {
        "available": True,
        "total": total,
        "agree": agree,
        "rate": round(agree / total, 4) if total else 0.0,
        "by_type": by_type,
    }


def _compute_calibration(df: pd.DataFrame) -> dict[str, Any]:
    """Actual agreement rate within each confidence band and score decile."""
    result: dict[str, Any] = {}

    # By band
    by_band: dict[str, dict] = {}
    for band in ("High", "Medium", "Low"):
        subset = df[df["_engine_band"] == band]
        if not subset.empty:
            by_band[band] = {
                "count": len(subset),
                "agree": int(subset["_agree"].sum()),
                "rate": round(float(subset["_agree"].mean()), 4),
            }
    result["by_band"] = by_band

    # By decile (0-10, 10-20, ..., 90-100)
    by_decile: dict[str, dict] = {}
    for lo in range(0, 100, 10):
        hi = lo + 10
        label = f"{lo}-{hi}"
        if hi == 100:
            subset = df[(df["_engine_score"] >= lo) & (df["_engine_score"] <= hi)]
        else:
            subset = df[(df["_engine_score"] >= lo) & (df["_engine_score"] < hi)]
        if not subset.empty:
            by_decile[label] = {
                "count": len(subset),
                "agree": int(subset["_agree"].sum()),
                "rate": round(float(subset["_agree"].mean()), 4),
            }
    result["by_decile"] = by_decile

    # Calibration assessment
    rates = [v["rate"] for v in by_band.values()]
    if len(rates) >= 2:
        high_rate = by_band.get("High", {}).get("rate", 0)
        low_rate = by_band.get("Low", {}).get("rate", 0)
        if high_rate > low_rate + 0.05:
            result["assessment"] = "Calibration holds: High band is more reliable than Low."
        elif abs(high_rate - low_rate) <= 0.05:
            result["assessment"] = (
                "Calibration is FLAT: High band is no more accurate than Low. "
                "Confidence scores are not informative."
            )
        else:
            result["assessment"] = (
                "Calibration is INVERTED: Low band is MORE accurate than High. "
                "Confidence scores are worse than useless — they will be trusted and are wrong."
            )
    else:
        result["assessment"] = "Insufficient data to assess calibration."

    return result


def _empty_metrics() -> dict[str, Any]:
    return {
        "confusion_matrix_counts": pd.DataFrame(),
        "confusion_matrix_values": pd.DataFrame(),
        "false_positives": pd.DataFrame(),
        "false_positive_count": 0,
        "false_positive_value": 0.0,
        "false_positive_rate": 0.0,
        "false_positive_value_rate": 0.0,
        "false_negatives": pd.DataFrame(),
        "false_negative_count": 0,
        "false_negative_value": 0.0,
        "false_negative_rate": 0.0,
        "per_classification": {},
        "difference_type_accuracy": {"available": False},
        "calibration": {"by_band": {}, "by_decile": {}, "assessment": "No aligned data."},
        "overall_agreement_count": 0,
        "overall_agreement_rate": 0.0,
        "total_aligned": 0,
    }
