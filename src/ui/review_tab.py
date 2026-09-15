"""Review tab \u2014 the primary screen.

Summary (counts + rupee values) first, then a filterable working list.
Default view: unreviewed exceptions (non-Matched), ascending confidence.
Every row shows match_reason in full, never behind an expander.
Expanding a row reveals full books/portal records side by side with
differing fields highlighted, plus the raw original row.

When no run is selected, shows a welcome state with a checklist of
source files for the current client/period/recon_type selection.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src import queries
from src.ui import ai_ui, discovery, state
from src.ui.formatting import (
    diff_fields,
    money,
    pretty_original_row,
    record_identity_line,
    review_state_of,
)
from src.ui.theme import (
    BAND_HELP,
    CLASSIFICATION_HELP,
    DIFFERENCE_TYPE_HELP,
    REVIEW_STATE_HELP,
)

CLASSIFICATIONS = ["Matched", "Not in Books", "Not in Portal", "Amount Difference"]
BANDS = ["High", "Medium", "Low"]
REVIEW_STATUS_OPTIONS = ["Unreviewed", "Reviewed", "Stale"]

# Fixed display order for the reconciliation report: most actionable first,
# Matched (least interesting) last.
GROUP_ORDER = ["Amount Difference", "Not in Portal", "Not in Books", "Matched"]

# Classifications whose rupee value counts toward "value at risk".
AT_RISK_CLASSIFICATIONS = ("Not in Portal", "Not in Books", "Amount Difference")


def _classification_help_text() -> str:
    """Plain-language explanation of the four classifications."""
    return "\n".join(f"{cls}: {CLASSIFICATION_HELP[cls]}" for cls in CLASSIFICATIONS)


def _band_help_text() -> str:
    return "\n".join(f"{band}: {BAND_HELP[band]}" for band in BANDS)


def _review_state_help_text() -> str:
    return "\n".join(
        f"{state}: {REVIEW_STATE_HELP[state]}" for state in REVIEW_STATUS_OPTIONS
    )


def _difference_type_help_text(options: list[str]) -> str:
    """Plain-language explanation of the difference_type values present
    in the current run. Falls back to a generic line for any value not
    in the constants dict (so a new difference_type from a future matcher
    still gets a sensible tooltip)."""
    if not options:
        return "No difference_type values in this run."
    lines = []
    for dt in options:
        lines.append(f"{dt}: {DIFFERENCE_TYPE_HELP.get(dt, 'Specific reason this transaction was flagged.')}")
    return "\n".join(lines)


def render_review_tab() -> None:
    ai_ui.render_app_warnings()

    run_id = state.get_selected_run_id()
    if run_id is None:
        _render_welcome()
        return

    client = st.session_state.get("ctx_client")
    period = st.session_state.get("ctx_period")
    recon_type = st.session_state.get("ctx_recon_type")

    df = state.get_results_cached(run_id)
    summary = state.get_summary_cached(run_id)
    review_state_map = queries.get_review_state(client, period, recon_type)

    df = df.copy()
    df["_review_state"] = df.apply(review_state_of, axis=1)
    df["_value"] = df.apply(_row_value, axis=1)

    _render_headline(summary, df)
    st.divider()
    _render_report(df, summary, run_id, client, period, recon_type, review_state_map)


# ---------------------------------------------------------------------------
# Welcome / landing state
# ---------------------------------------------------------------------------


def _render_welcome() -> None:
    """Show a friendly welcome state when no run is selected.

    If a client/period/recon_type is already chosen in the sidebar but
    no runs exist yet, show the source-file checklist so the user knows
    what's missing. If nothing is selected yet, prompt them to pick one.
    """
    client = st.session_state.get("ctx_client")
    period = st.session_state.get("ctx_period")
    recon_type = st.session_state.get("ctx_recon_type")

    st.markdown(
        """
        <div class="setu-welcome">
          <h3>Welcome to Setu Recon Review</h3>
          <p style="margin-bottom:0.5rem;">
            This tool compares your books against the GST/TDS portal and
            flags transactions that need a reviewer's attention.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not (client and period and recon_type):
        st.info(
            "Pick a **client**, **period**, and **recon type** in the sidebar to get started. "
            "If you don't see your client listed, check that the source files are in place under "
            "`data/&lt;client&gt;/&lt;period&gt;/`."
        )
        return

    st.markdown(
        f"**Selected context:** {client} &middot; {period} &middot; {recon_type}"
    )

    try:
        status = discovery.source_file_status(client, period, recon_type)
    except Exception as exc:  # noqa: BLE001
        st.warning(
            "Couldn't read the source-file status for this selection. "
            "You can still try the Run tab \u2014 it will tell you what's missing."
        )
        with st.expander("Details"):
            st.code(str(exc))
        return

    ready = discovery.run_is_executable(status, recon_type)

    items: list[str] = []
    for source_type, info in status.items():
        if info["present"]:
            files = ", ".join(info["files"])
            items.append(
                f'<li><span class="ok">\u2713 {source_type}</span> &mdash; {files}</li>'
            )
        else:
            items.append(
                f'<li><span class="miss">\u2717 {source_type}</span> &mdash; '
                f'missing (expected at <code>{info["path"]}</code>)</li>'
            )

    checklist_html = (
        '<div class="setu-welcome">'
        '<h3 style="margin-top:0;">Source files checklist</h3>'
        '<ul class="setu-checklist">' + "".join(items) + "</ul>"
    )
    if ready:
        checklist_html += (
            '<p style="margin-bottom:0;">All required files are present \u2014 '
            'go to the <strong>Run</strong> tab to execute a reconciliation.</p>'
        )
    else:
        checklist_html += (
            '<p style="margin-bottom:0;">A reconciliation needs the books-side '
            '(tally) file plus at least one portal-side file. '
            'Add the missing files above, then go to the <strong>Run</strong> tab.</p>'
        )
    checklist_html += "</div>"
    st.markdown(checklist_html, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Layer 1 — headline picture
# ---------------------------------------------------------------------------


def _value_at_risk(summary: dict[str, Any]) -> float:
    """Sum of rupee value across the three attention classifications."""
    value_by_cls = summary.get("value_by_classification", {})
    return sum(value_by_cls.get(cls, 0.0) for cls in AT_RISK_CLASSIFICATIONS)


def _render_headline(summary: dict[str, Any], df: pd.DataFrame) -> None:
    st.subheader("Reconciliation summary")

    total = summary.get("total_results", 0)
    by_cls = summary.get("by_classification", {})
    value_by_cls = summary.get("value_by_classification", {})
    value_at_risk = _value_at_risk(summary)

    reviewed = int((df["_review_state"] == "reviewed").sum())
    unreviewed = int((df["_review_state"] != "reviewed").sum())

    cols = st.columns(4)
    with cols[0]:
        st.metric("Total items", total)
    with cols[1]:
        st.metric(
            "Value at risk",
            money(value_at_risk),
            help="Not in Portal + Not in Books + Amount Difference, by value.",
        )
    with cols[2]:
        st.metric("Reviewed", reviewed)
    with cols[3]:
        st.metric("Unreviewed", unreviewed)


# ---------------------------------------------------------------------------
# Layer 2 + 3 — grouped reconciliation report
# ---------------------------------------------------------------------------


def _row_value(row: pd.Series) -> float:
    """Rupee value of a result, mirroring queries.get_run_summary's logic."""
    for side in ("books_record", "portal_record"):
        rec = row.get(side)
        if not isinstance(rec, dict):
            continue
        for key in ("invoice_value", "amount_paid_credited", "tax_deducted"):
            if key in rec and rec[key] is not None:
                try:
                    return abs(float(rec[key]))
                except (TypeError, ValueError):
                    continue
    return 0.0


def _render_report(
    df: pd.DataFrame, summary: dict[str, Any], run_id: int, client: str, period: str,
    recon_type: str, review_state_map: dict[str, dict],
) -> None:
    if df.empty:
        st.info(
            "This run produced no results. "
            "If you expected to see transactions here, check the Run tab \u2014 "
            "the source files may have been empty or unreadable."
        )
        return

    value_by_cls = summary.get("value_by_classification", {})

    # Expanders are open by default for Amount Difference, everything else
    # closed; Matched stays collapsed (least interesting bucket).
    default_open = {"Amount Difference": True, "Not in Portal": False, "Not in Books": False, "Matched": False}

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.subheader("What needs attention")

    for cls in GROUP_ORDER:
        group = df[df["classification"] == cls]
        if group.empty:
            continue

        count = len(group)
        value = value_by_cls.get(cls, group["_value"].sum() if "_value" in group.columns else 0.0)
        title = f"{cls} \u2014 {count} item(s) \u00b7 {money(value)}"
        help_txt = CLASSIFICATION_HELP.get(cls, "")

        with st.expander(title, expanded=default_open.get(cls, False)):
            _render_group_contents(
                group, cls, run_id, client, period, recon_type, review_state_map, help_txt
            )

    st.divider()
    _render_bulk_actions(df, client, period, recon_type, run_id)


def _render_group_contents(
    group: pd.DataFrame, cls: str, run_id: int, client: str, period: str,
    recon_type: str, review_state_map: dict[str, dict], help_txt: str,
) -> None:
    if help_txt:
        st.caption(help_txt)

    ai_config = ai_ui.get_ai_config()
    _ai_enabled = (
        ai_config is not None
        and cls in ai_config["scope"]["analyzable_classifications"]
    )

    # For Amount Difference, sub-group by difference_type at a second level.
    if cls == "Amount Difference":
        sub_types = group["difference_type"].fillna("(none)").unique().tolist()
        # Keep original engine order where possible, put "(none)" last.
        sub_types = sorted(
            sub_types, key=lambda t: (t == "(none)", t.lower())
        )
        for dt in sub_types:
            sub = group[group["difference_type"].fillna("(none)") == dt]
            sub_value = sub["_value"].sum() if "_value" in sub.columns else 0.0
            with st.expander(
                f"{dt} \u2014 {len(sub)} item(s) \u00b7 {money(sub_value)}",
                expanded=True,
            ):
                if _ai_enabled:
                    ai_ui.render_group_trigger(sub, run_id, ai_config)
                _render_scoped_filtered_items(
                    sub, run_id, client, period, recon_type, review_state_map
                )
    else:
        if _ai_enabled:
            ai_ui.render_group_trigger(group, run_id, ai_config)
        _render_scoped_filtered_items(
            group, run_id, client, period, recon_type, review_state_map
        )


def _render_scoped_filtered_items(
    group: pd.DataFrame, run_id: int, client: str, period: str, recon_type: str,
    review_state_map: dict[str, dict],
) -> None:
    """Render filter controls scoped to this group, then the matching items."""
    key_suffix = str(abs(hash((run_id, tuple(group.index.tolist())))) % 10_000_000)

    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            band_filter = st.multiselect(
                "Confidence band", BANDS, default=[], key=f"f_band_{key_suffix}",
                help=_band_help_text(),
            )
        with c2:
            status_filter = st.multiselect(
                "Review status", REVIEW_STATUS_OPTIONS, default=[], key=f"f_status_{key_suffix}",
                help=_review_state_help_text(),
            )
        with c3:
            search = st.text_input(
                "Search (party name, GSTIN/PAN, invoice number)", key=f"f_search_{key_suffix}"
            )

        c4, c5 = st.columns([1, 1])
        with c4:
            min_conf = st.slider("Minimum confidence", 0, 100, 0, key=f"f_min_conf_{key_suffix}")
        with c5:
            default_view = st.checkbox(
                "Show only unreviewed first", value=True, key=f"f_default_{key_suffix}",
                help="Uncheck to see reviewed rows too.",
            )

    filtered = _apply_group_filters(group, band_filter, status_filter, search, min_conf, default_view)
    st.caption(f"{len(filtered)} of {len(group)} items match this filter.")

    filtered = filtered.sort_values("confidence_score", ascending=True)

    for _, row in filtered.iterrows():
        _render_result_row(row, client, period, recon_type, run_id, review_state_map)


def _apply_group_filters(
    group: pd.DataFrame,
    band_filter: list[str],
    status_filter: list[str],
    search: str,
    min_conf: int,
    default_view: bool,
) -> pd.DataFrame:
    out = group

    if band_filter:
        out = out[out["confidence_band"].isin(band_filter)]
    if status_filter:
        status_map = {"Unreviewed": "unreviewed", "Reviewed": "reviewed", "Stale": "stale"}
        wanted = {status_map[s] for s in status_filter}
        out = out[out["_review_state"].isin(wanted)]
    if min_conf:
        out = out[out["confidence_score"] >= min_conf]
    if search:
        needle = search.strip().lower()
        fields = ["party_name", "gstin", "invoice_number", "pan", "deductee_name"]

        def _matches(row) -> bool:
            for side_key in ("books_record", "portal_record"):
                rec = row.get(side_key)
                if not isinstance(rec, dict):
                    continue
                for f in fields:
                    v = rec.get(f)
                    if v and needle in str(v).lower():
                        return True
            return False

        out = out[out.apply(_matches, axis=1)]

    if default_view:
        out = out[out["_review_state"] != "reviewed"]

    return out


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def _render_bulk_actions(
    filtered: pd.DataFrame, client: str, period: str, recon_type: str, run_id: int
) -> None:
    eligible = filtered[filtered["_review_state"] != "reviewed"]
    n = len(eligible)

    with st.container(border=True):
        st.markdown("**Bulk mark reviewed** \u2014 applies to the current filtered view.")
        confirm_key = f"confirm_bulk_{run_id}"
        if confirm_key not in st.session_state:
            st.session_state[confirm_key] = False

        note = st.text_input("Note applied to all bulk-reviewed rows (optional)", key="bulk_note")

        col1, col2 = st.columns([1, 3])
        with col1:
            request = st.button(f"Mark {n} filtered row(s) reviewed", disabled=n == 0)
        if request:
            st.session_state[confirm_key] = True

        if st.session_state[confirm_key]:
            st.warning(
                f"This will mark **{n}** row(s) reviewed under the current filter. "
                "This cannot be undone in bulk \u2014 you would need to clear each individually."
            )
            c_yes, c_no = st.columns(2)
            with c_yes:
                if st.button("Yes, mark all reviewed", type="primary", key="bulk_confirm_yes"):
                    for _, row in eligible.iterrows():
                        queries.mark_reviewed(
                            client, period, recon_type, row["fingerprint"],
                            note=note or None, result=row.to_dict(),
                        )
                    state.invalidate_results_cache(run_id)
                    st.session_state[confirm_key] = False
                    st.success(f"Marked {n} row(s) reviewed.")
                    st.rerun()
            with c_no:
                if st.button("Cancel", key="bulk_confirm_no"):
                    st.session_state[confirm_key] = False
                    st.rerun()


# ---------------------------------------------------------------------------
# Individual result row
# ---------------------------------------------------------------------------


def _render_result_row(
    row: pd.Series, client: str, period: str, recon_type: str, run_id: int,
    review_state_map: dict[str, dict],
) -> None:
    state_name = row["_review_state"]
    badge_color = {"reviewed": "green", "unreviewed": "gray", "stale": "violet"}[state_name]
    badge_text = {"reviewed": "Reviewed", "unreviewed": "Unreviewed", "stale": "STALE"}[state_name]

    cls_color = {
        "Matched": "green", "Amount Difference": "orange",
        "Not in Books": "red", "Not in Portal": "red",
    }.get(row["classification"], "gray")
    band_color = {"High": "green", "Medium": "orange", "Low": "red"}.get(row["confidence_band"], "gray")

    with st.container(border=True):
        top = st.columns([2, 2, 2, 2, 2])
        with top[0]:
            st.badge(row["classification"], color=cls_color)
            if pd.notna(row.get("difference_type")):
                st.caption(row["difference_type"])
        with top[1]:
            st.badge(f"{row['confidence_band']} ({row['confidence_score']:.0f})", color=band_color)
        with top[2]:
            st.badge(badge_text, color=badge_color)
        with top[3]:
            st.caption("Books")
            st.write(record_identity_line(row.get("books_record"), recon_type))
        with top[4]:
            st.caption("Portal")
            st.write(record_identity_line(row.get("portal_record"), recon_type))

        if state_name == "stale":
            prior = review_state_map.get(row["fingerprint"], {})
            prior_desc = prior.get("reviewed_classification") or "?"
            if prior.get("reviewed_difference_type"):
                prior_desc += f" / {prior['reviewed_difference_type']}"
            prior_desc += f" ({prior.get('reviewed_confidence_band') or '?'})"
            current_desc = row["classification"]
            if pd.notna(row.get("difference_type")):
                current_desc += f" / {row['difference_type']}"
            current_desc += f" ({row['confidence_band']})"
            st.error(
                f"Review is stale \u2014 approved as **{prior_desc}**, now **{current_desc}**. "
                "The reviewer's approval no longer applies."
            )

        st.markdown(f"**Reason:** {row['match_reason']}")

        with st.expander("Full records"):
            _render_full_records(row, recon_type)

        _render_review_controls(row, client, period, recon_type, run_id)

        _render_ai_analysis(row, recon_type, run_id)


def _render_full_records(row: pd.Series, recon_type: str) -> None:
    books = row.get("books_record")
    portal = row.get("portal_record")
    differing = diff_fields(books, portal)

    c1, c2 = st.columns(2)
    with c1:
        st.caption("Books record")
        _render_record_table(books, differing)
    with c2:
        st.caption("Portal record")
        _render_record_table(portal, differing)

    c3, c4 = st.columns(2)
    with c3:
        orig = pretty_original_row(books)
        if orig:
            st.caption("Raw original row (books)")
            st.code(orig, language="json")
    with c4:
        orig = pretty_original_row(portal)
        if orig:
            st.caption("Raw original row (portal)")
            st.code(orig, language="json")


def _render_record_table(record: Any, differing: set[str]) -> None:
    if not isinstance(record, dict):
        st.caption("\u2014 no record on this side \u2014")
        return
    data = []
    for k, v in record.items():
        if k == "original_row":
            continue
        marker = "\u26a0\ufe0f" if k in differing else ""
        data.append({"field": k, "value": "" if v is None else str(v), "differs": marker})
    st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch")


def _render_review_controls(
    row: pd.Series, client: str, period: str, recon_type: str, run_id: int
) -> None:
    fp = row["fingerprint"]
    key_base = f"{run_id}_{fp}_{row['result_id']}"

    c1, c2, c3 = st.columns([3, 1, 1])
    with c1:
        existing_note = row.get("reviewer_note")
        note = st.text_input(
            "Reviewer note",
            value=existing_note if pd.notna(existing_note) else "",
            key=f"note_{key_base}",
        )
    with c2:
        if st.button("Mark reviewed", key=f"markrev_{key_base}"):
            queries.mark_reviewed(
                client, period, recon_type, fp, note=note or None, result=row.to_dict()
            )
            state.invalidate_results_cache(run_id)
            st.rerun()
    with c3:
        if st.button("Clear review", key=f"clearrev_{key_base}", disabled=not row.get("reviewed") and not row.get("review_stale")):
            queries.clear_review(client, period, recon_type, fp)
            state.invalidate_results_cache(run_id)
            st.rerun()


def _render_ai_analysis(row: pd.Series, recon_type: str, run_id: int) -> None:
    """Render the AI Analysis sub-section for a single item (Layer 3).

    Delegates to ai_ui; renders nothing for ineligible classifications
    (e.g. Matched) and nothing when the AI config is unavailable. Placed
    below the existing books/portal comparison, match_reason, and review
    controls, all of which are unchanged.
    """
    ai_config = ai_ui.get_ai_config()
    if ai_config is None:
        return
    ai_ui.render_ai_analysis_section(row, recon_type, run_id, ai_config)
