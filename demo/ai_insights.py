"""Unit 2D — the AI post-matching analysis layer, built for real.

Called once per completed run, after ``execute_run`` returns. Given the run
summary plus the actual exception rows, it produces a plain-language narrative
an accountant — or a prospective client — can read without opening a
spreadsheet.

Scope, deliberately narrow (per Unit 2D and the demo's build prompt):

  * **On-demand only.** Never runs automatically inside ``execute_run``.
  * **Identification + suggestion only.** Never writes back to
    ``match_results``, never changes a classification, never proposes an
    automatic posting. Advisory text only.
  * **Never claims a classification the engine didn't produce.** Every figure
    and every pattern named comes from the engine's own output; the prompt
    forbids inventing anything and the response is validated for the numbers
    it cites.

Caching is a fingerprint on ``run_id`` — runs are immutable, so a run's
narrative can never go stale. Re-viewing a report therefore never re-calls
the API.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pandas as pd

from src import queries
from src.ingestion_ai import llm

from demo import session as demo_session

# The touchpoint this layer's model is resolved from in the central AI model
# registry (Setup → AI Models). Same key the Phase 1 analysis layer uses —
# this IS the reconciliation-explanation touchpoint, just applied per run
# rather than per item.
TOUCHPOINT_KEY = "recon_explanation"

# How many exception rows are sent. A demo run is small, but a real run can
# carry hundreds of rows; the narrative describes PATTERNS, so a bounded
# sample is both cheaper and sufficient. Rows are ordered by value so the
# most consequential ones survive the cut.
MAX_EXCEPTION_ROWS = 60

# Which exception rows are worth narrating. "Matched" is excluded — it is
# the healthy baseline, reported as a rate rather than as a finding.
_EXCEPTION_CLASSIFICATIONS = ("Amount Difference", "Not in Books", "Not in Portal")


class AIInsightsError(RuntimeError):
    """Raised when the narrative cannot be produced. The message carries a
    machine-readable type prefix (same convention as the rest of the
    project's AI layer) so the UI can explain the failure in plain language."""


# ---------------------------------------------------------------------------
# Model resolution + prompt
# ---------------------------------------------------------------------------


# Models whose value is multi-step reasoning. The registry's
# `recon_explanation` touchpoint is set to one of these because the Phase 1
# per-item analysis genuinely needs it — explaining a single tax variance
# involves real arithmetic.
#
# This layer's task is different: it summarises a whole run's already-computed
# results into a narrative. That is a writing task, not a reasoning one, and a
# reasoning model adds minutes of latency without adding anything to the
# output. So the demo routes this touchpoint to the config's cheaper/faster
# model instead — the "routed by complexity" pattern the build prompt asks
# for, applied to the task actually being performed.
_REASONING_MODEL_MARKERS = ("deepseek-r1", "deepseek-reasoner", "-r1", "o1", "o3", "thinking")


def _is_reasoning_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(marker in lowered for marker in _REASONING_MODEL_MARKERS)


def resolve_model() -> Optional[str]:
    """The model this layer should use for a run-level narrative.

    Resolution order:
      1. The central AI model registry (Setup → AI Models) — the single place
         every touchpoint's model is set — but only when it names a model
         suited to summarisation.
      2. ``config/ai_config.yaml``'s ``models.simple`` route.
      3. The LLM client's own default.

    Best-effort throughout: a registry or config problem must never stop the
    narrative being produced.
    """
    registry_model: Optional[str] = None
    try:
        from src.ai_models import service as ai_models

        registry_model = ai_models.effective_model(TOUCHPOINT_KEY)
    except Exception:  # noqa: BLE001
        registry_model = None

    if registry_model and not _is_reasoning_model(registry_model):
        return registry_model

    try:
        from src.ai_analysis import load_ai_config

        cfg = load_ai_config()
        simple = (cfg.get("models") or {}).get("simple")
        if simple:
            return simple
    except Exception:  # noqa: BLE001
        pass

    return registry_model


_SYSTEM_PROMPT = (
    "You are writing a short, plain-language summary of a completed GST/TDS "
    "reconciliation run for an Indian chartered accountancy firm. The report "
    "is read by an accountant, and will be shown to prospective clients as a "
    "demonstration — so it must be clear, specific and honest, never padded "
    "or over-confident.\n\n"
    "You are given the run's headline figures and its exception rows. The "
    "reconciliation engine has ALREADY classified every row. Your job is to "
    "describe what happened and suggest what to look at next.\n\n"
    "Hard rules:\n"
    "- Use ONLY the figures and rows you are given. Never invent an invoice "
    "number, amount, date, GSTIN, PAN, vendor name or section code.\n"
    "- Never state or imply a classification the engine did not produce. If "
    "the engine says 'Amount Difference', do not call it a mismatch of some "
    "other kind.\n"
    "- Describe PATTERNS across rows, not a roll-call of individual rows. "
    "Group by difference type and describe what the group has in common.\n"
    "- Never recommend an automatic or unreviewed posting to the books. Every "
    "suggested action must be something a person reviews and approves.\n"
    "- If the data does not support a conclusion, say so plainly rather than "
    "speculating.\n"
    "- Be concise. This is a summary, not an essay.\n\n"
    "Respond with a single valid JSON object and nothing else. No preamble, "
    "no markdown fences. Schema:\n"
    '{"headline": "<one sentence on overall health, citing the match rate>", '
    '"health_read": "<2-3 sentences reading the overall result>", '
    '"patterns": [{"title": "<short pattern name>", '
    '"difference_type": "<the engine difference type this pattern is drawn from>", '
    '"count": <number of rows in this pattern>, '
    '"narrative": "<2-3 sentences describing the pattern and what it likely means>", '
    '"actions": ["<a specific next step a person should take>", "..."]}], '
    '"data_quality_note": "<one sentence on any gaps or caveats, or \\"\\" if none>"}'
)


def _value_of(record: Any) -> float:
    """The rupee magnitude of a result row, from whichever field it carries."""
    if not isinstance(record, dict):
        return 0.0
    for key in ("invoice_value", "amount_paid_credited", "tax_deducted", "taxable_value"):
        if record.get(key) is not None:
            try:
                return abs(float(record[key]))
            except (TypeError, ValueError):
                continue
    return 0.0


def _build_payload(
    recon_type: str,
    summary: dict[str, Any],
    exceptions: pd.DataFrame,
) -> dict[str, Any]:
    """Assemble everything the model is given — and nothing else.

    Only this run's own figures and rows are sent. No config, no other runs,
    no other clients.
    """
    rows: list[dict[str, Any]] = []
    for _, row in exceptions.iterrows():
        books = row.get("books_record")
        portal = row.get("portal_record")
        rows.append({
            "classification": _clean(row.get("classification")),
            "difference_type": _clean(row.get("difference_type")),
            "confidence_band": _clean(row.get("confidence_band")),
            "match_reason": _clean(row.get("match_reason")),
            "books_value": round(_value_of(books), 2),
            "portal_value": round(_value_of(portal), 2),
            "books_party": _clean((books or {}).get("party_name") or (books or {}).get("deductee_name"))
            if isinstance(books, dict) else None,
            "portal_party": _clean((portal or {}).get("party_name") or (portal or {}).get("deductee_name"))
            if isinstance(portal, dict) else None,
        })

    return {
        "reconciliation_type": recon_type,
        "total_items": summary.get("total_results", 0),
        "by_classification": summary.get("by_classification", {}),
        "by_difference_type": summary.get("by_difference_type", {}),
        "by_confidence_band": summary.get("by_confidence_band", {}),
        "value_by_classification": summary.get("value_by_classification", {}),
        "exception_rows": rows,
    }


def _clean(value: Any) -> Optional[str]:
    """Normalise a pandas value to a JSON-safe string, collapsing NaN to None.

    pandas NaN is truthy, so a naive ``value or None`` renders the literal
    string "nan" into the prompt — which the model would then faithfully
    repeat as if it were a real value.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _build_user_prompt(payload: dict[str, Any], caveats: list[dict[str, Any]]) -> str:
    lines = ["Reconciliation run result:"]
    lines.append(json.dumps(payload, indent=1, ensure_ascii=False, default=str))
    if caveats:
        lines.append(
            "\nChecks the engine could NOT perform for this run (state these as "
            "limitations, do not treat them as findings):"
        )
        for caveat in caveats:
            lines.append(f"- {caveat.get('label')}: {caveat.get('detail')}")
    lines.append(
        "\nWrite the summary. Describe patterns, not individual rows. Cite the "
        "actual counts and figures given above."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def _validate(parsed: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """Validate the model's narrative against the engine's own output.

    This is the guard that keeps the acceptance check "the AI insights
    narrative never claims a classification the engine didn't produce" true
    by construction: a pattern naming a difference type the run does not
    contain is dropped, not displayed.
    """
    engine_diff_types = set((summary.get("by_difference_type") or {}).keys())

    headline = str(parsed.get("headline") or "").strip()
    health_read = str(parsed.get("health_read") or "").strip()

    patterns: list[dict[str, Any]] = []
    raw_patterns = parsed.get("patterns")
    if isinstance(raw_patterns, list):
        for raw in raw_patterns:
            if not isinstance(raw, dict):
                continue
            diff_type = str(raw.get("difference_type") or "").strip()
            # Drop any pattern the engine did not actually produce.
            if diff_type and engine_diff_types and diff_type not in engine_diff_types:
                continue
            actions = [
                str(a).strip()
                for a in (raw.get("actions") or [])
                if str(a).strip()
            ][:3]
            narrative = str(raw.get("narrative") or "").strip()
            if not narrative:
                continue
            try:
                count = int(raw.get("count"))
            except (TypeError, ValueError):
                count = None
            patterns.append({
                "title": str(raw.get("title") or diff_type or "Exception pattern").strip(),
                "difference_type": diff_type or None,
                "count": count,
                "narrative": narrative,
                "actions": actions,
            })

    return {
        "headline": headline,
        "health_read": health_read,
        "patterns": patterns,
        "data_quality_note": str(parsed.get("data_quality_note") or "").strip(),
    }


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def generate_insights(
    run_id: int,
    recon_type: str,
    session_id: str,
    *,
    use_cache: bool = True,
    db_path=None,
) -> dict[str, Any]:
    """Produce (or replay) the narrative for one completed run.

    Returns a dict with ``headline``, ``health_read``, ``patterns``,
    ``data_quality_note``, plus ``model``, ``cached``, ``summary`` and
    ``exception_count`` for the report and UI to use.

    Raises :class:`AIInsightsError` when the narrative cannot be produced.
    The caller surfaces the message; the run itself is never affected.
    """
    summary = queries.get_run_summary(run_id, db_path=db_path)
    run = queries.get_run(run_id, db_path=db_path) or {}
    caveats = run.get("caveats") or []

    # --- Cache: keyed on run_id, which is immutable per Unit 5 ---
    cache_key = str(run_id)
    if use_cache:
        cache = demo_session.read_ai_cache(session_id)
        cached_entry = cache.get(cache_key)
        if isinstance(cached_entry, dict) and cached_entry.get("headline"):
            return {**cached_entry, "cached": True}

    exceptions = _exceptions_frame(run_id, summary, db_path=db_path)

    if exceptions.empty:
        # Nothing to narrate. This is a real, good outcome — say so plainly
        # rather than spending a model call on "everything matched".
        result = _clean_run_result(summary, recon_type)
        _store(session_id, cache_key, result, use_cache=use_cache)
        return {**result, "cached": False}

    payload = _build_payload(recon_type, summary, exceptions)
    model = resolve_model()

    try:
        parsed, _latency, model_id, _raw = llm.call_llm_json(
            _SYSTEM_PROMPT,
            _build_user_prompt(payload, caveats),
            db_path=db_path,
            model_override=model,
        )
    except llm.LLMError as exc:
        raise AIInsightsError(str(exc)) from exc

    validated = _validate(parsed, summary)
    if not validated["headline"] and not validated["health_read"]:
        raise AIInsightsError(
            "parse_error|The model returned no usable summary. The reconciliation "
            "results themselves are unaffected."
        )

    result = {
        **validated,
        "model": model_id,
        "summary": summary,
        "exception_count": int(len(exceptions)),
        "recon_type": recon_type,
    }
    _store(session_id, cache_key, result, use_cache=use_cache)
    return {**result, "cached": False}


def _exceptions_frame(
    run_id: int, summary: dict[str, Any], *, db_path=None
) -> pd.DataFrame:
    """The run's exception rows, most valuable first.

    Every exception classification is included — the narrative needs to see
    the whole picture to describe patterns honestly. Rows are ordered by
    rupee magnitude so the most consequential ones survive the row cap.
    """
    frames = []
    for classification in _EXCEPTION_CLASSIFICATIONS:
        if classification in (summary.get("by_classification") or {}):
            frames.append(queries.get_results(run_id, classification=classification, db_path=db_path))
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return df
    df = df.copy()
    df["_value"] = df.apply(
        lambda r: _value_of(r.get("books_record")) or _value_of(r.get("portal_record")), axis=1
    )
    df = df.sort_values("_value", ascending=False)
    return df.head(MAX_EXCEPTION_ROWS).reset_index(drop=True)


def _clean_run_result(summary: dict[str, Any], recon_type: str) -> dict[str, Any]:
    """The narrative for a run with no exceptions — written, not generated."""
    total = summary.get("total_results", 0)
    matched = (summary.get("by_classification") or {}).get("Matched", 0)
    return {
        "headline": (
            f"All {total} {recon_type} records reconciled cleanly — nothing needs "
            f"your attention."
        ),
        "health_read": (
            f"Every one of the {matched} records in this run matched. There are no "
            f"exceptions to review, no differences to explain, and no value flagged."
        ),
        "patterns": [],
        "data_quality_note": "",
        "summary": summary,
        "exception_count": 0,
        "recon_type": recon_type,
        "model": None,
    }


def _store(
    session_id: str, cache_key: str, result: dict[str, Any], *, use_cache: bool
) -> None:
    """Persist the narrative in the session's cache file. Best-effort."""
    if not use_cache:
        return
    cache = demo_session.read_ai_cache(session_id)
    cache[cache_key] = result
    demo_session.write_ai_cache(session_id, cache)


# ---------------------------------------------------------------------------
# Presentation helpers (used by the chat UI and the report)
# ---------------------------------------------------------------------------


def teaser(insight: dict[str, Any]) -> str:
    """A one-line teaser of the top finding, for the chat stream."""
    patterns = insight.get("patterns") or []
    if patterns:
        top = patterns[0]
        count = top.get("count")
        prefix = f"{count} " if count else ""
        return f"{prefix}{top.get('title')} — {top.get('narrative', '')}".strip()
    return insight.get("headline") or ""


def match_rate(summary: dict[str, Any]) -> Optional[float]:
    """The share of records that matched, as a percentage, or None."""
    total = summary.get("total_results") or 0
    if not total:
        return None
    matched = (summary.get("by_classification") or {}).get("Matched", 0)
    return round(100.0 * matched / total, 1)