"""F6 §8 — Validation guardrails.

Run on EVERY path, including Path A — a deterministic parser that has
worked for six months must still prove itself on each run, because the
cost of a wrong number here is a wrong filing.

Checks, per §8's table:
  control_total        hard stop, ±₹1 tolerance
  row_accounting        hard stop, every exclusion named
  required_fields       hard stop, specific fields named
  gstin_validity         row-level flag
  date_sanity            row-level flag
  tax_arithmetic         row-level flag, escalates to run-level hard stop
                         above a 20% failure rate (the double-counted-
                         taxable-base defect is deliberately caught here)
  duplicate_detection     row-level flag, both rows retained
  re_upload_guard         block with override
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][A-Z][0-9A-Z]$")
_TAX_ARITHMETIC_ESCALATION_RATE = 0.20
CONTROL_TOTAL_TOLERANCE = 1.0
TAX_ARITHMETIC_TOLERANCE = 1.0
# CGST/SGST mirror assertion — the two sides restate the same amount, so
# only a rounding-level difference is acceptable (§5.4).
MIRROR_TOLERANCE = 0.01
# Implied rate = Tax / Taxable must match the header's rate within ₹1
# (§5.4) — ₹1.00 per §8, NOT ±0.50: the specimen carries rounding
# residuals of exactly ±0.50 that must not misfire.
IMPLIED_RATE_TOLERANCE = 1.0


@dataclass
class CheckResult:
    check: str
    result: str  # 'pass' | 'row_flag' | 'hard_stop'
    detail: str
    affected_rows: list[int] = field(default_factory=list)


@dataclass
class ValidationOutcome:
    results: list[CheckResult] = field(default_factory=list)
    row_flags: dict[int, list[str]] = field(default_factory=dict)  # row idx -> [issue codes]

    @property
    def hard_stopped(self) -> bool:
        return any(r.result == "hard_stop" for r in self.results)

    def to_json(self) -> list[dict[str, Any]]:
        return [
            {"check": r.check, "result": r.result, "detail": r.detail, "affected_rows": r.affected_rows}
            for r in self.results
        ]


def check_control_total(
    parsed_sum: float, stated_total: Optional[float], *, label: str,
) -> CheckResult:
    if stated_total is None:
        return CheckResult("control_total", "pass", f"No stated total available for {label} — check skipped.")
    delta = abs(parsed_sum - stated_total)
    if delta <= CONTROL_TOTAL_TOLERANCE:
        return CheckResult("control_total", "pass", f"{label}: parsed {parsed_sum:.2f} vs stated {stated_total:.2f} (±{CONTROL_TOTAL_TOLERANCE:.2f} ok).")
    return CheckResult(
        "control_total", "hard_stop",
        f"{label}: parsed sum {parsed_sum:.2f} does not match the file's own stated total "
        f"{stated_total:.2f} (delta {delta:.2f}, exceeds ±{CONTROL_TOTAL_TOLERANCE:.2f}). "
        "Never promoted, never passed to Module 2.",
    )


def check_row_accounting(row_count_read: int, row_count_parsed: int, exclusion_reasons: list[dict[str, Any]]) -> CheckResult:
    excluded_total = sum(item["count"] for item in exclusion_reasons)
    if row_count_read == row_count_parsed + excluded_total:
        return CheckResult(
            "row_accounting", "pass",
            f"rows_read={row_count_read} = rows_parsed={row_count_parsed} + rows_excluded={excluded_total}.",
        )
    return CheckResult(
        "row_accounting", "hard_stop",
        f"rows_read={row_count_read} does not equal rows_parsed={row_count_parsed} + "
        f"rows_excluded={excluded_total} ({row_count_read - row_count_parsed - excluded_total} unexplained). "
        "An unexplained row is an unparsed row.",
    )


def check_required_fields(rows: list[dict[str, Any]], required_fields: list[str]) -> CheckResult:
    if not rows:
        missing = list(required_fields)
    else:
        missing = [f for f in required_fields if not any(r.get(f) not in (None, "") for r in rows)]
    if not missing:
        return CheckResult("required_fields", "pass", "Every required field resolves to a rule.")
    return CheckResult(
        "required_fields", "hard_stop",
        f"Missing required field(s): {', '.join(missing)}.",
    )


def check_gstin_validity(rows: list[dict[str, Any]], *, gstin_field: str = "gstin") -> CheckResult:
    bad_rows = []
    for idx, row in enumerate(rows):
        val = str(row.get(gstin_field) or "").strip().upper()
        if val and not _GSTIN_RE.match(val):
            bad_rows.append(idx)
    if not bad_rows:
        return CheckResult("gstin_validity", "pass", "All present GSTINs are structurally valid.", [])
    return CheckResult(
        "gstin_validity", "row_flag",
        f"{len(bad_rows)} row(s) have a malformed GSTIN — flagged, run continues.",
        bad_rows,
    )


def check_date_sanity(rows: list[dict[str, Any]], *, date_field: str, period: Optional[str] = None) -> CheckResult:
    out_of_period = []
    for idx, row in enumerate(rows):
        val = row.get(date_field)
        if not val:
            continue
        if period and isinstance(val, str) and len(val) >= 7:
            if not val.startswith(period):
                out_of_period.append(idx)
    if not out_of_period:
        return CheckResult("date_sanity", "pass", "All parsed dates fall inside the declared period.", [])
    return CheckResult(
        "date_sanity", "row_flag",
        f"{len(out_of_period)} row(s) fall outside the declared period — flagged, often the cause of a mismatch.",
        out_of_period,
    )


def check_tax_arithmetic(rows: list[dict[str, Any]]) -> CheckResult:
    """taxable + Σtax + rounding_adjustment = invoice_value per row, ±₹1.
    Escalates to a run-level hard stop above a 20% failure rate — the
    deliberate catch for a systematic double-counted-taxable-base error
    that would otherwise look like "lots of exceptions" at row level."""
    checkable = [r for r in rows if r.get("invoice_value") not in (None, "")]
    if not checkable:
        return CheckResult("tax_arithmetic", "pass", "No invoice_value present to check.", [])
    failed = []
    for idx, row in enumerate(rows):
        if row.get("invoice_value") in (None, ""):
            continue
        try:
            taxable = float(row.get("taxable_value") or 0)
            tax = sum(float(row.get(f) or 0) for f in ("igst", "cgst", "sgst", "cess"))
            rounding = float(row.get("rounding_adjustment") or 0)
            invoice_value = float(row.get("invoice_value") or 0)
        except (TypeError, ValueError):
            failed.append(idx)
            continue
        if abs((taxable + tax + rounding) - invoice_value) > TAX_ARITHMETIC_TOLERANCE:
            failed.append(idx)

    failure_rate = len(failed) / len(checkable) if checkable else 0.0
    if not failed:
        return CheckResult("tax_arithmetic", "pass", "taxable + Σtax + rounding_adjustment = invoice_value on every checkable row.", [])
    if failure_rate > _TAX_ARITHMETIC_ESCALATION_RATE:
        return CheckResult(
            "tax_arithmetic", "hard_stop",
            f"{len(failed)}/{len(checkable)} rows ({failure_rate:.0%}) fail tax arithmetic — exceeds the "
            f"{_TAX_ARITHMETIC_ESCALATION_RATE:.0%} escalation threshold. Systematic failure indicates a "
            "mapping error (typically a double-counted taxable base). Run-level hard stop.",
            failed,
        )
    return CheckResult(
        "tax_arithmetic", "row_flag",
        f"{len(failed)}/{len(checkable)} rows ({failure_rate:.0%}) fail tax arithmetic — flagged, run continues.",
        failed,
    )


def check_mirror_columns(
    rows: list[dict[str, Any]], *, pairs: Optional[list[tuple[str, str]]] = None,
) -> CheckResult:
    """§5.4 mirror assertion — CGST Txbl = SGST Txbl and CGST Tax = SGST Tax.

    A Purchase Register restates the SAME taxable base and the SAME tax
    under both the CGST and SGST prefixes (an intra-state supply is split
    equally). The de-duplication rule (only one side's Txbl buckets feed
    `taxable_value`) is only SAFE while the two sides genuinely mirror each
    other. If they diverge, the de-duplication silently drops real value, so
    the divergence must be caught — row-flagged, escalating to a run-level
    hard stop above 20% (same escalation shape as tax_arithmetic, for the
    same reason: bulk-plausible wrong numbers).

    `pairs` is a list of (left_field, right_field) canonical-field pairs to
    compare; it is derived from the confirmed rate-bucket matrix by the
    caller, so this check never hard-codes slabs.
    """
    if not pairs:
        return CheckResult("mirror_columns", "pass", "No mirror column pairs to compare — check skipped.", [])
    failed: list[int] = []
    for idx, row in enumerate(rows):
        for left, right in pairs:
            lv, rv = row.get(left), row.get(right)
            if lv in (None, "") and rv in (None, ""):
                continue
            try:
                if abs(float(lv or 0) - float(rv or 0)) > MIRROR_TOLERANCE:
                    failed.append(idx)
                    break
            except (TypeError, ValueError):
                failed.append(idx)
                break
    if not failed:
        return CheckResult(
            "mirror_columns", "pass",
            f"CGST and SGST mirror each other on every row ({len(pairs)} pair(s) checked).", [],
        )
    rate = len(failed) / len(rows) if rows else 0.0
    if rate > _TAX_ARITHMETIC_ESCALATION_RATE:
        return CheckResult(
            "mirror_columns", "hard_stop",
            f"{len(failed)}/{len(rows)} rows ({rate:.0%}) break the CGST=SGST mirror — exceeds the "
            f"{_TAX_ARITHMETIC_ESCALATION_RATE:.0%} escalation threshold. The taxable-base de-duplication "
            "would silently drop real value. Run-level hard stop.",
            failed,
        )
    return CheckResult(
        "mirror_columns", "row_flag",
        f"{len(failed)}/{len(rows)} rows ({rate:.0%}) break the CGST=SGST mirror — flagged, run continues.",
        failed,
    )


def check_implied_rate(
    rows: list[dict[str, Any]], *, buckets: Optional[list[dict[str, Any]]] = None,
) -> CheckResult:
    """§5.4 implied-rate check — per row and bucket, |Tax − (rate/100) × Taxable| ≤ ₹1.

    This proves each *Tax* column is paired with the RIGHT *Txbl* column and
    that the rate printed in the header is real. It is the deterministic
    catch for a Tax/Txbl column swap (a swap leaves both columns populated
    and the totals plausible, so no other check would see it).

    `buckets` is derived from the confirmed rate-bucket matrix by the caller:
    each entry {"rate": float, "taxable": <source column>, "tax": <source column>}.
    `rate` is the header PERCENTAGE (5, 18, 2.5, 9 …), so it is divided by
    100 before multiplying.
    """
    if not buckets:
        return CheckResult("implied_rate", "pass", "No rate buckets to check — check skipped.", [])
    failed: list[int] = []
    for idx, row in enumerate(rows):
        for b in buckets:
            try:
                taxable = float(row.get(b["taxable"]) or 0)
                tax = float(row.get(b["tax"]) or 0)
                rate = float(b["rate"]) / 100.0
            except (TypeError, ValueError, KeyError):
                continue
            if taxable == 0 and tax == 0:
                continue
            if abs(tax - rate * taxable) > IMPLIED_RATE_TOLERANCE:
                failed.append(idx)
                break
    if not failed:
        return CheckResult(
            "implied_rate", "pass",
            f"Tax equals rate × taxable within ±{IMPLIED_RATE_TOLERANCE:.2f} on every populated bucket "
            f"({len(buckets)} bucket(s) checked).", [],
        )
    rate = len(failed) / len(rows) if rows else 0.0
    if rate > _TAX_ARITHMETIC_ESCALATION_RATE:
        return CheckResult(
            "implied_rate", "hard_stop",
            f"{len(failed)}/{len(rows)} rows ({rate:.0%}) fail the implied-rate check — exceeds the "
            f"{_TAX_ARITHMETIC_ESCALATION_RATE:.0%} escalation threshold. A systematic failure usually means "
            "a Tax column is paired with the wrong Txbl column (a swap). Run-level hard stop.",
            failed,
        )
    return CheckResult(
        "implied_rate", "row_flag",
        f"{len(failed)}/{len(rows)} rows ({rate:.0%}) fail the implied-rate check — flagged, run continues.",
        failed,
    )


def check_duplicate_detection(rows: list[dict[str, Any]], *, key_fields: tuple[str, ...] = ("gstin", "invoice_number", "invoice_date")) -> CheckResult:
    seen: dict[tuple, list[int]] = {}
    for idx, row in enumerate(rows):
        key = tuple(row.get(f) for f in key_fields)
        if all(v in (None, "") for v in key):
            continue
        seen.setdefault(key, []).append(idx)
    dup_indices = [idx for idxs in seen.values() if len(idxs) > 1 for idx in idxs]
    if not dup_indices:
        return CheckResult("duplicate_detection", "pass", "No duplicate (supplier GSTIN, document number, date) found.", [])
    return CheckResult(
        "duplicate_detection", "row_flag",
        f"{len(dup_indices)} row(s) share a duplicate (GSTIN, document number, date) — both rows retained.",
        dup_indices,
    )


def check_re_upload_guard(file_hash: str, prior_runs: list[dict[str, Any]]) -> CheckResult:
    matches = [r for r in prior_runs if r.get("file_hash") == file_hash]
    if not matches:
        return CheckResult("re_upload_guard", "pass", "No identical file previously ingested for this client/period/slot.")
    prior = matches[0]
    return CheckResult(
        "re_upload_guard", "hard_stop",
        f"Identical file already ingested (run #{prior.get('run_id')}, {prior.get('created_at')}). "
        "Blocked — explicit operator override required for a genuine re-run.",
    )


def run_all_checks(
    *, rows: list[dict[str, Any]], row_count_read: int, row_count_parsed: int,
    exclusion_reasons: list[dict[str, Any]], required_fields: list[str],
    control_totals: Optional[list[tuple[float, Optional[float], str]]] = None,
    date_field: Optional[str] = None, period: Optional[str] = None,
    file_hash: Optional[str] = None, prior_runs: Optional[list[dict[str, Any]]] = None,
    skip_re_upload_guard: bool = False,
    mirror_pairs: Optional[list[tuple[str, str]]] = None,
    rate_buckets: Optional[list[dict[str, Any]]] = None,
) -> ValidationOutcome:
    """Run every §8 guardrail and collect results. Hard-stop checks are
    still run in full (not short-circuited) so a Path B reviewer sees every
    problem at once, not one at a time across repeated attempts."""
    outcome = ValidationOutcome()

    outcome.results.append(check_row_accounting(row_count_read, row_count_parsed, exclusion_reasons))
    outcome.results.append(check_required_fields(rows, required_fields))

    for parsed_sum, stated_total, label in (control_totals or []):
        outcome.results.append(check_control_total(parsed_sum, stated_total, label=label))

    outcome.results.append(check_gstin_validity(rows))
    if date_field:
        outcome.results.append(check_date_sanity(rows, date_field=date_field, period=period))
    outcome.results.append(check_tax_arithmetic(rows))
    # §5.4's two named protections — run only when the caller derived them
    # from a confirmed rate-bucket matrix (so no slab is hard-coded here).
    if mirror_pairs:
        outcome.results.append(check_mirror_columns(rows, pairs=mirror_pairs))
    if rate_buckets:
        outcome.results.append(check_implied_rate(rows, buckets=rate_buckets))
    outcome.results.append(check_duplicate_detection(rows))

    if not skip_re_upload_guard and file_hash is not None:
        outcome.results.append(check_re_upload_guard(file_hash, prior_runs or []))

    for r in outcome.results:
        if r.result == "row_flag":
            for idx in r.affected_rows:
                outcome.row_flags.setdefault(idx, []).append(r.check)

    return outcome
