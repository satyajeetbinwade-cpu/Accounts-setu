"""Phase-4 calibration: does the score mean what it claims?

    python -m src.invoice_extract.calibration report [--client-id N] [--json]

A score is only worth having if it can be shown to be right. This module reads
the corrections log — including the "confirmed as is" entries — and measures, per
score band, how often a person actually changed the value. That is the whole
loop: the formula scores, people confirm or correct, and the numbers say which
band is trustworthy.

WHAT IT DOES NOT DO
-------------------
It NEVER changes a setting. An edit rate above the target is a finding, not an
instruction: the report says "the 80-89 band is edited 12% of the time", and a
person decides whether to change the threshold or a weight. A tool that tuned
itself would need nobody to be accountable for the result.

WHY THE BANDS
-------------
The bands are the ones the review screen and the bulk-approve gate use, so the
report's numbers and the gate's decision come from the same measurement rather
than two approximations of it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from src.invoice_extract import confidence as conf
from src.invoice_extract.confidence_schema import (
    ACTION_CONFIRMED,
    ACTION_EDITED,
    ACTION_MARKED_NOT_APPLICABLE,
    ACTION_MARKED_NOT_PRINTED,
)
from src.invoice_extract.service import InvoiceExtractError, _connect

#: The bands, low to high. The gate and the report share these exact edges.
BANDS: tuple[tuple[str, float, float], ...] = (
    ("<60", 0.0, 60.0),
    ("60-69", 60.0, 70.0),
    ("70-79", 70.0, 80.0),
    ("80-89", 80.0, 90.0),
    ("90-94", 90.0, 95.0),
    ("95-100", 95.0, 100.01),
)

#: The bulk-approve gate measures the "at or above" band the threshold defines.
GATE_BAND_MIN = 90.0


def _band_of(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    for name, low, high in BANDS:
        if low <= float(score) < high:
            return name
    return None


def _is_edit(action: str) -> bool:
    """Which actions count as "the score was wrong"?

    ``edited`` is unambiguous. ``marked_not_printed`` and
    ``marked_not_applicable`` mean the reviewer decided the field should not have
    a value at all — that is ALSO the score being wrong about a value being
    there, so both count as corrections of the score. ``confirmed_as_is`` is the
    opposite and is the denominator's other half.
    """
    return action in (ACTION_EDITED, ACTION_MARKED_NOT_PRINTED, ACTION_MARKED_NOT_APPLICABLE)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 4)


def _months(values: list[str]) -> int:
    """Distinct calendar months (YYYY-MM) in a list of ISO timestamps."""
    return len({str(value)[:7] for value in values if value})


def _dict_row(cur: sqlite3.Cursor, row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {c[0]: row[i] for i, c in enumerate(cur.description)}


def _rows(db_path, client_id: Optional[int]) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        sql = (
            "SELECT c.*, f.value_a, f.value_b, f.reads_agree, f.is_handwritten AS field_handwritten "
            "FROM invoice_field_corrections c "
            "LEFT JOIN invoice_doc_fields f ON f.field_id = c.field_id "
            "WHERE 1 = 1"
        )
        params: list[Any] = []
        if client_id is not None:
            sql += " AND c.client_id = ?"
            params.append(client_id)
        sql += " ORDER BY c.correction_id"
        cur = conn.execute(sql, tuple(params))
        return [_dict_row(cur, row) for row in cur.fetchall()]
    finally:
        conn.close()


def _bin_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One summary row per band, over the corrections in ``rows``."""
    out: list[dict[str, Any]] = []
    for name, _low, _high in BANDS:
        band_rows = [r for r in rows if _band_of(r.get("score_at_time")) == name]
        edits = sum(1 for r in band_rows if _is_edit(str(r["action"])))
        confirmations = sum(1 for r in band_rows if str(r["action"]) == ACTION_CONFIRMED)
        out.append({
            "band": name,
            "fields": len(band_rows),
            "confirmed_as_is": confirmations,
            "edited": edits,
            "edit_rate_pct": _rate(edits, len(band_rows)),
        })
    unscored = [r for r in rows if r.get("score_at_time") is None]
    if unscored:
        edits = sum(1 for r in unscored if _is_edit(str(r["action"])))
        out.append({
            "band": "no score", "fields": len(unscored),
            "confirmed_as_is": sum(1 for r in unscored if str(r["action"]) == ACTION_CONFIRMED),
            "edited": edits, "edit_rate_pct": _rate(edits, len(unscored)),
        })
    return out


def _group_summary(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or "unknown"), []).append(row)
    out: list[dict[str, Any]] = []
    for name in sorted(groups):
        group = groups[name]
        edits = sum(1 for r in group if _is_edit(str(r["action"])))
        out.append({
            key: name, "fields": len(group), "edited": edits,
            "edit_rate_pct": _rate(edits, len(group)),
        })
    return out


def _reader_agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """When a human decided an EDITED field, which reader had it right?

    Only fields whose two reads disagreed can say anything here — where the
    reads agreed, "who was right" has no content.
    """
    decided = [
        r for r in rows
        if str(r["action"]) == ACTION_EDITED and r.get("value_a") and r.get("value_b")
        and r.get("value_a") != r.get("value_b")
    ]
    a_right = b_right = neither = 0
    from src.invoice_extract import normalisers as nz

    for row in decided:
        from src.invoice_extract import fields_v2 as fv2

        kind = fv2.kind_of(str(row["field_key"])) or fv2.KIND_TEXT
        after = row.get("value_after")
        if after is None:
            neither += 1
            continue
        if nz.same_value(kind, after, row["value_a"]):
            a_right += 1
        elif nz.same_value(kind, after, row["value_b"]):
            b_right += 1
        else:
            neither += 1
    disagreements = sum(
        1 for r in rows if str(r.get("reads_agree") or "") in ("disagree", "missing_b")
    )
    return {
        "human_decisions_on_disagreements": len(decided),
        "read_a_right": a_right,
        "read_b_right": b_right,
        "neither": neither,
        "corrections_on_disagreeing_fields": disagreements,
        "note": (
            "Only fields whose two reads differed AND that a person edited can say which reader "
            "was right; a decision on a field where the reads agreed has no content here."
        ),
    }


def calibration_report(
    *, client_id: Optional[int] = None, db_path=None, top_mistakes: int = 10,
) -> dict[str, Any]:
    """The whole measurement, as data (the CLI renders it)."""
    rows = _rows(db_path, client_id)
    edited = [r for r in rows if _is_edit(str(r["action"]))]
    dangerous = sorted(
        [r for r in edited if r.get("score_at_time") is not None],
        key=lambda r: float(r["score_at_time"]), reverse=True,
    )[:top_mistakes]
    return {
        "client_id": client_id,
        "corrections": len(rows),
        "bands": _bin_rows(rows),
        "by_category": _group_summary(rows, "document_category"),
        "by_handwriting": _handwriting_summary(rows),
        "reader_agreement": _reader_agreement(rows),
        "highest_scoring_edits": [
            {
                "field_key": r["field_key"], "score_at_time": r.get("score_at_time"),
                "value_before": r.get("value_before"), "value_after": r.get("value_after"),
                "action": r["action"], "reason_code": r.get("reason_code"),
                "document_id": r.get("document_id"), "actor": r.get("actor"), "at": r.get("at"),
                "band": _band_of(r.get("score_at_time")),
            }
            for r in dangerous
        ],
        "months_of_history": _months([r.get("at") for r in rows]),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _handwriting_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, predicate in (
        ("printed", lambda r: not bool(r.get("is_handwritten"))),
        ("handwritten", lambda r: bool(r.get("is_handwritten"))),
    ):
        group = [r for r in rows if predicate(r)]
        edits = sum(1 for r in group if _is_edit(str(r["action"])))
        out.append({
            "handwriting": name, "fields": len(group), "edited": edits,
            "edit_rate_pct": _rate(edits, len(group)),
        })
    return out


# ---------------------------------------------------------------------------
# The bulk-approve gate
# ---------------------------------------------------------------------------


def bulk_approve_gate(client_id: Optional[int], *, db_path=None) -> dict[str, Any]:
    """May this client's >= 90 fields be bulk-approved?

    A function of RECORDED HISTORY and nothing else. There is deliberately no
    override flag and no "force" argument: the entire value of the gate is that
    it opens because the firm's own reviewed history supports it, not because
    somebody asked it to.

    Open when ALL of:
      * ``invoice_extract.bulk_approve_enabled`` is true (a person turned it on),
      * the >= 90 band has at least ``min_fields`` reviewed fields,
      * those fields span at least ``min_months`` distinct months,
      * their edit rate is at or below ``max_edit_rate_pct``.
    """
    rows = _rows(db_path, client_id)
    high = [
        r for r in rows
        if str(r["action"]) in (ACTION_CONFIRMED, ACTION_EDITED, ACTION_MARKED_NOT_PRINTED,
                                ACTION_MARKED_NOT_APPLICABLE)
        and r.get("score_at_time") is not None
        and float(r["score_at_time"]) >= GATE_BAND_MIN
    ]
    edits = sum(1 for r in high if _is_edit(str(r["action"])))
    edit_rate = _rate(edits, len(high))
    months = _months([r.get("at") for r in high])

    enabled_raw = conf._setting_raw(
        conf.KEY_BULK_ENABLED, client_id=client_id, db_path=db_path,
    )
    enabled = str(enabled_raw or "").strip().lower() in ("1", "true", "yes", "on")
    min_fields = int(_number(conf.KEY_BULK_MIN_FIELDS, 300, client_id=client_id, db_path=db_path))
    min_months = int(_number(conf.KEY_BULK_MIN_MONTHS, 3, client_id=client_id, db_path=db_path))
    max_rate = _number(conf.KEY_BULK_MAX_EDIT_RATE, 0.5, client_id=client_id, db_path=db_path)

    reasons: list[str] = []
    if not enabled:
        reasons.append(
            f"bulk approve is switched off ({conf.KEY_BULK_ENABLED} is false) — a person must turn "
            "it on"
        )
    if len(high) < min_fields:
        reasons.append(
            f"only {len(high)} reviewed field(s) score 90 or above; {min_fields} are required "
            "before the band can be trusted"
        )
    if months < min_months:
        reasons.append(
            f"that history spans {months} month(s); {min_months} are required so one unusual "
            "period cannot open the gate"
        )
    if edit_rate is None:
        reasons.append("no reviewed field scores 90 or above yet, so there is no edit rate to judge")
    elif edit_rate > max_rate:
        reasons.append(
            f"{edit_rate}% of the 90+ fields were corrected, above the {max_rate}% limit — the "
            "band is not yet trustworthy"
        )

    return {
        "client_id": client_id,
        "open": not reasons,
        "reasons": reasons,
        "reviewed_high_fields": len(high),
        "min_fields": min_fields,
        "months": months,
        "min_months": min_months,
        "edit_rate_pct": edit_rate,
        "max_edit_rate_pct": max_rate,
        "enabled": enabled,
    }


def _number(key: str, default: float, *, client_id=None, db_path=None) -> float:
    raw = conf._setting_raw(key, client_id=client_id, db_path=db_path)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render(report: dict[str, Any], gate: Optional[dict[str, Any]] = None) -> str:
    lines: list[str] = []
    lines.append("# Invoice Extraction — confidence calibration")
    lines.append("")
    lines.append(f"- Corrections recorded: **{report['corrections']}**")
    lines.append(f"- Months of history: **{report['months_of_history']}**")
    lines.append(f"- Client: {report['client_id'] if report['client_id'] is not None else 'all clients'}")
    lines.append("")
    lines.append("## Score bands")
    lines.append("")
    lines.append("| band | fields | confirmed as is | edited | edit rate |")
    lines.append("|---|---|---|---|---|")
    for row in report["bands"]:
        rate = "—" if row["edit_rate_pct"] is None else f"{row['edit_rate_pct']}%"
        lines.append(
            f"| {row['band']} | {row['fields']} | {row['confirmed_as_is']} | {row['edited']} | {rate} |"
        )
    lines.append("")
    lines.append("An edit rate that RISES as the band goes up is the signal that matters: it means "
                 "the high band is not the safe one.")
    lines.append("")
    lines.append("## By document category")
    lines.append("")
    lines.append("| category | fields | edited | edit rate |")
    lines.append("|---|---|---|---|")
    for row in report["by_category"]:
        rate = "—" if row["edit_rate_pct"] is None else f"{row['edit_rate_pct']}%"
        lines.append(f"| {row['document_category']} | {row['fields']} | {row['edited']} | {rate} |")
    lines.append("")
    lines.append("## Handwritten vs printed")
    lines.append("")
    lines.append("| source | fields | edited | edit rate |")
    lines.append("|---|---|---|---|")
    for row in report["by_handwriting"]:
        rate = "—" if row["edit_rate_pct"] is None else f"{row['edit_rate_pct']}%"
        lines.append(f"| {row['handwriting']} | {row['fields']} | {row['edited']} | {rate} |")
    lines.append("")
    agreement = report["reader_agreement"]
    lines.append("## Which reader was right")
    lines.append("")
    lines.append(f"- Corrections on fields whose two reads differed: {agreement['corrections_on_disagreeing_fields']}")
    lines.append(f"- Human decisions on those: {agreement['human_decisions_on_disagreements']}")
    lines.append(f"  - read A right: {agreement['read_a_right']}")
    lines.append(f"  - read B right: {agreement['read_b_right']}")
    lines.append(f"  - neither: {agreement['neither']}")
    lines.append(f"- {agreement['note']}")
    lines.append("")
    lines.append("## Highest-scoring fields that were still edited (the dangerous ones)")
    lines.append("")
    if report["highest_scoring_edits"]:
        lines.append("| score | band | field | before | after | action | reason | document |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for row in report["highest_scoring_edits"]:
            lines.append(
                f"| {row['score_at_time']} | {row['band']} | `{row['field_key']}` "
                f"| {row['value_before']} | {row['value_after']} | {row['action']} "
                f"| {row['reason_code'] or '—'} | {row['document_id']} |"
            )
    else:
        lines.append("None — no field scoring 90 or above has been edited.")
    lines.append("")
    if gate is not None:
        lines.append("## Bulk approve")
        lines.append("")
        lines.append(f"**{'OPEN' if gate['open'] else 'CLOSED'}** for client {gate['client_id']}")
        lines.append("")
        lines.append(
            f"- {gate['reviewed_high_fields']} reviewed field(s) at 90+ "
            f"(needs {gate['min_fields']}), spanning {gate['months']} month(s) "
            f"(needs {gate['min_months']}), edit rate "
            + ("n/a" if gate["edit_rate_pct"] is None else f"{gate['edit_rate_pct']}%")
            + f" (limit {gate['max_edit_rate_pct']}%), switch "
            + ("on" if gate["enabled"] else "off")
        )
        for reason in gate["reasons"]:
            lines.append(f"- CLOSED: {reason}")
        lines.append("")
        lines.append("This gate reads recorded history only — there is no override flag.")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.invoice_extract.calibration",
        description="Invoice Extraction Phase 4 — confidence calibration and the bulk-approve gate.",
    )
    parser.add_argument("cmd", choices=["report"])
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        report = calibration_report(client_id=args.client_id, db_path=args.db)
        gate = bulk_approve_gate(args.client_id, db_path=args.db)
        if args.json:
            print(json.dumps({**report, "bulk_approve": gate}, indent=2, default=str))
        else:
            print(_render(report, gate))
        return 0
    except InvoiceExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
