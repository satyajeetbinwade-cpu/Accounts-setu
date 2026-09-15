"""Module 2 — Reconciliation Engine UI (2A GST / 2B TDS / 2C Other).

Two screens, per the build prompt's clarified split:
  * Module 2's OWN screen — live eligible-credit figure as the headline,
    classification counts as the grouped layer, each group clickable into a
    pre-filtered detail view (Foundation 3.6 headline→grouped→detail).
  * Exception DETAIL screen — books-vs-portal side-by-side + a confidence
    badge per matched field/line, the full match_reason always visible in the
    main view (never a tooltip or collapsed expander), and an Accept / Reject
    / Escalate action row.

Module 8 owns the queue (list/filter/prioritize/assign); Module 2 owns the
exception detail screen reached by clicking an item in that queue. Module 8
doesn't exist in this repo yet, so this tab renders the interim queue itself
and labels the Module 8 routing with the Coming-with placeholder badge.

Every screen uses only Foundation tokens and the named components, aside from
the one explicitly-scoped new TDS six-step stepper pattern (which itself uses
only existing semantic color tokens).
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.module2 import service as m2
from src.ui import state
from src.ui.formatting import money
from src.ui.theme import (
    render_accent_pill,
    render_confidence_badge,
    render_info_banner,
    render_inline_reason,
    render_tds_chain_stepper,
    render_warning_banner,
)

SUB_TYPE_LABELS = {"2A": "2A — GST", "2B": "2B — TDS", "2C": "2C — Other"}

# Fixed display order: most actionable first.
CLASSIFICATION_ORDER = ["Amount Difference", "Not in Portal", "Not in Books"]


def render_module2_tab(current_user: dict[str, Any]) -> None:
    st.subheader("Reconciliation Engine")
    st.caption(
        "Module 2 — 2A GST · 2B TDS · 2C Other. Extends Phase 1's matching "
        "engine; every non-clean match becomes an exception."
    )

    # Module 8 (Action Center) → Module 2 handoff: if the queue routed the
    # user here to act on a specific exception, open its OWN detail screen
    # directly, with a "Back to Action Center" affordance. The detail screen
    # below is the same one reached from this tab's own queue.
    routed_exception_id = st.session_state.pop("m2_detail_from_action_center", None)
    if routed_exception_id is not None:
        st.session_state["m2_open_exception"] = routed_exception_id
        if st.button("← Back to Action Center", key="m2_back_to_action_center"):
            st.session_state.pop("m2_open_exception", None)
            st.session_state["_ac_open_detail_from_queue"] = False
            st.toast("Return to the Action Center tab.")
        _render_exception_detail(int(routed_exception_id), current_user)
        return

    client_id = _resolve_client_id()
    if client_id is None:
        st.info(
            "Pick a **client** in the sidebar (or create one under **Clients**) "
            "to see its reconciliation exceptions."
        )
        return

    view = st.radio(
        "View",
        ["Reconciliation overview", "Exception queue"],
        horizontal=True,
        key="m2_view",
    )

    if view == "Reconciliation overview":
        _render_overview(client_id, current_user)
    else:
        _render_queue(client_id, current_user)


def _goto_queue(cls: Optional[str]) -> None:
    """on_click callback for a grouped-entry button. Runs BEFORE the rerun
    (Streamlit executes callbacks at the start of the next run, before the
    widgets are re-instantiated), so it can safely set the radio's own key —
    which cannot be modified after the widget is created within a run."""
    st.session_state["m2_view"] = "Exception queue"
    if cls:
        st.session_state["m2_filter_classification"] = cls


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def _resolve_client_id() -> Optional[int]:
    """Resolve the sidebar's selected client folder to an F2 client_id.

    Tries an exact name match first, then a normalized prefix match (the
    data/ folder is often an abbreviated form of the F2 legal name, e.g.
    folder 'AcmeTextiles' vs legal name 'Acme Textiles Pvt Ltd')."""
    folder = st.session_state.get("ctx_client")
    if not folder:
        return None
    target = _normalize_name(folder)
    try:
        candidates = clients.list_clients(include_inactive=True)
    except Exception:  # noqa: BLE001
        return None
    for c in candidates:
        if _normalize_name(c["legal_name"]) == target:
            return c["client_id"]
    for c in candidates:
        norm = _normalize_name(c["legal_name"])
        if target and (norm.startswith(target) or target.startswith(norm)):
            return c["client_id"]
    return None


def _normalize_name(name: str) -> str:
    """Lowercase and strip spaces/punctuation for tolerant name matching."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# Module 2's own screen — headline → grouped → detail (Foundation 3.6)
# ---------------------------------------------------------------------------


def _render_overview(client_id: int, current_user: dict[str, Any]) -> None:
    period = st.session_state.get("ctx_period")
    summary = m2.exception_summary(client_id=client_id)

    # --- Headline: the live eligible-credit figure (2A) -----------------
    credit = m2.eligible_credit_for_client(client_id, period=period)
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    if credit is not None:
        st.markdown(
            f'<div style="font-size:34px;font-weight:700;color:var(--setu-text-primary);">'
            f'{money(credit["eligible_credit"])}</div>'
            f'<div style="font-size:12px;color:var(--setu-text-secondary);">'
            f'Eligible credit as things stand — {period or "latest run"} '
            f'(2A GST, recalculated on each run)</div>',
            unsafe_allow_html=True,
        )
        cols = st.columns(4)
        with cols[0]:
            st.metric("Total ITC claimed", money(credit["total_itc_claimed"]))
        with cols[1]:
            st.metric("Matched ITC", money(credit["matched_itc"]))
        with cols[2]:
            st.metric("At-risk ITC", money(credit["at_risk_itc"]))
        with cols[3]:
            st.metric("Blocked (17(5))", money(credit["blocked_credit_itc"]))
        if credit["reverse_charge_itc"]:
            st.caption(
                f"Reverse-charge ITC tracked separately: {money(credit['reverse_charge_itc'])} "
                "(claimable only once the tax is paid by the recipient)."
            )
    else:
        st.markdown(
            '<div style="font-size:34px;font-weight:700;color:var(--setu-text-muted);">—</div>'
            '<div style="font-size:12px;color:var(--setu-text-secondary);">'
            'Eligible credit — no matching run yet for this client.</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Grouped: classification counts, each clickable -----------------
    st.markdown("**Exceptions by classification**")
    by_cls = summary["by_classification"]
    if not by_cls:
        st.info("No exceptions recorded yet. Run a reconciliation, then generate exceptions.")
        return

    cols = st.columns(len(CLASSIFICATION_ORDER))
    for col, cls in zip(cols, CLASSIFICATION_ORDER):
        count = by_cls.get(cls, 0)
        with col:
            st.button(
                f"{cls} — {count}", key=f"m2_group_{cls}", use_container_width=True,
                on_click=_goto_queue, args=(cls,),
            )

    # Sub-type breakdown as accent pills (grouped layer).
    st.markdown("**By sub-scope**")
    pill_cols = st.columns(len(summary["by_sub_type"]) or 1)
    for col, (sub, count) in zip(pill_cols, sorted(summary["by_sub_type"].items())):
        with col:
            render_accent_pill(f"{SUB_TYPE_LABELS.get(sub, sub)} — {count}")

    if summary["escalated"]:
        render_warning_banner(
            f"{summary['escalated']} exception(s) are above the materiality threshold "
            "and have been escalated."
        )

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.caption(
        f"{summary['open']} open · {summary['total']} total. "
        "Click a classification above to open the pre-filtered queue."
    )


# ---------------------------------------------------------------------------
# Exception queue (interim — Module 8 owns the real queue)
# ---------------------------------------------------------------------------


def _render_queue(client_id: int, current_user: dict[str, Any]) -> None:
    st.caption(
        "Module 8 (Action Center) owns the unified queue — list, filter, prioritize, "
        "assign across every source module. This is Module 2's own scoped view of its "
        "exceptions; open the **Action Center** tab for the cross-module queue."
    )

    cls_filter = st.session_state.pop("m2_filter_classification", None)
    sub_filter = st.selectbox(
        "Sub-scope", ["All", "2A — GST", "2B — TDS", "2C — Other"], key="m2_sub_filter",
    )
    sub_type = None if sub_filter == "All" else sub_filter.split(" ")[0]

    exceptions = m2.list_exceptions(client_id=client_id, sub_type=sub_type, classification=cls_filter)
    if cls_filter:
        st.caption(f"Filtered to **{cls_filter}**.")

    if not exceptions:
        st.info("No exceptions match this filter.")
        return

    for exc in exceptions:
        _render_exception_row(exc, current_user)


def _render_exception_row(exc: dict[str, Any], current_user: dict[str, Any]) -> None:
    status = exc["status"]
    priority = exc["priority"]
    title = (
        f"#{exc['exception_id']} · {exc['classification']}"
        f"{' · ' + exc['difference_type'] if exc.get('difference_type') else ''}"
        f" · {SUB_TYPE_LABELS.get(exc['sub_type'], exc['sub_type'])}"
    )
    with st.expander(title, expanded=False):
        c1, c2 = st.columns([3, 1])
        with c1:
            # Confidence badge — rule-match vs AI-suggested (Foundation 3.1).
            if exc.get("confidence_source") == "ai":
                render_confidence_badge("ai", pct=int(exc.get("confidence_score") or 0))
            else:
                render_confidence_badge("rule", label=f"Rule match — {exc.get('confidence_band', '')}")
            if priority == "escalated":
                render_accent_pill("Above materiality — escalated")
            elif priority == "high":
                render_accent_pill("High priority")
        with c2:
            st.caption(f"Status: {status}")

        # Full match_reason always visible in the main view — never a tooltip.
        if exc.get("match_reason"):
            st.markdown(f"**Match reasoning:** {exc['match_reason']}")

        if st.button("Open exception detail", key=f"m2_open_{exc['exception_id']}"):
            st.session_state["m2_open_exception"] = exc["exception_id"]
            st.rerun()

    if st.session_state.get("m2_open_exception") == exc["exception_id"]:
        _render_exception_detail(exc["exception_id"], current_user)


# ---------------------------------------------------------------------------
# Exception detail screen
# ---------------------------------------------------------------------------


def _render_exception_detail(exception_id: int, current_user: dict[str, Any]) -> None:
    exc = m2.get_exception(exception_id)
    if exc is None:
        st.error("Exception not found.")
        return

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.markdown(f"### Exception #{exception_id} — {exc['classification']}")

    evidence = exc.get("evidence") or {}
    books = evidence.get("books_record")
    portal = evidence.get("portal_record")

    # --- Books vs portal side-by-side -----------------------------------
    col_b, col_p = st.columns(2)
    with col_b:
        st.markdown("**Books side**")
        _render_record(books)
    with col_p:
        st.markdown("**Portal side**")
        _render_record(portal)

    # --- Confidence badge per matched field/line ------------------------
    st.markdown("**Confidence**")
    if exc.get("confidence_source") == "ai":
        render_confidence_badge("ai", pct=int(exc.get("confidence_score") or 0))
    else:
        render_confidence_badge("rule", label=f"Rule match — {exc.get('confidence_band', '')}")

    # --- Full match_reason, always visible ------------------------------
    if exc.get("match_reason"):
        st.markdown(f"**Match reasoning:** {exc['match_reason']}")

    # --- TDS six-step chain (2B only) -----------------------------------
    if exc["sub_type"] == "2B":
        st.markdown("**TDS six-step chain**")
        rows = m2.tds_chain_for_exception(exception_id)
        # DB rows use stage_label/stage_state; the stepper renderer takes
        # label/state/detail.
        stages = [
            {"label": r["stage_label"], "state": r["stage_state"], "detail": r.get("detail")}
            for r in rows
        ]
        render_tds_chain_stepper(stages)

    # --- IMS recommendation panel (2A) ----------------------------------
    if exc["sub_type"] == "2A":
        _render_ims_panel(exc)

    # --- Accept / Reject / Escalate action row --------------------------
    _render_action_row(exc, current_user)


def _render_record(record: Optional[dict[str, Any]]) -> None:
    if not record:
        st.caption("(no record on this side)")
        return
    st.markdown('<div class="setu-card" style="padding:12px 16px;">', unsafe_allow_html=True)
    for k, v in record.items():
        if k.startswith("_") or v in (None, ""):
            continue
        st.markdown(
            f'<div style="font-size:13px;color:var(--setu-text-primary);">'
            f'<span style="color:var(--setu-text-secondary);font-size:12px;">{k}:</span> {v}</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)


def _render_ims_panel(exc: dict[str, Any]) -> None:
    """IMS recommendation (2A): visible reasoning, never auto-submitted."""
    st.markdown("**IMS recommendation**")
    st.markdown('<div class="setu-card" style="padding:12px 16px;">', unsafe_allow_html=True)
    rec = exc.get("recommendation")
    if rec:
        render_accent_pill(f"Recommend: {rec}")
        st.markdown(
            f'<div style="font-size:13px;color:var(--setu-text-primary);margin-top:6px;">'
            f'{exc.get("recommendation_reason", "")}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("No IMS recommendation recorded.")
    st.markdown('</div>', unsafe_allow_html=True)
    st.caption(
        "Module 2 recommends only — it never submits. C2's explicit Send gate "
        "executes once this is approved."
    )


def _render_action_row(exc: dict[str, Any], current_user: dict[str, Any]) -> None:
    st.markdown("**Resolve**")
    can_resolve = auth.has_permission(current_user, "module2.exceptions.resolve")
    if not can_resolve:
        st.button("Accept", disabled=True, key=f"m2_acc_{exc['exception_id']}")
        render_inline_reason("Requires Senior Accountant or above.")
        return

    note = st.text_input(
        "Resolution note (optional)", key=f"m2_note_{exc['exception_id']}"
    )
    c1, c2, c3 = st.columns(3)
    actor = current_user.get("username", "unknown")
    with c1:
        if st.button("Accept", key=f"m2_accept_{exc['exception_id']}", type="primary"):
            m2.resolve_exception(exc["exception_id"], resolution="accepted", note=note, actor=actor)
            st.session_state.pop("m2_open_exception", None)
            st.rerun()
    with c2:
        if st.button("Reject", key=f"m2_reject_{exc['exception_id']}"):
            m2.resolve_exception(exc["exception_id"], resolution="rejected", note=note, actor=actor)
            st.session_state.pop("m2_open_exception", None)
            st.rerun()
    with c3:
        if st.button("Escalate", key=f"m2_escalate_{exc['exception_id']}"):
            m2.resolve_exception(exc["exception_id"], resolution="escalated", note=note, actor=actor)
            st.session_state.pop("m2_open_exception", None)
            st.rerun()

    if exc.get("above_materiality"):
        render_info_banner(
            "This exception is above the materiality threshold — escalating routes it "
            "into Module 8's existing escalation/priority mechanism."
        )