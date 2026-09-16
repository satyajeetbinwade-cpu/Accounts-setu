"""Security events — the standalone Recent Security Events screen.

Read-only, Partner/Admin-gated. Merges C4's credential/DPDP event sink with
F1's login/permission events. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.security_state import SecurityState
from setu.views import shell


def _event_pill(variant, label) -> rx.Component:
    return rx.match(
        variant,
        ("danger", c.pill(label, variant="danger")),
        ("rule", c.pill(label, variant="rule")),
        ("accent", c.pill(label, variant="accent")),
        ("ai", c.pill(label, variant="ai")),
        c.pill(label, variant="placeholder"),
    )


def _type_chip(tc) -> rx.Component:
    active = SecurityState.filter_type == tc.event_type
    return rx.button(
        rx.hstack(
            _event_pill(tc.variant, tc.event_type),
            rx.text(
                tc.count.to_string(),
                font_size="12px",
                font_weight="700",
                color=rx.cond(active, "#FFFFFF", t.Color.TEXT_PRIMARY.value),
            ),
            spacing="2",
            align="center",
        ),
        on_click=SecurityState.set_filter_type(tc.event_type),
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        border=rx.cond(active, f"1px solid {t.Color.ACCENT.value}", f"1px solid {t.Color.BORDER.value}"),
        border_radius="10px",
        padding="6px 12px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _event_row(e) -> rx.Component:
    return rx.hstack(
        _event_pill(e.variant, e.event_type),
        rx.vstack(
            rx.hstack(
                rx.text(e.actor, style=t.TEXT["body"], font_weight="600"),
                rx.cond(
                    e.detail != "",
                    rx.text(f"— {e.detail}", style=t.TEXT["body"]),
                    rx.fragment(),
                ),
                spacing="1",
                align="baseline",
                wrap="wrap",
            ),
            rx.text(e.source_label, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.text(e.created_at, style=t.TEXT["micro"], white_space="nowrap"),
        width="100%",
        align="start",
        spacing="3",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def security_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Recent Security Events",
                "Failed logins, permission changes, credential access, DPDP decisions and "
                "filing sends — merged from the credential vault (C4) and identity layer (F1).",
            ),
            c.card(
                rx.vstack(
                    rx.hstack(
                        rx.vstack(
                            c.stat(SecurityState.total.to_string(), "event(s) shown"),
                            spacing="1",
                            align="start",
                        ),
                        rx.spacer(),
                        rx.cond(
                            SecurityState.filter_type != "All",
                            rx.button(
                                "Clear filter",
                                on_click=SecurityState.clear_filter,
                                size="1",
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_SECONDARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                            ),
                            rx.fragment(),
                        ),
                        width="100%",
                        align="start",
                    ),
                    c.divider(),
                    rx.text("By event type", style=t.TEXT["label"], font_weight="700"),
                    rx.hstack(
                        rx.foreach(SecurityState.breakdown, _type_chip),
                        spacing="2",
                        wrap="wrap",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            c.card(
                rx.vstack(
                    c.section_title(
                        "Activity log",
                        "Newest first. Read-only — this is an evidentiary record.",
                    ),
                    rx.cond(
                        SecurityState.events.length() > 0,
                        rx.vstack(
                            rx.foreach(SecurityState.events, _event_row),
                            spacing="0",
                            width="100%",
                        ),
                        c.empty_state("No security events recorded yet.", icon="shield-check"),
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )
