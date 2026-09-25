"""Load and validate matching rules YAML. Disposable PoC — fail loudly."""

from pathlib import Path
from typing import Any

import yaml


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "matching_rules.yaml"

REQUIRED_GST_KEYS = [
    "recon_type",
    "amount_tolerance",
    "rounding_tolerance",
    "date_tolerance_days",
    "fuzzy_threshold",
    "confidence_thresholds",
    "confidence_baselines",
    "ambiguity_margin",
    "document_type_map",
    "valid_rate_slabs",
    "invoice_number_fy_patterns",
]

REQUIRED_GST_BASELINE_KEYS = [
    "exact", "invoice_variant", "amount_date", "fuzzy_party",
    "cross_period", "ambiguity_penalty",
]

REQUIRED_TDS_KEYS = [
    "recon_type",
    "amount_tolerance",
    "rounding_tolerance",
    "date_tolerance_days",
    "fuzzy_threshold",
    "confidence_thresholds",
    "confidence_baselines",
    "no_pan_rate",
    "section_table",
]

REQUIRED_AMOUNT_TOLERANCE_KEYS = ["absolute", "percent"]
REQUIRED_CONFIDENCE_KEYS = ["high", "medium"]
REQUIRED_TDS_BASELINE_KEYS = ["challan", "deductee_level", "aggregated", "fuzzy",
                               "cross_quarter", "section_validation"]
REQUIRED_OTHER_KEYS = [
    "recon_type",
    "amount_tolerance",
    "rounding_tolerance",
    "date_tolerance_days",
    "fuzzy_threshold",
    "confidence_thresholds",
    "confidence_baselines",
    "sources",
]
REQUIRED_OTHER_BASELINE_KEYS = ["exact", "invoice_variant", "amount_date", "fuzzy_party"]
REQUIRED_RECON_TYPES = ["gst", "tds", "other"]


def _fail_missing(path: str) -> None:
    raise KeyError(f"Config is missing required key: {path}")


def _validate_common(name: str, block: dict[str, Any], required_keys: list[str]) -> None:
    for key in required_keys:
        if key not in block:
            _fail_missing(f"{name}.{key}")

    at = block["amount_tolerance"]
    if not isinstance(at, dict):
        _fail_missing(f"{name}.amount_tolerance.absolute")
    for key in REQUIRED_AMOUNT_TOLERANCE_KEYS:
        if key not in at:
            _fail_missing(f"{name}.amount_tolerance.{key}")

    ct = block["confidence_thresholds"]
    if not isinstance(ct, dict):
        _fail_missing(f"{name}.confidence_thresholds.high")
    for key in REQUIRED_CONFIDENCE_KEYS:
        if key not in ct:
            _fail_missing(f"{name}.confidence_thresholds.{key}")


def _validate_tds(block: dict[str, Any]) -> None:
    _validate_common("tds", block, REQUIRED_TDS_KEYS)
    bl = block["confidence_baselines"]
    if not isinstance(bl, dict):
        _fail_missing("tds.confidence_baselines.challan")
    for key in REQUIRED_TDS_BASELINE_KEYS:
        if key not in bl:
            _fail_missing(f"tds.confidence_baselines.{key}")
    if not isinstance(block["section_table"], dict) or not block["section_table"]:
        _fail_missing("tds.section_table (must contain at least one section)")


def _validate_gst(block: dict[str, Any]) -> None:
    _validate_common("gst", block, REQUIRED_GST_KEYS)
    bl = block["confidence_baselines"]
    if not isinstance(bl, dict):
        _fail_missing("gst.confidence_baselines.exact")
    for key in REQUIRED_GST_BASELINE_KEYS:
        if key not in bl:
            _fail_missing(f"gst.confidence_baselines.{key}")
    if not isinstance(block["valid_rate_slabs"], list) or not block["valid_rate_slabs"]:
        _fail_missing("gst.valid_rate_slabs (must be a non-empty list)")
    if not isinstance(block["invoice_number_fy_patterns"], list) or not block["invoice_number_fy_patterns"]:
        _fail_missing("gst.invoice_number_fy_patterns (must be a non-empty list)")


def _validate_other(block: dict[str, Any]) -> None:
    """2C "Other" — the six sources layered onto the SAME shared engine."""
    _validate_common("other", block, REQUIRED_OTHER_KEYS)
    bl = block["confidence_baselines"]
    if not isinstance(bl, dict):
        _fail_missing("other.confidence_baselines.exact")
    for key in REQUIRED_OTHER_BASELINE_KEYS:
        if key not in bl:
            _fail_missing(f"other.confidence_baselines.{key}")
    if not isinstance(block["sources"], dict) or not block["sources"]:
        _fail_missing("other.sources (must contain at least one 2C source)")


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Read the YAML config and validate all required keys exist.

    Raises KeyError naming the first missing key, or ValueError if the
    top-level recon_type blocks are absent.
    """
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping of recon types")

    # Ensure both recon type blocks are present, then validate each.
    missing = [rt for rt in REQUIRED_RECON_TYPES if rt not in raw]
    if missing:
        raise KeyError(f"Config is missing required key(s): {', '.join(missing)}")

    _validate_gst(raw["gst"])
    _validate_tds(raw["tds"])
    _validate_other(raw["other"])

    return raw


if __name__ == "__main__":
    cfg = load_config()
    print("Config loaded OK:", sorted(cfg.keys()))