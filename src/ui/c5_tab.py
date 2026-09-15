"""C5 UI — AI Instruction & Knowledge Library.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt + C5's
design table §4.5, which is extrapolated from the functional spec, not
flow-mapped):
- Three surfaces in one module: the instruction library (list/search/entry),
  the proposed-instruction approval queue (Headline → grouped → detail, 3.6),
  and per-instruction version history (Recent activity panel, 3.8).
- Instruction library: full-width table, search bar + touchpoint-tag and
  client-scope filters, hairline row dividers.
- Entry form: required short title, free-text explanation, touchpoint-tag
  multi-select, scope selector.
- Optional AI-drafted wording: a "Suggest wording" secondary button fills the
  textarea with a draft, labeled inline in ai-suggested amber text
  ("AI-drafted — review before saving") — never auto-saved as active. Reuses
  the ai-suggested semantic token as a LABEL only (not the 3.1 confidence
  badge, since no percentage applies to drafted text).
- Active/inactive: standard switch control.
- Conflicting-instructions warning: inline warning-amber banner, listing both
  side by side, never auto-resolved.
- Approval queue: headline count, grouped by touchpoint, detail rows with
  edit-and-confirm or reject — never auto-activates.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.c5 import service as c5
from src.clients import service as clients
from src.ui.theme import (
    render_inline_reason,
    render_warning_banner,
)

# Hub sections (radio).
_HUB_SECTIONS = [
    "Instruction Library",
    "Approval Queue",
]


def render_c5_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "c5.view"):
        st.warning("You don't have access to the AI Instruction & Knowledge Library.")
        return

    can_manage = auth.has_permission(current_user, "c5.manage")

    st.subheader("AI Instruction & Knowledge Library")
    st.caption("Judgment-shaped instructions that AI touchpoints read as runtime context.")

    section = st.radio(
        "Library section", _HUB_SECTIONS, horizontal=True, label_visibility="collapsed",
    )

    if section == "Approval Queue":
        _render_approval_queue(current_user, can_manage)
    else:
        _render_instruction_library(current_user, can_manage)


# ---------------------------------------------------------------------------
# Instruction library
# ---------------------------------------------------------------------------


def _render_instruction_library(current_user: dict[str, Any], can_manage: bool) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Instruction library</span>', unsafe_allow_html=True)
    st.caption("Search and filter before adding, to catch a near-duplicate first.")

    # Conflict surfacing — shown to Admin whenever active instructions
    # conflict for the same touchpoint; never auto-resolved.
    render_conflicts_banner()

    instructions = c5.list_instructions()

    # Search + filters.
    search = st.text_input(
        "Search", key="c5_search", label_visibility="collapsed",
        placeholder="Search by title or explanation\u2026",
    )
    touchpoints = c5.list_touchpoints(include_inactive=True)
    tp_labels = {t["key"]: t["label"] for t in touchpoints}
    tp_filter = st.multiselect(
        "Touchpoint", list(tp_labels.keys()), format_func=lambda k: tp_labels[k],
        key="c5_tp_filter",
    )
    scope_filter = st.radio(
        "Scope", ["All", "Firm-wide", "Client-specific"], horizontal=True, key="c5_scope_filter",
    )

    if can_manage:
        if st.button("+ New instruction", type="primary", key="c5_new_btn"):
            st.session_state["_c5_show_new_form"] = True

    if st.session_state.get("_c5_show_new_form") and can_manage:
        _render_instruction_form(current_user, instruction=None)

    # Apply filters.
    shown = instructions
    if search:
        s = search.lower()
        shown = [i for i in shown if s in (i["title"] or "").lower() or s in (i["explanation"] or "").lower()]
    if tp_filter:
        shown = [i for i in shown if set(tp_filter).issubset(set(i["tags"]))]
    if scope_filter == "Firm-wide":
        shown = [i for i in shown if i["scope"] == "firm"]
    elif scope_filter == "Client-specific":
        shown = [i for i in shown if i["scope"] == "client"]

    if not shown:
        st.info("No instructions match. Use **+ New instruction** to add one.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    # Full-width table, hairline row dividers.
    header = st.columns([3, 3, 2, 2, 2])
    for col, label in zip(header, ["Title", "Touchpoints", "Scope", "Status", "Actions"]):
        col.markdown(
            f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>',
            unsafe_allow_html=True,
        )
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    for inst in shown:
        row = st.columns([3, 3, 2, 2, 2])
        with row[0]:
            st.markdown(f"**{inst['title']}**")
            st.caption(inst["explanation"][:80] + ("\u2026" if len(inst["explanation"]) > 80 else ""))
        with row[1]:
            st.write(", ".join(tp_labels.get(t, t) for t in inst["tags"]) or "\u2014")
        with row[2]:
            if inst["scope"] == "firm":
                st.write("Firm-wide")
            else:
                st.write(f"Client #{inst['client_id']}")
        with row[3]:
            if inst["is_active"]:
                st.badge("Active", color="green")
            else:
                st.badge("Inactive", color="gray")
        with row[4]:
            if st.button("Open", key=f"c5_open_{inst['instruction_id']}", type="tertiary"):
                st.session_state["_c5_selected_instruction_id"] = inst["instruction_id"]
                st.rerun()
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

    # Detail drawer (selected instruction) — version history + edit.
    selected_id = st.session_state.get("_c5_selected_instruction_id")
    if selected_id is not None:
        inst = c5.get_instruction(selected_id)
        if inst is not None:
            _render_instruction_detail(current_user, can_manage, inst)


# ---------------------------------------------------------------------------
# Instruction form (create / edit)
# ---------------------------------------------------------------------------


def _render_instruction_form(
    current_user: dict[str, Any], instruction: Optional[dict[str, Any]],
) -> None:
    editing = instruction is not None
    st.markdown('<hr style="margin:12px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    st.markdown(
        f'<span class="setu-card-title">{"Edit instruction" if editing else "New instruction"}</span>',
        unsafe_allow_html=True,
    )

    touchpoints = c5.list_touchpoints(include_inactive=True)
    tp_labels = {t["key"]: t["label"] for t in touchpoints}

    title = st.text_input("Title *", value=instruction["title"] if editing else "", key="c5_form_title")
    explanation = st.text_area(
        "Explanation *", value=instruction["explanation"] if editing else "",
        key="c5_form_explanation", height=120,
    )
    tp_keys = st.multiselect(
        "Touchpoint tags", list(tp_labels.keys()), format_func=lambda k: tp_labels[k],
        default=instruction["tags"] if editing else [],
        key="c5_form_tags",
    )
    scope = st.radio(
        "Scope", ["Firm-wide", "Specific client"], horizontal=True, key="c5_form_scope",
        index=0 if (not editing or instruction["scope"] == "firm") else 1,
    )
    client_id: Optional[int] = None
    if scope == "Specific client":
        client_options = clients.list_clients(include_inactive=False)
        if not client_options:
            st.info("No active clients.")
            return
        client_name = st.selectbox("Client", [c["legal_name"] for c in client_options], key="c5_form_client")
        client_id = next((c["client_id"] for c in client_options if c["legal_name"] == client_name), None)

    # Optional AI-drafted wording (assistive only).
    _render_suggest_wording(explanation)

    if editing:
        change_summary = st.text_input("Change summary *", key="c5_form_summary")
        if st.button("Save changes", type="primary", key="c5_form_save_edit"):
            try:
                c5.edit_instruction(
                    instruction["instruction_id"], title=title, explanation=explanation,
                    scope="firm" if scope == "Firm-wide" else "client", client_id=client_id,
                    touchpoint_keys=tp_keys, change_summary=change_summary,
                    actor=current_user["username"],
                )
                st.success("Instruction updated — new version recorded.")
                st.session_state["_c5_show_new_form"] = False
                st.session_state["_c5_selected_instruction_id"] = None
                st.rerun()
            except c5.C5Error as exc:
                st.error(str(exc))
    else:
        if st.button("Create instruction", type="primary", key="c5_form_create"):
            try:
                c5.create_instruction(
                    title=title, explanation=explanation,
                    scope="firm" if scope == "Firm-wide" else "client", client_id=client_id,
                    touchpoint_keys=tp_keys, actor=current_user["username"],
                )
                st.success("Instruction created.")
                st.session_state["_c5_show_new_form"] = False
                st.rerun()
            except c5.C5Error as exc:
                st.error(str(exc))


def _render_suggest_wording(current_explanation: str) -> None:
    """Optional AI-drafted first-draft wording — assistive only, clearly a
    draft, never pre-filled as final and never auto-saved."""
    with st.expander("Suggest wording (optional AI draft)"):
        pattern = st.text_input("Describe the pattern", key="c5_suggest_pattern")
        if st.button("Suggest wording", type="secondary", key="c5_suggest_btn"):
            try:
                draft = c5.suggest_wording(pattern)
                st.session_state["_c5_draft"] = draft
            except c5.C5Error as exc:
                st.error(str(exc))
        if st.session_state.get("_c5_draft"):
            st.markdown(
                '<span style="color:var(--setu-ai-text);font-size:11px;font-weight:600;">'
                'AI-drafted \u2014 review before saving</span>',
                unsafe_allow_html=True,
            )
            st.text_area(
                "Draft wording", value=st.session_state["_c5_draft"], key="c5_draft_view",
                height=100, disabled=False,
            )
            if st.button("Use this draft", key="c5_use_draft"):
                # Copy the draft into the main explanation field (manual review
                # still required before saving — never auto-saved as active).
                st.session_state["c5_form_explanation"] = st.session_state["_c5_draft"]
                st.session_state["_c5_draft"] = ""
                st.rerun()


# ---------------------------------------------------------------------------
# Instruction detail (version history via 3.8 + active toggle)
# ---------------------------------------------------------------------------


def _render_instruction_detail(current_user: dict[str, Any], can_manage: bool, inst: dict[str, Any]) -> None:
    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(f'<span class="setu-card-title">Instruction: {inst["title"]}</span>', unsafe_allow_html=True)

    # Active / inactive toggle (standard switch).
    if can_manage:
        new_active = st.toggle(
            "Active", value=bool(inst["is_active"]), key=f"c5_toggle_{inst['instruction_id']}",
        )
        if new_active != bool(inst["is_active"]):
            c5.set_instruction_active(inst["instruction_id"], new_active, actor=current_user["username"])
            st.rerun()

    if can_manage:
        if st.button("Edit instruction", key=f"c5_edit_{inst['instruction_id']}"):
            st.session_state["_c5_edit_instruction_id"] = inst["instruction_id"]
            st.rerun()

    edit_id = st.session_state.get("_c5_edit_instruction_id")
    if edit_id == inst["instruction_id"] and can_manage:
        _render_instruction_form(current_user, inst)

    # Version history — Recent activity panel (3.8): timestamped list, change
    # summary + who + when, newest first. Superseded versions render greyed
    # but are never removed.
    st.markdown("#### Version history")
    versions = c5.instruction_versions(inst["instruction_id"])
    if not versions:
        st.caption("No versions recorded.")
    else:
        st.markdown('<div class="setu-card" style="padding:12px 18px;">', unsafe_allow_html=True)
        for i, v in enumerate(versions):
            is_current = i == 0
            opacity = "1" if is_current else "0.55"
            st.markdown(
                f'<div style="border-bottom:1px solid var(--setu-border);padding:8px 2px;opacity:{opacity};">'
                f'<span style="color:var(--setu-text-primary);font-size:13px;">{v["change_summary"]}</span>'
                f'<span style="float:right;color:var(--setu-text-secondary);font-size:11px;">'
                f'{v["created_at"].split("T")[0]} \u00b7 {v["changed_by"]}</span>'
                f'<div style="color:var(--setu-text-muted);font-size:11px;">{v["title"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Approval queue (Headline → grouped → detail, 3.6)
# ---------------------------------------------------------------------------


def _render_approval_queue(current_user: dict[str, Any], can_manage: bool) -> None:
    proposals = c5.list_proposals(status="pending")
    touchpoints = c5.list_touchpoints(include_inactive=True)
    tp_labels = {t["key"]: t["label"] for t in touchpoints}

    # Headline: count awaiting confirmation.
    st.markdown(
        f'<div style="font-size:34px;font-weight:700;color:var(--setu-text-primary);">'
        f'{len(proposals)}</div>',
        unsafe_allow_html=True,
    )
    st.caption("Proposals awaiting Admin confirmation")

    if not proposals:
        st.info("No proposals awaiting confirmation.")
        return

    # Grouped: by touchpoint.
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in proposals:
        for tag in p["tags"]:
            groups.setdefault(tag, []).append(p)
        if not p["tags"]:
            groups.setdefault("(untagged)", []).append(p)

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    for tag, members in groups.items():
        tag_label = tp_labels.get(tag, tag)
        st.markdown(f"#### {tag_label} \u2014 {len(members)} proposal(s)")
        for p in members:
            _render_proposal_row(current_user, can_manage, p)
        st.markdown('<hr style="margin:8px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


def _render_proposal_row(current_user: dict[str, Any], can_manage: bool, p: dict[str, Any]) -> None:
    st.markdown(
        f'<div style="padding:6px 0;">'
        f'<strong>{p["title"]}</strong> '
        f'<span style="color:var(--setu-text-secondary);font-size:11px;">'
        f'submitted by {p["submitted_by"]} \u00b7 {p["submitted_at"].split("T")[0]} \u00b7 {p["source"]}</span>'
        f'<div style="color:var(--setu-text-secondary);font-size:12px;">{p["explanation"]}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if not can_manage:
        return

    with st.expander("Edit and confirm / reject", expanded=False):
        title = st.text_input("Title", value=p["title"], key=f"c5_prop_title_{p['proposal_id']}")
        explanation = st.text_area("Explanation", value=p["explanation"], key=f"c5_prop_expl_{p['proposal_id']}", height=100)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Confirm (activate)", type="primary", key=f"c5_prop_approve_{p['proposal_id']}"):
                try:
                    c5.approve_proposal(
                        p["proposal_id"], title=title, explanation=explanation,
                        actor=current_user["username"],
                    )
                    st.success("Proposal approved and activated.")
                    st.rerun()
                except c5.C5Error as exc:
                    st.error(str(exc))
        with c2:
            if st.button("Reject", key=f"c5_prop_reject_{p['proposal_id']}"):
                c5.reject_proposal(p["proposal_id"], actor=current_user["username"])
                st.success("Proposal rejected.")
                st.rerun()


# ---------------------------------------------------------------------------
# Conflict surfacing (warning-amber banner)
# ---------------------------------------------------------------------------


def render_conflicts_banner() -> None:
    """Render the conflicting-instructions warning for Admin at review time.
    Both conflicting instructions are listed side by side; neither is
    auto-resolved. Called from the library view (and potentially other
    modules' review screens later)."""
    groups = c5.conflicting_groups()
    if not groups:
        return
    for g in groups:
        names = "\u00b7 ".join(i["title"] for i in g["instructions"])
        render_warning_banner(
            f"<strong>Conflicting instructions</strong> \u2014 {g['touchpoint']} "
            f"({g['scope']}): {names}. Both are active; resolve manually."
        )
