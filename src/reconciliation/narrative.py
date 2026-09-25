"""Reconcile → Review — the "Accountant's Read".

Two layers, same contract as Module 8's report narrative:

1. A DETERMINISTIC summary, always available, assembled only from the review
   model's own aggregates (never invented).
2. An optional AI draft through the existing Drafter/Verifier guard, on demand.
   If the draft names a figure that is not in the deterministic aggregates — or
   a classification this run did not produce — it is rejected and the rules
   template is shown instead.

The AI call goes through the SAME client every other touchpoint uses
(``src.ingestion_ai.llm``), reads the same C5 instruction context, and is
cached by a content fingerprint of the run's aggregates (mirroring Unit 2D's
fingerprint cache) so re-viewing never re-drafts.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from src.reconciliation.review import format_money

# Session-scoped fingerprint cache. Unit 2D persisted its cache against the
# `ai_analysis` table because it cached PER RESULT; this caches a whole-run
# summary, so the key is a hash of the deterministic aggregates. The cache is
# module-level (survives Reflex state rebuilds, cleared on restart) — it never
# changes a stored value and never introduces a new table.
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LIMIT = 32


def fingerprint(model: dict[str, Any]) -> str:
    """A stable content fingerprint of the aggregates an AI draft may cite."""
    kpi = model.get("kpi") or {}
    payload = {
        "recon_type": model.get("recon_type"),
        "kpi": kpi,
        "causes": [(s.get("cause"), s.get("count"), s.get("value")) for s in model.get("cause_segments") or []],
        "suppliers": [(s.get("party"), s.get("count"), s.get("value")) for s in (model.get("suppliers") or [])[:8]],
        "confidence": model.get("confidence_counts"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic template
# ---------------------------------------------------------------------------

def _cause_sentence(model: dict[str, Any]) -> str:
    segments = [s for s in (model.get("cause_segments") or []) if s.get("count")]
    if not segments:
        return ""
    top = max(segments, key=lambda s: s["count"])
    label = {
        "likely_ingestion_gap": "a books-side data-mapping gap",
        "rounding_only": "rounding only",
        "below_materiality": "differences below materiality",
        "value_difference": "genuine value differences",
        "missing_in_books": "invoices in the portal that are not in the books",
        "missing_in_portal": "invoices in the books that are not on the portal",
    }.get(top["cause"], top["label"])
    others = [s for s in segments if s is not top]
    tail = ""
    if others:
        second = max(others, key=lambda s: s["count"])
        tail = (
            f" The next largest group is {second['label'].lower()} "
            f"({second['count']} item(s), {format_money(second['value'])})."
        )
    return (
        f"The largest group is {label} — {top['count']} item(s) worth "
        f"{format_money(top['value'])}.{tail}"
    )


def deterministic_summary(model: dict[str, Any], *, period_label: str = "") -> dict[str, Any]:
    """3–5 plain sentences plus 2–4 bullets. Every figure is an aggregate."""
    kpi = model.get("kpi") or {}
    recon_type = model.get("recon_type") or "GST"
    total = kpi.get("total_count", 0)
    exceptions = kpi.get("exception_count", 0)
    matched = kpi.get("matched_count", 0)
    attention = kpi.get("attention_value", 0.0)
    reviewed = kpi.get("reviewed_count", 0)
    scope = f"{recon_type} reconciliation" + (f" for {period_label}" if period_label else "")

    sentences: list[str] = []
    if not total:
        sentences.append(f"No records were produced by this {scope} — there is nothing to review.")
    elif exceptions == 0:
        sentences.append(
            f"All {total} records tie out cleanly in this {scope} — there is nothing requiring attention."
        )
    else:
        sentences.append(
            f"{exceptions} of {total} records in this {scope} need your attention, carrying "
            f"{format_money(attention)} of value; the remaining {matched} matched automatically."
        )

    cause_sentence = _cause_sentence(model)
    if cause_sentence:
        sentences.append(cause_sentence)

    gap = next((s for s in (model.get("cause_segments") or []) if s["cause"] == "likely_ingestion_gap"), None)
    if gap and gap.get("count"):
        sentences.append(
            f"{gap['count']} of those items share one signature — the books carry no taxable value or tax "
            "while the invoice totals agree with the portal — which points at an ingestion mapping gap "
            "rather than a real difference."
        )

    judgement = next((g for g in (model.get("group_segments") or []) if g["group"] == "judgement"), None)
    if judgement and judgement.get("count"):
        present_judgement = [
            s["label"].lower() for s in (model.get("cause_segments") or [])
            if s["group"] == "judgement" and s.get("count")
        ]
        listing = ", ".join(present_judgement) if present_judgement else "entries that need a person"
        sentences.append(
            f"{judgement['count']} item(s) worth {format_money(judgement['value'])} need an accountant's "
            f"judgement: {listing}."
        )

    suppliers = model.get("suppliers") or []
    if suppliers:
        s = suppliers[0]
        sentences.append(
            f"Concentration is highest with {s['party']} ({s['count']} item(s), "
            f"{format_money(s['value'])}) — start there."
        )

    sentences.append(f"Review progress: {reviewed} of {total} entries marked reviewed.")

    bullets: list[str] = []
    if matched:
        bullets.append(f"{matched} record(s) reconciled automatically with no action needed.")
    if gap and gap.get("count"):
        bullets.append(
            f"Fix the books tax-column mapping first — it clears {gap['count']} item(s) "
            f"({format_money(gap['value'])}) without any accounting entry."
        )
    if judgement and judgement.get("count"):
        bullets.append(
            f"{judgement['count']} item(s) require judgement — check the supplier invoice or confirm the ledger."
        )
    conf = model.get("confidence_counts") or {}
    if conf:
        bullets.append(
            f"Match confidence across the exceptions: {conf.get('High', 0)} high, "
            f"{conf.get('Medium', 0)} medium, {conf.get('Low', 0)} low."
        )
    bullets.append("Nothing is filed from this screen — it only records your review decisions.")

    text = " ".join(sentences[:6])
    return {"text": text, "bullets": bullets[:4], "source": "rules"}


# ---------------------------------------------------------------------------
# Drafter / Verifier guard
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"₹?\s?([\d][\d,]*(?:\.\d+)?)")


def _allowed_numbers(model: dict[str, Any], period_label: str = "") -> set[str]:
    allowed: set[str] = set()

    def add(v: Any) -> None:
        try:
            allowed.add(f"{float(v):.2f}")
        except (TypeError, ValueError):
            pass

    kpi = model.get("kpi") or {}
    for v in kpi.values():
        if isinstance(v, (int, float)):
            add(v)
    for seg in (model.get("cause_segments") or []) + (model.get("group_segments") or []):
        add(seg.get("count"))
        add(seg.get("value"))
    for seg in model.get("classification_slices") or []:
        add(seg.get("count"))
        add(seg.get("value"))
    for s in model.get("suppliers") or []:
        add(s.get("count"))
        add(s.get("value"))
    for v in (model.get("confidence_counts") or {}).values():
        add(v)
    for v in (model.get("review_counts") or {}).values():
        add(v)
    # The period is legitimate context ("Aug 2026" → 2026), never a fabricated figure.
    for token in re.findall(r"\d+", str(period_label or "")):
        add(token)
    return allowed


def _present_cause_labels(model: dict[str, Any]) -> set[str]:
    return {s["cause"] for s in (model.get("cause_segments") or []) if s.get("count")}


# A phrase that NAMES a group must only appear when that group actually exists.
# Checked only when the run produced cause groups at all — an "all clean" run
# legitimately has none, and its own template is verified separately.
_CAUSE_PHRASES = {
    "missing_in_books": ("not in books", "not in the books"),
    "missing_in_portal": ("not in portal", "not on the portal"),
    "likely_ingestion_gap": ("ingestion gap", "data-mapping gap", "mapping gap"),
    "value_difference": ("value difference",),
    "rounding_only": ("rounding only",),
    "below_materiality": ("below materiality",),
}


def verify_summary(summary: dict[str, Any], model: dict[str, Any], *, period_label: str = "") -> dict[str, Any]:
    """Reject any figure the aggregates do not contain, or any group this run
    did not produce. This is what keeps the prose from contradicting the KPI."""
    violations: list[str] = []
    allowed = _allowed_numbers(model, period_label)
    present = _present_cause_labels(model)
    have_causes = bool(present)

    text = " ".join([summary.get("text") or ""] + list(summary.get("bullets") or []))
    for m in _NUM_RE.finditer(text):
        raw = m.group(1)
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if f"{val:.2f}" not in allowed:
            violations.append(f"Unverified figure: {raw}")

    if have_causes:
        lowered = text.lower()
        for cause, phrases in _CAUSE_PHRASES.items():
            for phrase in phrases:
                if phrase in lowered and cause not in present:
                    violations.append(f"Claims '{phrase}' but this run produced no such group")
                    break

    return {"passed": not violations, "violations": violations}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a senior Indian chartered accountant reading a completed "
    "reconciliation. Write a short, calm summary for a colleague. Use ONLY the "
    "figures and groups you are given — never invent an amount, party, invoice "
    "number or classification. Never say an item is filed or corrected. "
    "Prefer plain language over jargon. Respond as JSON: "
    '{"text": "<3 to 5 sentences>", "bullets": ["<up to 4 short actions>"]}'
)


def build_brief(model: dict[str, Any], *, period_label: str = "") -> str:
    kpi = model.get("kpi") or {}
    parts = [
        f"Recon type: {model.get('recon_type')}",
        f"Period: {period_label or 'not stated'}",
        f"Records: {kpi.get('total_count')}, exceptions: {kpi.get('exception_count')}, "
        f"matched: {kpi.get('matched_count')}",
        f"Value requiring attention: Rs {kpi.get('attention_value')}",
        "Cause groups (cause: count / value Rs): "
        + "; ".join(f"{s['label']}: {s['count']} / {s['value']}" for s in model.get("cause_segments") or []),
        "Classification (name: count / value Rs): "
        + "; ".join(f"{s['name']}: {s['count']} / {s['value']}" for s in model.get("classification_slices") or []),
        f"Confidence across exceptions: {model.get('confidence_counts')}",
        f"Reviewed: {kpi.get('reviewed_count')} of {kpi.get('total_count')}",
    ]
    suppliers = model.get("suppliers") or []
    if suppliers:
        parts.append(
            "Top suppliers by unmatched value (party: count / value Rs): "
            + "; ".join(f"{s['party']}: {s['count']} / {s['value']}" for s in suppliers[:5])
        )
    return "\n".join(parts)


def read_summary(
    model: dict[str, Any],
    *,
    period_label: str = "",
    allow_ai: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return the Accountant's Read. Always deterministic; AI only on request.

    Result keys: ``text``, ``bullets``, ``source`` ("rules" | "ai: <model>"),
    ``ai_error``, ``guard``.
    """
    template = deterministic_summary(model, period_label=period_label)
    template_guard = verify_summary(template, model, period_label=period_label)
    result = {
        "text": template["text"],
        "bullets": template["bullets"],
        "source": "rules",
        "ai_error": "",
        "guard": template_guard,
    }
    if not allow_ai:
        return result

    fp = fingerprint(model)
    if use_cache and fp in _CACHE:
        return dict(_CACHE[fp])

    try:
        from src.ingestion_ai import llm

        c5_context = _c5_context(model.get("recon_type") or "GST")
        brief = build_brief(model, period_label=period_label)
        if c5_context:
            brief = f"{brief}\n\nFirm guidance for this touchpoint:\n{c5_context}"
        parsed, _latency, model_id, _raw = llm.call_llm_json(_SYSTEM_PROMPT, brief, model_override=None)
        draft = {
            "text": str(parsed.get("text") or "").strip(),
            "bullets": [str(b).strip() for b in (parsed.get("bullets") or []) if str(b).strip()][:4],
        }
        if not draft["text"]:
            raise llm.LLMError("parse_error|empty summary")
        guard = verify_summary(draft, model, period_label=period_label)
        if not guard["passed"]:
            result["ai_error"] = "AI draft rejected by the verifier: " + "; ".join(guard["violations"][:3])
            result["guard"] = guard
            return result
        result = {
            "text": draft["text"],
            "bullets": draft["bullets"] or template["bullets"],
            "source": f"AI-drafted · {model_id}",
            "ai_error": "",
            "guard": guard,
        }
    except Exception as exc:  # noqa: BLE001 — the rules template is always the fallback
        result["ai_error"] = str(exc)
        return result

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[fp] = dict(result)
    return result


def _c5_context(recon_type: str) -> str:
    """Best-effort firm guidance for the explanation touchpoint."""
    try:
        from src.c5 import service as c5

        key = "tds_classification" if recon_type == "TDS" else "gst_classification"
        return c5.runtime_context(key) or ""
    except Exception:  # noqa: BLE001
        return ""
