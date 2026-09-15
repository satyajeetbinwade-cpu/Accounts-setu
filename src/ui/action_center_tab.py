"""Module 8 — Action Center UI (Recon Exceptions only).

The capstone working queue. Design implementation notes
(Setu_Phase2_UI_Foundation_Build_Prompt + Module 8's design table — NO new
component is introduced anywhere in this module):

- Unified queue hub: full-width table, filterable by client /
  module-of-origin / type / priority / age / assignee. Headline (total open
  items) → grouped (by module-of-origin / priority) → detail (the queue
  table itself) — Foundation 3.6.
- Row — materiality-gated exclusion: a checkbox at the row's start; rows
  above the materiality threshold render disabled/greyed with an inline
  reason beneath (Delete vs Deactivate 3.2's checkbox extension +
  Disabled-with-inline-reason 3.4). The disabled state is distinguishable
  without color alone (reduced contrast + the inline reason text).
- Row — confidence/classification: the confidence badge (3.1) inherited
  DIRECTLY from the origin's own tagging — passthrough only, never
  re-derived or re-labelled.
- Row — staleness flag: a small inline badge, "May be based on incomplete
  data", reusing F5's status token directly (not a new named component).
- Assignment / reassignment: inline assignee + due-date fields; any change
  writes a REAL F4 entry, with priority/due-date staying visible before and
  after. No mandatory reason prompt (assignment isn't a sensitive-field
  edit).
- Ageing / escalation: a visible escalation badge on an item past its
  configured age threshold, firing a C3-routed notification — the same
  escalation pattern as C2's ambiguous-filing state and F5's sync-failure
  escalation.
- Acting on an item: clicking a row routes directly into Module 2's own
  exception detail screen — Module 8 renders no resolution UI of its own.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.action_center import service as ac
from src.auth import service as auth
from src.ui.theme import (
    render_accent_pill,
    render_confidence_badge,
    render_inline_reason,
    render_placeholder_badge,
    render_warning_banner,
)

_PRIORITY_LABEL = {
    "escalated": "Escalated",
    "high": "High",
    "normal": "Normal",
    "low": "Low",
}

# Priority → semantic pill class (reuses existing token pills; no new color).
_PRIORITY_PILL = {
    "escalated": "danger",
    "high": "ai",
    "normal": "accent",
    "low": "placeholder",
}


def render_action_center_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "action_center.view"):
        st.warning("You don't have access to the Action Center.")
        return

    st.subheader("Action Center")
    st.caption(
        "The single working queue — every open Flagged Item, from every source "
        "module, in one place."
    )

    # Ageing/escalation runs on each render (idempotent) so a past-threshold
    # item visibly escalates without a separate cron.
    ac.run_ageing_escalation(actor=current_user.get("username", "system"))

    _render_queue(current_user)


# ---------------------------------------------------------------------------
# Unified queue — headline → grouped → detail (3.6)
# ---------------------------------------------------------------------------


def _render_queue(current_user: dict[str, Any]) -> None:
    all_items = ac.list_flagged_items(status="open")
    counts = ac.item_counts(all_items)

    # --- Headline: total open items -------------------------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(
        f'<div style="font-size:34px;font-weight:700;color:var(--setu-text-primary);">'
        f'{counts["total"]}</div>'
        f'<div style="font-size:12px;color:var(--setu-text-secondary);">'
        f'open item(s) across {len(counts["by_source"])} source module(s)</div>',
        unsafe_allow_html=True,
    )
    st.markdown('</div>', unsafe_allow_html=True)

    if not all_items:
        st.info("No open items in the queue.")
        _render_future_sources_note()
        return

    # --- Grouped: by module-of-origin + by priority ---------------------
    st.markdown("**By module of origin**")
    src_cols = st.columns(max(len(counts["by_source"]), 1))
    for col, (src, n) in zip(src_cols, sorted(counts["by_source"].items())):
        with col:
            render_accent_pill(f"{src} — {n}")

    st.markdown("**By priority**")
    pri_cols = st.columns(max(len(counts["by_priority"]), 1))
    for col, (pri, n) in zip(pri_cols, sorted(counts["by_priority"].items(), key=lambda kv: _PRIORITY_LABEL.get(kv[0], "z"))):
        with col:
            st.markdown(
                f'<span class="setu-token-pill {_PRIORITY_PILL.get(pri, "placeholder")}">'
                f'{_PRIORITY_LABEL.get(pri, pri)} — {n}</span>',
                unsafe_allow_html=True,
            )

    if counts["stale"]:
        render_warning_banner(
            f"{counts['stale']} item(s) may be based on incomplete data — F5 reports "
            "degraded sync health for the underlying client."
        )

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Filters (generic dimensions only) ------------------------------
    filtered = _render_filters(all_items)

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Batch action (surfaces the origin's own mechanism) -------------
    _render_batch_bar(filtered, current_user)

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Detail: the queue table itself ---------------------------------
    if not filtered:
        st.info("No items match these filters.")
        _render_future_sources_note()
        return

    for item in filtered:
        _render_item_row(item, current_user)

    _render_future_sources_note()


def _render_filters(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter bar — ONLY the generic dimensions (client / module-of-origin /
    type / priority / age / assignee)."""
    clients = sorted({i["client_name"] for i in items})
    sources = sorted({i["source_module"] for i in items})
    types = sorted({i["type"] for i in items})
    priorities = sorted({i["priority"] for i in items})
    assignees = sorted({i["assignee"] for i in items if i.get("assignee")})

    c1, c2, c3 = st.columns(3)
    with c1:
        f_client = st.selectbox("Client", ["All"] + clients, key="ac_f_client")
        f_source = st.selectbox("Module of origin", ["All"] + sources, key="ac_f_source")
    with c2:
        f_type = st.selectbox("Type", ["All"] + types, key="ac_f_type")
        f_priority = st.selectbox("Priority", ["All"] + priorities, key="ac_f_priority")
    with c3:
        f_assignee = st.selectbox("Assignee", ["All"] + assignees, key="ac_f_assignee")
        f_age = st.number_input("Min age (days)", min_value=0, value=0, step=1, key="ac_f_age")

    def _keep(item: dict[str, Any]) -> bool:
        if f_client != "All" and item["client_name"] != f_client:
            return False
        if f_source != "All" and item["source_module"] != f_source:
            return False
        if f_type != "All" and item["type"] != f_type:
            return False
        if f_priority != "All" and item["priority"] != f_priority:
            return False
        if f_assignee != "All" and item.get("assignee") != f_assignee:
            return False
        if f_age and (item.get("age_days") or 0) < int(f_age):
            return False
        return True

    return [i for i in items if _keep(i)]


def _render_batch_bar(items: list[dict[str, Any]], current_user: dict[str, Any]) -> None:
    """Batch action bar. Materiality-gated items are excluded at the QUEUE
    level; the origin's own mechanism is surfaced (never a Module 8 copy)."""
    eligible, excluded = ac.eligible_for_batch(items)
    st.markdown("**Batch action**")
    st.caption(
        f"{len(eligible)} item(s) eligible for batch approval · "
        f"{len(excluded)} excluded (materiality-gated)."
    )

    selected_refs = st.session_state.get("ac_batch_selected", set())
    if st.button(
        f"Batch approve {len(selected_refs)} selected",
        type="primary",
        disabled=not selected_refs,
        key="ac_batch_apply",
    ):
        chosen = [i for i in items if i["item_ref"] in selected_refs]
        result = ac.batch_action(chosen, actor=current_user.get("username", "unknown"))
        st.session_state["ac_batch_selected"] = set()
        st.success(
            f"Batch approval applied to {result['applied_total']} item(s) via the "
            "originating module's own mechanism."
        )
        st.rerun()


def _render_item_row(item: dict[str, Any], current_user: dict[str, Any]) -> None:
    """One generic queue row. Pulls ONLY from the shared Flagged Item shape —
    no origin-specific field names appear here."""
    exclusion_reason = ac.batch_exclusion_reason(item)
    selected = st.session_state.get("ac_batch_selected", set())

    st.markdown('<div class="setu-card" style="padding:14px 18px;">', unsafe_allow_html=True)
    c_check, c_main, c_meta = st.columns([0.6, 5, 2.4])

    # --- Materiality-gated checkbox (3.2 checkbox extension + 3.4) ------
    with c_check:
        if exclusion_reason:
            st.checkbox(
                "select", key=f"ac_chk_{item['item_ref']}", disabled=True,
                label_visibility="collapsed",
            )
            render_inline_reason(exclusion_reason)
        else:
            checked = item["item_ref"] in selected
            new_val = st.checkbox(
                "select", value=checked, key=f"ac_chk_{item['item_ref']}",
                label_visibility="collapsed",
            )
            if new_val and item["item_ref"] not in selected:
                selected.add(item["item_ref"])
                st.session_state["ac_batch_selected"] = selected
            elif not new_val and item["item_ref"] in selected:
                selected.discard(item["item_ref"])
                st.session_state["ac_batch_selected"] = selected

    # --- Main: type, confidence (passthrough), the "why" ----------------
    with c_main:
        st.markdown(
            f'<span style="font-size:13.5px;font-weight:700;color:var(--setu-text-primary);">'
            f'{item["type"]}</span> '
            f'<span style="font-size:12px;color:var(--setu-text-secondary);">'
            f'{item["origin_label"]} · {item["client_name"]}</span>',
            unsafe_allow_html=True,
        )
        # Confidence badge — passthrough of the origin's own tagging (3.1).
        if item.get("confidence_source") == "ai":
            render_confidence_badge("ai", pct=item.get("confidence_pct"))
        else:
            render_confidence_badge("rule", label=item.get("confidence_label") or "Rule match")

        # Priority + age + escalation + staleness (generic).
        st.markdown(
            f'<span class="setu-token-pill {_PRIORITY_PILL.get(item["priority"], "placeholder")}">'
            f'{_PRIORITY_LABEL.get(item["priority"], item["priority"])}</span>',
            unsafe_allow_html=True,
        )
        if item.get("age_escalated"):
            st.markdown(
                '<span class="setu-token-pill danger">Ageing — escalated</span>',
                unsafe_allow_html=True,
            )
        if item.get("staleness"):
            st.markdown(
                '<span class="setu-token-pill ai">May be based on incomplete data</span>',
                unsafe_allow_html=True,
            )

        # THE "why" — always visible before a person acts (never a tooltip).
        if item.get("reason"):
            st.markdown(
                f'<div style="font-size:13px;color:var(--setu-text-primary);margin-top:6px;">'
                f'{item["reason"]}</div>',
                unsafe_allow_html=True,
            )
        if item.get("evidence_summary"):
            st.caption(item["evidence_summary"])

    # --- Meta: assignee / due date / age + actions ----------------------
    with c_meta:
        st.caption(f"Age: {item.get('age_days', 0)} day(s)")
        st.caption(f"Assignee: {item.get('assignee') or 'Unassigned'}")
        st.caption(f"Due: {item.get('due_date') or '—'}")

        if st.button("Open in origin", key=f"ac_open_{item['item_ref']}"):
            _route_to_origin(item)
        with st.popover("Assign", use_container_width=True):
            _render_assign_controls(item, current_user)

    st.markdown('</div>', unsafe_allow_html=True)


def _render_assign_controls(item: dict[str, Any], current_user: dict[str, Any]) -> None:
    """Inline assignee + due-date controls. Any change writes a REAL F4
    entry; priority/due-date stay visible before and after. No mandatory
    reason prompt (assignment isn't a sensitive-field edit)."""
    st.caption("Reassigning never changes priority — it stays visible and unchanged.")
    assignee = st.text_input(
        "Assignee", value=item.get("assignee") or "", key=f"ac_assignee_{item['item_ref']}",
    )
    due = st.text_input(
        "Due date (YYYY-MM-DD)", value=item.get("due_date") or "", key=f"ac_due_{item['item_ref']}",
    )
    if st.button("Save assignment", key=f"ac_assign_save_{item['item_ref']}"):
        try:
            ac.assign_item(
                item["item_ref"], assignee=assignee or None, due_date=due or None,
                actor=current_user.get("username", "unknown"),
            )
            st.success("Assignment saved (logged to F4).")
            st.rerun()
        except ac.ActionCenterError as exc:
            st.error(str(exc))


def _route_to_origin(item: dict[str, Any]) -> None:
    """Route into the origin module's OWN detail screen — Module 8 renders no
    resolution UI of its own. Sets the origin's detail session key + a
    cross-tab handoff request, then switches to that tab."""
    route = ac.detail_route_for_item(item)
    if not route:
        st.toast("No detail screen is available for this item's origin yet.")
        return
    detail_key = route.get("detail_session_key")
    origin_id = route.get("origin_id")
    if detail_key and origin_id is not None:
        # Module 2's tab reads this on render and opens its own detail screen.
        st.session_state["m2_detail_from_action_center"] = origin_id
        st.session_state["_ac_open_detail_from_queue"] = True
        # Ask app.py to switch to the origin's tab on the next run.
        st.session_state["_goto_tab"] = route.get("target_tab_key", "Reconciliation")
        st.rerun()


def _render_future_sources_note() -> None:
    """Forward-flag note: the generic interface is the whole point of this
    module. Modules 1/4/5/6 slot in later with zero changes to the queue's
    core rendering/filtering code."""
    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.caption(
        "This queue is built against the shared Flagged Item interface. "
        "Additional sources slot in without a rebuild:"
    )
    cols = st.columns(4)
    for col, module in zip(cols, ["Module 1", "Module 4", "Module 5", "Module 6"]):
        with col:
            render_placeholder_badge(module)