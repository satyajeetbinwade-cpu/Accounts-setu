"""Alignment — joining engine results to baseline rows.

Alignment quality bounds the validity of all downstream metrics, so every
alignment decision is recorded (tier, key used) and coverage is reported
before any accuracy metric.

Three tiers, applied in order (each operates on rows unaligned by prior tiers):
  Tier 1: exact match on strong identifier
  Tier 2: identifier + amount within tolerance
  Tier 3: party name similarity + amount + date

Unaligned rows on either side are preserved, not dropped.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import yaml
from pathlib import Path
from rapidfuzz import fuzz

# Import the same normalisation the engine uses — alignment that normalises
# differently would produce phantom disagreements.
from src.matching.gst_matcher import _normalize_invoice_number

CRITERIA_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "verification_criteria.yaml"


def _load_alignment_config(criteria_path: Path | str | None = None) -> dict[str, Any]:
    p = Path(criteria_path) if criteria_path else CRITERIA_PATH
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("alignment", {})


def _within_amount_tolerance(a: float, b: float, cfg: dict) -> bool:
    diff = abs(a - b)
    if diff <= float(cfg.get("amount_tolerance_absolute", 10.0)):
        return True
    denom = max(abs(a), abs(b))
    if denom > 0 and (diff / denom * 100) <= float(cfg.get("amount_tolerance_percent", 1.0)):
        return True
    return False


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_str(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _extract_engine_identity(row: pd.Series, recon_type: str) -> dict[str, Any]:
    """Extract identifying fields from an engine result's books/portal record."""
    for side_key in ("books_record", "portal_record"):
        rec = row.get(side_key)
        if isinstance(rec, dict) and rec:
            return rec
    return {}


def _norm_inv(raw: Any) -> str:
    return _normalize_invoice_number(str(raw).strip()) if raw else ""


def align(
    engine_df: pd.DataFrame,
    baseline_df: pd.DataFrame,
    recon_type: str,
    *,
    criteria_path: str | Path | None = None,
) -> dict[str, Any]:
    """Align engine results to baseline rows.

    Returns a dict with:
      aligned: DataFrame of paired rows (engine + baseline + alignment_tier)
      engine_unaligned: DataFrame of engine results with no baseline match
      baseline_unaligned: DataFrame of baseline rows with no engine match
      coverage: dict of coverage statistics
    """
    cfg = _load_alignment_config(criteria_path)
    rt = recon_type.upper()

    # Tag each row with an index for tracking
    edf = engine_df.copy()
    bdf = baseline_df.copy()
    edf["_eidx"] = range(len(edf))
    bdf["_bidx"] = range(len(bdf))

    # Pre-compute normalised keys on engine side
    edf["_e_identity"] = edf.apply(lambda r: _extract_engine_identity(r, rt), axis=1)

    aligned_pairs: list[dict[str, Any]] = []
    matched_eidxs: set[int] = set()
    matched_bidxs: set[int] = set()

    if rt == "GST":
        aligned_pairs, matched_eidxs, matched_bidxs = _align_gst(edf, bdf, cfg)
    elif rt == "TDS":
        aligned_pairs, matched_eidxs, matched_bidxs = _align_tds(edf, bdf, cfg)
    else:
        raise ValueError(f"Unknown recon_type: {recon_type}")

    # Build result DataFrames
    aligned_df = pd.DataFrame(aligned_pairs) if aligned_pairs else pd.DataFrame()
    engine_unaligned = edf[~edf["_eidx"].isin(matched_eidxs)].drop(
        columns=["_eidx", "_e_identity"], errors="ignore"
    ).reset_index(drop=True)
    baseline_unaligned = bdf[~bdf["_bidx"].isin(matched_bidxs)].drop(
        columns=["_bidx"], errors="ignore"
    ).reset_index(drop=True)

    # Coverage
    total_baseline = len(bdf)
    total_engine = len(edf)
    aligned_baseline = len(matched_bidxs)
    aligned_engine = len(matched_eidxs)

    tier_counts = {}
    if not aligned_df.empty and "alignment_tier" in aligned_df.columns:
        tier_counts = aligned_df["alignment_tier"].value_counts().to_dict()

    coverage = {
        "total_baseline_rows": total_baseline,
        "total_engine_results": total_engine,
        "aligned_baseline_rows": aligned_baseline,
        "aligned_engine_results": aligned_engine,
        "baseline_coverage": aligned_baseline / total_baseline if total_baseline else 0.0,
        "engine_coverage": aligned_engine / total_engine if total_engine else 0.0,
        "by_tier": tier_counts,
        "baseline_unaligned_count": total_baseline - aligned_baseline,
        "engine_unaligned_count": total_engine - aligned_engine,
    }

    return {
        "aligned": aligned_df,
        "engine_unaligned": engine_unaligned,
        "baseline_unaligned": baseline_unaligned,
        "coverage": coverage,
    }


# ---------------------------------------------------------------------------
# GST alignment
# ---------------------------------------------------------------------------

def _align_gst(
    edf: pd.DataFrame, bdf: pd.DataFrame, cfg: dict
) -> tuple[list[dict], set[int], set[int]]:
    pairs: list[dict[str, Any]] = []
    matched_e: set[int] = set()
    matched_b: set[int] = set()

    # Pre-compute normalised invoice numbers
    def _e_gstin(row):
        return _safe_str(row["_e_identity"].get("gstin", "")).upper()

    def _e_inv(row):
        return _norm_inv(row["_e_identity"].get("invoice_number", ""))

    def _e_amount(row):
        return _safe_float(row["_e_identity"].get("invoice_value", 0))

    def _e_date(row):
        return _safe_str(row["_e_identity"].get("invoice_date", ""))

    def _e_party(row):
        return _safe_str(row["_e_identity"].get("party_name", ""))

    edf["_e_gstin"] = edf.apply(_e_gstin, axis=1)
    edf["_e_inv_norm"] = edf.apply(_e_inv, axis=1)
    edf["_e_amount"] = edf.apply(_e_amount, axis=1)
    edf["_e_date"] = edf.apply(_e_date, axis=1)
    edf["_e_party"] = edf.apply(_e_party, axis=1)

    b_gstin = bdf["gstin"].astype(str).str.strip().str.upper() if "gstin" in bdf.columns else pd.Series("", index=bdf.index)
    b_inv_norm = bdf["invoice_number"].apply(_norm_inv) if "invoice_number" in bdf.columns else pd.Series("", index=bdf.index)
    b_amount = bdf["invoice_value"].apply(_safe_float) if "invoice_value" in bdf.columns else pd.Series(0.0, index=bdf.index)
    b_date = bdf["invoice_date"].astype(str).str.strip() if "invoice_date" in bdf.columns else pd.Series("", index=bdf.index)
    b_party = bdf["party_name"].astype(str).str.strip() if "party_name" in bdf.columns else pd.Series("", index=bdf.index)

    bdf["_b_gstin"] = b_gstin
    bdf["_b_inv_norm"] = b_inv_norm
    bdf["_b_amount"] = b_amount
    bdf["_b_date"] = b_date
    bdf["_b_party"] = b_party

    # --- Tier 1: GSTIN + normalised invoice number ---
    for _, erow in edf.iterrows():
        eidx = erow["_eidx"]
        if eidx in matched_e:
            continue
        eg, ei = erow["_e_gstin"], erow["_e_inv_norm"]
        if not eg or not ei:
            continue
        candidates = bdf[
            (~bdf["_bidx"].isin(matched_b))
            & (bdf["_b_gstin"] == eg)
            & (bdf["_b_inv_norm"] == ei)
        ]
        if not candidates.empty:
            brow = candidates.iloc[0]
            pairs.append(_make_pair(erow, brow, 1))
            matched_e.add(eidx)
            matched_b.add(brow["_bidx"])

    # --- Tier 2: GSTIN + amount within tolerance ---
    for _, erow in edf.iterrows():
        eidx = erow["_eidx"]
        if eidx in matched_e:
            continue
        eg, ea = erow["_e_gstin"], erow["_e_amount"]
        if not eg:
            continue
        candidates = bdf[
            (~bdf["_bidx"].isin(matched_b))
            & (bdf["_b_gstin"] == eg)
        ]
        for _, brow in candidates.iterrows():
            if brow["_bidx"] in matched_b:
                continue
            if _within_amount_tolerance(ea, brow["_b_amount"], cfg):
                pairs.append(_make_pair(erow, brow, 2))
                matched_e.add(eidx)
                matched_b.add(brow["_bidx"])
                break

    # --- Tier 3: party name fuzzy + amount + date ---
    fuzzy_thresh = float(cfg.get("fuzzy_name_threshold", 80))
    date_tol = int(cfg.get("date_tolerance_days", 7))
    for _, erow in edf.iterrows():
        eidx = erow["_eidx"]
        if eidx in matched_e:
            continue
        ep, ea, ed = erow["_e_party"], erow["_e_amount"], erow["_e_date"]
        if not ep:
            continue
        for _, brow in bdf.iterrows():
            if brow["_bidx"] in matched_b:
                continue
            if not _within_amount_tolerance(ea, brow["_b_amount"], cfg):
                continue
            if fuzz.token_sort_ratio(ep, brow["_b_party"]) < fuzzy_thresh:
                continue
            if ed and brow["_b_date"]:
                try:
                    diff = abs((pd.Timestamp(ed) - pd.Timestamp(brow["_b_date"])).days)
                    if diff > date_tol:
                        continue
                except Exception:
                    pass
            pairs.append(_make_pair(erow, brow, 3))
            matched_e.add(eidx)
            matched_b.add(brow["_bidx"])
            break

    return pairs, matched_e, matched_b


# ---------------------------------------------------------------------------
# TDS alignment
# ---------------------------------------------------------------------------

def _align_tds(
    edf: pd.DataFrame, bdf: pd.DataFrame, cfg: dict
) -> tuple[list[dict], set[int], set[int]]:
    pairs: list[dict[str, Any]] = []
    matched_e: set[int] = set()
    matched_b: set[int] = set()

    def _e_pan(row):
        return _safe_str(row["_e_identity"].get("pan", "")).upper()

    def _e_section(row):
        return _safe_str(row["_e_identity"].get("section", ""))

    def _e_amount(row):
        return _safe_float(row["_e_identity"].get("tax_deducted",
                           row["_e_identity"].get("amount_paid_credited", 0)))

    def _e_party(row):
        return _safe_str(row["_e_identity"].get("deductee_name", ""))

    def _e_date(row):
        return _safe_str(row["_e_identity"].get("deposit_date", ""))

    edf["_e_pan"] = edf.apply(_e_pan, axis=1)
    edf["_e_section"] = edf.apply(_e_section, axis=1)
    edf["_e_amount"] = edf.apply(_e_amount, axis=1)
    edf["_e_party"] = edf.apply(_e_party, axis=1)
    edf["_e_date"] = edf.apply(_e_date, axis=1)

    b_pan = bdf["pan"].astype(str).str.strip().str.upper() if "pan" in bdf.columns else pd.Series("", index=bdf.index)
    b_section = bdf["section"].astype(str).str.strip() if "section" in bdf.columns else pd.Series("", index=bdf.index)
    b_amount = bdf["tax_deducted"].apply(_safe_float) if "tax_deducted" in bdf.columns else (
        bdf["amount_paid_credited"].apply(_safe_float) if "amount_paid_credited" in bdf.columns else pd.Series(0.0, index=bdf.index)
    )
    b_party = bdf["deductee_name"].astype(str).str.strip() if "deductee_name" in bdf.columns else pd.Series("", index=bdf.index)
    b_date = bdf["deposit_date"].astype(str).str.strip() if "deposit_date" in bdf.columns else pd.Series("", index=bdf.index)

    bdf["_b_pan"] = b_pan
    bdf["_b_section"] = b_section
    bdf["_b_amount"] = b_amount
    bdf["_b_party"] = b_party
    bdf["_b_date"] = b_date

    # --- Tier 1: PAN + section exact ---
    for _, erow in edf.iterrows():
        eidx = erow["_eidx"]
        if eidx in matched_e:
            continue
        ep, es = erow["_e_pan"], erow["_e_section"]
        if not ep or not es:
            continue
        candidates = bdf[
            (~bdf["_bidx"].isin(matched_b))
            & (bdf["_b_pan"] == ep)
            & (bdf["_b_section"] == es)
        ]
        if not candidates.empty:
            brow = candidates.iloc[0]
            pairs.append(_make_pair(erow, brow, 1))
            matched_e.add(eidx)
            matched_b.add(brow["_bidx"])

    # --- Tier 2: PAN + section + amount tolerance ---
    for _, erow in edf.iterrows():
        eidx = erow["_eidx"]
        if eidx in matched_e:
            continue
        ep, es, ea = erow["_e_pan"], erow["_e_section"], erow["_e_amount"]
        if not ep:
            continue
        candidates = bdf[
            (~bdf["_bidx"].isin(matched_b))
            & (bdf["_b_pan"] == ep)
        ]
        for _, brow in candidates.iterrows():
            if brow["_bidx"] in matched_b:
                continue
            if _within_amount_tolerance(ea, brow["_b_amount"], cfg):
                pairs.append(_make_pair(erow, brow, 2))
                matched_e.add(eidx)
                matched_b.add(brow["_bidx"])
                break

    # --- Tier 3: name fuzzy + amount + date ---
    fuzzy_thresh = float(cfg.get("fuzzy_name_threshold", 80))
    date_tol = int(cfg.get("date_tolerance_days", 7))
    for _, erow in edf.iterrows():
        eidx = erow["_eidx"]
        if eidx in matched_e:
            continue
        ep_name, ea, ed = erow["_e_party"], erow["_e_amount"], erow["_e_date"]
        if not ep_name:
            continue
        for _, brow in bdf.iterrows():
            if brow["_bidx"] in matched_b:
                continue
            if not _within_amount_tolerance(ea, brow["_b_amount"], cfg):
                continue
            if fuzz.token_sort_ratio(ep_name, brow["_b_party"]) < fuzzy_thresh:
                continue
            if ed and brow["_b_date"]:
                try:
                    diff = abs((pd.Timestamp(ed) - pd.Timestamp(brow["_b_date"])).days)
                    if diff > date_tol:
                        continue
                except Exception:
                    pass
            pairs.append(_make_pair(erow, brow, 3))
            matched_e.add(eidx)
            matched_b.add(brow["_bidx"])
            break

    return pairs, matched_e, matched_b


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pair(erow: pd.Series, brow: pd.Series, tier: int) -> dict[str, Any]:
    """Build a single aligned-pair record."""
    pair: dict[str, Any] = {}
    # Engine side
    for col in erow.index:
        if col.startswith("_"):
            continue
        pair[f"engine_{col}"] = erow[col]
    # Baseline side
    for col in brow.index:
        if col.startswith("_"):
            continue
        pair[f"baseline_{col}"] = brow[col]
    pair["alignment_tier"] = tier
    return pair
