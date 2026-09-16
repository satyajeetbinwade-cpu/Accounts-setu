"""F5 — Data Integrity & Validation Layer.

Data Integrity Queue (headline → grouped → detail), Sync Health,
Reconciliation-of-the-Reconciliation. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.f5_state import F5State
from setu.views import shell

_HUB = ["Data Integrity Queue", "Sync Health", "Reconciliation-of-the-Reconciliation"]


def _hub_button(label: str) -> rx.Component:
    active = F5State.section == label
    return rx.button(
        label,
        on_click=F5State.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _block_row(b) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(f"{b.issue_label} — {b.source_type}/{b.source_file}", style=t.TEXT["body"], font_weight="600"),
            rx.text(b.description, style=t.TEXT["micro"]),
            rx.text(f"Client: {b.client_label}", style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.button(
            "Open",
            on_click=F5State.open_block(b.block_id),
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_PRIMARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="8px",
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _severity_group(label: str, rows, variant: str) -> rx.Component:
    return rx.cond(
        rows.length() > 0,
        rx.vstack(
            c.pill(f"{label} — {rows.length()}", variant=variant),
            rx.vstack(rx.foreach(rows, _block_row), spacing="0", width="100%"),
            spacing="2",
            align="start",
            width="100%",
        ),
        rx.fragment(),
    )


def _queue_hub() -> rx.Component:
    return rx.cond(
        F5State.selected_block_id != 0,
        _block_detail(),
        rx.vstack(
            c.card(
                rx.vstack(
                    c.stat(F5State.open_count.to_string(), "open validation block(s)"),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.cond(
                F5State.open_count > 0,
                rx.vstack(
                    _severity_group("GSTIN/PAN-level", F5State.gstin_pan_blocks, "danger"),
                    _severity_group("Minor", F5State.minor_blocks, "ai"),
                    _severity_group("Cosmetic", F5State.cosmetic_blocks, "placeholder"),
                    spacing="4",
                    width="100%",
                    align="start",
                ),
                c.empty_state(
                    "No open validation blocks — every canonical file that's been checked structurally passed.",
                    icon="shield-check",
                ),
            ),
            spacing="4",
            width="100%",
            align="start",
        ),
    )


def _block_detail() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back to Data Integrity Queue",
            variant="ghost",
            color_scheme="gray",
            size="1",
            on_click=F5State.back_to_queue,
        ),
        c.card(
            rx.vstack(
                rx.text(F5State.detail_issue_label, style=t.TEXT["card_title"]),
                rx.text(F5State.detail_context, style=t.TEXT["micro"]),
                rx.text(f"Client: {F5State.detail_client}", style=t.TEXT["micro"]),
                rx.text(F5State.detail_description, style=t.TEXT["body"]),
                rx.cond(
                    F5State.detail_severity == "gstin_pan",
                    c.pill("Severity: GSTIN/PAN-level", variant="danger"),
                    rx.cond(
                        F5State.detail_severity == "cosmetic",
                        c.pill("Severity: Cosmetic", variant="placeholder"),
                        c.pill("Severity: Minor", variant="ai"),
                    ),
                ),
                rx.cond(
                    F5State.detail_status == "overridden",
                    rx.text(
                        f"Overridden by {F5State.detail_resolved_by} on {F5State.detail_resolved_at} — reason: \"{F5State.detail_override_reason}\"",
                        style=t.TEXT["label"],
                    ),
                    rx.fragment(),
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            F5State.detail_status == "overridden",
            rx.fragment(),
            c.card(
                rx.vstack(
                    rx.text("Override this block", style=t.TEXT["card_title"]),
                    rx.cond(
                        F5State.override_allowed,
                        rx.vstack(
                            rx.text("Reason for override (required before saving)", style=t.TEXT["label"]),
                            rx.input(
                                value=F5State.override_reason,
                                on_change=F5State.set_override_reason,
                                placeholder="e.g. verified against the source portal directly — GSTIN is correct as filed",
                                width="100%",
                            ),
                            rx.button(
                                "Override",
                                on_click=F5State.override_block,
                                disabled=F5State.override_reason == "",
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                            ),
                            rx.cond(
                                F5State.override_reason == "",
                                c.inline_reason("A reason is required before this override can be saved."),
                                rx.fragment(),
                            ),
                            spacing="2",
                            align="start",
                            width="100%",
                        ),
                        rx.vstack(
                            c.deactivate_button("Override", disabled=True),
                            c.inline_reason(
                                rx.cond(
                                    F5State.override_deny_reason != "",
                                    F5State.override_deny_reason,
                                    "You don't have permission to override this block.",
                                )
                            ),
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
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Sync Health
# ---------------------------------------------------------------------------


def _sync_row(r) -> rx.Component:
    return rx.hstack(
        rx.text(r.source_label, style=t.TEXT["body"], flex="2"),
        rx.box(c.live_status_chip(r.status, label=r.label), flex="2"),
        rx.box(
            rx.cond(
                r.is_stub,
                c.placeholder_badge("Module C2"),
                rx.text(rx.cond(r.checked_at != "", r.checked_at, "Never checked"), style=t.TEXT["micro"]),
            ),
            flex="3",
        ),
        width="100%",
        align="center",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _escalation_row(e) -> rx.Component:
    return rx.vstack(
        rx.text(e.message, style=t.TEXT["body"]),
        rx.text(f"{e.created_at} · {e.consecutive_fails} consecutive failure(s)", style=t.TEXT["micro"]),
        spacing="1",
        align="start",
        width="100%",
    )


def _sync_health() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text("Client:", style=t.TEXT["micro"]),
            rx.select(
                F5State.client_options.map(lambda o: o.legal_name),
                on_change=lambda v: F5State.set_sync_client(
                    F5State.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                ),
                placeholder="Select a client",
                width="280px",
            ),
            spacing="2",
            align="center",
        ),
        c.card(
            rx.vstack(
                rx.text("Sync health — per source", style=t.TEXT["card_title"]),
                rx.cond(
                    F5State.sync_rows.length() > 0,
                    rx.vstack(rx.foreach(F5State.sync_rows, _sync_row), spacing="0", width="100%"),
                    c.empty_state("No sync sources configured.", icon="plug"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            F5State.escalations.length() > 0,
            c.card(
                rx.vstack(
                    rx.text("Escalations", style=t.TEXT["card_title"]),
                    c.warning_banner("Persistent sync failures are escalated directly to a Partner."),
                    rx.vstack(rx.foreach(F5State.escalations, _escalation_row), spacing="3", width="100%"),
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
    )


# ---------------------------------------------------------------------------
# Reconciliation-of-the-reconciliation
# ---------------------------------------------------------------------------


def _ror_row(r) -> rx.Component:
    return rx.hstack(
        rx.text(f"Run {r.run_id}", style=t.TEXT["body"], flex="1", font_weight="600"),
        rx.text(r.matched_sum, style=t.TEXT["body"], flex="1"),
        rx.text(r.unmatched_sum, style=t.TEXT["body"], flex="1"),
        rx.text(r.excluded_sum, style=t.TEXT["body"], flex="1"),
        rx.text(r.ingested_total, style=t.TEXT["body"], flex="1"),
        rx.box(
            rx.cond(r.ties_out, c.pill("Ties out", variant="rule"), c.pill("Doesn't tie out", variant="danger")),
            flex="1",
        ),
        width="100%",
        align="center",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _recon_of_recon() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text("Client:", style=t.TEXT["micro"]),
            rx.select(
                F5State.client_options.map(lambda o: o.legal_name),
                on_change=lambda v: F5State.set_ror_client(
                    F5State.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                ),
                placeholder="Select a client",
                width="280px",
            ),
            spacing="2",
            align="center",
        ),
        c.card(
            rx.vstack(
                rx.text("Reconciliation-of-the-reconciliation", style=t.TEXT["card_title"]),
                rx.cond(
                    F5State.ror_rows.length() > 0,
                    rx.vstack(
                        rx.hstack(
                            rx.text("Run", style=t.TEXT["label"], flex="1"),
                            rx.text("Matched", style=t.TEXT["label"], flex="1"),
                            rx.text("Unmatched", style=t.TEXT["label"], flex="1"),
                            rx.text("Excluded", style=t.TEXT["label"], flex="1"),
                            rx.text("Ingested", style=t.TEXT["label"], flex="1"),
                            rx.text("Ties out", style=t.TEXT["label"], flex="1"),
                            width="100%",
                            padding="0 0 6px 0",
                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                        ),
                        rx.foreach(F5State.ror_rows, _ror_row),
                        spacing="0",
                        width="100%",
                    ),
                    rx.vstack(
                        rx.text("No matching runs yet — will populate once Module 2 is live.", style=t.TEXT["label"]),
                        c.placeholder_badge("Module 2"),
                        spacing="2",
                        align="start",
                    ),
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


def f5_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Data Integrity & Validation Layer",
                "The trust backbone — structural validity, completeness, and sync health checks.",
            ),
            rx.cond(F5State.flash != "", c.info_banner(F5State.flash), rx.fragment()),
            rx.cond(F5State.error != "", c.inline_reason(F5State.error), rx.fragment()),
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
            rx.cond(
                F5State.selected_block_id != 0,
                _block_detail(),
                rx.match(
                    F5State.section,
                    ("Sync Health", _sync_health()),
                    ("Reconciliation-of-the-Reconciliation", _recon_of_recon()),
                    _queue_hub(),
                ),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )