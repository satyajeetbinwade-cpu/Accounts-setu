"""TDS matching engine — deductor-side and deductee-side reconciliation.

Disposable PoC. Reads config-driven rates/thresholds from matching_rules.yaml.
No rate or threshold value appears in this source file.

Design decisions driven by how TDS differs from GST:
  (a) Granularity mismatch: supports many-to-one and one-to-many matching
      via aggregated passes that group by PAN + section + quarter.
  (b) No invoice number: matching is inherently weaker; confidence baselines
      sit below GST equivalents.
  (c) Timing is first-class: cross-quarter matching with explicit flagging.
  (d) Expected value is computable: section-rate validation detects short/excess
      deduction, wrong section, threshold crossings.

Known limitations — documented, not solved:
  - Lower/nil deduction certificates under 197 are not modelled; entries under
    a certificate will appear as rate mismatches.
  - Section 206AB higher-rate treatment for non-filers is not modelled.
  - Cross-financial-year timing differences are not handled (only adjacent
    quarters within the period supplied).
  - Aggregated matching assumes portal aggregation follows PAN + section +
    quarter; other aggregation patterns will not match.
  - Salary TDS (192) is computed on estimated annual liability and will not
    validate meaningfully against the flat-rate check.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import pandas as pd
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLASSIFICATIONS = ("Matched", "Not in Books", "Not in Portal", "Amount Difference")
DIFFERENCE_TYPES = (
    "Short Deduction",
    "Excess Deduction",
    "Rate Mismatch",
    "Section Mismatch",
    "Timing Difference",
    "Rounding",
    "Unexplained",
)

# PAN format: 5 alpha + 4 digit + 1 alpha
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------

def _quarter_for_date(date_str: str | None) -> str | None:
    """Return fiscal quarter label like 'Q1-2026' from a YYYY-MM-DD string.

    Fiscal quarters (Indian):
      Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar.
    """
    if not date_str or pd.isna(date_str) or str(date_str) == "NaT":
        return None
    try:
        dt = pd.Timestamp(date_str)
    except Exception:
        return None
    month = dt.month
    year = dt.year
    if month <= 3:
        return f"Q4-{year - 1}"
    elif month <= 6:
        return f"Q1-{year}"
    elif month <= 9:
        return f"Q2-{year}"
    else:
        return f"Q3-{year}"


def _adjacent_quarters(quarter: str | None) -> list[str]:
    """Return the two adjacent quarters (previous and next)."""
    # `quarter` may be None (no deposit date), or a float NaN that pandas
    # produced by normalising a None in a mixed column. bool(float('nan'))
    # is True, so a bare `if not quarter` would not catch it and re.match
    # would raise TypeError on the float — guard both cases explicitly.
    if quarter is None or (isinstance(quarter, float) and pd.isna(quarter)):
        return []
    if not isinstance(quarter, str):
        return []
    if not quarter:
        return []
    m = re.match(r"Q(\d)-(\d{4})", quarter)
    if not m:
        return []
    q, y = int(m.group(1)), int(m.group(2))
    result = []
    # previous
    if q == 1:
        result.append(f"Q4-{y - 1}")
    else:
        result.append(f"Q{q - 1}-{y}")
    # next
    if q == 4:
        result.append(f"Q1-{y + 1}")
    else:
        result.append(f"Q{q + 1}-{y}")
    return result


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_section_entry(config: dict, section: str) -> dict:
    """Retrieve a section table entry. Fail loudly if not in config."""
    table = config.get("section_table", {})
    entry = table.get(section)
    if entry is None:
        raise KeyError(
            f"Section {section!r} not found in config section_table. "
            f"Available sections: {sorted(table.keys())}. "
            f"Add this section to config/matching_rules.yaml before running."
        )
    return entry


def _get_applicable_rate(section_entry: dict) -> float:
    """Return the first / default rate for a section entry.

    For sections with rate variants, returns the 'default' rate if present,
    otherwise the first rate listed. A human refining the config can adjust.
    """
    rates = section_entry.get("rates", {})
    if "default" in rates:
        return float(rates["default"])
    if rates:
        return float(next(iter(rates.values())))
    return 0.0


def _get_all_rates(section_entry: dict) -> list[float]:
    """Return all rate variants for a section."""
    rates = section_entry.get("rates", {})
    return [float(v) for v in rates.values()]


# ---------------------------------------------------------------------------
# Amount comparison helpers
# ---------------------------------------------------------------------------

def _amounts_within_tolerance(a: float, b: float, config: dict) -> bool:
    tol = config["amount_tolerance"]
    diff = abs(a - b)
    if diff <= float(tol["absolute"]):
        return True
    if b != 0 and (diff / abs(b) * 100) <= float(tol["percent"]):
        return True
    return False


def _is_rounding_difference(a: float, b: float, config: dict) -> bool:
    diff = abs(a - b)
    return 0 < diff <= float(config["rounding_tolerance"])


def _date_within_tolerance(d1: str | None, d2: str | None, config: dict) -> bool:
    if not d1 or not d2 or pd.isna(d1) or pd.isna(d2):
        return True  # missing date → don't penalise
    try:
        dt1, dt2 = pd.Timestamp(d1), pd.Timestamp(d2)
        return abs((dt1 - dt2).days) <= int(config["date_tolerance_days"])
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _score(baseline: float, config: dict, *, proximity_factor: float = 1.0,
           cross_quarter: bool = False, group_size: int = 1) -> float:
    """Deterministic confidence score with deductions."""
    baselines = config["confidence_baselines"]
    s = baseline
    # proximity_factor: 1.0 = exact match, 0.0 = at tolerance edge
    s *= proximity_factor
    if cross_quarter:
        s += float(baselines["cross_quarter"])  # negative value = penalty
    if group_size > 1:
        penalty = float(config.get("aggregated_group_size_penalty", 2)) * (group_size - 1)
        s -= penalty
    return max(0.0, min(100.0, round(s, 2)))


def _band(score: float, config: dict) -> str:
    thresholds = config["confidence_thresholds"]
    if score >= float(thresholds["high"]):
        return "High"
    if score >= float(thresholds["medium"]):
        return "Medium"
    return "Low"


def _proximity(actual_diff: float, tolerance: float) -> float:
    """1.0 when exact, approaching 0.0 at the tolerance edge."""
    if tolerance <= 0:
        return 1.0
    return max(0.0, 1.0 - actual_diff / tolerance)


# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------

def _fmt_amount(v: float) -> str:
    """Indian-convention amount string."""
    return f"₹{v:,.2f}"


def _row_to_json(row: pd.Series) -> str:
    d = {}
    for k, v in row.items():
        if pd.isna(v):
            d[k] = None
        else:
            d[k] = v
    return json.dumps(d, default=str)


def _make_result(
    classification: str,
    confidence: float,
    config: dict,
    *,
    books_record: str | None = None,
    portal_record: str | None = None,
    match_reason: str,
    difference_type: str | None = None,
    matched_record_ids: list | None = None,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "confidence_score": confidence,
        "confidence_band": _band(confidence, config),
        "books_record": books_record,
        "portal_record": portal_record,
        "match_reason": match_reason,
        "difference_type": difference_type,
        "matched_record_ids": matched_record_ids,
    }


# ---------------------------------------------------------------------------
# Deductor-side matching
# ---------------------------------------------------------------------------

def match_tds_deductor(
    books_df: pd.DataFrame, portal_df: pd.DataFrame, config: dict
) -> list[dict[str, Any]]:
    """Deductor-side TDS reconciliation: books TDS payable vs TDS return/challan.

    Four passes, each operating on records unmatched by earlier passes:
      Pass 1 — Challan-anchored (strongest key)
      Pass 2 — Deductee-level (PAN + section + amount + date)
      Pass 3 — Aggregated (PAN + section + quarter group sums)
      Pass 4 — Fuzzy (name + section + amount)
    """
    results: list[dict[str, Any]] = []
    baselines = config["confidence_baselines"]

    books = books_df.copy()
    portal = portal_df.copy()

    # Add index-based IDs for tracking
    books["_bid"] = range(len(books))
    portal["_pid"] = range(len(portal))

    # Pre-compute quarters
    books["_quarter"] = books["deposit_date"].apply(_quarter_for_date)
    portal["_quarter"] = portal["deposit_date"].apply(_quarter_for_date)

    matched_bids: set[int] = set()
    matched_pids: set[int] = set()

    # --- Pass 1: Challan-anchored ---
    if "challan_number" in books.columns and "challan_number" in portal.columns:
        for _, pr in portal.iterrows():
            pid = pr["_pid"]
            if pid in matched_pids:
                continue
            ch = str(pr.get("challan_number", "")).strip()
            if not ch or ch == "None" or ch == "nan":
                continue
            candidates = books[
                (~books["_bid"].isin(matched_bids))
                & (books["challan_number"].astype(str).str.strip() == ch)
            ]
            if candidates.empty:
                continue
            for _, br in candidates.iterrows():
                bid = br["_bid"]
                if bid in matched_bids:
                    continue
                date_ok = _date_within_tolerance(
                    br.get("deposit_date"), pr.get("deposit_date"), config
                )
                if not date_ok:
                    continue
                tax_b = float(br.get("tax_deducted", 0) or 0)
                tax_p = float(pr.get("tax_deducted", 0) or 0)
                diff = abs(tax_b - tax_p)
                prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
                score = _score(float(baselines["challan"]), config, proximity_factor=prox)

                if _amounts_within_tolerance(tax_b, tax_p, config):
                    if _is_rounding_difference(tax_b, tax_p, config):
                        cls, dt = "Amount Difference", "Rounding"
                        reason = (
                            f"Matched on challan {ch}; rounding difference of "
                            f"{_fmt_amount(diff)} (books {_fmt_amount(tax_b)} vs portal {_fmt_amount(tax_p)})."
                        )
                    else:
                        cls, dt = "Matched", None
                        reason = (
                            f"Matched on challan {ch}"
                            f"{' (deposit ' + str(pr.get('deposit_date', '')) + ')' if pr.get('deposit_date') else ''}"
                            f"; books and portal agree at {_fmt_amount(tax_b)}."
                        )
                else:
                    cls, dt = "Amount Difference", _classify_amount_diff(tax_b, tax_p, br, config)
                    reason = (
                        f"Matched on challan {ch} but amount differs: books {_fmt_amount(tax_b)} "
                        f"vs portal {_fmt_amount(tax_p)}, difference {_fmt_amount(diff)}."
                    )
                results.append(_make_result(
                    cls, score, config,
                    books_record=_row_to_json(br.drop(["_bid", "_quarter"])),
                    portal_record=_row_to_json(pr.drop(["_pid", "_quarter"])),
                    match_reason=reason, difference_type=dt,
                ))
                matched_bids.add(bid)
                matched_pids.add(pid)
                break

    # --- Pass 2: Deductee-level (PAN + section + amount + date) ---
    for _, pr in portal.iterrows():
        pid = pr["_pid"]
        if pid in matched_pids:
            continue
        pan_p = str(pr.get("pan", "")).strip().upper()
        sec_p = str(pr.get("section", "")).strip()
        tax_p = float(pr.get("tax_deducted", 0) or 0)

        candidates = books[
            (~books["_bid"].isin(matched_bids))
            & (books["pan"].astype(str).str.strip().str.upper() == pan_p)
            & (books["section"].astype(str).str.strip() == sec_p)
        ]
        for _, br in candidates.iterrows():
            bid = br["_bid"]
            if bid in matched_bids:
                continue
            tax_b = float(br.get("tax_deducted", 0) or 0)
            if not _amounts_within_tolerance(tax_b, tax_p, config):
                continue
            if not _date_within_tolerance(
                br.get("deposit_date"), pr.get("deposit_date"), config
            ):
                continue
            diff = abs(tax_b - tax_p)
            prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
            score = _score(float(baselines["deductee_level"]), config, proximity_factor=prox)

            if _is_rounding_difference(tax_b, tax_p, config):
                cls, dt = "Amount Difference", "Rounding"
                reason = (
                    f"Deductee-level match: PAN {pan_p}, section {sec_p}; rounding difference "
                    f"{_fmt_amount(diff)} (books {_fmt_amount(tax_b)} vs portal {_fmt_amount(tax_p)})."
                )
            elif diff == 0:
                cls, dt = "Matched", None
                reason = (
                    f"Deductee-level match: PAN {pan_p}, section {sec_p}; "
                    f"books and portal agree at {_fmt_amount(tax_b)}."
                )
            else:
                cls, dt = "Amount Difference", _classify_amount_diff(tax_b, tax_p, br, config)
                reason = (
                    f"Deductee-level match: PAN {pan_p}, section {sec_p}; amount differs: "
                    f"books {_fmt_amount(tax_b)} vs portal {_fmt_amount(tax_p)}, difference {_fmt_amount(diff)}."
                )
            results.append(_make_result(
                cls, score, config,
                books_record=_row_to_json(br.drop(["_bid", "_quarter"])),
                portal_record=_row_to_json(pr.drop(["_pid", "_quarter"])),
                match_reason=reason, difference_type=dt,
            ))
            matched_bids.add(bid)
            matched_pids.add(pid)
            break

    # --- Pass 3: Aggregated (PAN + section + quarter group sums) ---
    _run_aggregated_pass_deductor(
        books, portal, config, matched_bids, matched_pids, results
    )

    # --- Pass 4: Fuzzy (name + section + amount) ---
    _run_fuzzy_pass(
        books, portal, config, matched_bids, matched_pids, results,
        name_field_books="deductee_name", name_field_portal="deductee_name",
        pass_label="Fuzzy deductor",
    )

    # --- Unmatched residuals ---
    _emit_unmatched(
        books, portal, config, matched_bids, matched_pids, results,
        side_label="deductor",
    )

    return results


def _run_aggregated_pass_deductor(
    books: pd.DataFrame, portal: pd.DataFrame, config: dict,
    matched_bids: set, matched_pids: set, results: list,
) -> None:
    baselines = config["confidence_baselines"]
    unmatched_books = books[~books["_bid"].isin(matched_bids)].copy()
    unmatched_portal = portal[~portal["_pid"].isin(matched_pids)].copy()

    if unmatched_books.empty or unmatched_portal.empty:
        return

    # Group books by PAN + section + quarter
    book_groups = (
        unmatched_books
        .groupby(["pan", "section", "_quarter"], dropna=False)
    )

    for (pan, section, quarter), b_group in book_groups:
        if pd.isna(pan) or pd.isna(section):
            continue
        pan = str(pan).strip().upper()
        section = str(section).strip()
        quarter = str(quarter) if not pd.isna(quarter) else None

        total_tax_books = b_group["tax_deducted"].astype(float).sum()
        bids_in_group = list(b_group["_bid"])
        group_size = len(bids_in_group)

        # Find portal records with same PAN + section + quarter
        p_candidates = unmatched_portal[
            (~unmatched_portal["_pid"].isin(matched_pids))
            & (unmatched_portal["pan"].astype(str).str.strip().str.upper() == pan)
            & (unmatched_portal["section"].astype(str).str.strip() == section)
            & (unmatched_portal["_quarter"] == quarter)
        ]
        if p_candidates.empty:
            continue

        total_tax_portal = p_candidates["tax_deducted"].astype(float).sum()
        pids_in_group = list(p_candidates["_pid"])

        if not _amounts_within_tolerance(total_tax_books, total_tax_portal, config):
            continue

        diff = abs(total_tax_books - total_tax_portal)
        prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
        score = _score(
            float(baselines["aggregated"]), config,
            proximity_factor=prox, group_size=group_size,
        )

        if _is_rounding_difference(total_tax_books, total_tax_portal, config) and diff > 0:
            cls, dt = "Amount Difference", "Rounding"
        elif diff == 0:
            cls, dt = "Matched", None
        else:
            cls, dt = "Amount Difference", "Unexplained"

        reason = (
            f"Aggregated match: {group_size} book entries for PAN {pan} under {section} "
            f"in {quarter or 'unknown quarter'} total {_fmt_amount(total_tax_books)}, "
            f"matching {len(pids_in_group)} portal line(s) of {_fmt_amount(total_tax_portal)}."
        )
        if diff > 0:
            reason += f" Difference: {_fmt_amount(diff)}."

        # Books record = first book entry (representative); portal record = first portal entry
        first_book = b_group.iloc[0]
        first_portal = p_candidates.iloc[0]
        results.append(_make_result(
            cls, score, config,
            books_record=_row_to_json(first_book.drop(["_bid", "_quarter"])),
            portal_record=_row_to_json(first_portal.drop(["_pid", "_quarter"])),
            match_reason=reason, difference_type=dt,
            matched_record_ids=bids_in_group + pids_in_group,
        ))
        matched_bids.update(bids_in_group)
        matched_pids.update(pids_in_group)


# ---------------------------------------------------------------------------
# Deductee-side matching
# ---------------------------------------------------------------------------

def match_tds_deductee(
    books_df: pd.DataFrame, portal_df: pd.DataFrame, config: dict
) -> list[dict[str, Any]]:
    """Deductee-side TDS reconciliation: TDS receivable in books vs 26AS credit.

    Four passes:
      Pass 1 — Deductor PAN + section + amount + quarter
      Pass 2 — Aggregated (group books by deductor PAN + section + quarter)
      Pass 3 — Cross-quarter (retry Pass 1 & 2 against adjacent quarters)
      Pass 4 — Fuzzy (deductor name + amount)
    """
    results: list[dict[str, Any]] = []
    baselines = config["confidence_baselines"]

    books = books_df.copy()
    portal = portal_df.copy()

    books["_bid"] = range(len(books))
    portal["_pid"] = range(len(portal))

    books["_quarter"] = books["deposit_date"].apply(_quarter_for_date)
    portal["_quarter"] = portal["deposit_date"].apply(_quarter_for_date)

    matched_bids: set[int] = set()
    matched_pids: set[int] = set()

    # --- Pass 1: Deductor PAN + section + amount + quarter ---
    _run_deductee_exact_pass(
        books, portal, config, matched_bids, matched_pids, results,
        cross_quarter=False,
    )

    # --- Pass 2: Aggregated ---
    _run_aggregated_pass_deductee(
        books, portal, config, matched_bids, matched_pids, results,
        cross_quarter=False,
    )

    # --- Pass 3: Cross-quarter (retry Pass 1 + 2 against adjacent quarters) ---
    if config.get("quarter_boundary_matching", True):
        _run_deductee_exact_pass(
            books, portal, config, matched_bids, matched_pids, results,
            cross_quarter=True,
        )
        _run_aggregated_pass_deductee(
            books, portal, config, matched_bids, matched_pids, results,
            cross_quarter=True,
        )

    # --- Pass 4: Fuzzy ---
    _run_fuzzy_pass(
        books, portal, config, matched_bids, matched_pids, results,
        name_field_books="deductee_name", name_field_portal="deductee_name",
        pass_label="Fuzzy deductee",
    )

    # --- Unmatched residuals ---
    _emit_unmatched(
        books, portal, config, matched_bids, matched_pids, results,
        side_label="deductee",
    )

    return results


def _run_deductee_exact_pass(
    books: pd.DataFrame, portal: pd.DataFrame, config: dict,
    matched_bids: set, matched_pids: set, results: list,
    *, cross_quarter: bool,
) -> None:
    baselines = config["confidence_baselines"]
    for _, br in books.iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        pan_b = str(br.get("pan", "")).strip().upper()
        sec_b = str(br.get("section", "")).strip()
        tax_b = float(br.get("tax_deducted", 0) or 0)
        q_b = br.get("_quarter")

        if cross_quarter:
            quarters_to_check = _adjacent_quarters(q_b)
        else:
            quarters_to_check = [q_b] if q_b else []

        for q_target in quarters_to_check:
            candidates = portal[
                (~portal["_pid"].isin(matched_pids))
                & (portal["pan"].astype(str).str.strip().str.upper() == pan_b)
                & (portal["section"].astype(str).str.strip() == sec_b)
                & (portal["_quarter"] == q_target)
            ]
            for _, pr in candidates.iterrows():
                pid = pr["_pid"]
                if pid in matched_pids:
                    continue
                tax_p = float(pr.get("tax_deducted", 0) or 0)
                if not _amounts_within_tolerance(tax_b, tax_p, config):
                    continue
                diff = abs(tax_b - tax_p)
                prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
                base = float(baselines["deductee_level"])
                score = _score(
                    base, config, proximity_factor=prox, cross_quarter=cross_quarter,
                )

                if cross_quarter:
                    dt = "Timing Difference"
                    cls = "Matched"
                    reason = (
                        f"Timing difference: book entry in {q_b} matched to portal entry in "
                        f"{q_target} — PAN {pan_b}, section {sec_b}, "
                        f"books {_fmt_amount(tax_b)}, portal {_fmt_amount(tax_p)}."
                    )
                    if diff > 0:
                        reason += f" Amount difference: {_fmt_amount(diff)}."
                elif _is_rounding_difference(tax_b, tax_p, config) and diff > 0:
                    cls, dt = "Amount Difference", "Rounding"
                    reason = (
                        f"Deductee match: PAN {pan_b}, section {sec_b}, quarter {q_b}; "
                        f"rounding difference {_fmt_amount(diff)}."
                    )
                elif diff == 0:
                    cls, dt = "Matched", None
                    reason = (
                        f"Deductee match: PAN {pan_b}, section {sec_b}, quarter {q_b}; "
                        f"books and portal agree at {_fmt_amount(tax_b)}."
                    )
                else:
                    cls = "Amount Difference"
                    dt = _classify_amount_diff(tax_b, tax_p, br, config)
                    reason = (
                        f"Deductee match: PAN {pan_b}, section {sec_b}, quarter {q_b}; "
                        f"amount differs: books {_fmt_amount(tax_b)} vs portal {_fmt_amount(tax_p)}."
                    )

                results.append(_make_result(
                    cls, score, config,
                    books_record=_row_to_json(br.drop(["_bid", "_quarter"])),
                    portal_record=_row_to_json(pr.drop(["_pid", "_quarter"])),
                    match_reason=reason, difference_type=dt,
                ))
                matched_bids.add(bid)
                matched_pids.add(pid)
                break
            if bid in matched_bids:
                break


def _run_aggregated_pass_deductee(
    books: pd.DataFrame, portal: pd.DataFrame, config: dict,
    matched_bids: set, matched_pids: set, results: list,
    *, cross_quarter: bool,
) -> None:
    baselines = config["confidence_baselines"]
    unmatched_books = books[~books["_bid"].isin(matched_bids)].copy()
    unmatched_portal = portal[~portal["_pid"].isin(matched_pids)].copy()
    if unmatched_books.empty or unmatched_portal.empty:
        return

    book_groups = unmatched_books.groupby(["pan", "section", "_quarter"], dropna=False)

    for (pan, section, quarter), b_group in book_groups:
        if pd.isna(pan) or pd.isna(section):
            continue
        pan = str(pan).strip().upper()
        section = str(section).strip()
        quarter = str(quarter) if not pd.isna(quarter) else None

        total_tax_books = b_group["tax_deducted"].astype(float).sum()
        bids_in_group = list(b_group["_bid"])
        group_size = len(bids_in_group)

        if cross_quarter:
            quarters_to_check = _adjacent_quarters(quarter)
        else:
            quarters_to_check = [quarter] if quarter else []

        for q_target in quarters_to_check:
            p_candidates = unmatched_portal[
                (~unmatched_portal["_pid"].isin(matched_pids))
                & (unmatched_portal["pan"].astype(str).str.strip().str.upper() == pan)
                & (unmatched_portal["section"].astype(str).str.strip() == section)
                & (unmatched_portal["_quarter"] == q_target)
            ]
            if p_candidates.empty:
                continue

            total_tax_portal = p_candidates["tax_deducted"].astype(float).sum()
            pids_in_group = list(p_candidates["_pid"])

            if not _amounts_within_tolerance(total_tax_books, total_tax_portal, config):
                continue

            diff = abs(total_tax_books - total_tax_portal)
            prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
            score = _score(
                float(baselines["aggregated"]), config,
                proximity_factor=prox, group_size=group_size,
                cross_quarter=cross_quarter,
            )

            if cross_quarter:
                dt = "Timing Difference"
                cls = "Matched"
                reason = (
                    f"Aggregated timing difference: {group_size} book entries for PAN {pan} "
                    f"under {section} in {quarter or 'unknown'} total {_fmt_amount(total_tax_books)}, "
                    f"matching {len(pids_in_group)} portal line(s) of {_fmt_amount(total_tax_portal)} "
                    f"in {q_target}."
                )
            elif _is_rounding_difference(total_tax_books, total_tax_portal, config) and diff > 0:
                cls, dt = "Amount Difference", "Rounding"
                reason = (
                    f"Aggregated match: {group_size} book entries for PAN {pan} under {section} "
                    f"in {quarter or 'unknown quarter'} total {_fmt_amount(total_tax_books)}, "
                    f"rounding difference {_fmt_amount(diff)} vs portal {_fmt_amount(total_tax_portal)}."
                )
            elif diff == 0:
                cls, dt = "Matched", None
                reason = (
                    f"Aggregated match: {group_size} book entries for PAN {pan} under {section} "
                    f"in {quarter or 'unknown quarter'} total {_fmt_amount(total_tax_books)}, "
                    f"matching {len(pids_in_group)} portal line(s) of {_fmt_amount(total_tax_portal)}."
                )
            else:
                cls, dt = "Amount Difference", "Unexplained"
                reason = (
                    f"Aggregated match: {group_size} book entries for PAN {pan} under {section} "
                    f"in {quarter or 'unknown quarter'} total {_fmt_amount(total_tax_books)}, "
                    f"vs portal {_fmt_amount(total_tax_portal)}, difference {_fmt_amount(diff)}."
                )

            first_book = b_group.iloc[0]
            first_portal = p_candidates.iloc[0]
            results.append(_make_result(
                cls, score, config,
                books_record=_row_to_json(first_book.drop(["_bid", "_quarter"])),
                portal_record=_row_to_json(first_portal.drop(["_pid", "_quarter"])),
                match_reason=reason, difference_type=dt,
                matched_record_ids=bids_in_group + pids_in_group,
            ))
            matched_bids.update(bids_in_group)
            matched_pids.update(pids_in_group)
            break


# ---------------------------------------------------------------------------
# Shared fuzzy pass
# ---------------------------------------------------------------------------

def _run_fuzzy_pass(
    books: pd.DataFrame, portal: pd.DataFrame, config: dict,
    matched_bids: set, matched_pids: set, results: list,
    *, name_field_books: str, name_field_portal: str, pass_label: str,
) -> None:
    """Fuzzy name + section + amount. Never matches on name alone."""
    baselines = config["confidence_baselines"]
    threshold = float(config["fuzzy_threshold"])
    for _, br in books.iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        name_b = str(br.get(name_field_books, "")).strip()
        sec_b = str(br.get("section", "")).strip()
        tax_b = float(br.get("tax_deducted", 0) or 0)
        if not name_b or not sec_b:
            continue

        best_score = -1.0
        best_pr = None
        best_pid = None
        best_fuzz_score = 0

        for _, pr in portal.iterrows():
            pid = pr["_pid"]
            if pid in matched_pids:
                continue
            sec_p = str(pr.get("section", "")).strip()
            if sec_p != sec_b:
                continue
            tax_p = float(pr.get("tax_deducted", 0) or 0)
            if not _amounts_within_tolerance(tax_b, tax_p, config):
                continue
            name_p = str(pr.get(name_field_portal, "")).strip()
            fuzz_score = fuzz.token_sort_ratio(name_b.lower(), name_p.lower())
            if fuzz_score < threshold:
                continue
            diff = abs(tax_b - tax_p)
            prox = _proximity(diff, float(config["amount_tolerance"]["absolute"]))
            candidate_score = _score(
                float(baselines["fuzzy"]), config, proximity_factor=prox,
            )
            if candidate_score > best_score:
                best_score = candidate_score
                best_pr = pr
                best_pid = pid
                best_fuzz_score = fuzz_score

        if best_pr is not None:
            pr = best_pr
            tax_p = float(pr.get("tax_deducted", 0) or 0)
            diff = abs(tax_b - tax_p)
            if diff == 0:
                cls, dt = "Matched", None
            elif _is_rounding_difference(tax_b, tax_p, config):
                cls, dt = "Amount Difference", "Rounding"
            else:
                cls, dt = "Amount Difference", _classify_amount_diff(tax_b, tax_p, br, config)

            reason = (
                f"{pass_label} match: name similarity {best_fuzz_score:.0f}% "
                f"('{name_b}' ↔ '{str(pr.get(name_field_portal, '')).strip()}'), "
                f"section {sec_b}, books {_fmt_amount(tax_b)} vs portal {_fmt_amount(tax_p)}."
            )
            if diff > 0:
                reason += f" Difference: {_fmt_amount(diff)}."

            results.append(_make_result(
                cls, best_score, config,
                books_record=_row_to_json(br.drop(["_bid", "_quarter"])),
                portal_record=_row_to_json(pr.drop(["_pid", "_quarter"])),
                match_reason=reason, difference_type=dt,
            ))
            matched_bids.add(bid)
            matched_pids.add(best_pid)


# ---------------------------------------------------------------------------
# Unmatched residuals
# ---------------------------------------------------------------------------

def _emit_unmatched(
    books: pd.DataFrame, portal: pd.DataFrame, config: dict,
    matched_bids: set, matched_pids: set, results: list,
    *, side_label: str,
) -> None:
    baselines = config["confidence_baselines"]
    # Unmatched books → "Not in Portal"
    for _, br in books.iterrows():
        bid = br["_bid"]
        if bid in matched_bids:
            continue
        pan = str(br.get("pan", "")).strip().upper()
        sec = str(br.get("section", "")).strip()
        tax = float(br.get("tax_deducted", 0) or 0)
        reason = (
            f"Not in Portal: PAN {pan}, {sec}, {_fmt_amount(tax)} — "
            f"no challan, deductee-level, aggregated or fuzzy candidate found in any pass."
        )
        results.append(_make_result(
            "Not in Portal", 0.0, config,
            books_record=_row_to_json(br.drop(["_bid", "_quarter"])),
            portal_record=None,
            match_reason=reason,
        ))
        matched_bids.add(bid)

    # Unmatched portal → "Not in Books"
    for _, pr in portal.iterrows():
        pid = pr["_pid"]
        if pid in matched_pids:
            continue
        pan = str(pr.get("pan", "")).strip().upper()
        sec = str(pr.get("section", "")).strip()
        tax = float(pr.get("tax_deducted", 0) or 0)
        reason = (
            f"Not in Books: PAN {pan}, {sec}, {_fmt_amount(tax)} — "
            f"no matching book entry found across any pass."
        )
        results.append(_make_result(
            "Not in Books", 0.0, config,
            books_record=None,
            portal_record=_row_to_json(pr.drop(["_pid", "_quarter"])),
            match_reason=reason,
        ))
        matched_pids.add(pid)


# ---------------------------------------------------------------------------
# Amount-difference sub-classification
# ---------------------------------------------------------------------------

def _classify_amount_diff(
    tax_books: float, tax_portal: float, book_row: pd.Series, config: dict
) -> str:
    """Determine the specific difference_type for an Amount Difference."""
    section = str(book_row.get("section", "")).strip()
    amount = float(book_row.get("amount_paid_credited", 0) or 0)

    # Try rate-based classification
    try:
        sec_entry = _get_section_entry(config, section)
        all_rates = _get_all_rates(sec_entry)
        if all_rates and amount > 0:
            expected_taxes = [amount * r / 100.0 for r in all_rates]
            # Check if portal matches any expected rate but books don't
            books_matches_any = any(
                abs(tax_books - et) <= float(config["rounding_tolerance"])
                for et in expected_taxes
            )
            portal_matches_any = any(
                abs(tax_portal - et) <= float(config["rounding_tolerance"])
                for et in expected_taxes
            )
            if not books_matches_any and not portal_matches_any:
                return "Rate Mismatch"
    except KeyError:
        pass

    if _is_rounding_difference(tax_books, tax_portal, config):
        return "Rounding"

    if tax_books < tax_portal:
        return "Short Deduction"
    if tax_books > tax_portal:
        return "Excess Deduction"
    return "Unexplained"


# ---------------------------------------------------------------------------
# Section / rate / threshold validation (books-internal)
# ---------------------------------------------------------------------------

def validate_section_rates(
    books_df: pd.DataFrame, config: dict
) -> list[dict[str, Any]]:
    """Validate books records against the section table. Books-internal only.

    Checks:
      1. Expected tax = amount × section rate; flags short/excess deduction.
      2. Threshold crossings: cumulative per PAN + section vs aggregate threshold,
         single transaction vs single-transaction threshold.
      3. Missing/invalid PAN without higher no-PAN rate applied.

    Returns result dicts with portal_record=None and populated difference_type.
    """
    results: list[dict[str, Any]] = []
    baselines = config["confidence_baselines"]
    rounding_tol = float(config["rounding_tolerance"])
    no_pan_rate = float(config["no_pan_rate"])

    books = books_df.copy()
    books["_bid"] = range(len(books))

    # --- Per-entry rate check ---
    for _, row in books.iterrows():
        section = str(row.get("section", "")).strip()
        amount = float(row.get("amount_paid_credited", 0) or 0)
        tax_deducted = float(row.get("tax_deducted", 0) or 0)
        pan = str(row.get("pan", "")).strip().upper()

        if not section or amount == 0:
            continue

        try:
            sec_entry = _get_section_entry(config, section)
        except KeyError:
            # Unknown section — can't validate
            continue

        all_rates = _get_all_rates(sec_entry)
        if not all_rates:
            continue

        # Skip salary TDS — not meaningfully validatable
        if section == "192":
            continue

        # Check each possible rate
        expected_taxes = [(r, amount * r / 100.0) for r in all_rates]
        closest_rate, closest_expected = min(
            expected_taxes, key=lambda x: abs(x[1] - tax_deducted)
        )
        diff = tax_deducted - closest_expected

        if abs(diff) <= rounding_tol:
            continue  # Within rounding — not an exception

        score = _score(float(baselines["section_validation"]), config)

        if abs(diff) > rounding_tol:
            # Check if any other rate would explain the deduction
            other_rate_match = any(
                abs(tax_deducted - et) <= rounding_tol
                for r, et in expected_taxes if r != closest_rate
            )
            if other_rate_match:
                # The deduction matches a different rate variant — could be correct
                # but section rates entry may need a different variant
                continue

            if diff < 0:
                dt = "Short Deduction"
                reason = (
                    f"Short Deduction: {_fmt_amount(amount)} under {section} should attract "
                    f"{_fmt_amount(closest_expected)} at {closest_rate}%; books show "
                    f"{_fmt_amount(tax_deducted)} deducted — shortfall {_fmt_amount(abs(diff))}."
                )
            else:
                dt = "Excess Deduction"
                reason = (
                    f"Excess Deduction: {_fmt_amount(amount)} under {section} should attract "
                    f"{_fmt_amount(closest_expected)} at {closest_rate}%; books show "
                    f"{_fmt_amount(tax_deducted)} deducted — excess {_fmt_amount(abs(diff))}."
                )

            results.append(_make_result(
                "Amount Difference", score, config,
                books_record=_row_to_json(row.drop(["_bid"])),
                portal_record=None,
                match_reason=reason, difference_type=dt,
            ))

    # --- Missing / invalid PAN check ---
    for _, row in books.iterrows():
        pan = str(row.get("pan", "")).strip().upper()
        section = str(row.get("section", "")).strip()
        amount = float(row.get("amount_paid_credited", 0) or 0)
        tax_deducted = float(row.get("tax_deducted", 0) or 0)

        if not section or amount == 0:
            continue

        pan_missing_or_invalid = (not pan or pan == "NONE" or pan == "NAN" or not _PAN_RE.match(pan))
        if not pan_missing_or_invalid:
            continue

        expected_no_pan = amount * no_pan_rate / 100.0
        if abs(tax_deducted - expected_no_pan) <= rounding_tol:
            continue  # Correct higher rate applied

        score = _score(float(baselines["section_validation"]), config)
        reason = (
            f"PAN missing or invalid ({pan or 'blank'}); higher no-PAN rate of {no_pan_rate}% "
            f"should apply — expected {_fmt_amount(expected_no_pan)} on {_fmt_amount(amount)}, "
            f"but {_fmt_amount(tax_deducted)} was deducted."
        )
        results.append(_make_result(
            "Amount Difference", score, config,
            books_record=_row_to_json(row.drop(["_bid"])),
            portal_record=None,
            match_reason=reason, difference_type="Rate Mismatch",
        ))

    # --- Threshold crossing checks ---
    # Single-transaction threshold
    for _, row in books.iterrows():
        section = str(row.get("section", "")).strip()
        amount = float(row.get("amount_paid_credited", 0) or 0)
        tax_deducted = float(row.get("tax_deducted", 0) or 0)

        if not section:
            continue
        try:
            sec_entry = _get_section_entry(config, section)
        except KeyError:
            continue

        st_threshold = float(sec_entry.get("single_transaction_threshold", 0))
        if st_threshold > 0 and amount > st_threshold and tax_deducted == 0:
            score = _score(float(baselines["section_validation"]), config)
            reason = (
                f"Single transaction of {_fmt_amount(amount)} under {section} exceeds "
                f"threshold of {_fmt_amount(st_threshold)} but no TDS was deducted."
            )
            results.append(_make_result(
                "Amount Difference", score, config,
                books_record=_row_to_json(row.drop(["_bid"])),
                portal_record=None,
                match_reason=reason, difference_type="Short Deduction",
            ))

    # Annual aggregate threshold
    agg_groups = books.groupby(["pan", "section"], dropna=False)
    for (pan, section), group in agg_groups:
        if pd.isna(pan) or pd.isna(section):
            continue
        pan = str(pan).strip().upper()
        section = str(section).strip()
        try:
            sec_entry = _get_section_entry(config, section)
        except KeyError:
            continue
        agg_threshold = float(sec_entry.get("annual_aggregate_threshold", 0))
        if agg_threshold <= 0:
            continue

        cumulative = group["amount_paid_credited"].astype(float).sum()
        total_tds = group["tax_deducted"].astype(float).sum()

        if cumulative > agg_threshold and total_tds == 0:
            score = _score(float(baselines["section_validation"]), config)
            reason = (
                f"Aggregate payments to PAN {pan} under {section} total {_fmt_amount(cumulative)}, "
                f"exceeding annual threshold of {_fmt_amount(agg_threshold)}, "
                f"but no TDS was deducted across {len(group)} transactions."
            )
            first_row = group.iloc[0]
            results.append(_make_result(
                "Amount Difference", score, config,
                books_record=_row_to_json(first_row.drop(["_bid"])),
                portal_record=None,
                match_reason=reason, difference_type="Short Deduction",
                matched_record_ids=list(group["_bid"]),
            ))

    return results