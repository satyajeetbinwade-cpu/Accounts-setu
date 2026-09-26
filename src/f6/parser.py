"""F6 — the generic, config-driven parser.

Reads a FormatVersion's parse_config (see seed_formats.py for the shape)
and produces canonical rows + row accounting + control-total inputs. The
parser itself is GENERIC — a GSTN template change is a new config row,
never a code change (§5's stated design goal).

Runs identically on Path A (known format) and immediately after Path B
confirmation (before promotion) — the same function, so a confirmed
mapping is proven against the exact code that will run it in production
forever after, not a preview approximation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from src.f6.fingerprint import _normalize_label, flatten_two_row_header


@dataclass
class ParsedSheetResult:
    sheet_name: str
    role: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    drill_down_rows: list[dict[str, Any]] = field(default_factory=list)  # pre-aggregation rows
    row_count_read: int = 0
    row_count_parsed: int = 0
    row_count_excluded: int = 0
    exclusion_reasons: list[dict[str, Any]] = field(default_factory=list)
    control_total_candidates: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedFileResult:
    sheet_results: list[ParsedSheetResult] = field(default_factory=list)
    recognised_not_parsed: list[str] = field(default_factory=list)

    @property
    def all_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sr in self.sheet_results:
            if sr.role == "line_items":
                rows.extend(sr.rows)
        return rows

    @property
    def row_count_read(self) -> int:
        return sum(sr.row_count_read for sr in self.sheet_results)

    @property
    def row_count_parsed(self) -> int:
        return sum(sr.row_count_parsed for sr in self.sheet_results)

    @property
    def row_count_excluded(self) -> int:
        return sum(sr.row_count_excluded for sr in self.sheet_results)

    @property
    def exclusion_reasons(self) -> list[dict[str, Any]]:
        merged: dict[str, int] = {}
        for sr in self.sheet_results:
            for item in sr.exclusion_reasons:
                merged[item["reason"]] = merged.get(item["reason"], 0) + item["count"]
        return [{"reason": r, "count": c} for r, c in merged.items()]


_TRANSFORM_DATE_RE = re.compile(r"^parse_date:(.+)$")
_TRANSFORM_PERIOD_RE = re.compile(r"^parse_period:(.+)$")


def _apply_scalar_transform(value: Any, transform: Optional[str], date_format: str) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if transform is None:
        return value
    m = _TRANSFORM_DATE_RE.match(transform)
    if m:
        fmt = m.group(1)
        raw = str(value).strip()
        try:
            parsed = pd.to_datetime(raw, format=fmt, errors="coerce")
            if pd.isna(parsed):
                # The layout declares one separator/order but a real export
                # may ship another ("29-09-2026" where the config says
                # "%d/%m/%Y"). Retry day-first rather than silently dropping
                # EVERY date in the file — only as a fallback, so a correctly
                # formatted file is untouched.
                parsed = pd.to_datetime(raw, dayfirst=True, errors="coerce")
            if pd.isna(parsed):
                return None
            return parsed.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return None
    if transform == "yes_no_to_bool":
        s = str(value).strip().lower()
        return s in ("yes", "y", "true", "1")
    if transform == "strip_trailing_period":
        return str(value).strip().rstrip(".")
    m = _TRANSFORM_PERIOD_RE.match(transform)
    if m:
        # "Aug'26" / "Aug-25" -> "2026-08" style period. Best-effort parse.
        raw = str(value).strip()
        raw = raw.replace("'", "-")
        try:
            parsed = pd.to_datetime("01-" + raw, format="%d-%b-%y", errors="coerce")
            if pd.isna(parsed):
                parsed = pd.to_datetime("01-" + raw, format="%d-%b-%Y", errors="coerce")
            if pd.notna(parsed):
                return parsed.strftime("%Y-%m")
        except Exception:  # noqa: BLE001
            pass
        return raw
    return value


def _get_column(row_dict: dict[str, Any], flattened_columns: list[str], label: str) -> Any:
    """flattened_columns is positional (index-aligned to the row); look up
    by normalized label."""
    target = _normalize_label(label)
    for key, val in row_dict.items():
        if _normalize_label(key) == target:
            return val
    return None


def _read_headerless(raw_bytes_or_path: Any, sheet_name: Optional[str]) -> pd.DataFrame:
    kwargs: dict[str, Any] = {"header": None, "dtype": str}
    if sheet_name is not None:
        return pd.read_excel(raw_bytes_or_path, sheet_name=sheet_name, **kwargs)
    return pd.read_csv(raw_bytes_or_path, header=None, dtype=str, keep_default_na=False)


def _build_flattened_frame(
    raw: pd.DataFrame, header_cfg: dict[str, Any], data_start_row: int,
) -> tuple[list[str], pd.DataFrame]:
    if header_cfg["type"] == "pair":
        parent_idx, child_idx = header_cfg["rows"]
        parent_row = list(raw.iloc[parent_idx]) if parent_idx < len(raw) else []
        child_row = list(raw.iloc[child_idx]) if child_idx < len(raw) else []
        flattened = flatten_two_row_header(parent_row, child_row)
    else:
        idx = header_cfg["row"]
        header_row = list(raw.iloc[idx]) if idx < len(raw) else []
        flattened = [_normalize_label(c) or f"col_{i}" for i, c in enumerate(header_row)]

    data = raw.iloc[data_start_row:].reset_index(drop=True)
    data.columns = flattened[: len(data.columns)]
    return flattened, data


def _is_total_row(row: pd.Series, marker: dict[str, Any]) -> bool:
    col = _normalize_label(marker["column"])
    contains = marker["contains"].lower()
    for label, value in row.items():
        if _normalize_label(label) == col:
            return contains in str(value).strip().lower()
    return False


def _is_blank_row(row: pd.Series) -> bool:
    return all((v is None) or (str(v).strip() in ("", "nan", "None")) for v in row)


def _apply_aggregate(
    row: pd.Series, source_columns: list[str], transform: Optional[str],
) -> float:
    """Sum the listed flattened columns. `dedupe_taxable_base_cgst_sgst_
    restate` (§5.4's named error-prone rule): CGST and SGST rate-bucket
    taxable columns restate the SAME base — only count each bucket ONCE
    even though it appears under both the CGST and SGST prefix in the
    listed source_columns (the config lists CGST-side columns only, by
    design, so no special-casing is needed here beyond a plain sum — the
    caller is responsible for listing exactly one side's columns)."""
    total = 0.0
    for col in source_columns:
        val = _get_column(row.to_dict(), [], col)
        try:
            total += float(str(val).strip() or 0)
        except (TypeError, ValueError):
            continue
    return total


def parse_sheet(
    raw: pd.DataFrame, sheet_name: str, sheet_cfg: dict[str, Any], date_format: str,
) -> ParsedSheetResult:
    role = sheet_cfg.get("role", "line_items")
    result = ParsedSheetResult(sheet_name=sheet_name, role=role)

    if role != "line_items":
        return result  # summary_ignored / deferred / out_of_scope — recognised, never parsed

    header_cfg = sheet_cfg["header"]
    data_start = sheet_cfg["data_start_row"]
    _, data = _build_flattened_frame(raw, header_cfg, data_start)

    result.row_count_read = len(data)
    exclusion = sheet_cfg.get("row_exclusion", {})
    total_marker = exclusion.get("total_row_marker")

    kept_rows: list[pd.Series] = []
    excluded_blank = 0
    excluded_total = 0
    control_total: dict[str, float] = {}

    for _, row in data.iterrows():
        if _is_blank_row(row):
            excluded_blank += 1
            continue
        if total_marker and _is_total_row(row, total_marker):
            excluded_total += 1
            # Capture the file's own stated totals for §8's control-total check.
            for col in sheet_cfg.get("control_total", {}).get("columns", []):
                val = _get_column(row.to_dict(), [], col)
                try:
                    control_total[col] = control_total.get(col, 0.0) + float(str(val).strip() or 0)
                except (TypeError, ValueError):
                    continue
            continue
        kept_rows.append(row)

    if excluded_blank:
        result.exclusion_reasons.append({"reason": "trailing_blank_row", "count": excluded_blank})
    if excluded_total:
        result.exclusion_reasons.append({"reason": "total_row", "count": excluded_total})
    result.row_count_excluded = excluded_blank + excluded_total
    result.control_total_candidates = control_total

    field_rules = sheet_cfg.get("field_rules", [])
    for row in kept_rows:
        canonical_row: dict[str, Any] = {"_source_sheet": sheet_name}
        drill_down_needed = False
        for rule in field_rules:
            field_name = rule["canonical_field"]
            kind = rule["kind"]
            if kind == "unavailable":
                canonical_row[field_name] = None
                continue
            if kind == "direct":
                col = rule["source_columns"][0]
                raw_val = _get_column(row.to_dict(), [], col)
                canonical_row[field_name] = raw_val
            elif kind == "derived":
                col = rule["source_columns"][0]
                raw_val = _get_column(row.to_dict(), [], col)
                canonical_row[field_name] = _apply_scalar_transform(raw_val, rule.get("transform"), date_format)
            elif kind == "aggregate":
                transform = rule.get("transform")
                if transform == "group_by_document_key":
                    # GSTR-1 rate-row aggregation happens ACROSS rows, not
                    # within one — handled by aggregate_by_document_key()
                    # after this per-row pass. Stash the raw per-rate value
                    # for that later pass and mark this row needing
                    # drill-down retention.
                    col = rule["source_columns"][0]
                    canonical_row[field_name] = _get_column(row.to_dict(), [], col)
                    drill_down_needed = True
                else:
                    canonical_row[field_name] = _apply_aggregate(row, rule["source_columns"], transform)
        if drill_down_needed:
            result.drill_down_rows.append(dict(canonical_row))
        result.rows.append(canonical_row)

    result.row_count_parsed = len(result.rows)
    return result


def aggregate_by_document_key(
    rows: list[dict[str, Any]], *, key_fields: tuple[str, ...] = ("gstin", "invoice_number"),
    sum_fields: tuple[str, ...] = ("taxable_value", "igst", "cgst", "sgst", "cess"),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """§5.3 rate-row aggregation: group GSTR-1's one-row-per-tax-rate lines
    into one row per document. Returns (aggregated_rows, drill_down_rows)
    — drill_down_rows is every pre-aggregation row, retained verbatim
    (the C1-confirmed pre_aggregation_drill_down field addition)."""
    drill_down = [dict(r) for r in rows]
    groups: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for row in rows:
        key = tuple(row.get(k) for k in key_fields)
        if key not in groups:
            groups[key] = dict(row)
            for f in sum_fields:
                groups[key][f] = 0.0
            order.append(key)
        for f in sum_fields:
            try:
                groups[key][f] = float(groups[key].get(f) or 0) + float(row.get(f) or 0)
            except (TypeError, ValueError):
                continue
    aggregated = [groups[k] for k in order]
    return aggregated, drill_down


def _best_matching_sheet_key(raw: pd.DataFrame, sheets_cfg: dict[str, Any]) -> Optional[str]:
    """For a single-sheet CSV that must match exactly ONE of a multi-sheet
    config's line_items entries (real GSTN exports are often split into
    one CSV per sheet — see IMS's per-file B2B / B2B-CN specimens), pick
    the config entry whose expected source columns overlap the CSV's
    actual header the most. Returns None if nothing overlaps at all (the
    caller then correctly reports the file as unrecognised for THIS
    config, rather than force-fitting the wrong sheet's rules)."""
    best_key, best_score = None, 0
    for key, cfg in sheets_cfg.items():
        if cfg.get("role") != "line_items":
            continue
        header_cfg = cfg.get("header", {"type": "single", "row": 0})
        data_start = cfg.get("data_start_row", 1)
        try:
            _, data = _build_flattened_frame(raw, header_cfg, data_start)
        except Exception:  # noqa: BLE001
            continue
        actual_cols = {_normalize_label(c) for c in data.columns}
        expected_cols = {
            _normalize_label(col)
            for rule in cfg.get("field_rules", [])
            for col in rule.get("source_columns", [])
        }
        score = len(actual_cols & expected_cols)
        if score > best_score:
            best_score, best_key = score, key
    return best_key if best_score > 0 else None


def parse_file(
    file_path_or_bytes: Any, parse_config: dict[str, Any], *, is_excel: bool,
) -> ParsedFileResult:
    """Parse an entire workbook/CSV against a FormatVersion's parse_config.
    Returns ParsedFileResult with per-sheet accounting + recognised-not-
    parsed list (§5.1's "deferral is a recognised_not_parsed state")."""
    result = ParsedFileResult()
    date_format = parse_config.get("date_format", "%d-%m-%Y")
    sheets_cfg = parse_config.get("sheets", {})

    if is_excel:
        try:
            xl = pd.ExcelFile(file_path_or_bytes)
            real_sheet_names = xl.sheet_names
        except Exception:  # noqa: BLE001
            real_sheet_names = []
        name_lookup = {_normalize_label(n): n for n in real_sheet_names}
        csv_matched_key = None
        # A file whose real sheet name doesn't match ANY config key by
        # name (e.g. a client's export literally called "Sheet1" instead
        # of the GSTN-style "Purchase Register (Bill-wise)" the config was
        # transcribed from) still needs to reach its line_items rules.
        # Structurally match the leftover sheets the same way a single-
        # sheet CSV is matched, rather than silently skipping them.
        unmatched_real_sheets = [n for n in real_sheet_names if _normalize_label(n) not in
                                  {_normalize_label(k) for k in sheets_cfg}]
        structural_match_for_real_sheet: dict[str, str] = {}
        if unmatched_real_sheets:
            unmatched_cfg_keys = {
                k for k in sheets_cfg
                if sheets_cfg[k].get("role") == "line_items" and _normalize_label(k) not in name_lookup
            }
            unmatched_cfg = {k: sheets_cfg[k] for k in unmatched_cfg_keys}
            for real_name in unmatched_real_sheets:
                probe = pd.read_excel(file_path_or_bytes, sheet_name=real_name, header=None, dtype=str)
                match = _best_matching_sheet_key(probe, unmatched_cfg)
                if match:
                    structural_match_for_real_sheet[real_name] = match
    else:
        # A CSV is a single implicit sheet — it can only ever satisfy ONE
        # of a multi-sheet config's line_items entries. Detect which one
        # structurally rather than parsing it once per config entry (that
        # would wrongly apply every OTHER sheet's field_rules against
        # columns they don't describe, producing an all-None row set).
        csv_raw_probe = pd.read_csv(file_path_or_bytes, header=None, dtype=str, keep_default_na=False)
        csv_matched_key = _best_matching_sheet_key(csv_raw_probe, sheets_cfg)
        name_lookup = {}
        structural_match_for_real_sheet = {}

    for cfg_sheet_key, sheet_cfg in sheets_cfg.items():
        if is_excel:
            real_name = name_lookup.get(_normalize_label(cfg_sheet_key))
            if real_name is None:
                # Fall back to a structural match found above.
                real_name = next(
                    (rn for rn, matched_key in structural_match_for_real_sheet.items() if matched_key == cfg_sheet_key),
                    None,
                )
                if real_name is None:
                    continue  # sheet not present in THIS file — fine, not every client has every sheet
            raw = pd.read_excel(file_path_or_bytes, sheet_name=real_name, header=None, dtype=str)
        else:
            if sheet_cfg.get("role") == "line_items":
                if cfg_sheet_key != csv_matched_key:
                    continue  # this CSV isn't shaped like this sheet — skip, don't force-fit
                raw = csv_raw_probe
            else:
                continue  # a single-sheet CSV can't ALSO be a deferred/summary sheet
            real_name = cfg_sheet_key

        role = sheet_cfg.get("role", "line_items")
        if role != "line_items":
            result.recognised_not_parsed.append(real_name or cfg_sheet_key)
            result.sheet_results.append(ParsedSheetResult(sheet_name=real_name or cfg_sheet_key, role=role))
            continue

        sheet_result = parse_sheet(raw, real_name or cfg_sheet_key, sheet_cfg, date_format)
        result.sheet_results.append(sheet_result)

        # GSTR-1-style rate-row aggregation, applied post-parse per sheet.
        needs_doc_agg = any(
            r.get("kind") == "aggregate" and r.get("transform") == "group_by_document_key"
            for r in sheet_cfg.get("field_rules", [])
        )
        if needs_doc_agg and sheet_result.rows:
            aggregated, drill = aggregate_by_document_key(sheet_result.rows)
            sheet_result.drill_down_rows = drill
            sheet_result.rows = aggregated
            sheet_result.row_count_parsed = len(aggregated)

        if sheet_cfg.get("allow_empty") and sheet_result.row_count_parsed == 0:
            sheet_result.warnings.append(f"{real_name or cfg_sheet_key}: 0 rows — valid empty sheet, not a failure.")

    return result
