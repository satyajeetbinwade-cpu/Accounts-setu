"""Baseline ingestion — reads the firm's manual reconciliation export.

Configuration-driven and correctable: every column name, verdict value
and skip rule comes from config/baseline_mapping.yaml, so the first
mismatch with the real file is a config edit, not a code change.

Any verdict value not in the configured verdict_map causes a loud named
failure — never a silent drop, never a default to Matched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

MAPPING_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "baseline_mapping.yaml"

CANONICAL_CLASSIFICATIONS = {"Matched", "Not in Books", "Not in Portal", "Amount Difference"}


class BaselineLoadError(ValueError):
    """Raised when the baseline cannot be loaded or mapped."""


def load_mapping(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else MAPPING_PATH
    if not p.exists():
        raise FileNotFoundError(f"Baseline mapping not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_baseline(
    baseline_path: str | Path,
    recon_type: str,
    *,
    mapping_path: str | Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load and validate a baseline file.

    Returns (baseline_df, load_summary).

    baseline_df columns: all configured identifier fields (canonical names),
    plus 'baseline_verdict' (one of the four canonical classifications),
    plus 'baseline_difference_type' (if configured), plus 'raw_verdict'
    (the original firm text).

    load_summary: row counts, skip counts by reason, unmapped verdict check.

    Raises BaselineLoadError on any structural problem.
    """
    mapping = load_mapping(mapping_path)
    recon_key = recon_type.lower()
    if recon_key not in mapping:
        raise BaselineLoadError(
            f"No baseline mapping for recon_type {recon_type!r}. "
            f"Available: {sorted(mapping.keys())}"
        )

    cfg = mapping[recon_key]
    baseline_path = Path(baseline_path)
    if not baseline_path.exists():
        raise FileNotFoundError(f"Baseline file not found: {baseline_path}")

    # --- Read Excel ---
    sheet = cfg.get("sheet_name", 0)
    header_row = int(cfg.get("header_row", 0))
    try:
        raw_df = pd.read_excel(baseline_path, sheet_name=sheet, header=header_row, dtype=str)
    except Exception as exc:
        raise BaselineLoadError(
            f"Failed to read baseline file '{baseline_path}', sheet '{sheet}', "
            f"header_row {header_row}: {exc}"
        ) from exc

    raw_count = len(raw_df)
    summary: dict[str, Any] = {
        "raw_row_count": raw_count,
        "baseline_path": str(baseline_path),
        "sheet_name": sheet,
        "skipped": {},
        "baseline_is_exhaustive": cfg.get("baseline_is_exhaustive", False),
    }

    # --- Validate required columns exist ---
    columns_map: dict[str, str] = cfg.get("columns", {})
    verdict_col = cfg.get("verdict_column")
    if not verdict_col:
        raise BaselineLoadError("verdict_column not set in baseline mapping")

    # Verdict column is mandatory; identifier columns are warned if missing
    # but not fatal (baselines vary in completeness)
    if verdict_col not in raw_df.columns:
        raise BaselineLoadError(
            f"Baseline file is missing verdict column '{verdict_col}'. "
            f"Found columns: {sorted(raw_df.columns.tolist())}. "
            f"Update config/baseline_mapping.yaml to match the real file."
        )
    # Check which identifier columns are present vs missing
    present_cols: dict[str, str] = {}
    missing_cols: list[str] = []
    for canonical, raw_col in columns_map.items():
        if raw_col in raw_df.columns:
            present_cols[canonical] = raw_col
        else:
            missing_cols.append(f"'{raw_col}' (-> {canonical})")
    if missing_cols:
        print(f"[baseline] WARNING: Optional columns not found: {missing_cols}. "
              f"Alignment may be limited.")
        summary["missing_columns"] = missing_cols
    # Replace columns_map with only present columns for downstream use
    columns_map = present_cols

    # --- Apply skip rules ---
    skip_rules = cfg.get("skip_rules", {})
    kept_mask = pd.Series(True, index=raw_df.index)
    skip_counts: dict[str, int] = {}

    # Skip blank identifier rows
    if skip_rules.get("skip_blank_identifier"):
        id_cols = list(columns_map.values())
        if id_cols:
            primary_id = id_cols[0]  # first configured identifier
            blank_mask = raw_df[primary_id].isna() | (raw_df[primary_id].astype(str).str.strip() == "")
            n = blank_mask.sum()
            if n > 0:
                skip_counts["blank_identifier"] = int(n)
                kept_mask &= ~blank_mask

    # Skip total rows
    total_markers = skip_rules.get("total_row_markers", {})
    if total_markers and total_markers.get("column") and total_markers.get("values"):
        tcol = total_markers["column"]
        tvals = {str(v).strip().lower() for v in total_markers["values"]}
        if tcol in raw_df.columns:
            total_mask = raw_df[tcol].astype(str).str.strip().str.lower().isin(tvals)
            n = total_mask.sum()
            if n > 0:
                skip_counts["total_row"] = int(n)
                kept_mask &= ~total_mask

    # Skip section heading rows
    heading_markers = skip_rules.get("section_heading_markers", {})
    if heading_markers and heading_markers.get("column") and heading_markers.get("patterns"):
        hcol = heading_markers["column"]
        patterns = heading_markers["patterns"]
        if hcol in raw_df.columns:
            combined_pattern = "|".join(f"({p})" for p in patterns)
            heading_mask = raw_df[hcol].astype(str).str.strip().str.match(
                combined_pattern, case=False, na=False
            )
            n = heading_mask.sum()
            if n > 0:
                skip_counts["section_heading"] = int(n)
                kept_mask &= ~heading_mask

    summary["skipped"] = skip_counts
    df = raw_df[kept_mask].copy().reset_index(drop=True)
    summary["kept_row_count"] = len(df)

    # --- Rename columns to canonical names ---
    rename_map = {raw_col: canonical for canonical, raw_col in columns_map.items()}
    df = df.rename(columns=rename_map)

    # --- Map verdicts ---
    verdict_map: dict[str, str] = cfg.get("verdict_map", {})
    if not verdict_map:
        raise BaselineLoadError("verdict_map is empty in baseline mapping — cannot map verdicts")

    df["raw_verdict"] = df[verdict_col].astype(str).str.strip()
    # Skip rows with blank verdict
    blank_verdict_mask = df["raw_verdict"].isin(["", "nan", "None", "NaN"])
    blank_verdict_count = int(blank_verdict_mask.sum())
    if blank_verdict_count > 0:
        summary["skipped"]["blank_verdict"] = blank_verdict_count
        df = df[~blank_verdict_mask].reset_index(drop=True)
        summary["kept_row_count"] = len(df)

    # Check for unmapped verdicts — this is the critical safety check
    unique_verdicts = set(df["raw_verdict"].unique())
    mapped_verdicts = set(verdict_map.keys())
    unmapped = unique_verdicts - mapped_verdicts
    if unmapped:
        raise BaselineLoadError(
            f"Baseline contains verdict value(s) not in verdict_map: {sorted(unmapped)}. "
            f"Mapped values: {sorted(mapped_verdicts)}. "
            f"Add these to config/baseline_mapping.yaml verdict_map before proceeding. "
            f"Do NOT default them to Matched — each must be explicitly mapped."
        )

    # Validate that all map targets are canonical
    bad_targets = {v for v in verdict_map.values() if v not in CANONICAL_CLASSIFICATIONS}
    if bad_targets:
        raise BaselineLoadError(
            f"verdict_map contains invalid target classifications: {bad_targets}. "
            f"Valid: {sorted(CANONICAL_CLASSIFICATIONS)}"
        )

    df["baseline_verdict"] = df["raw_verdict"].map(verdict_map)

    # --- Optional difference_type ---
    diff_type_col = cfg.get("difference_type_column")
    if diff_type_col and diff_type_col in raw_df.columns:
        df["baseline_difference_type"] = df[diff_type_col].astype(str).str.strip()
        df.loc[df["baseline_difference_type"].isin(["", "nan", "None", "NaN"]),
               "baseline_difference_type"] = None
    else:
        df["baseline_difference_type"] = None

    # --- Identify rows lacking a usable identifier ---
    id_fields_present = [c for c in columns_map.keys() if c in df.columns]
    primary_id_field = id_fields_present[0] if id_fields_present else None
    if primary_id_field:
        no_id_mask = df[primary_id_field].isna() | (df[primary_id_field].astype(str).str.strip() == "")
        summary["rows_without_identifier"] = int(no_id_mask.sum())
    else:
        summary["rows_without_identifier"] = len(df)

    # --- Classification counts ---
    summary["by_classification"] = df["baseline_verdict"].value_counts().to_dict()

    # --- Print summary ---
    _print_baseline_summary(summary)

    return df, summary


def _print_baseline_summary(summary: dict[str, Any]) -> None:
    print(f"\n[baseline] Loaded: {summary['baseline_path']}")
    print(f"  Sheet: {summary['sheet_name']}")
    print(f"  Raw rows: {summary['raw_row_count']}")
    for reason, count in summary["skipped"].items():
        print(f"  Skipped ({reason}): {count}")
    print(f"  Kept rows: {summary['kept_row_count']}")
    print(f"  Rows without usable identifier: {summary['rows_without_identifier']}")
    print(f"  Exhaustive baseline: {summary['baseline_is_exhaustive']}")
    print(f"  By classification: {summary['by_classification']}")
