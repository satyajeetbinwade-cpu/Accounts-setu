"""F5 UI — Data Integrity & Validation Layer.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt +
F5's own design table — no new component is introduced):
- Data Integrity Queue hub: headline count of open validation blocks,
  grouped by severity/client, detail rows resolvable inline (3.6
  Headline -> grouped -> detail). A separate top-level nav item — never
  merged into F3-AI's Unified Review Queue (F3-AI has its own, distinct
  "couldn't map" queue; this is F5's own, distinct "value itself isn't
  valid" queue).
- Block detail: card stating exactly what failed, why, and its severity
  in plain language, never a generic error string.
- Override action: inline reason field (3.3 reason-capture-before-save)
  beneath the block detail; stays disabled until populated. A below-tier
  role attempting a GSTIN/PAN-level override on a Manager-only block
  sees it disabled with its inline reason (3.4 disabled-with-inline-
  reason): "Requires Manager or above."
- Sync health indicator: reuses the Live connection status chip (3.7) —
  its first reuse outside credential/portal connectivity — mapped onto
  the SAME three states the design table calls for (Connected/healthy,
  Needs attention/degraded, Failed) rather than inventing a fourth color.
- Reconciliation-of-the-reconciliation tab: its own tab inside the Data
  Integrity Queue, rendering ONLY the dormant placeholder state (3.5
  Coming-with-[Module] badge) — structurally present, not hidden, and
  makes no fabricated data calls (reads the real, always-empty table).
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.f5 import service as f5
from src.ui.theme import (
    render_inline_reason,
    render_live_status_chip,
    render_placeholder_badge,
    render_warning_banner,
)

_HUB_SECTIONS = ["Data Integrity Queue", "Sync Health", "Reconciliation-of-the-Reconciliation"]

SK_SELECTED_BLOCK_ID = "_f5_selected_block_id"

# Reuse the SAME three-state connectivity shape (3.7) — no fourth color.
_SYNC_TO_LIVE_CHIP = {
    "succeeded": ("connected", "Connected"),
    "not_attempted": ("needs_reauth", "Not attempted"),
    "attempted_failed": ("expired", "Failed"),
}

_SOURCE_LABELS = {
    "tally": "Tally", "gst_portal": "GST Portal", "traces": "TRACES", "bank": "Bank",
}

_SEVERITY_LABEL = {"cosmetic": "Cosmetic", "minor": "Minor", "gstin_pan": "GSTIN/PAN-level"}


def render_f5_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "f5.view"):
        st.warning("You don't have access to the Data Integrity & Validation Layer.")
        return

    st.subheader("Data Integrity & Validation Layer")
    st.caption("The trust backbone — structural validity, completeness, and sync health checks.")

    selected_block_id = st.session_state.get(SK_SELECTED_BLOCK_ID)
    if selected_block_id is not None:
        block = f5.get_block(selected_block_id)
        if block is not None:
            _render_block_detail(block, current_user)
            return
        st.session_state[SK_SELECTED_BLOCK_ID] = None

    section = st.radio("F5 section", _HUB_SECTIONS, horizontal=True, label_visibility="collapsed")

    if section == "Sync Health":
        _render_sync_health(current_user)
    elif section == "Reconciliation-of-the-Reconciliation":
        _render_recon_of_recon(current_user)
    else:
        _render_queue_hub(current_user)


# ---------------------------------------------------------------------------
# Data Integrity Queue — headline -> grouped -> detail (3.6)
# ---------------------------------------------------------------------------


def _render_queue_hub(current_user: dict[str, Any]) -> None:
    open_blocks = f5.list_blocks(status="open")

    st.markdown(
        f'<div class="setu-card"><span style="font-size:34px;font-weight:700;color:var(--setu-text-primary);">'
        f'{len(open_blocks)}</span> '
        f'<span style="color:var(--setu-text-secondary);font-size:13px;">open validation block(s)</span></div>',
        unsafe_allow_html=True,
    )

    if not open_blocks:
        st.info("No open validation blocks — every canonical file that's been checked structurally passed.")
        return

    all_clients = {c["client_id"]: c["legal_name"] for c in clients.list_clients(include_inactive=True)}

    # Grouped by severity, then by client within each severity group.
    by_severity: dict[str, list[dict[str, Any]]] = {}
    for b in open_blocks:
        by_severity.setdefault(b["severity"], []).append(b)

    for severity in ("gstin_pan", "minor", "cosmetic"):
        rows = by_severity.get(severity)
        if not rows:
            continue
        css = "danger" if severity == "gstin_pan" else "ai"
        st.markdown(
            f'<span class="setu-token-pill {css}">{_SEVERITY_LABEL[severity]} — {len(rows)}</span>',
            unsafe_allow_html=True,
        )
        by_client: dict[int, list[dict[str, Any]]] = {}
        for b in rows:
            by_client.setdefault(b["client_id"], []).append(b)
        for client_id, client_rows in by_client.items():
            client_name = all_clients.get(client_id, f"Client #{client_id}")
            with st.expander(f"{client_name} — {len(client_rows)} block(s)"):
                for b in client_rows:
                    c1, c2 = st.columns([5, 1])
                    with c1:
                        st.write(f"**{b['issue_type'].replace('_', ' ').title()}** — {b['source_type']}/{b['source_file']}")
                        st.caption(b["description"])
                    with c2:
                        if st.button("Open", key=f"f5_open_block_{b['block_id']}"):
                            st.session_state[SK_SELECTED_BLOCK_ID] = b["block_id"]
                            st.rerun()
                    st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


def _render_block_detail(block: dict[str, Any], current_user: dict[str, Any]) -> None:
    if st.button("← Back to Data Integrity Queue"):
        st.session_state[SK_SELECTED_BLOCK_ID] = None
        st.rerun()

    client = clients.get_client(block["client_id"])
    client_name = client["legal_name"] if client else f"Client #{block['client_id']}"

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(f'<span class="setu-card-title">{block["issue_type"].replace("_", " ").title()}</span>', unsafe_allow_html=True)
    st.caption(f"{client_name} · {block['source_type']}/{block['source_file']} · {block['recon_type']}")
    st.write(block["description"])
    css = "danger" if block["severity"] == "gstin_pan" else "ai"
    st.markdown(
        f'<span class="setu-token-pill {css}">Severity: {_SEVERITY_LABEL[block["severity"]]}</span>',
        unsafe_allow_html=True,
    )
    if block["status"] == "overridden":
        st.markdown(
            f'<div style="margin-top:8px;color:var(--setu-text-secondary);font-size:13px;">'
            f'Overridden by {block["resolved_by"]} on {block["resolved_at"]} — reason: "{block["override_reason"]}"</div>',
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)
        return
    st.markdown('</div>', unsafe_allow_html=True)

    _render_override_control(block, current_user)


def _render_override_control(block: dict[str, Any], current_user: dict[str, Any]) -> None:
    """3.3 reason-capture-before-save for the override action; 3.4
    disabled-with-inline-reason for a below-tier role attempting a
    GSTIN/PAN-level override."""
    actor_role = current_user.get("role_name", "")
    allowed, deny_reason = f5.can_override_block(block, actor_role)

    perm_needed = "f5.override.gstin_pan" if block["severity"] == "gstin_pan" else "f5.override.minor"
    has_perm = auth.has_permission(current_user, perm_needed)

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Override this block</span>', unsafe_allow_html=True)

    if not allowed or not has_perm:
        st.button("Override", disabled=True, key=f"f5_override_disabled_{block['block_id']}")
        render_inline_reason(deny_reason or "You don't have permission to override this block.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    reason = st.text_input(
        "Reason for override (required before saving)", key=f"f5_override_reason_{block['block_id']}",
        placeholder="e.g. verified against the source portal directly — GSTIN is correct as filed",
    )
    save_disabled = not reason.strip()
    if st.button("Override", key=f"f5_override_save_{block['block_id']}", disabled=save_disabled, type="primary"):
        try:
            f5.override_block(
                block["block_id"], actor=current_user["username"], actor_role=actor_role, reason=reason,
            )
            st.success("Block overridden — logged as an Audit Trail Entry and an Edit History Entry.")
            st.session_state[SK_SELECTED_BLOCK_ID] = None
            st.rerun()
        except f5.F5Error as exc:
            st.error(str(exc))
    if save_disabled:
        render_inline_reason("A reason is required before this override can be saved.")
    st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sync Health — per client per source, Live connection status chip (3.7)
# ---------------------------------------------------------------------------


def _render_sync_health(current_user: dict[str, Any]) -> None:
    all_clients = clients.list_clients(include_inactive=False)
    if not all_clients:
        st.info("No clients onboarded yet — add one from the Clients tab first.")
        return
    labels = {c["client_id"]: c["legal_name"] for c in all_clients}
    client_id = st.selectbox("Client", list(labels.keys()), format_func=lambda cid: labels[cid], key="f5_sync_client_picker")

    rows = f5.sync_health_for_client(client_id)

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Sync health — per source</span>', unsafe_allow_html=True)
    for r in rows:
        css_status, label = _SYNC_TO_LIVE_CHIP.get(r["status"], ("needs_reauth", r["status"]))
        c1, c2, c3 = st.columns([2, 2, 3])
        with c1:
            st.write(_SOURCE_LABELS.get(r["source"], r["source"]))
        with c2:
            render_live_status_chip(css_status, label=label)
        with c3:
            if r["is_stub"]:
                render_placeholder_badge("Module C2")
            else:
                st.caption(r.get("checked_at") or "Never checked")
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

    escalations = f5.list_escalations(client_id=client_id)
    if escalations:
        render_warning_banner(
            f"{len(escalations)} escalation(s) sent directly to Partner for persistent sync failure on this client."
        )
        with st.expander("Escalation history"):
            for e in escalations:
                st.caption(f"{e['created_at']} · {_SOURCE_LABELS.get(e['source'], e['source'])} · {e['message']}")


# ---------------------------------------------------------------------------
# Reconciliation-of-the-reconciliation — dormant tab (3.5 placeholder)
# ---------------------------------------------------------------------------


def _render_recon_of_recon(current_user: dict[str, Any]) -> None:
    all_clients = clients.list_clients(include_inactive=False)
    client_id = None
    if all_clients:
        labels = {c["client_id"]: c["legal_name"] for c in all_clients}
        client_id = st.selectbox(
            "Client", list(labels.keys()), format_func=lambda cid: labels[cid], key="f5_ror_client_picker",
        )

    results = f5.recon_of_recon_for_client(client_id) if client_id is not None else []

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Reconciliation-of-the-reconciliation</span>', unsafe_allow_html=True)
    if not results:
        st.caption("No matching runs yet — will populate once Module 2 is live.")
        render_placeholder_badge("Module 2")
    else:
        # Structurally present for when Module 2 starts writing real rows —
        # not reachable this build (results is always empty above).
        for r in results:
            st.write(r)
    st.markdown('</div>', unsafe_allow_html=True)
