"""Ingestion + normalization for GST/TDS source files. No matching logic.

Reads raw CSV/Excel exports, maps columns via config/source_mappings.yaml,
normalizes values, and returns canonical pandas DataFrames. Disposable PoC —
fail loudly on missing columns, flag (don't fix) data-quality issues.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from src.data_paths import source_data_path

MAPPINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "source_mappings.yaml"

# --- Canonical schemas (structural, not tunable -> code, not YAML) ---------

GST_CANONICAL_FIELDS = [
    "source_type",
    "source_file",
    "gstin",
    "party_name",
    "invoice_number",
    "invoice_date",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "cess",
    "total_tax",
    "invoice_value",
    "original_row",
]

TDS_CANONICAL_FIELDS = [
    "source_type",
    "source_file",
    "pan",
    "deductee_name",
    "section",
    "amount_paid_credited",
    "tax_deducted",
    "tax_deposited",
    "deposit_date",
    "challan_number",
    "certificate_number",
    "original_row",
]

GST_NUMERIC_FIELDS = ["taxable_value", "cgst", "sgst", "igst", "cess", "invoice_value"]
TDS_NUMERIC_FIELDS = ["amount_paid_credited", "tax_deducted", "tax_deposited"]

GST_UPPER_FIELDS = ["gstin"]
TDS_UPPER_FIELDS = ["pan"]

GST_DATE_FIELDS = ["invoice_date"]
TDS_DATE_FIELDS = ["deposit_date"]

# Source types that unambiguously belong to one recon type.
GST_PORTAL_SOURCE_TYPES = {"gstr2b", "ims"}
TDS_PORTAL_SOURCE_TYPES = {"form26as", "tds"}
# "tally" feeds both recon types (it's the books side for GST and TDS).


# --- Config loading ----------------------------------------------------------


def _load_mappings() -> dict[str, Any]:
    with open(MAPPINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_recon_type(source_type: str, recon_type: Optional[str]) -> str:
    if source_type in GST_PORTAL_SOURCE_TYPES:
        return "GST"
    if source_type in TDS_PORTAL_SOURCE_TYPES:
        return "TDS"
    if source_type == "tally":
        if recon_type not in ("GST", "TDS"):
            raise ValueError(
                "source_type 'tally' feeds both GST and TDS recon; "
                "pass recon_type='GST' or recon_type='TDS' explicitly."
            )
        return recon_type
    raise ValueError(f"Unknown source_type: {source_type!r}")


# --- File discovery / reading -----------------------------------------------


def _find_source_file(
    client: str, period: str, source_type: str, recon_type: Optional[str] = None
) -> Optional[str]:
    """Return the first CSV/Excel filename found in the source dir, or None.

    For the 'tally' source (which feeds both GST and TDS recon), `recon_type`
    narrows candidates to files whose headers carry the columns that recon
    type actually needs. This prevents, e.g., a GST purchase register from
    being returned when loading the TDS books side. When no candidate matches
    the expected columns, falls back to the first file so that
    ``load_source_file``'s explicit column validation still raises the clear,
    actionable error rather than silently misbehaving.
    """
    directory = source_data_path(client, period, source_type)
    candidates: list[Path] = []
    for pattern in ("*.csv", "*.xlsx", "*.xls"):
        candidates.extend(sorted(directory.glob(pattern)))
    if not candidates:
        return None

    if source_type == "tally" and recon_type is not None:
        required = _tally_required_raw_columns(recon_type)
        for path in candidates:
            if _header_has_columns(path, required):
                return path.name

    return candidates[0].name


def _tally_required_raw_columns(recon_type: str) -> list[str]:
    """Raw column names a tally file must carry to serve ``recon_type``.

    Derived from source_mappings.yaml (not hardcoded): the raw columns that
    map to this recon type's canonical fields. A GST purchase register shares
    none of the TDS columns, so this cleanly separates the two books exports
    that can both live in the same ``tally/`` directory.
    """
    mappings = _load_mappings()
    columns_map = mappings["tally"].get("columns", {})
    canonical_fields = (
        GST_CANONICAL_FIELDS if recon_type == "GST" else TDS_CANONICAL_FIELDS
    )
    expected = _required_raw_columns(columns_map, canonical_fields)
    return sorted(set(expected.values()))


def _header_has_columns(path: Path, required: list[str]) -> bool:
    """True if the file's header row includes every required raw column."""
    if not required:
        return True
    try:
        ext = path.suffix.lower()
        if ext == ".csv":
            header = list(pd.read_csv(path, nrows=0).columns)
        else:
            header = list(pd.read_excel(path, nrows=0).columns)
    except Exception:
        return False
    header_set = {str(c).strip() for c in header}
    return all(r in header_set for r in required)


def _read_raw(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str)
    raise ValueError(f"Unsupported file extension: {ext} ({path})")


# --- Column validation / mapping --------------------------------------------


def _required_raw_columns(
    columns_map: dict[str, str], canonical_fields: list[str]
) -> dict[str, str]:
    """canonical_field -> raw_column, restricted to fields we actually need."""
    needed = set(canonical_fields)
    result: dict[str, str] = {}
    for raw_col, canonical_field in columns_map.items():
        if canonical_field in needed:
            result[canonical_field] = raw_col
    return result


def _validate_columns(
    raw_df: pd.DataFrame, expected: dict[str, str], source_type: str
) -> None:
    found = set(raw_df.columns)
    missing = {
        canonical_field: raw_col
        for canonical_field, raw_col in expected.items()
        if raw_col not in found
    }
    if missing:
        expected_list = sorted(expected.values())
        found_list = sorted(found)
        missing_list = [f"{raw_col!r} (-> {field})" for field, raw_col in missing.items()]
        raise ValueError(
            f"Missing expected column(s) in {source_type!r} source file: "
            f"{missing_list}. Expected columns: {expected_list}. "
            f"Found columns: {found_list}."
        )


# --- Normalization -----------------------------------------------------------


def _coerce_numeric(series: pd.Series) -> tuple[pd.Series, list[int]]:
    """Coerce to numeric; blank/non-numeric -> 0. Returns (series, affected row indices)."""
    cleaned = series.astype(str).str.strip().replace({"": None})
    numeric = pd.to_numeric(cleaned, errors="coerce")
    # Any row that failed to parse (blank or non-numeric) is "affected".
    affected_mask = numeric.isna()
    affected_positions = list(series.index[affected_mask])
    numeric = numeric.fillna(0.0)
    return numeric, affected_positions


def _normalize_dates(series: pd.Series, fmt: str, *, field: str = "", context: str = "") -> pd.Series:
    """Parse dates using `fmt`; rows that don't match become blank (NaT).

    A wrong `fmt` (e.g. configured as %d-%m-%Y for a source that actually
    exports %Y-%m-%d) causes every row to fail silently otherwise, which
    is exactly the kind of mismatch that broke real exports before — so
    warn loudly whenever more than a token number of rows fail to parse.
    """
    raw = series.astype(str).str.strip()
    parsed = pd.to_datetime(raw, format=fmt, errors="coerce")
    non_blank = raw.replace({"": None, "nan": None, "None": None}).notna()
    failed = int((parsed.isna() & non_blank).sum())
    total_non_blank = int(non_blank.sum())
    if failed and total_non_blank and failed == total_non_blank:
        print(
            f"[WARN] {context}: ALL {failed} non-blank '{field}' value(s) failed to parse "
            f"with format {fmt!r} (e.g. {raw[non_blank].iloc[0]!r}). "
            f"The date format in config/source_mappings.yaml is likely wrong for this source \u2014 "
            f"dates will be blank in results."
        )
    elif failed:
        print(
            f"[WARN] {context}: {failed} of {total_non_blank} '{field}' value(s) failed to parse "
            f"with format {fmt!r} and will be blank."
        )
    return parsed.dt.strftime("%Y-%m-%d")


def _trim_strings(df: pd.DataFrame, fields: list[str]) -> None:
    for field in fields:
        if field in df.columns:
            df[field] = df[field].astype(str).str.strip()


# --- Core loader --------------------------------------------------------------


def load_source_file(
    client: str,
    period: str,
    source_type: str,
    filename: str,
    recon_type: Optional[str] = None,
) -> pd.DataFrame:
    """Load one raw source file into its canonical DataFrame.

    recon_type ('GST'/'TDS') is required when source_type == 'tally' since
    that source feeds both recon types; it's inferred automatically for
    portal sources (gstr2b/ims -> GST, form26as/tds -> TDS).
    """
    resolved_recon_type = _resolve_recon_type(source_type, recon_type)
    canonical_fields = (
        GST_CANONICAL_FIELDS if resolved_recon_type == "GST" else TDS_CANONICAL_FIELDS
    )

    directory = source_data_path(client, period, source_type)
    path = directory / filename
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    mappings = _load_mappings()
    if source_type not in mappings:
        raise KeyError(f"No column mapping defined for source_type {source_type!r}")
    source_cfg = mappings[source_type]
    columns_map: dict[str, str] = source_cfg.get("columns", {})
    date_columns: dict[str, str] = source_cfg.get("date_columns", {})

    raw_df = _read_raw(path)

    expected = _required_raw_columns(columns_map, canonical_fields)
    _validate_columns(raw_df, expected, source_type)

    # Keep the full original row (raw, unmapped) as JSON for traceability.
    original_rows = raw_df.apply(lambda row: json.dumps(row.to_dict()), axis=1)

    # Build canonical frame: rename mapped raw columns, drop the rest.
    rename_map = {raw_col: field for field, raw_col in expected.items()}
    canonical_df = raw_df.rename(columns=rename_map)[list(rename_map.values())]

    # Any canonical field not present in this source (e.g. tally has no
    # tax_deposited) gets added as an empty column.
    for field in canonical_fields:
        if field in ("source_type", "source_file", "total_tax", "original_row"):
            continue
        if field not in canonical_df.columns:
            canonical_df[field] = None

    canonical_df["original_row"] = original_rows.values
    canonical_df["source_type"] = source_type
    canonical_df["source_file"] = filename

    # --- Normalize strings ---
    string_fields = [
        f
        for f in canonical_fields
        if f
        not in (
            "source_type",
            "source_file",
            "original_row",
            "total_tax",
            *GST_NUMERIC_FIELDS,
            *TDS_NUMERIC_FIELDS,
            *GST_DATE_FIELDS,
            *TDS_DATE_FIELDS,
        )
    ]
    _trim_strings(canonical_df, string_fields)

    for field in GST_UPPER_FIELDS + TDS_UPPER_FIELDS:
        if field in canonical_df.columns:
            canonical_df[field] = canonical_df[field].astype(str).str.upper().str.strip()

    # --- Normalize dates ---
    date_fields = GST_DATE_FIELDS if resolved_recon_type == "GST" else TDS_DATE_FIELDS
    for field in date_fields:
        if field in canonical_df.columns and canonical_df[field].notna().any():
            fmt = date_columns.get(field, "%d-%m-%Y")
            canonical_df[field] = _normalize_dates(
                canonical_df[field], fmt, field=field, context=f"{source_type}/{filename}"
            )

    # --- Coerce numeric fields, tracking affected rows for the warning ---
    numeric_fields = GST_NUMERIC_FIELDS if resolved_recon_type == "GST" else TDS_NUMERIC_FIELDS
    all_affected_rows: set[int] = set()
    for field in numeric_fields:
        if field in canonical_df.columns:
            coerced, affected = _coerce_numeric(canonical_df[field])
            canonical_df[field] = coerced
            all_affected_rows.update(affected)
    if all_affected_rows:
        print(
            f"[WARN] {source_type}/{filename}: coerced/defaulted numeric values to 0 "
            f"in row(s) {sorted(all_affected_rows)}"
        )

    # --- Derived field: total_tax (GST only) ---
    if resolved_recon_type == "GST":
        canonical_df["total_tax"] = (
            canonical_df["cgst"] + canonical_df["sgst"] + canonical_df["igst"] + canonical_df["cess"]
        )

    canonical_df = canonical_df[canonical_fields]

    _print_validation_summary(canonical_df, source_type, filename, all_affected_rows)

    return canonical_df.reset_index(drop=True)


def _print_validation_summary(
    df: pd.DataFrame, source_type: str, filename: str, affected_rows: set[int]
) -> None:
    row_count = len(df)
    coerced_count = len(affected_rows)
    duplicate_count = int(df.duplicated(keep=False).sum())
    print(
        f"[SUMMARY] {source_type}/{filename}: rows={row_count}, "
        f"rows_with_coerced_numeric={coerced_count}, "
        f"exact_duplicate_rows={duplicate_count} (flagged only, not dropped)"
    )


# --- Convenience wrappers -----------------------------------------------------


def load_gst_pair(
    client: str,
    period: str,
    selected_files: Optional[dict[str, str]] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (books_df, portal_df) for GST recon: tally vs gstr2b/ims.

    ``selected_files`` maps source_type -> filename, letting callers pin
    exactly which on-disk file feeds the run. When a source type is absent
    from the mapping (or the mapping is None), auto-discovery falls back to
    the previous behaviour of scanning the source directory.
    """
    sel = selected_files or {}
    books_file = sel.get("tally") or _find_source_file(client, period, "tally", recon_type="GST")
    if not books_file:
        raise FileNotFoundError(
            f"No 'tally' source file found for client={client!r} period={period!r}"
        )
    books_df = load_source_file(client, period, "tally", books_file, recon_type="GST")

    for portal_source in ("gstr2b", "ims"):
        portal_file = sel.get(portal_source) or _find_source_file(client, period, portal_source)
        if portal_file:
            portal_df = load_source_file(client, period, portal_source, portal_file)
            return books_df, portal_df

    raise FileNotFoundError(
        f"No 'gstr2b' or 'ims' source file found for client={client!r} period={period!r}"
    )


def load_tds_pair(
    client: str,
    period: str,
    selected_files: Optional[dict[str, str]] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (books_df, portal_df) for TDS recon: tally vs form26as/tds.

    ``selected_files`` maps source_type -> filename (see load_gst_pair).
    """
    sel = selected_files or {}
    books_file = sel.get("tally") or _find_source_file(client, period, "tally", recon_type="TDS")
    if not books_file:
        raise FileNotFoundError(
            f"No 'tally' source file found for client={client!r} period={period!r}"
        )
    books_df = load_source_file(client, period, "tally", books_file, recon_type="TDS")

    for portal_source in ("form26as", "tds"):
        portal_file = sel.get(portal_source) or _find_source_file(client, period, portal_source)
        if portal_file:
            portal_df = load_source_file(client, period, portal_source, portal_file)
            return books_df, portal_df

    raise FileNotFoundError(
        f"No 'form26as' or 'tds' source file found for client={client!r} period={period!r}"
    )


# --- 2C "Other" generic loader ------------------------------------------------
# The six 2C sources (bank / vendor_ledger / form26as / opening_balances /
# loan_sheet / salary) share a generic reference/date/amount identity. Rather
# than a bespoke canonical schema per source, 2C reads a generic frame — the
# matching-key configuration (Module 2's MatchingKeyConfig) decides which
# columns matter, so a new 2C source is a configuration change, not a code
# path. This is the SAME shared engine's ingestion, kept deliberately thin.

OTHER_CANONICAL_FIELDS = [
    "source_type", "source_file", "reference", "date", "amount", "party_name",
    "narration", "original_row",
]

# Column-name aliases accepted for the generic 2C fields. Matching is done on
# a normalized key (lowercased, spaces/underscores/periods/slashes stripped)
# so raw headers like "Invoice No." or "Txn Date" resolve without a bespoke
# per-source mapping.
_OTHER_ALIASES: dict[str, tuple[str, ...]] = {
    "reference": ("reference", "invoicenumber", "vouchernumber", "challannumber", "ref",
                  "invoiceno", "voucherno", "docno"),
    "date": ("date", "invoicedate", "depositdate", "txndate", "transactiondate", "voucherdate"),
    "amount": ("amount", "invoicevalue", "amountpaidcredited", "taxdeducted", "value",
               "txnamount", "debit", "credit"),
    "party_name": ("partyname", "deducteename", "party", "name", "suppliername"),
    "narration": ("narration", "description", "particulars", "remarks", "narrationtext"),
}


def _normalize_header(name: Any) -> str:
    """Normalize a raw header into a lookup key: lowercase, strip spaces,
    underscores, periods, hyphens and slashes."""
    return re.sub(r"[\s._\-/]", "", str(name).strip().lower())


def load_other_source_file(client: str, period: str, source_type: str, filename: str) -> pd.DataFrame:
    """Load a 2C source file into the generic 2C canonical frame."""
    directory = source_data_path(client, period, source_type)
    path = directory / filename
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    raw_df = _read_raw(path)
    normalized = {_normalize_header(c): c for c in raw_df.columns}

    out = pd.DataFrame()
    for field, aliases in _OTHER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                out[field] = raw_df[normalized[alias]]
                break
        else:
            out[field] = None

    out["original_row"] = raw_df.apply(lambda row: json.dumps(row.to_dict()), axis=1)
    out["source_type"] = source_type
    out["source_file"] = filename

    if "amount" in out.columns:
        out["amount"], _ = _coerce_numeric(out["amount"])
    if "date" in out.columns and out["date"].notna().any():
        # 2C sources are heterogeneous, so try the common formats rather than
        # assuming one. First format that parses the most rows wins.
        out["date"] = _normalize_dates_flexible(
            out["date"], field="date", context=f"{source_type}/{filename}"
        )

    return out[OTHER_CANONICAL_FIELDS].reset_index(drop=True)


def _normalize_dates_flexible(series: pd.Series, *, field: str = "", context: str = "") -> pd.Series:
    """Parse dates trying several common formats (ISO, DD-MM-YYYY,
    DD/MM/YYYY, MM/DD/YYYY) and keep the format that parses the most rows.
    Used by the heterogeneous 2C sources, which don't share one export
    format the way the GST/TDS sources do."""
    raw = series.astype(str).str.strip()
    non_blank = raw.replace({"": None, "nan": None, "None": None}).notna()
    best = None
    best_ok = -1
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y"):
        parsed = pd.to_datetime(raw, format=fmt, errors="coerce")
        ok = int(parsed.notna().sum())
        if ok > best_ok:
            best, best_ok = parsed, ok
    if best is None:
        return raw
    failed = int((best.isna() & non_blank).sum())
    if failed:
        print(
            f"[WARN] {context}: {failed} '{field}' value(s) could not be parsed "
            "with any known format and will be blank."
        )
    return best.dt.strftime("%Y-%m-%d")


def load_other_pair(
    client: str,
    period: str,
    source_key: str,
    selected_files: Optional[dict[str, str]] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (books_df, portal_df) for a 2C source: tally vs the 2C source.

    ``source_key`` is one of the six 2C sources (bank / vendor_ledger /
    form26as / opening_balances / loan_sheet / salary). The books side is the
    same tally export used by 2A/2B, read through the generic 2C loader.
    """
    sel = selected_files or {}
    books_file = sel.get("tally") or _find_source_file(client, period, "tally")
    if not books_file:
        raise FileNotFoundError(
            f"No 'tally' source file found for client={client!r} period={period!r}"
        )
    books_df = load_other_source_file(client, period, "tally", books_file)

    portal_file = sel.get(source_key) or _find_source_file(client, period, source_key)
    if not portal_file:
        raise FileNotFoundError(
            f"No {source_key!r} source file found for client={client!r} period={period!r}"
        )
    portal_df = load_other_source_file(client, period, source_key, portal_file)
    return books_df, portal_df