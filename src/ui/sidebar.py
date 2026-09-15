"""Sidebar: signed-in identity, the app's navigation grouping, and the
optional run picker for the advanced screens.

The GUIDED flow (`src/ui/reconcile_flow.py`) is the default way into a
finished report and owns its own Client / Period / Recon type selection
(Stage 1). The sidebar deliberately does NOT duplicate that: re-selecting
the same context in two places was the main way the old flat-tab app let a
user's context drift out from under them.

The advanced screens (Review / Run / Compare / Config / Export) still read
the run held in session state (src.ui.state.get_selected_run_id()).
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src import queries
from src.ui import discovery, state
from src.ui.formatting import run_label


def render_sidebar(current_user: Optional[dict[str, Any]] = None) -> None:
    st.sidebar.title("Setu \u2014 Recon Review")
    st.sidebar.caption("Reconcile is the guided path. Run picker lives under Run.")

    if current_user is not None:
        st.sidebar.caption(
            f"**{current_user['display_name']}** \u00b7 {current_user['role_name']}"
        )
    st.sidebar.divider()

    # Context row — reflects whatever the guided flow (or a previous screen)
    # last selected. There are deliberately NO widgets here: the Reconcile
    # flow's Stage 1 is the one place Client / Period / Recon type is chosen,
    # so nothing can be re-selected behind the user's back.
    client = st.session_state.get("ctx_client")
    period = st.session_state.get("ctx_period")
    recon_type = st.session_state.get("ctx_recon_type")

    st.sidebar.markdown("**Current context**")
    if client and period and recon_type:
        st.sidebar.caption(f"{client} \u00b7 {period} \u00b7 {recon_type}")
    else:
        st.sidebar.caption(
            "Not set yet \u2014 choose a client, period and recon type on the "
            "**Reconcile** screen."
        )

    st.sidebar.divider()

    if not client:
        return

    try:
        periods = discovery.list_periods(client)
    except Exception as exc:  # noqa: BLE001
        st.sidebar.error(f"Couldn't list periods for {client}.")
        with st.sidebar.expander("Details"):
            st.sidebar.code(str(exc))
        return

    if not periods or not recon_type:
        return

    # Run picker — only useful for the advanced screens, so it stays scoped
    # and secondary instead of being the first thing on the page.
    with st.sidebar.expander("Pick a run (advanced screens)", expanded=False):
        try:
            runs_df = queries.list_runs(client=client, period=period, recon_type=recon_type)
        except Exception as exc:  # noqa: BLE001
            st.error("Couldn't load the list of runs.")
            st.code(str(exc))
            state.set_selected_run_id(None)
            return

        if runs_df.empty:
            st.info(
                "No runs yet for this context. Use the **Reconcile** screen to "
                "run one, or the **Run** tool for the manual path."
            )
            state.set_selected_run_id(None)
            return

        run_ids = runs_df["run_id"].tolist()
        labels = {}
        for run_id in run_ids:
            run_row = runs_df[runs_df["run_id"] == run_id].iloc[0].to_dict()
            try:
                summary = state.get_summary_cached(int(run_id))
            except Exception:
                summary = None
            labels[run_id] = run_label(run_row, summary)

        current = state.get_selected_run_id()
        if current not in run_ids:
            current = run_ids[0]  # newest first

        chosen = st.selectbox(
            "Run",
            run_ids,
            index=run_ids.index(current),
            format_func=lambda rid: labels.get(rid, f"Run {rid}"),
            key="sb_run_select",
        )

        if chosen != state.get_selected_run_id():
            state.set_selected_run_id(chosen)
            state.invalidate_results_cache(chosen)

        run_row = runs_df[runs_df["run_id"] == chosen].iloc[0]
        st.caption(
            f"Run **{chosen}** \u00b7 {run_row['run_timestamp'].replace('T', ' ').split('.')[0]}"
        )
