"""F2 — Client Profile & Master Data.

Roster (full-width hairline table, no card wrapper, and now no inline form)
+ profile screen (header card, health strip, five client-level tabs). Uses
only Foundation components.

The 27-Sep-2026 revision removed the GSTINBranch sub-entity: one GST
registration = one EndClient. So the roster has no "Branches" column, the
profile has no branch switcher, and the former "Branch details" + "Client
settings" panels are ONE "Details" tab — rendered exactly once, never
duplicated at the bottom of every tab.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.client_state import PROFILE_TABS, ClientState
from setu.views import shell
from setu.views.documents_page import embedded_vault


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
        rx.text("PAN / TAN", style=t.TEXT["label"], flex="2"),
        rx.text("GSTIN", style=t.TEXT["label"], flex="2"),
        rx.text("Assigned team", style=t.TEXT["label"], flex="2"),
        rx.text("Status", style=t.TEXT["label"], flex="1"),
        width="100%",
        padding="0 4px 6px 4px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _identity_cell(pan, tan) -> rx.Component:
    """The "PAN / TAN" column: PAN on top, TAN beneath when present.

    Both are optional individually, but at least one is required — so a dash
    here means the record genuinely has neither.
    """
    return rx.vstack(
        rx.cond(pan != "", rx.text(pan, style=t.TEXT["body"]), rx.text("—", style=t.TEXT["body"])),
        rx.cond(tan != "", rx.text(tan, style=t.TEXT["micro"]), rx.fragment()),
        spacing="0",
        align="start",
        width="100%",
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
        rx.box(_identity_cell(row.pan, row.tan), flex="2"),
        rx.box(
            rx.cond(row.gstin != "", rx.text(row.gstin, style=t.TEXT["body"]), rx.text("—", style=t.TEXT["body"])),
            flex="2",
        ),
        rx.text(row.assigned_team, style=t.TEXT["body"], flex="2"),
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


def _roster() -> rx.Component:
    return rx.vstack(
        c.page_header("Client Roster", "Every End-Client on the platform. Search, filter, and open a profile."),
        rx.cond(ClientState.flash != "", c.info_banner(ClientState.flash), rx.fragment()),
        rx.hstack(
            rx.input(
                value=ClientState.roster_search,
                on_change=ClientState.set_roster_search,
                placeholder="Search by legal name, PAN, TAN, or GSTIN…",
                flex="1",
            ),
            # Navigates to the standalone New Client screen — this page
            # deliberately renders NO form fields.
            rx.button(
                "+ New client",
                on_click=ClientState.new_client,
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
                rx.text(ClientState.identity_line, style=t.TEXT["label"]),
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


def _profile_actions() -> rx.Component:
    """The profile's escape hatches: back to the Roster, and straight into
    creating a new client. Both must be plainly visible — the profile and the
    roster share one route, so these are the only way off the profile."""
    return rx.hstack(
        c.back_button("← Back to Client Roster", ClientState.back_to_roster),
        rx.spacer(),
        rx.button(
            "+ New client",
            on_click=ClientState.new_client,
            disabled=~ClientState.can_manage,
            background=t.Color.ACCENT.value,
            color="#FFFFFF",
        ),
        width="100%",
        align="center",
        spacing="3",
    )


# ---------------------------------------------------------------------------
# Details tab — the former "Branch details" + "Client settings", now ONE tab
# ---------------------------------------------------------------------------


def _identity_field(
    *,
    label: str,
    hint: str,
    value,
    on_change,
    dirty,
    reason,
    on_reason_change,
    on_save,
    blocked_reason,
) -> rx.Component:
    """A Manager+-gated identity field (PAN / TAN / GSTIN).

    Editing reveals a reason-capture-before-save field beneath the field being
    edited and Save stays disabled until it is filled (Foundation 3.3). When
    the field is blocked — by the caller's role, or for the GSTIN by open
    reconciliation work — the Save control is disabled with its inline reason
    visible (Foundation 3.4), never hidden.
    """
    return c.card(
        rx.vstack(
            rx.text(label, style=t.TEXT["card_title"]),
            rx.input(value=value, on_change=on_change, placeholder=hint, width="100%"),
            # Blocked is a property of the RECORD + the caller's role, not of
            # what has been typed, so it is stated up front rather than only
            # after an edit attempt (Foundation 3.4).
            rx.cond(blocked_reason != "", c.inline_reason(blocked_reason), rx.fragment()),
            rx.cond(
                dirty,
                rx.vstack(
                    _field(
                        f"Reason for this {label} change (required before saving)",
                        reason,
                        on_reason_change,
                        placeholder="e.g. correcting a data-entry error on onboarding",
                    ),
                    rx.button(
                        f"Save {label} change",
                        on_click=on_save,
                        disabled=(reason == "")
                        | (blocked_reason != "")
                        | (~ClientState.can_edit_gstin_pan),
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    rx.cond(
                        reason == "",
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
    )


def _client_details_card() -> rx.Component:
    """Legal name, assigned team and the (optional) primary contact block.

    One form, one Save: the service validates the WHOLE record against the
    stored row merged with these values, so the legal name can never be
    blanked from here.
    """
    return c.card(
        rx.vstack(
            rx.text("Client details", style=t.TEXT["card_title"]),
            rx.text(
                "Each GST registration is its own client — there are no branches under a client.",
                style=t.TEXT["micro"],
            ),
            rx.grid(
                _field("Legal name *", ClientState.d_name, ClientState.set_d_name),
                _field("Assigned team", ClientState.d_team, ClientState.set_d_team),
                columns="2",
                spacing="4",
                width="100%",
            ),
            rx.grid(
                _field("Primary contact email", ClientState.d_email, ClientState.set_d_email),
                _field("Primary contact phone", ClientState.d_phone, ClientState.set_d_phone),
                columns="2",
                spacing="4",
                width="100%",
            ),
            _field(
                "Primary contact address (include the state)",
                ClientState.d_address,
                ClientState.set_d_address,
            ),
            rx.cond(
                ClientState.details_missing != "",
                c.inline_reason(ClientState.details_missing),
                rx.fragment(),
            ),
            rx.button(
                "Save details",
                on_click=ClientState.save_details,
                disabled=(~ClientState.can_manage) | (ClientState.details_missing != ""),
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _deactivate_card() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Client status", style=t.TEXT["card_title"]),
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
            spacing="2",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _details_tab() -> rx.Component:
    return rx.vstack(
        _client_details_card(),
        _identity_field(
            label="GSTIN",
            hint="27AABCM1234K1Z9",
            value=ClientState.d_gstin,
            on_change=ClientState.set_d_gstin,
            dirty=ClientState.gstin_dirty,
            reason=ClientState.d_gstin_reason,
            on_reason_change=ClientState.set_d_gstin_reason,
            on_save=ClientState.save_gstin_change,
            blocked_reason=ClientState.gstin_blocked_reason,
        ),
        _identity_field(
            label="PAN",
            hint="AABCM1234K",
            value=ClientState.d_pan,
            on_change=ClientState.set_d_pan,
            dirty=ClientState.pan_dirty,
            reason=ClientState.d_pan_reason,
            on_reason_change=ClientState.set_d_pan_reason,
            on_save=ClientState.save_pan_change,
            blocked_reason=ClientState.identity_blocked_reason,
        ),
        _identity_field(
            label="TAN",
            hint="MUMA12345B",
            value=ClientState.d_tan,
            on_change=ClientState.set_d_tan,
            dirty=ClientState.tan_dirty,
            reason=ClientState.d_tan_reason,
            on_reason_change=ClientState.set_d_tan_reason,
            on_save=ClientState.save_tan_change,
            blocked_reason=ClientState.identity_blocked_reason,
        ),
        _deactivate_card(),
        spacing="4",
        width="100%",
        align="start",
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
# Documents tab — the scoped F3 document vault, embedded for this client.
# ---------------------------------------------------------------------------


def _documents_tab() -> rx.Component:
    return embedded_vault()


# ---------------------------------------------------------------------------
# Profile screen
# ---------------------------------------------------------------------------


def _profile() -> rx.Component:
    return rx.vstack(
        _profile_actions(),
        rx.cond(ClientState.flash != "", c.info_banner(ClientState.flash), rx.fragment()),
        _profile_header(),
        _health_strip(),
        rx.hstack(
            *[_tab_button(label) for label in PROFILE_TABS],
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
            ("Details", _details_tab()),
            ("Contacts", _contacts_tab()),
            ("Chart of Accounts", _coa_tab()),
            ("Historical Snapshot", _historical_tab()),
            ("Documents", _documents_tab()),
            _details_tab(),
        ),
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
