"""C4 UI — Security & Credential Vault.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt +
C4's design table §3c):
- Security Overview is a new screen — the first real (non-reference-
  mockup) use of the headline\u2192grouped\u2192detail layout template (3.6):
  a headline count of items needing attention (open DPDP requests +
  recent event volume), a grouped breakdown beneath (events by type,
  last 30 days, each clickable into the filtered detail log), and the
  Recent activity panel (3.8) underneath as the full log's permanent
  home.
- Backup/DR status reuses the placeholder badge's (3.5) visual shape but
  with its own required label ("Not yet configured \u2014 pending hosting
  decision") rather than "Coming with [Module]" phrasing.
- Credential rotation is write-only: old value never shown, new value
  never redisplayed after save. Each credential row shows the Live
  connection status chip (3.7) plus a masked reference and a "Rotate"
  action (never "Reveal").
- Rotation history is a Recent activity panel (3.8) instance: timestamp +
  who rotated, no value column, newest first.
- DPDP deletion request flow: submit (client + scope + legal basis) \u2192
  scope & exemptions read-only summary (deletable in primary text,
  exempt in muted/secondary text) \u2192 Partner approval gate, both
  Approve and Reject requiring a reason first (component 3.3,
  reason-capture-before-save).
- Data isolation gets no dedicated UI, deliberately, per the build
  prompt — it's an automated-testing concern at the data layer, not a
  screen.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.vault import service as vault
from src.ui.theme import (
    render_activity_panel,
    render_inline_reason,
    render_live_status_chip,
    render_placeholder_pill,
)

_HUB_SECTIONS = ["Security Overview", "Credential Vault", "DPDP Deletion Requests"]


def render_vault_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "vault.view"):
        st.warning("You don't have access to the Security & Credential Vault.")
        return

    st.subheader("Security & Credential Vault")
    st.caption(
        "Encrypted secrets vault, End-Client data isolation, security-event "
        "logging, and DPDP Act compliance controls."
    )

    section = st.radio(
        "Vault section", _HUB_SECTIONS, horizontal=True, label_visibility="collapsed",
    )

    if section == "Credential Vault":
        _render_credential_vault(current_user)
    elif section == "DPDP Deletion Requests":
        _render_dpdp_workflow(current_user)
    else:
        _render_security_overview(current_user)


# ---------------------------------------------------------------------------
# Security Overview \u2014 headline \u2192 grouped \u2192 detail (3.6)
# ---------------------------------------------------------------------------


def _render_security_overview(current_user: dict[str, Any]) -> None:
    open_requests = vault.list_dpdp_requests(status="pending")
    breakdown = vault.event_type_breakdown_last_30_days()
    recent_event_volume = sum(breakdown.values())

    # --- Headline ---------------------------------------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Needs attention</span>', unsafe_allow_html=True)
    cols = st.columns(2)
    with cols[0]:
        st.metric("Open DPDP requests", len(open_requests))
        st.caption("Last 30 days, all clients")
    with cols[1]:
        st.metric("Security events (30d)", recent_event_volume)
        st.caption("Credential access, rotations, and DPDP decisions")
    st.markdown('</div>', unsafe_allow_html=True)

    # --- Grouped: events by type, last 30 days -----------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Events by type \u2014 last 30 days</span>', unsafe_allow_html=True)
    if not breakdown:
        st.caption("No events recorded in the last 30 days.")
    else:
        group_cols = st.columns(max(len(breakdown), 1))
        for col, (event_type, count) in zip(group_cols, sorted(breakdown.items(), key=lambda kv: -kv[1])):
            with col:
                if st.button(f"{event_type}\n({count})", key=f"vault_group_{event_type}"):
                    st.session_state["_vault_detail_filter"] = event_type
    st.markdown('</div>', unsafe_allow_html=True)

    # --- Backup/DR status \u2014 structural placeholder, own label -----
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Backup / disaster recovery</span>', unsafe_allow_html=True)
    dr = vault.backup_dr_status()
    render_placeholder_pill(dr["label"])
    st.caption(f"RTO target: {dr['rto']}")
    st.caption(f"RPO target: {dr['rpo']}")
    st.markdown('</div>', unsafe_allow_html=True)

    # --- Open DPDP requests awaiting approval ------------------------
    if open_requests:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Open DPDP requests awaiting approval</span>', unsafe_allow_html=True)
        for req in open_requests:
            st.write(f"**#{req['request_id']} \u2014 {req['client_label']}** \u2014 {req['scope']}")
            st.caption(f"Submitted by {req['submitted_by']} on {req['submitted_at'][:10]}")
        st.caption("Go to the **DPDP Deletion Requests** section to decide.")
        st.markdown('</div>', unsafe_allow_html=True)

    # --- Detail: the full log (Recent activity panel, permanent home) --
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    detail_filter = st.session_state.get("_vault_detail_filter")
    title = "Recent security events \u2014 full log"
    if detail_filter:
        title += f" (filtered: {detail_filter})"
        if st.button("Clear filter", key="vault_clear_filter"):
            st.session_state["_vault_detail_filter"] = None
            st.rerun()
    st.markdown(f'<span class="setu-card-title">{title}</span>', unsafe_allow_html=True)
    st.caption(
        "Permanent home for F1's Recent Security Events and C3-ext's Recent "
        "fallbacks panels, per the flow-mapping pass."
    )
    events = vault.merged_security_events(limit=200)
    if detail_filter:
        events = [e for e in events if e["event_type"] == detail_filter]
    rows = [
        {
            "title": f"{e['event_type']} \u2014 {e['actor']}",
            "detail": e["detail"],
            "timestamp": e["created_at"],
        }
        for e in events
    ]
    render_activity_panel(rows, empty_message="No security events recorded yet.")
    st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Credential Vault \u2014 encrypted secrets, rotation
# ---------------------------------------------------------------------------


def _render_credential_vault(current_user: dict[str, Any]) -> None:
    can_manage = auth.has_permission(current_user, "vault.credentials.manage")

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Encrypted credentials</span>', unsafe_allow_html=True)
    st.caption(
        "Real encryption-at-rest. Admin manages this vault but never sees raw "
        "secret values \u2014 only the masked reference below is ever displayed."
    )

    creds = vault.list_credentials()
    if not creds:
        st.info("No credentials stored yet.")
    for c in creds:
        row = st.columns([3, 2, 2, 2])
        with row[0]:
            label = f" ({c['label']})" if c.get("label") else ""
            st.markdown(f"**{c['service']}**{label}")
            st.caption(c["masked_ref"])
        with row[1]:
            render_live_status_chip(c["status"])
        with row[2]:
            if can_manage:
                with st.popover("Rotate"):
                    _render_rotation_form(current_user, c)
        with row[3]:
            if can_manage and st.button("Deactivate", key=f"vault_deact_{c['credential_id']}"):
                vault.deactivate_credential(c["credential_id"], actor=current_user["username"])
                st.rerun()
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    if can_manage:
        with st.expander("+ Add credential"):
            service = st.text_input("Service (e.g. Tally, GST Portal, TRACES, OpenRouter)", key="vault_add_service")
            label = st.text_input("Label (optional)", key="vault_add_label")
            client_scope = st.checkbox("Scope to a specific client", key="vault_add_scoped")
            client_id: Optional[int] = None
            if client_scope:
                client_options = clients.list_clients(include_inactive=False)
                if client_options:
                    client_name = st.selectbox(
                        "Client", [c["legal_name"] for c in client_options], key="vault_add_client",
                    )
                    client_id = next(
                        (c["client_id"] for c in client_options if c["legal_name"] == client_name), None,
                    )
            secret = st.text_input("Secret / API key", type="password", key="vault_add_secret")
            if st.button("Add credential", type="primary", key="vault_add_btn"):
                try:
                    vault.add_credential(
                        service, secret, label=label or None, client_id=client_id,
                        actor=current_user["username"],
                    )
                    st.success(f"'{service}' credential added \u2014 encrypted at rest.")
                    st.rerun()
                except vault.VaultError as exc:
                    st.error(str(exc))

    st.markdown('</div>', unsafe_allow_html=True)

    # --- Rotation history \u2014 Recent activity panel (3.8) -------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Rotation history</span>', unsafe_allow_html=True)
    st.caption("Timestamp and who rotated only \u2014 no value column, ever.")
    history = vault.list_rotation_history(limit=100)
    rows = [
        {
            "title": f"{h['service']}" + (f" ({h['label']})" if h.get("label") else ""),
            "detail": f"rotated by {h['rotated_by']}",
            "timestamp": h["rotated_at"],
        }
        for h in history
    ]
    render_activity_panel(rows, empty_message="No rotations recorded yet.")
    st.markdown('</div>', unsafe_allow_html=True)


def _render_rotation_form(current_user: dict[str, Any], credential: dict[str, Any]) -> None:
    """Write-only rotation form \u2014 old value never shown, new value never
    redisplayed after save."""
    st.caption(f"Rotating {credential['service']}. The old secret is never shown here.")
    new_secret = st.text_input(
        "New secret / API key", type="password", key=f"vault_rotate_secret_{credential['credential_id']}",
    )
    if st.button("Save new secret", type="primary", key=f"vault_rotate_save_{credential['credential_id']}"):
        try:
            vault.rotate_credential(
                credential["credential_id"], new_secret, actor=current_user["username"],
            )
            st.success("Credential rotated. The new value will not be shown again.")
            st.rerun()
        except vault.VaultError as exc:
            st.error(str(exc))


# ---------------------------------------------------------------------------
# DPDP deletion-request workflow
# ---------------------------------------------------------------------------


def _render_dpdp_workflow(current_user: dict[str, Any]) -> None:
    can_submit = auth.has_permission(current_user, "vault.dpdp.submit")
    can_approve = auth.has_permission(current_user, "vault.dpdp.approve")

    if can_submit:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Submit a DPDP deletion request</span>', unsafe_allow_html=True)
        _render_dpdp_submit_form(current_user)
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Pending requests</span>', unsafe_allow_html=True)
    pending = vault.list_dpdp_requests(status="pending")
    if not pending:
        st.info("No pending DPDP deletion requests.")
    for req in pending:
        _render_dpdp_request_card(current_user, req, can_approve=can_approve)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Decided requests</span>', unsafe_allow_html=True)
    decided = [
        r for r in vault.list_dpdp_requests() if r["status"] != "pending"
    ]
    if not decided:
        st.caption("No decided requests yet.")
    for req in decided:
        badge = "Approved" if req["status"] == "approved" else "Rejected"
        color = "green" if req["status"] == "approved" else "red"
        st.write(f"**#{req['request_id']} \u2014 {req['client_label']}**")
        st.badge(badge, color=color)
        st.caption(
            f"Decided by {req['decided_by']} on {req['decided_at'][:10] if req['decided_at'] else ''} \u2014 "
            f"{req['decision_reason']}"
        )
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


def _render_dpdp_submit_form(current_user: dict[str, Any]) -> None:
    client_options = clients.list_clients(include_inactive=False)
    if not client_options:
        st.info("No active clients to select.")
        return
    client_name = st.selectbox(
        "Client", [c["legal_name"] for c in client_options], key="dpdp_submit_client",
    )
    client = next((c for c in client_options if c["legal_name"] == client_name), None)
    scope = st.text_area("Scope of data requested for deletion *", key="dpdp_submit_scope")
    legal_basis = st.text_input("Legal basis *", key="dpdp_submit_basis")

    st.caption("Scope & exemptions preview \u2014 informational, not a warning:")
    deletable_text = st.text_area(
        "Deletable items (one per line)", key="dpdp_submit_deletable",
        help="Items considered deletable under this request.",
    )
    exempt_text = st.text_area(
        "Statutorily-exempt items (one per line)", key="dpdp_submit_exempt",
        help="Items NOT deleted \u2014 exempt under statutory retention.",
    )

    if st.button("Submit request", type="primary", key="dpdp_submit_btn"):
        deletable = [line.strip() for line in deletable_text.splitlines() if line.strip()]
        exempt = [line.strip() for line in exempt_text.splitlines() if line.strip()]
        try:
            vault.submit_dpdp_request(
                client_id=client["client_id"] if client else None,
                client_label=client_name,
                scope=scope,
                legal_basis=legal_basis,
                deletable_items=deletable,
                exempt_items=exempt,
                actor=current_user["username"],
            )
            st.success("DPDP deletion request submitted, awaiting Partner approval.")
            st.rerun()
        except vault.VaultError as exc:
            st.error(str(exc))


def _render_dpdp_request_card(current_user: dict[str, Any], req: dict[str, Any], *, can_approve: bool) -> None:
    with st.container(border=True):
        st.write(f"**#{req['request_id']} \u2014 {req['client_label']}**")
        st.caption(f"Legal basis: {req['legal_basis']}")
        st.caption(f"Submitted by {req['submitted_by']} on {req['submitted_at'][:10]}")

        # Scope & exemptions \u2014 read-only summary, deletable in primary
        # text, exempt in muted/secondary text.
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Deletable**")
            if req["deletable_items"]:
                for item in req["deletable_items"]:
                    st.markdown(f"- {item}")
            else:
                st.caption("None listed.")
        with col_b:
            st.markdown(
                '<span style="color:var(--setu-text-muted);font-weight:600;">Statutorily exempt</span>',
                unsafe_allow_html=True,
            )
            if req["exempt_items"]:
                for item in req["exempt_items"]:
                    st.markdown(
                        f'<span style="color:var(--setu-text-muted);">- {item}</span>',
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("None listed.")

        if can_approve:
            key_reason = f"dpdp_reason_{req['request_id']}"
            reason = st.text_input(
                "Reason (required for either Approve or Reject) *", key=key_reason,
            )
            decide_disabled = not reason.strip()
            col_approve, col_reject = st.columns(2)
            with col_approve:
                if st.button(
                    "Approve", key=f"dpdp_approve_{req['request_id']}", type="primary",
                    disabled=decide_disabled,
                ):
                    try:
                        vault.decide_dpdp_request(
                            req["request_id"], approve=True, reason=reason,
                            actor=current_user["username"],
                        )
                        st.success("Request approved and executed.")
                        st.rerun()
                    except vault.VaultError as exc:
                        st.error(str(exc))
            with col_reject:
                if st.button(
                    "Reject", key=f"dpdp_reject_{req['request_id']}",
                    disabled=decide_disabled,
                ):
                    try:
                        vault.decide_dpdp_request(
                            req["request_id"], approve=False, reason=reason,
                            actor=current_user["username"],
                        )
                        st.info("Request rejected \u2014 no data was deleted.")
                        st.rerun()
                    except vault.VaultError as exc:
                        st.error(str(exc))
            if decide_disabled:
                render_inline_reason("A reason is required before this decision can be saved.")
        else:
            st.caption("Partner approval required \u2014 you don't have decision rights on this request.")
