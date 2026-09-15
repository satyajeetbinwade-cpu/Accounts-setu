"""F2 UI \u2014 Client Roster (entry point) + Client Profile screen.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt +
F2's design table):
- Client Roster: full-width table on the page-background, hairline row
  dividers, no card wrapper (this is the landing screen, not a widget).
  "+ New client" is the one informational-accent primary button.
- Profile screen: header in a card; branch switcher as a segmented
  control (informational-accent for the selected branch); client-level
  tabs (Contacts, Chart of Accounts, Historical Snapshot, Documents) stay
  constant across branch selection.
- Health summary strip: placeholder badge (3.5) only, directly under the
  header, full width.
- Documents tab: F3 retrofit \u2014 renders F3's real scoped vault view
  (src/ui/documents_tab.py's render_scoped_vault, embedded=True) in place
  of the former "Coming with Module F3" placeholder badge.
- Chart of accounts / historical snapshot: bulk paste/import card (plain
  textarea) + primary action button, "Add row" secondary button below.
- GSTIN/PAN edit: inline reason field (3.3) beneath the field, Save
  disabled until populated.
- Branch Delete vs Deactivate: 3.2 + 3.4 (disabled-with-inline-reason).
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.f5 import service as f5
from src.ui.theme import render_inline_reason, render_placeholder_badge, render_view_history

_SYNC_TO_LIVE_CHIP = {
    "succeeded": ("connected", "Connected"),
    "not_attempted": ("needs_reauth", "Not attempted"),
    "attempted_failed": ("expired", "Failed"),
}
_SOURCE_LABELS = {"tally": "Tally", "gst_portal": "GST Portal", "traces": "TRACES", "bank": "Bank"}

SK_SELECTED_CLIENT_ID = "_f2_selected_client_id"
SK_SELECTED_BRANCH_ID = "_f2_selected_branch_id"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_clients_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "clients.profile.view"):
        st.warning("You don't have access to Client Profiles.")
        return

    selected_id = st.session_state.get(SK_SELECTED_CLIENT_ID)
    if selected_id is not None:
        client = clients.get_client(selected_id)
        if client is not None:
            _render_profile_screen(client, current_user)
            return
        st.session_state[SK_SELECTED_CLIENT_ID] = None

    _render_roster(current_user)


# ---------------------------------------------------------------------------
# Client Roster \u2014 the real F2 entry point
# ---------------------------------------------------------------------------


def _render_roster(current_user: dict[str, Any]) -> None:
    st.subheader("Client Roster")
    st.caption("Every End-Client on the platform. Search, filter, and open a profile.")

    top_l, top_r = st.columns([4, 1])
    with top_l:
        search = st.text_input(
            "Search by name, PAN, or GSTIN", key="f2_roster_search", label_visibility="collapsed",
            placeholder="Search by legal name, PAN, or GSTIN\u2026",
        )
    with top_r:
        can_manage = auth.has_permission(current_user, "clients.profile.manage")
        new_client_clicked = st.button(
            "+ New client", type="primary", disabled=not can_manage, width="stretch",
        )

    show_inactive = st.toggle("Show deactivated clients", value=False, key="f2_roster_show_inactive")

    all_clients = clients.list_clients(include_inactive=show_inactive)
    if search:
        s = search.lower()
        all_clients = [
            c for c in all_clients
            if s in (c["legal_name"] or "").lower()
            or s in (c["pan"] or "").lower()
            or s in (c.get("primary_gstin") or "").lower()
        ]

    if new_client_clicked:
        st.session_state["_f2_show_new_client_form"] = True

    if st.session_state.get("_f2_show_new_client_form") and can_manage:
        _render_new_client_form(current_user)

    if not all_clients:
        st.info("No clients yet. Use **+ New client** to onboard the first one.")
        return

    # Full-width table on the page background, hairline row dividers, no
    # card wrapper \u2014 per the F2 design table (this is the landing
    # screen, not a widget).
    header = st.columns([3, 2, 2, 1, 1])
    for col, label in zip(header, ["Legal name", "Primary GSTIN", "Assigned team", "Branches", "Status"]):
        col.markdown(f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>', unsafe_allow_html=True)
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    for c in all_clients:
        row = st.columns([3, 2, 2, 1, 1])
        with row[0]:
            if st.button(c["legal_name"], key=f"open_client_{c['client_id']}", type="tertiary"):
                st.session_state[SK_SELECTED_CLIENT_ID] = c["client_id"]
                st.session_state[SK_SELECTED_BRANCH_ID] = None
                st.rerun()
        with row[1]:
            st.write(c.get("primary_gstin") or "\u2014")
        with row[2]:
            st.write(c.get("assigned_team") or "\u2014")
        with row[3]:
            st.write(str(c.get("branch_count", 0)))
        with row[4]:
            if c["is_active"]:
                st.badge("Active", color="green")
            else:
                st.badge("Deactivated", color="gray")
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


def _render_new_client_form(current_user: dict[str, Any]) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">New client</span>', unsafe_allow_html=True)
    with st.form("f2_new_client_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            legal_name = st.text_input("Legal name *")
            pan = st.text_input("PAN")
            assigned_team = st.text_input("Assigned team")
        with c2:
            initial_gstin = st.text_input("Primary GSTIN (optional \u2014 can add branches later)")
            initial_state = st.text_input("State (for the primary GSTIN, optional)")
        submitted = st.form_submit_button("Create client", type="primary")

    if submitted:
        try:
            client_id = clients.create_client(
                legal_name=legal_name, pan=pan or None, assigned_team=assigned_team or None,
                initial_gstin=initial_gstin or None, initial_state=initial_state or None,
                actor=current_user["username"],
            )
            st.session_state["_f2_show_new_client_form"] = False
            st.session_state[SK_SELECTED_CLIENT_ID] = client_id
            st.session_state[SK_SELECTED_BRANCH_ID] = None
            st.success(f"Client '{legal_name}' created.")
            st.rerun()
        except clients.ClientError as exc:
            st.error(str(exc))
    st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Profile screen
# ---------------------------------------------------------------------------


def _render_health_strip(client: dict[str, Any]) -> None:
    """F5 -> F2 retrofit: second row of the health summary strip, real
    sync health per data source (Live connection status chip, 3.7,
    reused — its first reuse outside credential/portal connectivity).
    The first row's GSTIN-status framing stays a placeholder for the
    pieces this strip still can't show (e.g. Module 7-fed figures)."""
    st.markdown(
        '<div class="setu-card" style="padding:12px 18px;display:flex;align-items:center;gap:10px;">'
        '<span style="color:var(--setu-text-secondary);font-size:13px;">Live health status \u2014 '
        'coming with Books Readiness Check and Dashboards</span>'
        '<span class="setu-token-pill placeholder">Coming with Module 7 / Dashboards</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    try:
        rows = f5.sync_health_for_client(client["client_id"])
    except Exception:  # noqa: BLE001 \u2014 the health strip must never break the profile screen
        rows = []
    if not rows:
        return
    chip_html = ""
    for r in rows:
        css_status, label = _SYNC_TO_LIVE_CHIP.get(r["status"], ("needs_reauth", r["status"]))
        source_label = _SOURCE_LABELS.get(r["source"], r["source"])
        _cls = {"connected": "connected", "needs_reauth": "needs_reauth", "expired": "expired"}[css_status]
        chip_html += (
            f'<span style="margin-right:10px;color:var(--setu-text-secondary);font-size:12px;">{source_label}: '
            f'<span class="setu-live-chip {_cls}">{label}</span></span>'
        )
    st.markdown(
        f'<div class="setu-card" style="padding:10px 18px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;">'
        f'{chip_html}</div>',
        unsafe_allow_html=True,
    )


def _render_profile_screen(client: dict[str, Any], current_user: dict[str, Any]) -> None:
    if st.button("\u2190 Back to Client Roster"):
        st.session_state[SK_SELECTED_CLIENT_ID] = None
        st.rerun()

    can_manage = auth.has_permission(current_user, "clients.profile.manage")

    branches = clients.list_branches(client["client_id"], include_inactive=True)

    # Header in a card.
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    header_l, header_r = st.columns([3, 1])
    with header_l:
        st.markdown(f'<span class="setu-card-title" style="font-size:1.1rem;">{client["legal_name"]}</span>', unsafe_allow_html=True)
        st.caption(f"PAN: {client.get('pan') or '\u2014'} \u00b7 Assigned team: {client.get('assigned_team') or '\u2014'}")
        # F4 — reusable "View History" trigger, inline next to the record
        # header. Read-only; shows this client's own field-edit history.
        render_view_history("client", client["client_id"], current_user=current_user, label="View History")
    with header_r:
        if client["is_active"]:
            st.badge("Active", color="green")
        else:
            st.badge("Deactivated", color="gray")
    st.markdown('</div>', unsafe_allow_html=True)

    # Health summary strip \u2014 first row was a structural placeholder
    # only; F5 RETROFIT adds a second row with REAL sync health per data
    # source (Live connection status chip, 3.7), alongside a placeholder
    # for whatever this strip still can't show (e.g. Module 7-fed
    # figures), directly under the client header, full width.
    _render_health_strip(client)

    if not branches:
        st.info("No GSTIN/branches yet for this client.")
        selected_branch = None
    else:
        # Branch switcher \u2014 segmented control, informational-accent for
        # the selected branch.
        branch_labels = {b["branch_id"]: f'{b["gstin"]}{" (primary)" if b["is_primary"] else ""}' for b in branches}
        current_branch_id = st.session_state.get(SK_SELECTED_BRANCH_ID)
        if current_branch_id not in branch_labels:
            current_branch_id = branches[0]["branch_id"]
        chosen = st.segmented_control(
            "Branch", list(branch_labels.keys()), format_func=lambda bid: branch_labels[bid],
            default=current_branch_id, key=f"f2_branch_switch_{client['client_id']}",
        )
        st.session_state[SK_SELECTED_BRANCH_ID] = chosen if chosen is not None else current_branch_id
        selected_branch = next((b for b in branches if b["branch_id"] == st.session_state[SK_SELECTED_BRANCH_ID]), branches[0])

    # Client-level tabs \u2014 stay constant across branch selection.
    tab_branch, tab_contacts, tab_coa, tab_hist, tab_docs = st.tabs(
        ["Branch details", "Contacts", "Chart of Accounts", "Historical Snapshot", "Documents"]
    )

    with tab_branch:
        _render_branch_tab(client, selected_branch, current_user, can_manage)
    with tab_contacts:
        _render_contacts_tab(client, current_user, can_manage)
    with tab_coa:
        _render_coa_tab(client, current_user, can_manage)
    with tab_hist:
        _render_historical_tab(client, current_user, can_manage)
    with tab_docs:
        _render_documents_tab(client, current_user)

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    with st.expander("Client settings"):
        _render_client_settings(client, current_user, can_manage)


# ---------------------------------------------------------------------------
# Branch details tab
# ---------------------------------------------------------------------------


def _render_branch_tab(
    client: dict[str, Any], branch: Optional[dict[str, Any]], current_user: dict[str, Any], can_manage: bool,
) -> None:
    if branch is None:
        st.info("Add a GSTIN/branch below to get started.")
    else:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        c1, c2 = st.columns([3, 1])
        with c1:
            _render_gstin_field(client, branch, current_user)
            # F4 — reusable "View History" trigger for this branch record.
            render_view_history(
                "branch", branch["branch_id"], current_user=current_user,
                client_id=client["client_id"], label="View History",
            )
            branch_name = st.text_input("Branch name", value=branch.get("branch_name") or "", key=f"bname_{branch['branch_id']}")
            address = st.text_area("Address", value=branch.get("address") or "", key=f"baddr_{branch['branch_id']}", height=68)
            state = st.text_input("State", value=branch.get("state") or "", key=f"bstate_{branch['branch_id']}")
            status = st.selectbox(
                "Status", ["Not Started", "In Progress", "Under Review", "Filed", "Blocked"],
                index=["Not Started", "In Progress", "Under Review", "Filed", "Blocked"].index(branch["status"])
                if branch["status"] in ["Not Started", "In Progress", "Under Review", "Filed", "Blocked"] else 0,
                key=f"bstatus_{branch['branch_id']}", disabled=not can_manage,
                help="Independent per-branch status \u2014 GST filings are GSTIN-specific.",
            )
            if can_manage and st.button("Save branch details", key=f"bsave_{branch['branch_id']}"):
                clients.update_branch_field(branch["branch_id"], "branch_name", branch_name, actor=current_user["username"])
                clients.update_branch_field(branch["branch_id"], "address", address, actor=current_user["username"])
                clients.update_branch_field(branch["branch_id"], "state", state, actor=current_user["username"])
                clients.update_branch_field(branch["branch_id"], "status", status, actor=current_user["username"])
                st.success("Branch details saved.")
                st.rerun()
        with c2:
            _render_delete_vs_deactivate(branch, current_user, can_manage)
        st.markdown('</div>', unsafe_allow_html=True)

    if can_manage:
        with st.expander("+ Add another GSTIN/branch"):
            with st.form(f"f2_add_branch_{client['client_id']}", clear_on_submit=True):
                new_gstin = st.text_input("GSTIN *")
                new_name = st.text_input("Branch name")
                new_state = st.text_input("State")
                new_address = st.text_area("Address", height=60)
                add_clicked = st.form_submit_button("Add branch", type="primary")
            if add_clicked:
                try:
                    new_id = clients.add_branch(
                        client["client_id"], gstin=new_gstin, branch_name=new_name or None,
                        address=new_address or None, state=new_state or None, actor=current_user["username"],
                    )
                    st.session_state[SK_SELECTED_BRANCH_ID] = new_id
                    st.success(f"Branch '{new_gstin}' added \u2014 extends this profile, no re-onboarding needed.")
                    st.rerun()
                except clients.ClientError as exc:
                    st.error(str(exc))


def _render_gstin_field(client: dict[str, Any], branch: dict[str, Any], current_user: dict[str, Any]) -> None:
    """3.3 Reason-capture-before-save for a GSTIN edit."""
    key_val = f"gstin_val_{branch['branch_id']}"
    key_reason = f"gstin_reason_{branch['branch_id']}"
    key_dirty = f"gstin_dirty_{branch['branch_id']}"

    new_gstin = st.text_input("GSTIN", value=branch.get("gstin") or "", key=key_val)
    is_dirty = new_gstin != (branch.get("gstin") or "")
    st.session_state[key_dirty] = is_dirty

    reason = ""
    if is_dirty:
        reason = st.text_input(
            "Reason for this GSTIN change (required before saving)", key=key_reason,
            placeholder="e.g. correcting a data-entry error on onboarding",
        )
        save_disabled = not reason.strip()
        if st.button("Save GSTIN change", key=f"gstin_save_{branch['branch_id']}", disabled=save_disabled, type="primary"):
            try:
                clients.update_branch_field(
                    branch["branch_id"], "gstin", new_gstin, actor=current_user["username"], reason=reason,
                )
                st.success("GSTIN updated.")
                st.rerun()
            except clients.ClientError as exc:
                st.error(str(exc))
        if save_disabled:
            render_inline_reason("A reason is required before this change can be saved.")


def _render_delete_vs_deactivate(branch: dict[str, Any], current_user: dict[str, Any], can_manage: bool) -> None:
    """3.2 Delete vs Deactivate + 3.4 disabled-with-inline-reason."""
    st.caption("Branch actions")
    if branch["is_active"]:
        if st.button("Deactivate", key=f"bdeact_{branch['branch_id']}", disabled=not can_manage, width="stretch"):
            clients.set_branch_active(branch["branch_id"], False, actor=current_user["username"])
            st.rerun()
    else:
        if st.button("Reactivate", key=f"breact_{branch['branch_id']}", disabled=not can_manage, width="stretch"):
            clients.set_branch_active(branch["branch_id"], True, actor=current_user["username"])
            st.rerun()

    allowed, reason = clients.can_delete_branch(branch["branch_id"])
    st.button(
        "Delete", key=f"bdel_{branch['branch_id']}", disabled=(not allowed) or (not can_manage),
        width="stretch",
    )
    if not allowed:
        render_inline_reason(reason or "Delete isn't available.")


# ---------------------------------------------------------------------------
# Contacts tab (internal-reference only)
# ---------------------------------------------------------------------------


def _render_contacts_tab(client: dict[str, Any], current_user: dict[str, Any], can_manage: bool) -> None:
    st.caption("Internal reference only \u2014 not linked to the shared End-Client login.")
    contacts = clients.list_contacts(client["client_id"])

    for contact in contacts:
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown(f"**{contact['name']}** \u2014 {contact.get('role_title') or 'Contact'}")
                st.caption(f"{contact.get('email') or '\u2014'} \u00b7 {contact.get('phone') or '\u2014'}")
                if contact.get("notes"):
                    st.caption(contact["notes"])
            with c2:
                if can_manage and st.button("Remove", key=f"rmcontact_{contact['contact_id']}"):
                    clients.remove_contact(contact["contact_id"], actor=current_user["username"])
                    st.rerun()

    if can_manage:
        with st.expander("+ Add contact"):
            with st.form(f"f2_add_contact_{client['client_id']}", clear_on_submit=True):
                c1, c2 = st.columns(2)
                with c1:
                    name = st.text_input("Name *")
                    role_title = st.text_input("Role / title")
                with c2:
                    email = st.text_input("Email")
                    phone = st.text_input("Phone")
                notes = st.text_area("Notes", height=60)
                add_clicked = st.form_submit_button("Add contact", type="primary")
            if add_clicked:
                try:
                    clients.add_contact(
                        client["client_id"], name=name, role_title=role_title or None,
                        email=email or None, phone=phone or None, notes=notes or None,
                        actor=current_user["username"],
                    )
                    st.success(f"Contact '{name}' added.")
                    st.rerun()
                except clients.ClientError as exc:
                    st.error(str(exc))

    render_placeholder_badge("F1 retrofit \u2014 provisioning uses this contact list")


# ---------------------------------------------------------------------------
# Chart of Accounts tab
# ---------------------------------------------------------------------------


def _render_coa_tab(client: dict[str, Any], current_user: dict[str, Any], can_manage: bool) -> None:
    st.caption("Manual entry \u2014 bulk paste/import is the primary path. Tally-sync is NOT built this phase.")

    if can_manage:
        st.info(clients.coa_edit_warning(), icon="\u2139\ufe0f")

        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Bulk paste / import</span>', unsafe_allow_html=True)
        bulk_text = st.text_area(
            "Paste rows: code, name, type (tab or comma separated, one per line)",
            height=140, key=f"coa_bulk_{client['client_id']}",
            placeholder="1001, Cash in Hand, Asset\n2001, Sales Revenue, Income",
        )
        if st.button("Import rows", type="primary", key=f"coa_import_{client['client_id']}"):
            try:
                n = clients.bulk_import_coa(client["client_id"], bulk_text, actor=current_user["username"])
                st.success(f"Imported {n} row(s).")
                st.rerun()
            except clients.ClientError as exc:
                st.error(str(exc))
        st.markdown('</div>', unsafe_allow_html=True)

        with st.expander("+ Add row (one-by-one)"):
            c1, c2, c3 = st.columns(3)
            with c1:
                code = st.text_input("Code", key=f"coa_code_{client['client_id']}")
            with c2:
                name = st.text_input("Name", key=f"coa_name_{client['client_id']}")
            with c3:
                account_type = st.text_input("Type", key=f"coa_type_{client['client_id']}")
            if st.button("Add row", key=f"coa_addrow_{client['client_id']}"):
                try:
                    clients.add_coa_row(client["client_id"], code, name, account_type, actor=current_user["username"])
                    st.success("Row added.")
                    st.rerun()
                except clients.ClientError as exc:
                    st.error(str(exc))

    rows = clients.list_coa(client["client_id"])
    if not rows:
        st.info("No chart-of-accounts entries yet.")
        return

    header = st.columns([2, 4, 2, 1])
    for col, label in zip(header, ["Code", "Name", "Type", ""]):
        col.markdown(f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>', unsafe_allow_html=True)
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    for r in rows:
        row = st.columns([2, 4, 2, 1])
        row[0].write(r["code"])
        row[1].write(r["name"])
        row[2].write(r.get("account_type") or "\u2014")
        with row[3]:
            if can_manage and st.button("\u2715", key=f"coa_del_{r['coa_id']}"):
                clients.delete_coa_row(r["coa_id"], actor=current_user["username"])
                st.rerun()
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Historical Snapshot tab
# ---------------------------------------------------------------------------


def _render_historical_tab(client: dict[str, Any], current_user: dict[str, Any], can_manage: bool) -> None:
    st.caption("Prior year closing/audited figures, stored per fiscal year.")

    years = clients.list_snapshot_years(client["client_id"])
    fiscal_year_choice = st.selectbox(
        "Fiscal year", years + (["(new)"] if True else []), index=0 if years else 0,
        key=f"hist_year_select_{client['client_id']}",
    ) if years else "(new)"

    if fiscal_year_choice == "(new)" or not years:
        fiscal_year = st.text_input(
            "New fiscal year (e.g. 2024-25)", key=f"hist_new_year_{client['client_id']}",
        )
    else:
        fiscal_year = fiscal_year_choice

    if can_manage:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Bulk paste / import</span>', unsafe_allow_html=True)
        bulk_text = st.text_area(
            "Paste rows: line item, amount, notes (tab or comma separated, one per line)",
            height=140, key=f"hist_bulk_{client['client_id']}",
            placeholder="Closing Cash, 125000, Per audited FY23-24 books",
        )
        if st.button("Import rows", type="primary", key=f"hist_import_{client['client_id']}"):
            try:
                n = clients.bulk_import_snapshot(
                    client["client_id"], fiscal_year, bulk_text, actor=current_user["username"],
                )
                st.success(f"Imported {n} row(s) for {fiscal_year}.")
                st.rerun()
            except clients.ClientError as exc:
                st.error(str(exc))
        st.markdown('</div>', unsafe_allow_html=True)

        with st.expander("+ Add row (one-by-one)"):
            c1, c2, c3 = st.columns(3)
            with c1:
                line_item = st.text_input("Line item", key=f"hist_item_{client['client_id']}")
            with c2:
                amount = st.text_input("Amount", key=f"hist_amt_{client['client_id']}")
            with c3:
                notes = st.text_input("Notes", key=f"hist_notes_{client['client_id']}")
            if st.button("Add row", key=f"hist_addrow_{client['client_id']}"):
                try:
                    clients.add_snapshot_row(
                        client["client_id"], fiscal_year, line_item, amount, notes, actor=current_user["username"],
                    )
                    st.success("Row added.")
                    st.rerun()
                except clients.ClientError as exc:
                    st.error(str(exc))

    rows = clients.list_snapshots(client["client_id"], fiscal_year if fiscal_year in years else None)
    if not rows:
        st.info("No historical snapshot rows yet for this fiscal year.")
        return

    header = st.columns([2, 3, 2, 3, 1])
    for col, label in zip(header, ["Fiscal year", "Line item", "Amount", "Notes", ""]):
        col.markdown(f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>', unsafe_allow_html=True)
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    for r in rows:
        row = st.columns([2, 3, 2, 3, 1])
        row[0].write(r["fiscal_year"])
        row[1].write(r["line_item"])
        row[2].write(r.get("amount") or "\u2014")
        row[3].write(r.get("notes") or "\u2014")
        with row[4]:
            if can_manage and st.button("\u2715", key=f"hist_del_{r['snapshot_id']}"):
                clients.delete_snapshot_row(r["snapshot_id"], actor=current_user["username"])
                st.rerun()
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Documents tab — F3 retrofit: real scoped vault view, replacing the
# "Coming with Module F3" placeholder badge that lived here previously.
# ---------------------------------------------------------------------------


def _render_documents_tab(client: dict[str, Any], current_user: dict[str, Any]) -> None:
    from src.ui.documents_tab import render_scoped_vault

    if not auth.has_permission(current_user, "documents.view"):
        render_placeholder_badge("Module F3")
        st.caption("You don't have access to the Document & Data Repository.")
        return
    render_scoped_vault(client["client_id"], current_user, embedded=True)


# ---------------------------------------------------------------------------
# Client-level settings (legal name / PAN / assigned team / deactivate)
# ---------------------------------------------------------------------------


def _render_client_settings(client: dict[str, Any], current_user: dict[str, Any], can_manage: bool) -> None:
    if not can_manage:
        st.caption("You don't have permission to edit this client's settings.")
        return

    legal_name = st.text_input("Legal name", value=client["legal_name"], key=f"cname_{client['client_id']}")
    assigned_team = st.text_input("Assigned team", value=client.get("assigned_team") or "", key=f"cteam_{client['client_id']}")
    if st.button("Save name / team", key=f"csave_{client['client_id']}"):
        clients.update_client_field(client["client_id"], "legal_name", legal_name, actor=current_user["username"])
        clients.update_client_field(client["client_id"], "assigned_team", assigned_team, actor=current_user["username"])
        st.success("Saved.")
        st.rerun()

    # 3.3 Reason-capture-before-save for PAN.
    key_pan = f"pan_val_{client['client_id']}"
    new_pan = st.text_input("PAN", value=client.get("pan") or "", key=key_pan)
    if new_pan != (client.get("pan") or ""):
        reason = st.text_input(
            "Reason for this PAN change (required before saving)", key=f"pan_reason_{client['client_id']}",
        )
        save_disabled = not reason.strip()
        if st.button("Save PAN change", key=f"pan_save_{client['client_id']}", disabled=save_disabled, type="primary"):
            try:
                clients.update_client_field(
                    client["client_id"], "pan", new_pan, actor=current_user["username"], reason=reason,
                )
                st.success("PAN updated.")
                st.rerun()
            except clients.ClientError as exc:
                st.error(str(exc))
        if save_disabled:
            render_inline_reason("A reason is required before this change can be saved.")

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    if client["is_active"]:
        if st.button("Deactivate client", key=f"cdeact_{client['client_id']}"):
            clients.set_client_active(client["client_id"], False, actor=current_user["username"])
            st.rerun()
        st.caption("Deactivation is soft-delete only \u2014 full history is preserved.")
    else:
        if st.button("Reactivate client", key=f"creact_{client['client_id']}"):
            clients.set_client_active(client["client_id"], True, actor=current_user["username"])
            st.rerun()

    # C1 -> F2 retrofit: link to this client's rule overrides, pre-filtered
    # into C1's Rules Workspace scope switcher for that client.
    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    if auth.has_permission(current_user, "rules.view"):
        st.markdown("**Rule overrides** \u2014 see and edit this client's rule overrides in the Rules tab.")
        if st.button("Rule overrides for this client", key=f"crulelink_{client['client_id']}"):
            st.session_state["_c1_preselect_client_id"] = client["client_id"]
            st.toast("Open the Rules tab to view this client's overrides.")
    else:
        st.caption("Rule overrides require the Rules tab (rules.view permission).")
