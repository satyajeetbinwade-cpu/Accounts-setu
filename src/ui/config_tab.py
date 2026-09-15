"""Config tab \u2014 read-only. Shows current matching_rules.yaml next to the
selected run's config_snapshot, highlighting differences, so it's easy to
tell whether a run was produced under the settings currently on disk.

No config editing here by design \u2014 config is edited in the file.
"""

from __future__ import annotations

import difflib

import streamlit as st

from src.config_loader import CONFIG_PATH
from src.ui import state
from src import queries


def render_config_tab() -> None:
    run_id = state.get_selected_run_id()

    try:
        current_text = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        st.error(
            "Couldn't read the current matching_rules.yaml. "
            "Check that the file exists and is readable."
        )
        with st.expander("Details"):
            st.code(str(exc))
        return

    if run_id is None:
        st.info(
            "Select a run in the sidebar to compare its config snapshot against "
            "the current matching_rules.yaml. The current file is shown below for reference."
        )
        st.subheader("Current matching_rules.yaml")
        st.code(current_text, language="yaml")
        return

    try:
        run = queries.get_run(run_id)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't load run {run_id}.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    snapshot_text = run["config_snapshot"] if run else ""

    same = current_text == snapshot_text
    if same:
        st.success(
            f"Run {run_id} was produced under the config currently on disk \u2014 no differences."
        )
    else:
        st.warning(
            f"Run {run_id}'s config snapshot **differs** from the current matching_rules.yaml. "
            "Results from this run were not produced under today's settings."
        )

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Current matching_rules.yaml")
        st.code(current_text, language="yaml")
    with c2:
        st.subheader(f"Run {run_id} config_snapshot")
        st.code(snapshot_text, language="yaml")

    if not same:
        st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
        st.subheader("Diff (current \u2192 run snapshot)")
        diff_lines = list(
            difflib.unified_diff(
                snapshot_text.splitlines(),
                current_text.splitlines(),
                fromfile=f"run_{run_id}_snapshot",
                tofile="current",
                lineterm="",
            )
        )
        st.code("\n".join(diff_lines) or "(no textual diff)", language="diff")
