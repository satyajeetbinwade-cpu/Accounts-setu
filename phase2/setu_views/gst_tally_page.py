"""GST Portal & Tally Integration page — fetch, preview, confirm, push."""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from phase2.setu_state.gst_tally_state import GstTallyState


_TABS = ["GST Portal", "Tally", "Connections", "Sync History"]


def _tab_button(label: str) -> rx.Component:
    active = GstTallyState.active_tab == label
    return rx.button(
        label,
        on_click=GstTallyState.set_active_tab(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
    )


def _client_selector() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Select Client", style=t.TEXT["card_title"]),
            rx.cond(
                GstTallyState.clients.length() > 0,
                rx.grid(
                    rx.foreach(
                        GstTallyState.clients,
                        lambda cl: rx.button(
                            cl["legal_name"],
                            on_click=GstTallyState.select_client(cl["client_id"]),
                            variant="soft",
                            width="100%",
                            justify="start",
                            background=rx.cond(
                                GstTallyState.selected_client_id == cl["client_id"],
                                t.Color.ACCENT.value,
                                "transparent",
                            ),
                            color=rx.cond(
                                GstTallyState.selected_client_id == cl["client_id"],
                                "#FFFFFF",
                                t.Color.TEXT_PRIMARY.value,
                            ),
                        ),
                    ),
                    columns="3", spacing="2", width="100%",
                ),
                c.empty_state("No clients.", icon="building-2"),
            ),
            spacing="3", align="start", width="100%",
        ),
        width="100%",
    )


# ---- GST Portal tab ----

def _gst_portal_fetch() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                rx.text("Fetch from GST Portal", style=t.TEXT["card_title"]),
                rx.cond(
                    GstTallyState.client_branches.length() > 0,
                    rx.grid(
                        rx.vstack(
                            rx.text("GSTIN", style=t.TEXT["label"]),
                            rx.select(
                                [b["gstin"] for b in GstTallyState.client_branches],
                                value=GstTallyState.selected_gstin,
                                on_change=GstTallyState.select_gstin,
                                width="100%",
                            ),
                            spacing="1", align="start", width="100%",
                        ),
                        rx.vstack(
                            rx.text("Data type", style=t.TEXT["label"]),
                            rx.select(
                                ["returns", "invoices", "filing_calendar"],
                                value=GstTallyState.fetch_data_type,
                                on_change=GstTallyState.set_fetch_data_type,
                                width="100%",
                            ),
                            spacing="1", align="start", width="100%",
                        ),
                        rx.vstack(
                            rx.text("Period (MMYYYY)", style=t.TEXT["label"]),
                            rx.input(
                                value=GstTallyState.fetch_period,
                                on_change=GstTallyState.set_fetch_period,
                                placeholder="e.g. 082026",
                                width="100%",
                            ),
                            spacing="1", align="start", width="100%",
                        ),
                        columns="3", spacing="4", width="100%",
                    ),
                    c.empty_state("No GSTIN/branches for this client.", icon="map-pin"),
                ),
                rx.hstack(
                    rx.button(
                        rx.cond(GstTallyState.is_loading, rx.spinner(size="1"), rx.fragment()),
                        rx.cond(GstTallyState.is_loading, "Fetching...", "Fetch from GST Portal"),
                        on_click=GstTallyState.fetch_gst_data,
                        disabled=GstTallyState.is_loading,
                        background=t.Color.ACCENT.value, color="#FFFFFF",
                    ),
                    spacing="3",
                ),
                spacing="4", align="start", width="100%",
            ),
            width="100%",
        ),
        spacing="4", width="100%", align="start",
    )


# ---- Tally tab ----

def _tally_fetch() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                rx.text("Fetch from Tally", style=t.TEXT["card_title"]),
                rx.grid(
                    rx.vstack(
                        rx.text("Data type", style=t.TEXT["label"]),
                        rx.select(
                            ["coa", "ledgers", "vouchers"],
                            value=GstTallyState.tally_data_type,
                            on_change=GstTallyState.set_tally_data_type,
                            width="100%",
                        ),
                        spacing="1", align="start", width="100%",
                    ),
                    rx.vstack(
                        rx.text("Period (YYYYMMDD — for vouchers)", style=t.TEXT["label"]),
                        rx.input(
                            value=GstTallyState.fetch_period,
                            on_change=GstTallyState.set_fetch_period,
                            placeholder="e.g. 20260831",
                            width="100%",
                        ),
                        spacing="1", align="start", width="100%",
                    ),
                    columns="2", spacing="4", width="100%",
                ),
                rx.button(
                    rx.cond(GstTallyState.is_loading, rx.spinner(size="1"), rx.fragment()),
                    rx.cond(GstTallyState.is_loading, "Fetching...", "Fetch from Tally"),
                    on_click=GstTallyState.fetch_tally_data,
                    disabled=GstTallyState.is_loading,
                    background=t.Color.ACCENT.value, color="#FFFFFF",
                ),
                spacing="4", align="start", width="100%",
            ),
            width="100%",
        ),
        c.card(
            rx.vstack(
                rx.text("Push Reconciliation to Tally", style=t.TEXT["card_title"]),
                rx.vstack(
                    rx.text("Period", style=t.TEXT["label"]),
                    rx.input(
                        value=GstTallyState.push_period,
                        on_change=GstTallyState.set_push_period,
                        placeholder="e.g. 202608",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                rx.vstack(
                    rx.text("Adjustment Entries (JSON)", style=t.TEXT["label"]),
                    rx.text_area(
                        value=GstTallyState.push_entries,
                        on_change=GstTallyState.set_push_entries,
                        placeholder='[{"narration": "ITC adjustment Aug 2026", "date": "20260801", "entries": [{"ledger": "ITC Input", "amount": 5000, "type": "Dr"}, {"ledger": "ITC Output", "amount": 5000, "type": "Cr"}]}]',
                        rows="8",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                rx.button(
                    "Prepare Push (Preview)",
                    on_click=GstTallyState.push_to_tally,
                    background=t.Color.ACCENT.value, color="#FFFFFF",
                ),
                rx.text(
                    "Entries go through admin preview → confirm before being pushed to Tally.",
                    style=t.TEXT["micro"],
                ),
                spacing="4", align="start", width="100%",
            ),
            width="100%",
        ),
        spacing="4", width="100%", align="start",
    )


# ---- Connections tab ----

def _gst_credentials_form() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("GST Portal Credentials", style=t.TEXT["card_title"]),
            rx.cond(
                GstTallyState.gst_credentials.length() > 0,
                rx.vstack(
                    rx.foreach(
                        GstTallyState.gst_credentials,
                        lambda cred: rx.hstack(
                            rx.text(cred.get("gstin", ""), font_weight="600"),
                            rx.text(cred.get("username", ""), style=t.TEXT["label"]),
                            c.pill("Connected", variant="rule"),
                            width="100%",
                            spacing="3",
                            padding="8px 0",
                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                        ),
                    ),
                    spacing="0", width="100%",
                ),
                c.empty_state("No GST credentials configured.", icon="key-round"),
            ),
            rx.box(height="8px"),
            rx.text("Add new credentials", style=t.TEXT["section_title"]),
            rx.grid(
                rx.vstack(
                    rx.text("GSTIN", style=t.TEXT["label"]),
                    rx.input(
                        value=GstTallyState.cred_gstin,
                        on_change=GstTallyState.set_cred_gstin,
                        placeholder="GSTIN for this credential",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                rx.vstack(
                    rx.text("Username", style=t.TEXT["label"]),
                    rx.input(
                        value=GstTallyState.cred_username,
                        on_change=GstTallyState.set_cred_username,
                        placeholder="GST portal username",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                rx.vstack(
                    rx.text("Password", style=t.TEXT["label"]),
                    rx.input(
                        value=GstTallyState.cred_password,
                        on_change=GstTallyState.set_cred_password,
                        type="password",
                        placeholder="GST portal password",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                columns="3", spacing="4", width="100%",
            ),
            rx.button(
                "Save Credentials",
                on_click=GstTallyState.save_gst_credentials,
                background=t.Color.ACCENT.value, color="#FFFFFF",
            ),
            spacing="4", align="start", width="100%",
        ),
        width="100%",
    )


def _tally_connection_form() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Tally Connection", style=t.TEXT["card_title"]),
            rx.cond(
                GstTallyState.tally_connections.length() > 0,
                rx.vstack(
                    rx.foreach(
                        GstTallyState.tally_connections,
                        lambda conn: rx.hstack(
                            rx.text(f"{conn.get('host', '')}:{conn.get('port', '')}", font_weight="600"),
                            rx.text(conn.get("company_name", ""), style=t.TEXT["label"]),
                            c.pill("Active", variant="rule"),
                            width="100%",
                            spacing="3",
                            padding="8px 0",
                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                        ),
                    ),
                    spacing="0", width="100%",
                ),
                c.empty_state("No Tally connections.", icon="server"),
            ),
            rx.box(height="8px"),
            rx.text("Configure connection", style=t.TEXT["section_title"]),
            rx.grid(
                rx.vstack(
                    rx.text("Host", style=t.TEXT["label"]),
                    rx.input(
                        value=GstTallyState.tally_host,
                        on_change=GstTallyState.set_tally_host,
                        placeholder="localhost",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                rx.vstack(
                    rx.text("Port", style=t.TEXT["label"]),
                    rx.input(
                        value=GstTallyState.tally_port,
                        on_change=GstTallyState.set_tally_port,
                        placeholder="9000",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                rx.vstack(
                    rx.text("Company name", style=t.TEXT["label"]),
                    rx.input(
                        value=GstTallyState.tally_company,
                        on_change=GstTallyState.set_tally_company,
                        placeholder="(optional) Company name in Tally",
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
                ),
                columns="3", spacing="4", width="100%",
            ),
            rx.hstack(
                rx.button(
                    "Save Connection",
                    on_click=GstTallyState.save_tally_connection,
                    background=t.Color.ACCENT.value, color="#FFFFFF",
                ),
                rx.button(
                    "Test Connection",
                    on_click=GstTallyState.test_tally_connection,
                    variant="soft",
                    color_scheme="gray",
                ),
                spacing="3",
            ),
            spacing="4", align="start", width="100%",
        ),
        width="100%",
    )


def _connections_tab() -> rx.Component:
    return rx.vstack(
        _gst_credentials_form(),
        _tally_connection_form(),
        spacing="4", width="100%", align="start",
    )


# ---- Sync history tab ----

def _sync_history() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text("Sync Operations", style=t.TEXT["card_title"]),
                rx.spacer(),
                rx.button(
                    "Refresh",
                    on_click=GstTallyState.load_sync_operations,
                    size="1", variant="soft", color_scheme="gray",
                ),
                width="100%", align="center",
            ),
            rx.hstack(
                rx.text("Pending approvals:", style=t.TEXT["label"]),
                rx.cond(
                    GstTallyState.pending_ops.length() > 0,
                    rx.text(GstTallyState.pending_ops.length().to_string(), font_weight="700", color=t.Color.AI_ON.value),
                    rx.text("0", font_weight="600"),
                ),
                spacing="2", align="center",
            ),
            rx.box(height="8px"),
            rx.cond(
                GstTallyState.pending_ops.length() > 0,
                rx.vstack(
                    rx.text("Operations awaiting your confirmation:", style=t.TEXT["section_title"]),
                    rx.foreach(
                        GstTallyState.pending_ops,
                        lambda op: c.card(
                            rx.hstack(
                                rx.vstack(
                                    rx.text(
                                        f"{op.get('source', '').upper()} — {op.get('data_type', '')} ({op.get('direction', '')})",
                                        font_weight="600",
                                    ),
                                    rx.text(
                                        f"Client #{op.get('client_id', '')} · Period: {op.get('period', 'N/A')}",
                                        style=t.TEXT["micro"],
                                    ),
                                    flex="1",
                                    spacing="1",
                                ),
                                rx.button(
                                    "Preview",
                                    on_click=GstTallyState.preview_op(op["op_id"]),
                                    size="1", variant="soft", color_scheme="gray",
                                ),
                                spacing="3", width="100%", align="center",
                            ),
                            width="100%", padding="12px",
                        ),
                    ),
                    spacing="2", width="100%",
                ),
                rx.fragment(),
            ),
            rx.cond(
                GstTallyState.selected_op is not None,
                c.card(
                    rx.vstack(
                        rx.text("Operation Preview", style=t.TEXT["card_title"]),
                        rx.text(f"ID: #{GstTallyState.selected_op['op_id']}"),
                        rx.text(f"Source: {GstTallyState.selected_op['source']}"),
                        rx.text(f"Direction: {GstTallyState.selected_op['direction']}"),
                        rx.text(f"Data type: {GstTallyState.selected_op['data_type']}"),
                        rx.box(
                            rx.text("Details:"),
                            rx.text(GstTallyState.preview_summary, style=t.TEXT["micro"], font_family="monospace"),
                            width="100%",
                        ),
                        rx.hstack(
                            rx.button(
                                "✅ Confirm & Execute",
                                on_click=GstTallyState.confirm_op(GstTallyState.selected_op["op_id"]),
                                background=t.Color.RULE.value, color="#FFFFFF",
                            ),
                            rx.button(
                                "❌ Cancel",
                                on_click=GstTallyState.cancel_op(GstTallyState.selected_op["op_id"]),
                                variant="soft", color_scheme="red",
                            ),
                            spacing="3",
                        ),
                        spacing="3", align="start", width="100%",
                    ),
                    background="#F8FFEF",
                    border=f"1px solid {t.Color.RULE.value}",
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.cond(
                GstTallyState.sync_operations.length() > 0,
                rx.vstack(
                    rx.text("All operations (last 50):", style=t.TEXT["section_title"]),
                    rx.foreach(
                        GstTallyState.sync_operations,
                        lambda op: rx.hstack(
                            rx.text(f"#{op['op_id']}", style=t.TEXT["micro"], width="40px"),
                            rx.text(f"{op['source']}/{op['data_type']}", width="140px"),
                            rx.text(op["direction"], width="60px"),
                            c.pill(op["status"], variant=rx.match(
                                op["status"],
                                ("completed", "rule"),
                                ("confirmed", "rule"),
                                ("failed", "danger"),
                                ("previewing", "ai"),
                                "placeholder",
                            )),
                            rx.text(op.get("created_at", ""), style=t.TEXT["micro"]),
                            width="100%", align="center",
                            padding="6px 0",
                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                            spacing="2",
                        ),
                    ),
                    spacing="0", width="100%",
                ),
                c.empty_state("No sync operations yet.", icon="clock"),
            ),
            spacing="4", align="start", width="100%",
        ),
        width="100%",
    )


# ---- Main page ----

def gst_tally_page() -> rx.Component:
    return rx.vstack(
        c.page_header(
            "GST Portal & Tally Integration",
            "Fetch data from GST Portal and Tally. Push reconciliation entries after admin confirmation.",
        ),
        rx.cond(GstTallyState.flash != "", c.info_banner(GstTallyState.flash), rx.fragment()),
        _client_selector(),
        rx.cond(
            GstTallyState.selected_client_id != 0,
            rx.vstack(
                rx.hstack(
                    *[_tab_button(label) for label in _TABS],
                    spacing="2", wrap="wrap",
                    padding="4px",
                    background=t.Color.SURFACE.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="12px",
                    width="100%",
                ),
                rx.match(
                    GstTallyState.active_tab,
                    ("GST Portal", rx.vstack(_gst_portal_fetch(), _sync_history(), spacing="4", width="100%")),
                    ("Tally", _tally_fetch()),
                    ("Connections", _connections_tab()),
                    ("Sync History", _sync_history()),
                    _gst_portal_fetch(),
                ),
                spacing="4", width="100%", align="start",
            ),
            c.empty_state("Select a client above to configure integrations.", icon="plug"),
        ),
        spacing="5", width="100%", align="start",
        on_mount=GstTallyState.load_clients,
    )
