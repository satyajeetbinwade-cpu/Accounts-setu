"""AI Analysis UI — Layer 2 group triggers + Layer 3 per-item sub-section.

Presentation only. All logic lives in src/ai_analysis.py. On-demand: nothing
here fires an API call except in response to an explicit button click. The
AI config is loaded lazily and cached in session state; if it can't be loaded
the AI UI degrades to a warning (never a crash, never a silent absence).

Every AI-produced statement is labelled with an "AI" marker so a reviewer is
never in doubt about which system produced it.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src import db
from src import ai_analysis

_AI_CONFIG_KEY = "_ai_config"
_AI_CONFIG_ERROR_KEY = "_ai_config_error"
_AI_WARNINGS_KEY = "_ai_app_warnings"

AI_LABEL = "\U0001F916 AI"


# ---------------------------------------------------------------------------
# Config (cached) + app-level warnings
# ---------------------------------------------------------------------------


def _load_ai_config() -> dict[str, Any] | None:
    """Load (and cache) the AI config. Returns None on failure, recording the
    error so it can be surfaced as an app-level warning once."""
    if _AI_CONFIG_KEY in st.session_state:
        return st.session_state[_AI_CONFIG_KEY]
    try:
        cfg = ai_analysis.load_ai_config()
    except ai_analysis.AIAnalysisError as exc:
        st.session_state[_AI_CONFIG_ERROR_KEY] = str(exc)
        st.session_state[_AI_CONFIG_KEY] = None
        return None
    st.session_state[_AI_CONFIG_KEY] = cfg
    st.session_state[_AI_CONFIG_ERROR_KEY] = None
    return cfg


def _config_error_text() -> str | None:
    return st.session_state.get(_AI_CONFIG_ERROR_KEY)


def _record_warning(error_type: str, detail: str) -> None:
    """Record an app-level warning for auth_error/config_error (they affect
    every subsequent call, not just the triggered item)."""
    if error_type not in ("auth_error", "config_error"):
        return
    st.session_state.setdefault(_AI_WARNINGS_KEY, {})[error_type] = detail


def render_app_warnings() -> None:
    """Render any accumulated app-level AI warnings (auth/config)."""
    warnings = st.session_state.get(_AI_WARNINGS_KEY, {})
    if not warnings:
        return
    if "config_error" in warnings:
        st.warning(
            f"{AI_LABEL} \u2014 AI analysis is unavailable: {warnings['config_error']}"
        )
    if "auth_error" in warnings:
        st.error(
            f"{AI_LABEL} \u2014 Authentication error: {warnings['auth_error']}. "
            "Check the OPENROUTER_API_KEY environment variable."
        )


def ai_config_available() -> bool:
    """True if the AI config loaded and AI analysis is usable."""
    return _load_ai_config() is not None


def get_ai_config() -> dict[str, Any] | None:
    """Public accessor for the (cached) AI config, or None if unavailable."""
    return _load_ai_config()


# ---------------------------------------------------------------------------
# Eligibility / routing hint
# ---------------------------------------------------------------------------


def is_eligible(classification: str, config: dict[str, Any]) -> bool:
    return ai_analysis.is_analyzable_classification(classification, config)


def route_hint(
    classification: str, difference_type: Any, config: dict[str, Any]
) -> tuple[str, str]:
    """(model_id, routing_reason) for display on the not-analyzed trigger."""
    return ai_analysis.select_model(classification, difference_type, config)


# ---------------------------------------------------------------------------
# Layer 2 — group-level trigger
# ---------------------------------------------------------------------------


def render_group_trigger(
    group: pd.DataFrame,
    run_id: int,
    config: dict[str, Any],
) -> None:
    """Render the group-level "Analyze group" button for a Layer 2 header.

    Computes and displays the cached-vs-new split before firing, refuses to
    fire above the configured ceiling, and runs sequentially with a progress
    indicator. A failure on one item does not abort the rest.
    """
    result_ids = [int(r) for r in group["result_id"].tolist()]

    try:
        conn = db.get_connection()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"{AI_LABEL} \u2014 could not open database: {exc}")
        return

    try:
        cached, new = ai_analysis.count_cached_and_new(conn, result_ids, config)
    except ai_analysis.AIAnalysisError as exc:
        st.warning(f"{AI_LABEL} \u2014 {exc}")
        return
    finally:
        conn.close()

    limit = int(config["limits"].get("max_items_per_group_call", 25))
    total = cached + new
    label = f"Analyze group \u2014 {total} item(s), {cached} cached, {new} new API call(s)"

    over = new > limit
    disabled = over or total == 0

    if over:
        st.caption(
            f"{AI_LABEL} \u2014 {new} fresh API calls exceeds the ceiling of {limit}. "
            "Analyze in smaller sub-groups or item by item."
        )

    if st.button(label, key=f"ai_group_{run_id}_{hash(tuple(result_ids)) % 1_000_000_000}", disabled=disabled):
        _run_group(result_ids, run_id, config, total)


def _run_group(result_ids: list[int], run_id: int, config: dict[str, Any], total: int) -> None:
    """Execute a group analysis sequentially with a progress indicator."""
    conn = db.get_connection()
    try:
        progress = st.progress(0)
        status = st.empty()
        done = 0
        failed = 0
        for i, rid in enumerate(result_ids, start=1):
            status.write(f"{AI_LABEL} analyzing item {i} of {total}\u2026")
            try:
                out = ai_analysis.analyze_result(conn, rid, config, force=False)
                if out.get("status") == "failed":
                    failed += 1
                    _record_warning(out.get("error_type"), out.get("error_detail") or "")
            except ai_analysis.AIAnalysisError as exc:
                failed += 1
                _record_warning("config_error", str(exc))
            done += 1
            progress.progress(done / total)
        status.write(
            f"{AI_LABEL} group done \u2014 {done} item(s), {failed} failed."
        )
    finally:
        conn.close()
    st.rerun()


# ---------------------------------------------------------------------------
# Layer 3 — per-item AI analysis sub-section
# ---------------------------------------------------------------------------


def render_ai_analysis_section(
    row: pd.Series,
    recon_type: str,
    run_id: int,
    config: dict[str, Any],
) -> None:
    """Render the AI Analysis sub-section inside a Layer 3 item expansion.

    Placed below the existing comparison/review controls by the caller. Renders
    one of four states: not analyzed / analyzing / analyzed / failed. Returns
    early (renders nothing) for ineligible (e.g. Matched) items.
    """
    classification = row["classification"]
    if not is_eligible(classification, config):
        return

    result_id = int(row["result_id"])
    fingerprint = row.get("fingerprint")

    conn = db.get_connection()
    try:
        latest = ai_analysis.get_latest_analysis(conn, result_id)
        cached = None
        if latest is None and fingerprint:
            cached = ai_analysis.get_cached_analysis(conn, fingerprint)
    finally:
        conn.close()

    st.markdown("---")
    st.markdown(f"**{AI_LABEL} Analysis**")
    st.caption(
        "On-demand review aid. Nothing is posted or corrected automatically \u2014 "
        "an accountant reviews and acts."
    )

    if latest is not None and latest["status"] == "success":
        _render_analyzed(row, recon_type, run_id, config, latest, is_cached=False)
    elif latest is not None and latest["status"] == "failed":
        _render_failed(row, recon_type, run_id, config, latest)
    elif cached is not None:
        _render_analyzed(row, recon_type, run_id, config, cached, is_cached=True)
    else:
        _render_not_analyzed(row, recon_type, run_id, config)


# --- state renderers --------------------------------------------------------


def _render_not_analyzed(row, recon_type, run_id, config) -> None:
    result_id = int(row["result_id"])
    model_id, _reason = route_hint(row["classification"], row.get("difference_type"), config)
    st.caption(f"Would route to model: {model_id}")
    if st.button("Analyze this item", key=f"ai_item_{run_id}_{result_id}", type="primary"):
        _run_single(result_id, config, force=False)


def _run_single(result_id: int, config: dict[str, Any], force: bool) -> None:
    conn = db.get_connection()
    try:
        with st.spinner(f"{AI_LABEL} analyzing\u2026"):
            out = ai_analysis.analyze_result(conn, result_id, config, force=force)
        if out.get("status") == "failed":
            _record_warning(out.get("error_type"), out.get("error_detail") or "")
    except ai_analysis.AIAnalysisError as exc:
        _record_warning("config_error", str(exc))
    finally:
        conn.close()
    st.rerun()


def _render_analyzed(row, recon_type, run_id, config, rec: dict, *, is_cached: bool) -> None:
    result_id = int(row["result_id"])

    badges = st.columns([1, 1, 1, 1])
    with badges[0]:
        st.badge(f"AI \u00b7 {rec.get('confidence', '?')}", color=_confidence_color(rec.get("confidence")))
    with badges[1]:
        st.badge(f"model: {rec.get('model_used', '?')}", color="blue")
    if is_cached:
        with badges[2]:
            ts = rec.get("created_at", "")[:19].replace("T", " ")
            st.badge(f"cached \u00b7 {ts}", color="gray")

    if rec.get("data_sufficient") is False or rec.get("data_sufficient") == 0:
        st.warning(f"{AI_LABEL} \u2014 insufficient data: this item needs information the system does not have.")

    st.markdown(f"**{rec.get('issue_summary', '')}**")

    causes = rec.get("probable_causes")
    if isinstance(causes, list) and causes:
        for c in causes:
            st.markdown(f"- {c}")

    if rec.get("suggested_fix"):
        st.markdown(f"**Suggested fix:** {rec['suggested_fix']}")

    if rec.get("reasoning"):
        with st.expander("Why the model concluded this"):
            st.write(rec["reasoning"])

    # Re-analyze forces a fresh call and inserts a new row (never updates).
    if st.button("Re-analyze", key=f"ai_re_{run_id}_{result_id}"):
        _run_single(result_id, config, force=True)


def _render_failed(row, recon_type, run_id, config, rec: dict) -> None:
    result_id = int(row["result_id"])
    error_type = rec.get("error_type") or "api_error"
    detail = rec.get("error_detail") or ""
    st.error(f"{AI_LABEL} \u2014 analysis failed ({error_type})")
    if detail:
        st.caption(detail)
    if st.button("Retry", key=f"ai_retry_{run_id}_{result_id}"):
        _run_single(result_id, config, force=True)


def _confidence_color(confidence: Any) -> str:
    return {
        "high": "green",
        "medium": "orange",
        "low": "red",
    }.get(confidence, "gray")