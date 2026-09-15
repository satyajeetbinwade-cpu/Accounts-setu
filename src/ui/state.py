"""Session-state plumbing: the selected run and cached query results.

Streamlit re-runs the whole script on every interaction. To avoid re-running
matching/queries needlessly, results for the currently selected run are
cached in st.session_state and invalidated explicitly on run change or
after a review action (see invalidate_results_cache()).
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src import queries

SK_SELECTED_RUN_ID = "selected_run_id"
SK_RESULTS_CACHE = "_results_cache"          # {run_id: DataFrame}
SK_SUMMARY_CACHE = "_summary_cache"          # {run_id: dict}
SK_CONFIRM_BULK = "_confirm_bulk_review"
SK_FILE_SELECTIONS = "_file_selections"      # {(client,period,recon_type): {source_type: filename}}


def init_state() -> None:
    if SK_SELECTED_RUN_ID not in st.session_state:
        st.session_state[SK_SELECTED_RUN_ID] = None
    if SK_RESULTS_CACHE not in st.session_state:
        st.session_state[SK_RESULTS_CACHE] = {}
    if SK_SUMMARY_CACHE not in st.session_state:
        st.session_state[SK_SUMMARY_CACHE] = {}
    if SK_FILE_SELECTIONS not in st.session_state:
        st.session_state[SK_FILE_SELECTIONS] = {}


def get_file_selections(client: str, period: str, recon_type: str) -> dict[str, str]:
    """Return the per-source-type file selections for a context, or {}."""
    key = (client, period, recon_type)
    return dict(st.session_state[SK_FILE_SELECTIONS].get(key, {}))


def set_file_selection(
    client: str, period: str, recon_type: str, source_type: str, filename: str | None
) -> None:
    """Record (or clear, when filename is None) a source_type -> file choice."""
    key = (client, period, recon_type)
    sel = dict(st.session_state[SK_FILE_SELECTIONS].get(key, {}))
    if filename is None:
        sel.pop(source_type, None)
    else:
        sel[source_type] = filename
    st.session_state[SK_FILE_SELECTIONS][key] = sel


def get_selected_run_id() -> Optional[int]:
    return st.session_state.get(SK_SELECTED_RUN_ID)


def set_selected_run_id(run_id: Optional[int]) -> None:
    if st.session_state.get(SK_SELECTED_RUN_ID) != run_id:
        st.session_state[SK_SELECTED_RUN_ID] = run_id


def invalidate_results_cache(run_id: Optional[int] = None) -> None:
    """Drop cached results/summary for one run, or all runs if None."""
    if run_id is None:
        st.session_state[SK_RESULTS_CACHE] = {}
        st.session_state[SK_SUMMARY_CACHE] = {}
    else:
        st.session_state[SK_RESULTS_CACHE].pop(run_id, None)
        st.session_state[SK_SUMMARY_CACHE].pop(run_id, None)


def get_results_cached(run_id: int):
    cache = st.session_state[SK_RESULTS_CACHE]
    if run_id not in cache:
        cache[run_id] = queries.get_results(run_id)
    return cache[run_id]


def get_summary_cached(run_id: int) -> dict[str, Any]:
    cache = st.session_state[SK_SUMMARY_CACHE]
    if run_id not in cache:
        cache[run_id] = queries.get_run_summary(run_id)
    return cache[run_id]
