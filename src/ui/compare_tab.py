"""Compare tab \u2014 what did a config change actually do?

Select two runs of the same client/period/recon_type and show aggregate
movement between classification buckets, then the individual results whose
classification changed, both verdicts and both match_reason values.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src import queries
from src.ui.formatting import money


def render_compare_tab() -> None:
    client = st.session_state.get("ctx_client")
    period = st.session_state.get("ctx_period")
    recon_type = st.session_state.get("ctx_recon_type")

    if not (client and period and recon_type):
        st.info(
            "Pick a **client**, **period**, and **recon type** in the sidebar first. "
            "Then come back here to compare two runs."
        )
        return

    try:
        runs_df = queries.list_runs(client=client, period=period, recon_type=recon_type)
    except Exception as exc:  # noqa: BLE001
        st.warning("Couldn't load the list of runs for this selection.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    if runs_df.empty:
        st.info(
            "No runs yet for this client/period/recon type. "
            "Go to the **Run** tab and execute one first."
        )
        return

    if len(runs_df) < 2:
        st.info(
            "Need at least two runs for this client/period/recon type to compare. "
            "Execute another run from the **Run** tab and come back."
        )
        return

    run_ids = runs_df["run_id"].tolist()
    labels = {
        rid: f"Run {rid} \u2014 {runs_df[runs_df.run_id == rid].iloc[0]['run_timestamp'].split('.')[0].replace('T', ' ')}"
        for rid in run_ids
    }

    c1, c2 = st.columns(2)
    with c1:
        run_a = st.selectbox("Run A (baseline)", run_ids, index=min(1, len(run_ids) - 1),
                              format_func=lambda r: labels[r], key="cmp_run_a")
    with c2:
        run_b = st.selectbox("Run B (new)", run_ids, index=0,
                              format_func=lambda r: labels[r], key="cmp_run_b")

    if run_a == run_b:
        st.warning("Select two different runs to compare.")
        return

    try:
        result = queries.compare_runs(run_a, run_b)
    except Exception as exc:  # noqa: BLE001
        st.error("Couldn't compare these two runs.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    st.subheader("Movement")
    st.caption(
        f"{result['changed_count']} result(s) changed classification, "
        f"{result['unchanged_count']} unchanged between run {run_a} and run {run_b}."
    )

    # Headline metrics \u2014 scannable in two seconds.
    head_cols = st.columns(3)
    with head_cols[0]:
        st.metric("Changed", result["changed_count"])
    with head_cols[1]:
        st.metric("Unchanged", result["unchanged_count"])
    with head_cols[2]:
        st.metric(
            "Change rate",
            f"{(result['changed_count'] / max(result['changed_count'] + result['unchanged_count'], 1)) * 100:.1f}%",
        )

    movement = result["movement"]
    rows = []
    for from_cls, tos in movement.items():
        for to_cls, count in tos.items():
            rows.append({"from": from_cls, "to": to_cls, "count": count, "moved": from_cls != to_cls})
    if rows:
        st.markdown("**Bucket-to-bucket movement**")
        move_df = pd.DataFrame(rows).sort_values(["moved", "count"], ascending=[False, False])
        st.dataframe(move_df, hide_index=True, width="stretch")

    only_a, only_b = result["only_in_a"], result["only_in_b"]
    c3, c4 = st.columns(2)
    with c3:
        st.metric(f"Only in run {run_a}", len(only_a))
    with c4:
        st.metric(f"Only in run {run_b}", len(only_b))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.subheader("Changed results")
    changed = result["changed"]
    if changed.empty:
        st.info("No classification changes between these two runs.")
    else:
        for _, row in changed.iterrows():
            with st.container(border=True):
                c1, c2 = st.columns(2)
                with c1:
                    st.caption(f"Run {run_a}")
                    st.markdown(
                        f"**{row['classification_a']}**"
                        + (f" / {row['difference_type_a']}" if pd.notna(row.get("difference_type_a")) else "")
                        + f" \u2014 {row['confidence_band_a']} ({row['confidence_score_a']:.0f})"
                    )
                    st.write(row["match_reason_a"])
                with c2:
                    st.caption(f"Run {run_b}")
                    st.markdown(
                        f"**{row['classification_b']}**"
                        + (f" / {row['difference_type_b']}" if pd.notna(row.get("difference_type_b")) else "")
                        + f" \u2014 {row['confidence_band_b']} ({row['confidence_score_b']:.0f})"
                    )
                    st.write(row["match_reason_b"])
