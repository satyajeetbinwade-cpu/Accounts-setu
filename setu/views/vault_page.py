"""C4 — Security & Credential Vault.

Security Overview (headline → grouped → detail), Credential Vault, DPDP
Deletion Requests. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.vault_state import VaultState
from setu.views import shell

_HUB = ["Security Overview", "Credential Vault", "DPDP Deletion Requests"]


def _hub_button(label: str) -> rx.Component:
    active = VaultState.section == label
    return rx.button(
        label,
        on_click=VaultState.set_section(label),
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
# Security Overview
# ---------------------------------------------------------------------------


def _event_type_button(item) -> rx.Component:
    return rx.button(
        rx.vstack(
            rx.text(item.event_type, font_size="12px", font_weight="600"),
            rx.text(item.count.to_string(), font_size="18px", font_weight="700", color=t.Color.ACCENT.value),
            spacing="0",
            align="center",
        ),
        on_click=VaultState.filter_events(item.event_type),
        variant="soft",
        background="transparent",
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="10px",
        padding="8px 14px",
        _hover={"border_color": t.Color.ACCENT.value, "background": "#F1F4FA"},
    )


def _security_overview() -> rx.Component:
    return rx.vstack(
        # Headline
        c.card(
            rx.vstack(
                rx.text("Needs attention", style=t.TEXT["card_title"]),
                rx.hstack(
                    c.stat(VaultState.open_dpdp_count.to_string(), "Open DPDP requests"),
                    c.stat(VaultState.event_volume_30d.to_string(), "Security events (30d)"),
                    spacing="8",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # Grouped — events by type
        c.card(
            rx.vstack(
                rx.text("Events by type — last 30 days", style=t.TEXT["card_title"]),
                rx.cond(
                    VaultState.breakdown.length() > 0,
                    rx.hstack(
                        rx.foreach(VaultState.breakdown, _event_type_button),
                        spacing="3",
                        wrap="wrap",
                    ),
                    rx.text("No events recorded in the last 30 days.", style=t.TEXT["label"]),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # Backup / DR — structural placeholder
        c.card(
            rx.vstack(
                rx.text("Backup / disaster recovery", style=t.TEXT["card_title"]),
                c.placeholder_pill(VaultState.backup_label),
                rx.text(f"RTO target: {VaultState.backup_rto}", style=t.TEXT["micro"]),
                rx.text(f"RPO target: {VaultState.backup_rpo}", style=t.TEXT["micro"]),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # Open DPDP awaiting approval
        rx.cond(
            VaultState.dpdp_pending.length() > 0,
            c.card(
                rx.vstack(
                    rx.text("Open DPDP requests awaiting approval", style=t.TEXT["card_title"]),
                    rx.foreach(
                        VaultState.dpdp_pending,
                        lambda req: rx.vstack(
                            rx.text(f"#{req.request_id} — {req.client_label} — {req.scope}", style=t.TEXT["body"]),
                            rx.text(f"Submitted by {req.submitted_by} on {req.submitted_at}", style=t.TEXT["micro"]),
                            spacing="1",
                            align="start",
                        ),
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        # Detail — full activity log
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.text(
                        rx.cond(
                            VaultState.detail_filter != "",
                            f"Recent security events — full log (filtered: {VaultState.detail_filter})",
                            "Recent security events — full log",
                        ),
                        style=t.TEXT["card_title"],
                    ),
                    rx.spacer(),
                    rx.cond(
                        VaultState.detail_filter != "",
                        rx.button("Clear filter", size="1", variant="soft", on_click=VaultState.clear_filter),
                        rx.fragment(),
                    ),
                    width="100%",
                    align="center",
                ),
                c.activity_panel(
                    VaultState.events,
                    empty_message="No security events recorded yet.",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Credential Vault
# ---------------------------------------------------------------------------


def _credential_row(cred) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(
                rx.cond(cred.label != "", f"{cred.service} ({cred.label})", cred.service),
                font_size="13px",
                font_weight="600",
            ),
            rx.text(cred.masked_ref, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="3",
        ),
        rx.box(c.live_status_chip(cred.status), flex="2"),
        rx.cond(
            VaultState.can_manage,
            rx.popover.root(
                rx.popover.trigger(
                    rx.button(
                        "Rotate",
                        size="1",
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_PRIMARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="8px",
                    ),
                ),
                rx.popover.content(
                    rx.vstack(
                        rx.text(f"Rotating {cred.service}", style=t.TEXT["label"]),
                        rx.text("The old secret is never shown here.", style=t.TEXT["micro"]),
                        rx.input(
                            value=VaultState.rotate_secret[cred.credential_id.to_string()],
                            on_change=lambda v: VaultState.set_rotate_secret(cred.credential_id, v),
                            type="password",
                            placeholder="New secret / API key",
                            width="100%",
                        ),
                        rx.button(
                            "Save new secret",
                            on_click=VaultState.rotate_credential(cred.credential_id),
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        spacing="2",
                        align="start",
                        width="240px",
                    ),
                    side="bottom",
                ),
            ),
            rx.fragment(),
        ),
        rx.cond(
            VaultState.can_manage,
            c.deactivate_button(
                "Deactivate",
                on_click=VaultState.deactivate_credential(cred.credential_id),
                size="1",
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _credential_vault() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                rx.text("Encrypted credentials", style=t.TEXT["card_title"]),
                rx.text(
                    "Real encryption-at-rest. Admin manages this vault but never sees raw secret values — only the masked reference below is ever displayed.",
                    style=t.TEXT["label"],
                ),
                rx.cond(
                    VaultState.credentials.length() > 0,
                    rx.vstack(rx.foreach(VaultState.credentials, _credential_row), spacing="0", width="100%"),
                    c.empty_state("No credentials stored yet.", icon="key-round"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            VaultState.can_manage,
            c.card(
                rx.vstack(
                    rx.text("+ Add credential", style=t.TEXT["card_title"]),
                    rx.cond(VaultState.add_error != "", c.inline_reason(VaultState.add_error), rx.fragment()),
                    rx.grid(
                        _field(
                            "Service (e.g. Tally, GST Portal, TRACES, OpenRouter)",
                            VaultState.add_service,
                            VaultState.set_add_service,
                        ),
                        _field("Label (optional)", VaultState.add_label, VaultState.set_add_label),
                        columns="2",
                        spacing="4",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.switch(
                            checked=VaultState.add_scoped,
                            on_change=VaultState.set_add_scoped,
                            color_scheme="indigo",
                        ),
                        rx.text("Scope to a specific client", style=t.TEXT["label"]),
                        spacing="2",
                        align="center",
                    ),
                    rx.cond(
                        VaultState.add_scoped,
                        rx.select(
                            VaultState.client_options.map(lambda o: o.legal_name),
                            on_change=lambda v: VaultState.set_add_client_id(
                                VaultState.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                            ),
                            placeholder="Select a client",
                            width="280px",
                        ),
                        rx.fragment(),
                    ),
                    _field("Secret / API key", VaultState.add_secret, VaultState.set_add_secret, type_="password"),
                    rx.button(
                        "Add credential",
                        on_click=VaultState.add_credential,
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
        c.card(
            rx.vstack(
                rx.text("Rotation history", style=t.TEXT["card_title"]),
                rx.text("Timestamp and who rotated only — no value column, ever.", style=t.TEXT["label"]),
                c.activity_panel(
                    VaultState.rotation_history,
                    empty_message="No rotations recorded yet.",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# DPDP Deletion Requests
# ---------------------------------------------------------------------------


def _dpdp_pending_card(req) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text(f"#{req.request_id} — {req.client_label}", style=t.TEXT["card_title"]),
            rx.text(f"Legal basis: {req.legal_basis}", style=t.TEXT["label"]),
            rx.text(f"Submitted by {req.submitted_by} on {req.submitted_at}", style=t.TEXT["micro"]),
            rx.grid(
                rx.vstack(
                    rx.text("Deletable", style=t.TEXT["card_title"]),
                    rx.cond(
                        req.deletable_items.length() > 0,
                        rx.vstack(
                            rx.foreach(req.deletable_items, lambda i: rx.text(f"• {i}", style=t.TEXT["body"])),
                            spacing="1",
                            align="start",
                        ),
                        rx.text("None listed.", style=t.TEXT["micro"]),
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Statutorily exempt", style=t.TEXT["label"]),
                    rx.cond(
                        req.exempt_items.length() > 0,
                        rx.vstack(
                            rx.foreach(
                                req.exempt_items,
                                lambda i: rx.text(f"• {i}", style=t.TEXT["micro"]),
                            ),
                            spacing="1",
                            align="start",
                        ),
                        rx.text("None listed.", style=t.TEXT["micro"]),
                    ),
                    spacing="1",
                    align="start",
                ),
                columns="2",
                spacing="4",
                width="100%",
            ),
            rx.cond(
                VaultState.can_approve,
                rx.vstack(
                    rx.text("Reason (required for either Approve or Reject) *", style=t.TEXT["label"]),
                    rx.input(
                        value=VaultState.dpdp_reason[req.request_id.to_string()],
                        on_change=lambda v: VaultState.set_dpdp_reason(req.request_id, v),
                        width="100%",
                    ),
                    rx.cond(
                        VaultState.dpdp_reason[req.request_id.to_string()] == "",
                        c.inline_reason("A reason is required before this request can be decided."),
                        rx.fragment(),
                    ),
                    rx.hstack(
                        rx.button(
                            "Approve",
                            on_click=VaultState.decide_dpdp(req.request_id, True),
                            disabled=VaultState.dpdp_reason[req.request_id.to_string()] == "",
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.button(
                            "Reject",
                            on_click=VaultState.decide_dpdp(req.request_id, False),
                            disabled=VaultState.dpdp_reason[req.request_id.to_string()] == "",
                            variant="soft",
                            background="transparent",
                            color=t.Color.DANGER.value,
                            border=f"1px solid {t.Color.DANGER.value}",
                            border_radius="9px",
                        ),
                        spacing="2",
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                c.inline_reason("Requires Partner approval — vault.dpdp.approve."),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _dpdp_decided_row(req) -> rx.Component:
    return rx.hstack(
        rx.text(f"#{req.request_id} — {req.client_label}", style=t.TEXT["body"], flex="3"),
        rx.box(
            rx.cond(
                req.status == "approved",
                c.pill("Approved", variant="rule"),
                c.pill("Rejected", variant="danger"),
            ),
            flex="2",
        ),
        rx.text(
            f"Decided by {req.decided_by} on {req.decided_at} — {req.decision_reason}",
            style=t.TEXT["micro"],
            flex="5",
        ),
        width="100%",
        align="center",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _dpdp_workflow() -> rx.Component:
    return rx.vstack(
        rx.cond(
            VaultState.can_submit,
            c.card(
                rx.vstack(
                    rx.text("Submit a DPDP deletion request", style=t.TEXT["card_title"]),
                    rx.select(
                        VaultState.client_options.map(lambda o: o.legal_name),
                        on_change=lambda v: VaultState.set_dpdp_client_id(
                            VaultState.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                        ),
                        placeholder="Select a client",
                        width="320px",
                    ),
                    rx.text("Scope of data requested for deletion *", style=t.TEXT["label"]),
                    rx.text_area(
                        value=VaultState.dpdp_scope,
                        on_change=VaultState.set_dpdp_scope,
                        rows="2",
                        width="100%",
                    ),
                    _field("Legal basis *", VaultState.dpdp_basis, VaultState.set_dpdp_basis),
                    rx.grid(
                        rx.vstack(
                            rx.text("Deletable items (one per line)", style=t.TEXT["label"]),
                            rx.text_area(
                                value=VaultState.dpdp_deletable,
                                on_change=VaultState.set_dpdp_deletable,
                                rows="3",
                                width="100%",
                            ),
                            spacing="1",
                            align="start",
                        ),
                        rx.vstack(
                            rx.text("Statutorily-exempt items (one per line)", style=t.TEXT["label"]),
                            rx.text_area(
                                value=VaultState.dpdp_exempt,
                                on_change=VaultState.set_dpdp_exempt,
                                rows="3",
                                width="100%",
                            ),
                            spacing="1",
                            align="start",
                        ),
                        columns="2",
                        spacing="4",
                        width="100%",
                    ),
                    rx.button(
                        "Submit request",
                        on_click=VaultState.submit_dpdp,
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
        c.card(
            rx.vstack(
                rx.text("Pending requests", style=t.TEXT["card_title"]),
                rx.cond(
                    VaultState.dpdp_pending.length() > 0,
                    rx.vstack(rx.foreach(VaultState.dpdp_pending, _dpdp_pending_card), spacing="3", width="100%"),
                    c.empty_state("No pending DPDP deletion requests.", icon="file-check"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        c.card(
            rx.vstack(
                rx.text("Decided requests", style=t.TEXT["card_title"]),
                rx.cond(
                    VaultState.dpdp_decided.length() > 0,
                    rx.vstack(rx.foreach(VaultState.dpdp_decided, _dpdp_decided_row), spacing="0", width="100%"),
                    rx.text("No decided requests yet.", style=t.TEXT["micro"]),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def vault_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Security & Credential Vault",
                "Encrypted secrets vault, End-Client data isolation, security-event logging, and DPDP Act compliance controls.",
            ),
            rx.cond(VaultState.flash != "", c.info_banner(VaultState.flash), rx.fragment()),
            rx.cond(VaultState.error != "", c.inline_reason(VaultState.error), rx.fragment()),
            rx.hstack(
                *[_hub_button(label) for label in _HUB],
                spacing="2",
                wrap="wrap",
                padding="4px",
                background=t.Color.SURFACE.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
            ),
            rx.match(
                VaultState.section,
                ("Security Overview", _security_overview()),
                ("Credential Vault", _credential_vault()),
                ("DPDP Deletion Requests", _dpdp_workflow()),
                _security_overview(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )