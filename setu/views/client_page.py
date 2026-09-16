"""F2 — Client Profile & Master Data.

Roster (full-width hairline table, no card wrapper) + profile screen (header
card, health strip, branch switcher, five client-level tabs). Uses only
Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.client_state import BRANCH_STATUSES, ClientState
from setu.views import shell

_TABS = ["Branch details", "Contacts", "Chart of Accounts", "Historical Snapshot", "Documents"]


def _tab_button(label: str) -> rx.Component:
    active = ClientState.profile_tab == label
    return rx.button(
        label,
        on_click=ClientState.set_profile_tab(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _field(label: str, value, on_change, *, placeholder: str = "", type_: str = "text") -> rx.Component:
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.input(value=value, on_change=on_change, placeholder=placeholder, type=type_, width="100%"),
        spacing="1",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


def _roster_header() -> rx.Component:
    return rx.hstack(
        rx.text("Legal name", style=t.TEXT["label"], flex="3"),
        rx.text("Primary GSTIN", style=t.TEXT["label"], flex="2"),
        rx.text("Assigned team", style=t.TEXT["label"], flex="2"),
        rx.text("Branches", style=t.TEXT["label"], flex="1"),
        rx.text("Status", style=t.TEXT["label"], flex="1"),
        width="100%",
        padding="0 4px 6px 4px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _roster_row(row) -> rx.Component:
    return rx.hstack(
        rx.button(
            row.legal_name,
            on_click=ClientState.open_client(row.client_id),
            variant="ghost",
            color_scheme="gray",
            size="1",
            justify="start",
            flex="3",
            color=t.Color.TEXT_PRIMARY.value,
            font_weight="600",
            _hover={"color": t.Color.ACCENT.value},
        ),
        rx.text(row.primary_gstin, style=t.TEXT["body"], flex="2"),
        rx.text(row.assigned_team, style=t.TEXT["body"], flex="2"),
        rx.text(row.branch_count.to_string(), style=t.TEXT["body"], flex="1"),
        rx.box(
            rx.cond(
                row.is_active,
                c.pill("Active", variant="rule"),
                c.pill("Deactivated", variant="placeholder"),
            ),
            flex="1",
        ),
        width="100%",
        align="center",
        padding="8px 4px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _new_client_form() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("New client", style=t.TEXT["card_title"]),
            rx.cond(ClientState.nc_error != "", c.inline_reason(ClientState.nc_error), rx.fragment()),
            rx.grid(
                _field("Legal name *", ClientState.nc_legal_name, ClientState.set_nc_legal_name),
                _field("PAN", ClientState.nc_pan, ClientState.set_nc_pan),
                _field("Assigned team", ClientState.nc_team, ClientState.set_nc_team),
                _field(
                    "Primary GSTIN (optional — add branches later)",
                    ClientState.nc_gstin,
                    ClientState.set_nc_gstin,
                ),
                _field("State (for the primary GSTIN)", ClientState.nc_state, ClientState.set_nc_state),
                columns="2",
                spacing="4",
                width="100%",
            ),
            rx.hstack(
                rx.button(
                    "Create client",
                    on_click=ClientState.create_client,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button("Cancel", variant="soft", color_scheme="gray", on_click=ClientState.toggle_new_client_form),
                spacing="2",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _roster() -> rx.Component:
    return rx.vstack(
        c.page_header("Client Roster", "Every End-Client on the platform. Search, filter, and open a profile."),
        rx.cond(ClientState.flash != "", c.info_banner(ClientState.flash), rx.fragment()),
        rx.hstack(
            rx.input(
                value=ClientState.roster_search,
                on_change=ClientState.set_roster_search,
                placeholder="Search by legal name, PAN, or GSTIN…",
                flex="1",
            ),
            rx.button(
                "+ New client",
                on_click=ClientState.toggle_new_client_form,
                disabled=~ClientState.can_manage,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
            ),
            width="100%",
            align="center",
            spacing="3",
        ),
        rx.hstack(
            rx.switch(
                checked=ClientState.show_inactive,
                on_change=ClientState.set_show_inactive,
                color_scheme="indigo",
            ),
            rx.text("Show deactivated clients", style=t.TEXT["label"]),
            spacing="2",
            align="center",
        ),
        rx.cond(ClientState.show_new_client_form, _new_client_form(), rx.fragment()),
        rx.cond(
            ClientState.roster.length() > 0,
            rx.vstack(
                _roster_header(),
                rx.foreach(ClientState.roster, _roster_row),
                spacing="0",
                width="100%",
            ),
            c.empty_state("No clients yet. Use + New client to onboard the first one.", icon="building-2"),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Profile — header + health strip
# ---------------------------------------------------------------------------


def _health_strip() -> rx.Component:
    return rx.vstack(
        # Row 1: structural placeholder (per F2 spec — wired to nothing).
        rx.hstack(
            rx.text(
                "Live health status — coming with Books Readiness Check and Dashboards",
                style=t.TEXT["label"],
            ),
            rx.spacer(),
            c.placeholder_badge("Module 7 / Dashboards"),
            width="100%",
            align="center",
            padding="10px 18px",
        ),
        # Row 2: F5 retrofit — real per-source sync health.
        rx.cond(
            ClientState.sync_chips.length() > 0,
            rx.hstack(
                rx.foreach(
                    ClientState.sync_chips,
                    lambda chip: rx.hstack(
                        rx.text(f"{chip.source_label}:", style=t.TEXT["micro"]),
                        c.live_status_chip(chip.status, label=chip.label),
                        spacing="1",
                        align="center",
                    ),
                ),
                spacing="4",
                wrap="wrap",
                width="100%",
                padding="10px 18px",
            ),
            rx.fragment(),
        ),
        spacing="0",
        width="100%",
        background=t.Color.SURFACE.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius=t.RADIUS,
        box_shadow=t.CARD_SHADOW,
    )


def _profile_header() -> rx.Component:
    return c.card(
        rx.hstack(
            rx.vstack(
                rx.text(ClientState.client_name, font_size="20px", font_weight="700", color=t.Color.TEXT_PRIMARY.value),
                rx.text(
                    f"PAN: {ClientState.client_pan} · Assigned team: {ClientState.client_team}",
                    style=t.TEXT["label"],
                ),
                spacing="1",
                align="start",
            ),
            rx.spacer(),
            # F4 — the reusable View History trigger, inline next to the record.
            c.view_history(
                "View History",
                ClientState.client_history,
                count=ClientState.client_history.length(),
            ),
            rx.cond(
                ClientState.client_active,
                c.pill("Active", variant="rule"),
                c.pill("Deactivated", variant="placeholder"),
            ),
            width="100%",
            align="center",
            spacing="3",
        ),
        width="100%",
    )


def _branch_switcher() -> rx.Component:
    return rx.cond(
        ClientState.branches.length() > 0,
        rx.hstack(
            rx.text("Branch:", style=t.TEXT["micro"]),
            rx.foreach(
                ClientState.branches,
                lambda b: rx.button(
                    rx.cond(b.is_primary, f"{b.gstin} (primary)", b.gstin),
                    on_click=ClientState.select_branch(b.branch_id),
                    size="1",
                    variant="soft",
                    background=rx.cond(
                        ClientState.selected_branch_id == b.branch_id,
                        t.Color.ACCENT.value,
                        "transparent",
                    ),
                    color=rx.cond(
                        ClientState.selected_branch_id == b.branch_id,
                        "#FFFFFF",
                        t.Color.TEXT_SECONDARY.value,
                    ),
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
            ),
            spacing="2",
            wrap="wrap",
            align="center",
            width="100%",
        ),
        rx.fragment(),
    )


# ---------------------------------------------------------------------------
# Branch details tab
# ---------------------------------------------------------------------------


def _branch_details() -> rx.Component:
    return rx.cond(
        ClientState.branches.length() > 0,
        rx.vstack(
            c.card(
                rx.hstack(
                    rx.text("Branch details", style=t.TEXT["card_title"]),
                    rx.spacer(),
                    # F4 — per-branch edit history (GSTIN edits are mandatory-reason).
                    c.view_history(
                        "View History",
                        ClientState.branch_history,
                        count=ClientState.branch_history.length(),
                    ),
                    width="100%",
                    align="center",
                ),
                rx.grid(
                    _field("Branch name", ClientState.b_name, ClientState.set_b_name),
                    _field("State", ClientState.b_state, ClientState.set_b_state),
                    rx.vstack(
                        rx.text("Status", style=t.TEXT["label"]),
                        rx.select(
                            BRANCH_STATUSES,
                            value=ClientState.b_status,
                            on_change=ClientState.set_b_status,
                            width="100%",
                        ),
                        rx.text(
                            "Independent per-branch status — GST filings are GSTIN-specific.",
                            style=t.TEXT["micro"],
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    rx.vstack(
                        rx.text("Address", style=t.TEXT["label"]),
                        rx.text_area(
                            value=ClientState.b_address,
                            on_change=ClientState.set_b_address,
                            width="100%",
                            rows="2",
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    columns="2",
                    spacing="4",
                    width="100%",
                ),
                rx.box(height="12px"),
                rx.button(
                    "Save branch details",
                    on_click=ClientState.save_branch_details,
                    disabled=~ClientState.can_manage,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                width="100%",
            ),
            # GSTIN edit — 3.3 reason-capture-before-save.
            c.card(
                rx.vstack(
                    rx.text("GSTIN", style=t.TEXT["card_title"]),
                    _field("GSTIN", ClientState.b_gstin, ClientState.set_b_gstin),
                    rx.cond(
                        ClientState.gstin_dirty,
                        rx.vstack(
                            _field(
                                "Reason for this GSTIN change (required before saving)",
                                ClientState.b_gstin_reason,
                                ClientState.set_b_gstin_reason,
                                placeholder="e.g. correcting a data-entry error on onboarding",
                            ),
                            rx.button(
                                "Save GSTIN change",
                                on_click=ClientState.save_gstin_change,
                                disabled=ClientState.b_gstin_reason == "",
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                            ),
                            rx.cond(
                                ClientState.b_gstin_reason == "",
                                c.inline_reason("A reason is required before this change can be saved."),
                                rx.fragment(),
                            ),
                            spacing="2",
                            align="start",
                            width="100%",
                        ),
                        rx.fragment(),
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            # 3.2 Delete vs Deactivate + 3.4 disabled-with-inline-reason.
            c.card(
                rx.vstack(
                    rx.text("Branch actions", style=t.TEXT["card_title"]),
                    rx.hstack(
                        rx.cond(
                            ClientState.branches.length() > 0,
                            rx.foreach(
                                ClientState.branches,
                                lambda b: rx.cond(
                                    b.branch_id == ClientState.selected_branch_id,
                                    rx.hstack(
                                        rx.cond(
                                            b.is_active,
                                            c.deactivate_button(
                                                "Deactivate",
                                                on_click=ClientState.set_branch_active(b.branch_id, False),
                                                disabled=~ClientState.can_manage,
                                                size="1",
                                            ),
                                            c.deactivate_button(
                                                "Reactivate",
                                                on_click=ClientState.set_branch_active(b.branch_id, True),
                                                disabled=~ClientState.can_manage,
                                                size="1",
                                            ),
                                        ),
                                        c.delete_button(
                                            "Delete",
                                            on_click=ClientState.delete_branch(b.branch_id),
                                            disabled=True,
                                            size="1",
                                        ),
                                        spacing="2",
                                    ),
                                    rx.fragment(),
                                ),
                            ),
                            rx.fragment(),
                        ),
                        spacing="2",
                    ),
                    c.inline_reason(
                        "Can't delete — open reconciliation work references this branch. Deactivate instead."
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            # Add another branch.
            rx.cond(
                ClientState.can_manage,
                c.card(
                    rx.vstack(
                        rx.text("+ Add another GSTIN/branch", style=t.TEXT["card_title"]),
                        rx.grid(
                            _field("GSTIN *", ClientState.ab_gstin, ClientState.set_ab_gstin),
                            _field("Branch name", ClientState.ab_name, ClientState.set_ab_name),
                            _field("State", ClientState.ab_state, ClientState.set_ab_state),
                            _field("Address", ClientState.ab_address, ClientState.set_ab_address),
                            columns="2",
                            spacing="4",
                            width="100%",
                        ),
                        rx.button(
                            "Add branch",
                            on_click=ClientState.add_branch,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                rx.fragment(),
            ),
            spacing="4",
            width="100%",
            align="start",
        ),
        c.empty_state("No GSTIN/branches yet for this client.", icon="map-pin"),
    )


# ---------------------------------------------------------------------------
# Contacts tab
# ---------------------------------------------------------------------------


def _contact_row(contact) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.hstack(
                rx.text(contact.name, font_size="13px", font_weight="600"),
                rx.text(f"— {contact.role_title}", style=t.TEXT["label"]),
                spacing="2",
                align="baseline",
            ),
            rx.text(f"{contact.email} · {contact.phone}", style=t.TEXT["micro"]),
            rx.cond(contact.notes != "", rx.text(contact.notes, style=t.TEXT["micro"]), rx.fragment()),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.cond(
            ClientState.can_manage,
            rx.icon_button(
                rx.icon("x", size=14),
                variant="ghost",
                color_scheme="gray",
                size="1",
                on_click=ClientState.remove_contact(contact.contact_id),
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _contacts_tab() -> rx.Component:
    return rx.vstack(
        rx.text("Internal reference only — not linked to the shared End-Client login.", style=t.TEXT["label"]),
        rx.cond(
            ClientState.contacts.length() > 0,
            rx.vstack(rx.foreach(ClientState.contacts, _contact_row), spacing="0", width="100%"),
            c.empty_state("No contacts yet.", icon="users"),
        ),
        rx.cond(
            ClientState.can_manage,
            c.card(
                rx.vstack(
                    rx.text("+ Add contact", style=t.TEXT["card_title"]),
                    rx.grid(
                        _field("Name *", ClientState.ac_name, ClientState.set_ac_name),
                        _field("Role / title", ClientState.ac_role, ClientState.set_ac_role),
                        _field("Email", ClientState.ac_email, ClientState.set_ac_email),
                        _field("Phone", ClientState.ac_phone, ClientState.set_ac_phone),
                        _field("Notes", ClientState.ac_notes, ClientState.set_ac_notes),
                        columns="2",
                        spacing="4",
                        width="100%",
                    ),
                    rx.button(
                        "Add contact",
                        on_click=ClientState.add_contact,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        c.placeholder_badge("F1 retrofit — provisioning uses this contact list"),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Chart of accounts tab
# ---------------------------------------------------------------------------


def _coa_row(row) -> rx.Component:
    return rx.hstack(
        rx.text(row.code, style=t.TEXT["body"], flex="2"),
        rx.text(row.name, style=t.TEXT["body"], flex="4"),
        rx.text(row.account_type, style=t.TEXT["body"], flex="2"),
        rx.cond(
            ClientState.can_manage,
            rx.icon_button(
                rx.icon("x", size=14),
                variant="ghost",
                color_scheme="gray",
                size="1",
                on_click=ClientState.delete_coa_row(row.coa_id),
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _coa_tab() -> rx.Component:
    return rx.vstack(
        rx.text(
            "Manual entry — bulk paste/import is the primary path. Tally-sync is NOT built this phase.",
            style=t.TEXT["label"],
        ),
        rx.cond(ClientState.can_manage, c.warning_banner(ClientState.coa_warning), rx.fragment()),
        rx.cond(
            ClientState.can_manage,
            c.card(
                rx.vstack(
                    rx.text("Bulk paste / import", style=t.TEXT["card_title"]),
                    rx.text_area(
                        value=ClientState.coa_bulk,
                        on_change=ClientState.set_coa_bulk,
                        placeholder="1001, Cash in Hand, Asset\n2001, Sales Revenue, Income",
                        rows="5",
                        width="100%",
                    ),
                    rx.button(
                        "Import rows",
                        on_click=ClientState.import_coa,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        rx.cond(
            ClientState.can_manage,
            c.card(
                rx.vstack(
                    rx.text("+ Add row (one-by-one)", style=t.TEXT["card_title"]),
                    rx.grid(
                        _field("Code", ClientState.coa_code, ClientState.set_coa_code),
                        _field("Name", ClientState.coa_name, ClientState.set_coa_name),
                        _field("Type", ClientState.coa_type, ClientState.set_coa_type),
                        columns="3",
                        spacing="4",
                        width="100%",
                    ),
                    rx.button("Add row", variant="soft", color_scheme="gray", on_click=ClientState.add_coa_row),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        rx.cond(
            ClientState.coa.length() > 0,
            rx.vstack(rx.foreach(ClientState.coa, _coa_row), spacing="0", width="100%"),
            c.empty_state("No chart-of-accounts entries yet.", icon="table"),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Historical snapshot tab
# ---------------------------------------------------------------------------


def _snapshot_row(row) -> rx.Component:
    return rx.hstack(
        rx.text(row.fiscal_year, style=t.TEXT["body"], flex="2"),
        rx.text(row.line_item, style=t.TEXT["body"], flex="3"),
        rx.text(row.amount, style=t.TEXT["body"], flex="2"),
        rx.text(row.notes, style=t.TEXT["body"], flex="3"),
        rx.cond(
            ClientState.can_manage,
            rx.icon_button(
                rx.icon("x", size=14),
                variant="ghost",
                color_scheme="gray",
                size="1",
                on_click=ClientState.delete_snapshot_row(row.snapshot_id),
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _historical_tab() -> rx.Component:
    return rx.vstack(
        rx.text("Prior year closing/audited figures, stored per fiscal year.", style=t.TEXT["label"]),
        rx.hstack(
            rx.text("Fiscal year", style=t.TEXT["label"]),
            rx.cond(
                ClientState.snapshot_years.length() > 0,
                rx.select(
                    ClientState.snapshot_years,
                    value=ClientState.snapshot_year,
                    on_change=ClientState.set_snapshot_year,
                    width="180px",
                ),
                rx.input(
                    value=ClientState.snap_new_year,
                    on_change=ClientState.set_snap_new_year,
                    placeholder="e.g. 2024-25",
                    width="180px",
                ),
            ),
            spacing="2",
            align="center",
        ),
        rx.cond(
            ClientState.can_manage,
            c.card(
                rx.vstack(
                    rx.text("Bulk paste / import", style=t.TEXT["card_title"]),
                    rx.text_area(
                        value=ClientState.snap_bulk,
                        on_change=ClientState.set_snap_bulk,
                        placeholder="Closing Cash, 125000, Per audited FY23-24 books",
                        rows="5",
                        width="100%",
                    ),
                    rx.button(
                        "Import rows",
                        on_click=ClientState.import_snapshot,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        rx.cond(
            ClientState.can_manage,
            c.card(
                rx.vstack(
                    rx.text("+ Add row (one-by-one)", style=t.TEXT["card_title"]),
                    rx.grid(
                        _field("Line item", ClientState.snap_item, ClientState.set_snap_item),
                        _field("Amount", ClientState.snap_amount, ClientState.set_snap_amount),
                        _field("Notes", ClientState.snap_notes, ClientState.set_snap_notes),
                        columns="3",
                        spacing="4",
                        width="100%",
                    ),
                    rx.button("Add row", variant="soft", color_scheme="gray", on_click=ClientState.add_snapshot_row),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        rx.cond(
            ClientState.snapshots.length() > 0,
            rx.vstack(rx.foreach(ClientState.snapshots, _snapshot_row), spacing="0", width="100%"),
            c.empty_state("No historical snapshot rows yet for this fiscal year.", icon="history"),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Documents tab (F3 retrofit placeholder until F3 is ported)
# ---------------------------------------------------------------------------


def _documents_tab() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(c.placeholder_badge("Module F3"), rx.spacer(), width="100%"),
            rx.text(
                "The scoped document vault renders here once the Document & Data Repository is migrated.",
                style=t.TEXT["body"],
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Client settings
# ---------------------------------------------------------------------------


def _client_settings() -> rx.Component:
    return rx.cond(
        ClientState.can_manage,
        c.card(
            rx.vstack(
                rx.text("Client settings", style=t.TEXT["card_title"]),
                rx.grid(
                    _field("Legal name", ClientState.cs_name, ClientState.set_cs_name),
                    _field("Assigned team", ClientState.cs_team, ClientState.set_cs_team),
                    columns="2",
                    spacing="4",
                    width="100%",
                ),
                rx.button(
                    "Save name / team",
                    variant="soft",
                    color_scheme="gray",
                    on_click=ClientState.save_client_name_team,
                ),
                c.divider(),
                _field("PAN", ClientState.cs_pan, ClientState.set_cs_pan),
                rx.cond(
                    ClientState.pan_dirty,
                    rx.vstack(
                        _field(
                            "Reason for this PAN change (required before saving)",
                            ClientState.cs_pan_reason,
                            ClientState.set_cs_pan_reason,
                        ),
                        rx.button(
                            "Save PAN change",
                            on_click=ClientState.save_pan_change,
                            disabled=ClientState.cs_pan_reason == "",
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.cond(
                            ClientState.cs_pan_reason == "",
                            c.inline_reason("A reason is required before this change can be saved."),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="start",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                c.divider(),
                rx.cond(
                    ClientState.client_active,
                    rx.vstack(
                        c.deactivate_button(
                            "Deactivate client",
                            on_click=ClientState.set_client_active(False),
                        ),
                        rx.text(
                            "Deactivation is soft-delete only — full history is preserved.",
                            style=t.TEXT["micro"],
                        ),
                        spacing="1",
                        align="start",
                    ),
                    c.deactivate_button(
                        "Reactivate client",
                        on_click=ClientState.set_client_active(True),
                    ),
                ),
                spacing="4",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


# ---------------------------------------------------------------------------
# Profile screen
# ---------------------------------------------------------------------------


def _profile() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back to Client Roster",
            variant="ghost",
            color_scheme="gray",
            size="1",
            on_click=ClientState.back_to_roster,
        ),
        rx.cond(ClientState.flash != "", c.info_banner(ClientState.flash), rx.fragment()),
        _profile_header(),
        _health_strip(),
        _branch_switcher(),
        rx.hstack(
            *[_tab_button(label) for label in _TABS],
            spacing="2",
            wrap="wrap",
            padding="4px",
            background=t.Color.SURFACE.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="12px",
            width="100%",
        ),
        rx.match(
            ClientState.profile_tab,
            ("Branch details", _branch_details()),
            ("Contacts", _contacts_tab()),
            ("Chart of Accounts", _coa_tab()),
            ("Historical Snapshot", _historical_tab()),
            ("Documents", _documents_tab()),
            _branch_details(),
        ),
        _client_settings(),
        spacing="4",
        width="100%",
        align="start",
    )


def clients_page() -> rx.Component:
    return shell.shell(
        rx.cond(
            ClientState.selected_client_id != 0,
            _profile(),
            _roster(),
        )
    )