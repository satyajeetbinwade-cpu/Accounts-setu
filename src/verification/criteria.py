"""Exit criteria evaluation.

Loads config/verification_criteria.yaml. Any criterion left null shows
'NOT YET ASSESSABLE' — never a default threshold, never a silent pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

CRITERIA_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "verification_criteria.yaml"


def load_criteria(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path else CRITERIA_PATH
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def evaluate_criteria(
    recon_type: str,
    metrics: dict[str, Any],
    coverage: dict[str, Any],
    adjudicated: Optional[dict[str, Any]] = None,
    *,
    criteria_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Evaluate exit criteria. Returns a list of criterion result dicts,
    each with name, threshold, actual, status ('PASS', 'FAIL', 'NOT YET ASSESSABLE').
    """
    all_criteria = load_criteria(criteria_path)
    rt_criteria = all_criteria.get(recon_type.lower(), {})

    results: list[dict[str, Any]] = []

    def _check(name: str, threshold_key: str, actual_value: float, lower_is_better: bool = True):
        threshold = rt_criteria.get(threshold_key)
        if threshold is None:
            results.append({
                "name": name,
                "threshold": "UNSET",
                "actual": actual_value,
                "status": "NOT YET ASSESSABLE",
            })
        else:
            threshold = float(threshold)
            if lower_is_better:
                passed = actual_value <= threshold
            else:
                passed = actual_value >= threshold
            results.append({
                "name": name,
                "threshold": threshold,
                "actual": round(actual_value, 6),
                "status": "PASS" if passed else "FAIL",
            })

    _check("False-positive rate (count)", "false_positive_rate_max",
           metrics.get("false_positive_rate", 0.0))
    _check("False-positive count", "false_positive_count_max",
           float(metrics.get("false_positive_count", 0)))
    _check("False-positive value (₹)", "false_positive_value_max",
           metrics.get("false_positive_value", 0.0))
    _check("False-negative rate (count)", "false_negative_rate_max",
           metrics.get("false_negative_rate", 0.0))
    _check("Alignment coverage (baseline)", "min_alignment_coverage",
           coverage.get("baseline_coverage", 0.0), lower_is_better=False)

    # Calibration: High band agreement rate
    calib = metrics.get("calibration", {})
    high_band = calib.get("by_band", {}).get("High", {})
    high_rate = high_band.get("rate", 0.0)
    _check("High-band agreement rate", "min_high_band_agreement",
           high_rate, lower_is_better=False)

    # Unadjudicated share
    if adjudicated:
        _check("Unadjudicated share", "max_unadjudicated_share",
               adjudicated.get("unadjudicated_share", 1.0))
    else:
        results.append({
            "name": "Unadjudicated share",
            "threshold": rt_criteria.get("max_unadjudicated_share", "UNSET"),
            "actual": "No adjudication file provided",
            "status": "NOT YET ASSESSABLE",
        })

    return results
