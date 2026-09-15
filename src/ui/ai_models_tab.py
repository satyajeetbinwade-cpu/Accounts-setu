"""AI Models — the central model registry screen.

One place to see and change which model every AI touchpoint uses, with a
dropdown populated from OpenRouter's real catalogue.

Design notes (Setu_Phase2_UI_Foundation_Build_Prompt):
- Headline → grouped → detail (3.6): the headline is the count of
  touchpoints and how many are unassigned; the grouped layer is the
  category; the detail is one card per touchpoint.
- The catalogue source is stated plainly (live fetch vs bundled fallback)
  rather than implied — a dropdown that silently shows a stale list is
  worse than one that says it's stale.
- A blocked save renders its reason inline (3.4), never as a tooltip.
- No new component or token is introduced; this reuses the existing card,
  pill, banner and inline-reason primitives.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.ai_models import catalog as catalog_mod
from src.ai_models import service as ai_models
from src.auth import service as auth
from src.ui.theme import (
    render_accent_pill,
    render_inline_reason,
    render_live_status_chip,
    render_warning_banner,
)

# Permission that gates editing. Reuses the existing ingestion-model
# permission rather than inventing a new one: the same people who could
# already change the ingestion model are the people who should be able to
# change any model.
_MANAGE_PERMISSION = "ingestion_ai.llm.manage"

_CATEGORY_ORDER = ["Ingestion", "Extraction", "Analysis"]


def render_ai_models_tab(current_user: dict[str, Any]) -> None:
    can_manage = auth.has_permission(current_user, _MANAGE_PERMISSION)

    st.subheader("AI Models")
    st.caption(
        "Every AI touchpoint in the platform, and the model it calls. "
        "Pick from OpenRouter's live catalogue or enter an id directly."
    )

    summary = ai_models.assignment_summary()
    catalog_state = ai_models.catalog_status()

    # --- Headline (3.6) -------------------------------------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    cols = st.columns([2, 2, 3])
    with cols[0]:
        st.metric("AI touchpoints", summary["total"])
    with cols[1]:
        st.metric("Unassigned", summary["unassigned"])
    with cols[2]:
        st.markdown("**Model catalogue**")
        if catalog_state["live"]:
            render_live_status_chip("connected", label=f"{catalog_state['count']} models")
            st.caption(
                f"Fetched from OpenRouter "
                f"{str(catalog_state['fetched_at']).replace('T', ' ').split('.')[0]}."
            )
        else:
            render_live_status_chip("needs_reauth", label="Bundled list")
            st.caption(
                "Showing the bundled fallback list. Refresh to load OpenRouter's "
                "full catalogue."
            )
    st.markdown('</div>', unsafe_allow_html=True)

    if summary["unassigned"]:
        render_warning_banner(
            f"{summary['unassigned']} touchpoint(s) have no primary model. "
            "Calls to those touchpoints will fall back to their module's own default."
        )

    # --- Catalogue refresh ----------------------------------------------
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("Refresh model list", key="aim_refresh", disabled=not can_manage):
            with st.spinner("Fetching OpenRouter's model list\u2026"):
                try:
                    result = ai_models.refresh_catalog(actor=current_user["username"])
                    st.success(f"Loaded {result['count']} models from OpenRouter.")
                    st.rerun()
                except catalog_mod.CatalogError as exc:
                    st.error("Couldn't refresh the model list.")
                    render_inline_reason(str(exc))
    with c2:
        if not can_manage:
            render_inline_reason(
                "Requires Admin or Partner — changing a model affects every run."
            )
        else:
            st.caption(
                "The catalogue is a convenience, not a constraint: any valid "
                "`vendor/model` id is accepted even if it isn't listed yet."
            )

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Grouped by category, then detail per touchpoint ----------------
    assignments = ai_models.list_assignments()
    if not assignments:
        st.info("No AI touchpoints are registered yet.")
        return

    options = ai_models.model_options()
    option_ids = [o["model_id"] for o in options]
    labels = {o["model_id"]: ai_models.model_label(o) for o in options}

    for category in _CATEGORY_ORDER:
        rows = [a for a in assignments if a["category"] == category]
        if not rows:
            continue
        st.markdown(f"**{category}**")
        for row in rows:
            _render_touchpoint(row, option_ids, labels, can_manage, current_user)

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    _render_change_log()


def _render_touchpoint(
    row: dict[str, Any],
    option_ids: list[str],
    labels: dict[str, str],
    can_manage: bool,
    current_user: dict[str, Any],
) -> None:
    key = row["touchpoint_key"]
    primary = row.get("primary_model") or ""
    fallback = row.get("fallback_model") or ""

    with st.container(border=True):
        head_l, head_r = st.columns([3, 2])
        with head_l:
            st.markdown(f"**{row['label']}**")
            st.caption(f"`{key}`")
        with head_r:
            if not row.get("is_active"):
                render_accent_pill("Inactive")
            elif primary:
                render_accent_pill(f"Primary: {primary}")
            else:
                render_inline_reason("No primary model assigned.")

        if row.get("description"):
            st.caption(row["description"])

        # --- Model pickers ----------------------------------------------
        # A model already assigned but absent from the catalogue is still
        # offered, so opening this screen never silently changes a value.
        primary_options = _with_current(option_ids, primary)
        fallback_options = _with_current(option_ids, fallback)

        c1, c2 = st.columns(2)
        with c1:
            chosen_primary = st.selectbox(
                "Primary model",
                primary_options,
                index=primary_options.index(primary) if primary in primary_options else 0,
                format_func=lambda mid: labels.get(mid, mid),
                key=f"aim_primary_{key}",
                disabled=not can_manage,
            )
        with c2:
            chosen_fallback = st.selectbox(
                "Fallback model",
                fallback_options,
                index=fallback_options.index(fallback) if fallback in fallback_options else 0,
                format_func=lambda mid: labels.get(mid, mid),
                key=f"aim_fallback_{key}",
                disabled=not can_manage,
            )

        with st.expander("Or enter a model id directly", expanded=False):
            custom_primary = st.text_input(
                "Primary model id", value="", key=f"aim_custom_primary_{key}",
                placeholder="vendor/model", disabled=not can_manage,
            )
            custom_fallback = st.text_input(
                "Fallback model id", value="", key=f"aim_custom_fallback_{key}",
                placeholder="vendor/model", disabled=not can_manage,
            )

        if can_manage:
            if st.button("Save model assignment", key=f"aim_save_{key}", type="primary"):
                final_primary = (custom_primary or "").strip() or chosen_primary
                final_fallback = (custom_fallback or "").strip() or chosen_fallback
                try:
                    ai_models.set_assignment_models(
                        key,
                        primary_model=final_primary,
                        fallback_model=final_fallback,
                        actor=current_user["username"],
                    )
                    st.success(f"`{key}` now uses `{final_primary}`.")
                    st.rerun()
                except ai_models.AIModelsError as exc:
                    st.error("That assignment wasn't saved.")
                    render_inline_reason(str(exc))
        else:
            render_inline_reason("Read-only — requires Admin or Partner.")

        if row.get("updated_at"):
            st.caption(
                f"Last changed {str(row['updated_at']).replace('T', ' ').split('.')[0]} "
                f"by {row.get('updated_by') or 'unknown'}."
            )


def _with_current(option_ids: list[str], current: str) -> list[str]:
    """Ensure the currently-assigned id is selectable even when it isn't in
    the catalogue — otherwise opening the screen would silently change it.

    Always returns a non-empty list, so the selectbox can never be built
    with zero options.
    """
    options = list(option_ids)
    if current and current not in options:
        options.insert(0, current)
    if not options:
        options = [current or ""]
    return options


def _render_change_log() -> None:
    rows = ai_models.list_change_log(limit=25)
    if not rows:
        return
    with st.expander("Recent model changes", expanded=False):
        for row in rows:
            when = str(row.get("created_at") or "").replace("T", " ").split(".")[0]
            st.markdown(
                f'<div style="font-size:13px;color:var(--setu-text-primary);">'
                f'<span style="color:var(--setu-text-secondary);font-size:12px;">'
                f"{when} · {row.get('actor')}:</span> {row.get('detail') or row.get('action')}</div>",
                unsafe_allow_html=True,
            )
