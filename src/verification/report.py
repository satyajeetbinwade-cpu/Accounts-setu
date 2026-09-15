"""Report generation — Excel workbook + plain-text stdout digest.

Written so an accountant who did not build the system can read it and
reach their own conclusion. Full match_reason text, readable widths,
wrapped text, frozen headers.

Alignment coverage appears before any accuracy metric. If coverage is below
the floor, the summary opens with a warning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src import queries
from src.verification.baseline import load_baseline
from src.verification.alignment import align
from src.verification.metrics import compute_metrics
from src.verification.analysis import (
    build_disagreements,
    cause_summary,
    export_for_adjudication,
    import_adjudicated,
    compute_adjudicated_metrics,
)
from src.verification.criteria import evaluate_criteria, load_criteria

_WRAP_COLUMNS = {"match_reason", "engine_match_reason"}
_MATCH_REASON_WIDTH = 90
_DEFAULT_COL_WIDTH = 22
_HEADER_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
_WARNING_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")


def generate_report(
    run_id: int,
    baseline_path: str | Path,
    output_path: str | Path,
    *,
    adjudicated_path: str | Path | None = None,
    mapping_path: str | Path | None = None,
    criteria_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Generate the full verification report.

    Returns the path of the written Excel workbook.
    """
    # --- Load run and engine results ---
    run = queries.get_run(run_id, db_path=db_path)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    recon_type = run["recon_type"]
    engine_df = queries.get_results(run_id, db_path=db_path)

    # --- Load baseline ---
    baseline_df, baseline_summary = load_baseline(
        baseline_path, recon_type, mapping_path=mapping_path
    )

    # --- Align ---
    alignment = align(engine_df, baseline_df, recon_type, criteria_path=criteria_path)
    aligned_df = alignment["aligned"]
    coverage = alignment["coverage"]

    # --- Metrics ---
    metrics = compute_metrics(aligned_df, recon_type)

    # --- Disagreement analysis ---
    disagreements = build_disagreements(aligned_df, recon_type)
    causes = cause_summary(disagreements)

    # --- Adjudication (optional) ---
    adjudicated_metrics = None
    if adjudicated_path:
        adj_df = import_adjudicated(adjudicated_path)
        adjudicated_metrics = compute_adjudicated_metrics(disagreements, adj_df)

    # --- Exit criteria ---
    criteria_results = evaluate_criteria(
        recon_type, metrics, coverage, adjudicated_metrics,
        criteria_path=criteria_path,
    )

    # Load the alignment config for coverage floor check
    all_criteria = load_criteria(criteria_path)
    alignment_cfg = all_criteria.get("alignment", {})
    coverage_floor = float(alignment_cfg.get("coverage_warning_floor", 0.80))
    coverage_below_floor = coverage["baseline_coverage"] < coverage_floor

    # --- Write report ---
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        _write_summary_sheet(
            writer, run, baseline_summary, coverage, metrics,
            criteria_results, adjudicated_metrics,
            coverage_below_floor, causes,
        )
        _write_confusion_matrix(writer, metrics)
        _write_false_positives(writer, metrics)
        _write_false_negatives(writer, metrics)
        _write_disagreements(writer, disagreements, causes)
        _write_unaligned(writer, alignment)
        _write_calibration(writer, metrics)
        _write_diff_type_accuracy(writer, metrics)

        if adjudicated_metrics:
            _write_adjudication_sheet(writer, adjudicated_metrics, metrics)

    # --- Print digest to stdout ---
    _print_digest(run_id, recon_type, coverage, metrics, causes,
                  adjudicated_metrics, coverage_below_floor)

    return output_path


# ---------------------------------------------------------------------------
# Sheet writers
# ---------------------------------------------------------------------------

def _fmt_sheet(writer, sheet_name: str, df: pd.DataFrame) -> None:
    """Write a DataFrame to a sheet with standard formatting."""
    if df.empty:
        df = pd.DataFrame({"(no data)": ["No records"]})
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    ws.freeze_panes = "A2"
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    for idx, col_name in enumerate(df.columns, start=1):
        letter = get_column_letter(idx)
        if col_name in _WRAP_COLUMNS:
            ws.column_dimensions[letter].width = _MATCH_REASON_WIDTH
            for row in range(2, min(len(df) + 2, 5000)):
                ws.cell(row=row, column=idx).alignment = Alignment(
                    wrap_text=True, vertical="top"
                )
        else:
            max_len = max(
                [len(str(col_name))]
                + [len(str(v)) for v in df[col_name].astype(str).head(200)]
            )
            ws.column_dimensions[letter].width = min(max(max_len + 2, 10), _DEFAULT_COL_WIDTH)


def _write_summary_sheet(
    writer, run, baseline_summary, coverage, metrics,
    criteria_results, adjudicated_metrics,
    coverage_below_floor, causes,
):
    ws = writer.book.create_sheet("Summary", 0)
    r = 1

    # --- Warnings first ---
    if coverage_below_floor:
        ws.cell(row=r, column=1, value="⚠ WARNING: Alignment coverage is below floor").font = Font(
            bold=True, color="FF0000", size=12
        )
        ws.cell(row=r, column=1).fill = _WARNING_FILL
        r += 1
        ws.cell(row=r, column=1, value=(
            f"Baseline coverage: {coverage['baseline_coverage']:.1%}. "
            "Accuracy figures below apply only to the aligned subset and are "
            "NOT yet meaningful for the full population."
        ))
        r += 2

    unadj_count = 0
    if adjudicated_metrics:
        unadj_count = adjudicated_metrics.get("unadjudicated_count", 0)
        total_disagree = len(metrics.get("false_positives", [])) + len(metrics.get("false_negatives", []))
        if total_disagree > 0 and unadj_count / max(total_disagree, 1) > 0.5:
            ws.cell(row=r, column=1, value=(
                f"⚠ WARNING: {unadj_count} disagreements remain unadjudicated "
                f"({unadj_count}/{total_disagree}). Adjudicated metrics are partial."
            )).font = Font(bold=True, color="FF0000")
            r += 2

    # --- Run metadata ---
    ws.cell(row=r, column=1, value="Verification Report").font = Font(bold=True, size=14)
    r += 2

    meta = [
        ("Run ID", run["run_id"]),
        ("Client", run["client"]),
        ("Period", run["period"]),
        ("Recon Type", run["recon_type"]),
        ("Run Timestamp", run["run_timestamp"]),
        ("Source Files", ", ".join(run.get("source_file_names", []))),
        ("Baseline File", baseline_summary.get("baseline_path", "")),
        ("Baseline Exhaustive", str(baseline_summary.get("baseline_is_exhaustive", False))),
        ("Baseline Rows (kept)", baseline_summary.get("kept_row_count", 0)),
        ("Report Generated", datetime.now(timezone.utc).isoformat()),
    ]
    for label, value in meta:
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.cell(row=r, column=2, value=str(value))
        r += 1

    # --- Alignment coverage ---
    r += 1
    ws.cell(row=r, column=1, value="Alignment Coverage").font = Font(bold=True, size=12)
    r += 1
    cov_rows = [
        ("Total baseline rows", coverage["total_baseline_rows"]),
        ("Total engine results", coverage["total_engine_results"]),
        ("Aligned baseline rows", coverage["aligned_baseline_rows"]),
        ("Aligned engine results", coverage["aligned_engine_results"]),
        ("Baseline coverage", f"{coverage['baseline_coverage']:.1%}"),
        ("Engine coverage", f"{coverage['engine_coverage']:.1%}"),
        ("Baseline unaligned", coverage["baseline_unaligned_count"]),
        ("Engine unaligned", coverage["engine_unaligned_count"]),
    ]
    for tier, count in coverage.get("by_tier", {}).items():
        cov_rows.append((f"  Tier {tier} alignments", count))
    for label, value in cov_rows:
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.cell(row=r, column=2, value=str(value))
        r += 1

    ws.cell(row=r, column=1, value=(
        "All metrics below apply ONLY to the aligned subset."
    )).font = Font(italic=True)
    r += 2

    # --- Headline: false-positive rate ---
    ws.cell(row=r, column=1, value="HEADLINE: False-Positive Rate").font = Font(bold=True, size=12, color="CC0000")
    r += 1
    ws.cell(row=r, column=1, value="False-positive count").font = Font(bold=True)
    ws.cell(row=r, column=2, value=metrics["false_positive_count"])
    r += 1
    ws.cell(row=r, column=1, value="False-positive rate (by count)").font = Font(bold=True)
    ws.cell(row=r, column=2, value=f"{metrics['false_positive_rate']:.2%}")
    r += 1
    ws.cell(row=r, column=1, value="False-positive value (₹)").font = Font(bold=True)
    ws.cell(row=r, column=2, value=f"₹{metrics['false_positive_value']:,.2f}")
    r += 1
    ws.cell(row=r, column=1, value="False-positive rate (by value)").font = Font(bold=True)
    ws.cell(row=r, column=2, value=f"{metrics['false_positive_value_rate']:.2%}")
    r += 2

    # --- False-negative rate ---
    ws.cell(row=r, column=1, value="False-Negative Rate").font = Font(bold=True, size=12)
    r += 1
    ws.cell(row=r, column=1, value="False-negative count").font = Font(bold=True)
    ws.cell(row=r, column=2, value=metrics["false_negative_count"])
    r += 1
    ws.cell(row=r, column=1, value="False-negative rate").font = Font(bold=True)
    ws.cell(row=r, column=2, value=f"{metrics['false_negative_rate']:.2%}")
    r += 1
    ws.cell(row=r, column=1, value="False-negative value (₹)").font = Font(bold=True)
    ws.cell(row=r, column=2, value=f"₹{metrics['false_negative_value']:,.2f}")
    r += 2

    # --- Adjudicated (if available) ---
    if adjudicated_metrics:
        ws.cell(row=r, column=1, value="Adjudicated Metrics").font = Font(bold=True, size=12)
        r += 1
        for label, value in [
            ("Adjudicated disagreements", adjudicated_metrics["adjudicated_count"]),
            ("Unadjudicated", adjudicated_metrics["unadjudicated_count"]),
            ("FP after adjudication", adjudicated_metrics["fp_after_adjudication"]),
            ("FN after adjudication", adjudicated_metrics["fn_after_adjudication"]),
            ("Both defensible", adjudicated_metrics["both_defensible_count"]),
        ]:
            ws.cell(row=r, column=1, value=label).font = Font(bold=True)
            ws.cell(row=r, column=2, value=str(value))
            r += 1
        r += 1

    # --- Disagreement causes ---
    if causes:
        ws.cell(row=r, column=1, value="Disagreement Causes").font = Font(bold=True, size=12)
        r += 1
        for cause, stats in causes.items():
            ws.cell(row=r, column=1, value=cause)
            ws.cell(row=r, column=2, value=f"count={stats['count']}")
            ws.cell(row=r, column=3, value=f"₹{stats['total_engine_value']:,.2f}")
            r += 1
        r += 1

    # --- Exit criteria ---
    ws.cell(row=r, column=1, value="Exit Criteria").font = Font(bold=True, size=12)
    r += 1
    ws.cell(row=r, column=1, value="Criterion").font = Font(bold=True)
    ws.cell(row=r, column=2, value="Threshold").font = Font(bold=True)
    ws.cell(row=r, column=3, value="Actual").font = Font(bold=True)
    ws.cell(row=r, column=4, value="Status").font = Font(bold=True)
    r += 1
    for cr in criteria_results:
        ws.cell(row=r, column=1, value=cr["name"])
        ws.cell(row=r, column=2, value=str(cr["threshold"]))
        ws.cell(row=r, column=3, value=str(cr["actual"]))
        status_cell = ws.cell(row=r, column=4, value=cr["status"])
        if cr["status"] == "FAIL":
            status_cell.font = Font(bold=True, color="FF0000")
        elif cr["status"] == "NOT YET ASSESSABLE":
            status_cell.font = Font(italic=True, color="808080")
        else:
            status_cell.font = Font(bold=True, color="008000")
        r += 1

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 25
    ws.column_dimensions["D"].width = 25


def _write_confusion_matrix(writer, metrics):
    cm = metrics.get("confusion_matrix_counts")
    if cm is not None and not cm.empty:
        cm.to_excel(writer, sheet_name="Confusion Matrix (Counts)")
        ws = writer.sheets["Confusion Matrix (Counts)"]
        ws.freeze_panes = "B2"
    cm_val = metrics.get("confusion_matrix_values")
    if cm_val is not None and not cm_val.empty:
        cm_val.to_excel(writer, sheet_name="Confusion Matrix (Values)")
        ws2 = writer.sheets["Confusion Matrix (Values)"]
        ws2.freeze_panes = "B2"


def _write_false_positives(writer, metrics):
    fp = metrics.get("false_positives", pd.DataFrame())
    display_cols = [c for c in fp.columns if not c.startswith("_")]
    df = fp[display_cols] if not fp.empty else pd.DataFrame({"(no false positives)": []})
    if not df.empty:
        df = df.sort_values("_value" if "_value" in fp.columns else df.columns[0], ascending=False) if "_value" in fp.columns else df
    _fmt_sheet(writer, "False Positives", df.head(5000))


def _write_false_negatives(writer, metrics):
    fn = metrics.get("false_negatives", pd.DataFrame())
    display_cols = [c for c in fn.columns if not c.startswith("_")]
    df = fn[display_cols] if not fn.empty else pd.DataFrame({"(no false negatives)": []})
    _fmt_sheet(writer, "False Negatives", df.head(5000))


def _write_disagreements(writer, disagreements, causes):
    if disagreements.empty:
        _fmt_sheet(writer, "All Disagreements", pd.DataFrame({"(no disagreements)": []}))
        return
    _fmt_sheet(writer, "All Disagreements", disagreements)


def _write_unaligned(writer, alignment):
    bu = alignment.get("baseline_unaligned", pd.DataFrame())
    eu = alignment.get("engine_unaligned", pd.DataFrame())
    _fmt_sheet(writer, "Unaligned Baseline", bu if not bu.empty else pd.DataFrame({"(all baseline rows aligned)": []}))
    _fmt_sheet(writer, "Unaligned Engine", eu if not eu.empty else pd.DataFrame({"(all engine results aligned)": []}))


def _write_calibration(writer, metrics):
    calib = metrics.get("calibration", {})
    rows = []
    for band, data in calib.get("by_band", {}).items():
        rows.append({"type": "band", "bucket": band, **data})
    for decile, data in calib.get("by_decile", {}).items():
        rows.append({"type": "decile", "bucket": decile, **data})
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    if not df.empty:
        _fmt_sheet(writer, "Calibration", df)
        # Add assessment text
        ws = writer.sheets["Calibration"]
        assessment = calib.get("assessment", "")
        r = len(df) + 3
        ws.cell(row=r, column=1, value="Assessment").font = Font(bold=True)
        ws.cell(row=r + 1, column=1, value=assessment)


def _write_diff_type_accuracy(writer, metrics):
    dta = metrics.get("difference_type_accuracy", {})
    if not dta.get("available"):
        return
    rows = []
    for dt, data in dta.get("by_type", {}).items():
        rows.append({"difference_type": dt, **data})
    if rows:
        _fmt_sheet(writer, "Difference Type Accuracy", pd.DataFrame(rows))


def _write_adjudication_sheet(writer, adjudicated_metrics, raw_metrics):
    rows = [
        {"Metric": "Raw FP count", "Value": raw_metrics["false_positive_count"]},
        {"Metric": "FP after adjudication", "Value": adjudicated_metrics["fp_after_adjudication"]},
        {"Metric": "Raw FN count", "Value": raw_metrics["false_negative_count"]},
        {"Metric": "FN after adjudication", "Value": adjudicated_metrics["fn_after_adjudication"]},
        {"Metric": "Both defensible", "Value": adjudicated_metrics["both_defensible_count"]},
        {"Metric": "Adjudicated", "Value": adjudicated_metrics["adjudicated_count"]},
        {"Metric": "Unadjudicated", "Value": adjudicated_metrics["unadjudicated_count"]},
    ]
    _fmt_sheet(writer, "Adjudication", pd.DataFrame(rows))


# ---------------------------------------------------------------------------
# Plain-text digest
# ---------------------------------------------------------------------------

def _print_digest(
    run_id, recon_type, coverage, metrics, causes,
    adjudicated_metrics, coverage_below_floor,
):
    print("\n" + "=" * 70)
    print(f"VERIFICATION DIGEST — Run {run_id} ({recon_type})")
    print("=" * 70)

    if coverage_below_floor:
        print("⚠ ALIGNMENT COVERAGE BELOW FLOOR — metrics are not yet meaningful")

    print(f"\nAlignment coverage:")
    print(f"  Baseline: {coverage['aligned_baseline_rows']}/{coverage['total_baseline_rows']} "
          f"({coverage['baseline_coverage']:.1%})")
    print(f"  Engine:   {coverage['aligned_engine_results']}/{coverage['total_engine_results']} "
          f"({coverage['engine_coverage']:.1%})")
    for tier, count in coverage.get("by_tier", {}).items():
        print(f"    Tier {tier}: {count}")

    print(f"\nFalse-positive count: {metrics['false_positive_count']} "
          f"(rate: {metrics['false_positive_rate']:.2%})")
    print(f"False-positive value: ₹{metrics['false_positive_value']:,.2f} "
          f"(rate: {metrics['false_positive_value_rate']:.2%})")
    print(f"False-negative count: {metrics['false_negative_count']} "
          f"(rate: {metrics['false_negative_rate']:.2%})")
    print(f"False-negative value: ₹{metrics['false_negative_value']:,.2f}")

    if adjudicated_metrics:
        print(f"\nAdjudicated: FP→{adjudicated_metrics['fp_after_adjudication']}, "
              f"FN→{adjudicated_metrics['fn_after_adjudication']}, "
              f"unadjudicated={adjudicated_metrics['unadjudicated_count']}")

    # 3 largest disagreements by value
    print("\nLargest disagreements by value:")
    # (we don't have the disagreements DF here directly, so summarise from causes)
    if causes:
        sorted_causes = sorted(causes.items(), key=lambda x: x[1]["total_engine_value"], reverse=True)
        for cause, stats in sorted_causes[:3]:
            print(f"  {cause}: {stats['count']} items, ₹{stats['total_engine_value']:,.2f}")
    else:
        print("  (no disagreements)")

    print("=" * 70)


# ---------------------------------------------------------------------------
# Multi-period
# ---------------------------------------------------------------------------

def generate_multi_period_report(
    run_ids: list[int],
    baseline_paths: list[str | Path],
    output_path: str | Path,
    *,
    adjudicated_paths: list[str | Path | None] | None = None,
    mapping_path: str | Path | None = None,
    criteria_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Run verification across several periods and write a combined report.

    Each run_id corresponds to a baseline_path at the same index.
    """
    if len(run_ids) != len(baseline_paths):
        raise ValueError("run_ids and baseline_paths must have the same length")

    adj_paths = adjudicated_paths or [None] * len(run_ids)
    all_period_results: list[dict[str, Any]] = []

    for i, (rid, bpath) in enumerate(zip(run_ids, baseline_paths)):
        run = queries.get_run(rid, db_path=db_path)
        if run is None:
            raise ValueError(f"Run {rid} not found")
        recon_type = run["recon_type"]
        engine_df = queries.get_results(rid, db_path=db_path)
        baseline_df, baseline_summary = load_baseline(bpath, recon_type, mapping_path=mapping_path)
        alignment_result = align(engine_df, baseline_df, recon_type, criteria_path=criteria_path)
        metrics = compute_metrics(alignment_result["aligned"], recon_type)

        all_period_results.append({
            "run_id": rid,
            "period": run["period"],
            "recon_type": recon_type,
            "coverage": alignment_result["coverage"],
            "metrics": metrics,
        })

    # Write a summary comparing periods
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for pr in all_period_results:
        rows.append({
            "run_id": pr["run_id"],
            "period": pr["period"],
            "recon_type": pr["recon_type"],
            "baseline_coverage": f"{pr['coverage']['baseline_coverage']:.1%}",
            "fp_count": pr["metrics"]["false_positive_count"],
            "fp_rate": f"{pr['metrics']['false_positive_rate']:.2%}",
            "fp_value": f"₹{pr['metrics']['false_positive_value']:,.2f}",
            "fn_count": pr["metrics"]["false_negative_count"],
            "fn_rate": f"{pr['metrics']['false_negative_rate']:.2%}",
            "agreement_rate": f"{pr['metrics']['overall_agreement_rate']:.2%}",
        })
    summary_df = pd.DataFrame(rows)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Multi-Period Summary", index=False)
        ws = writer.sheets["Multi-Period Summary"]
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True)

    print(f"\n[report] Multi-period summary written to {output_path}")
    print(summary_df.to_string(index=False))

    return output_path
