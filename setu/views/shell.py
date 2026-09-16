"""The app shell: fixed sidebar navigation + content area.

Reproduces the Streamlit app's sidebar (identity block, current context,
grouped nav) and the three-area information architecture. The content area is
filled by whichever page is routed.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state import AuthState, NavState
from setu.state.auth_state import NavArea, NavScreen

SIDEBAR_WIDTH = "248px"


def _nav_link(screen: NavScreen) -> rx.Component:
    """One sidebar link. The active route gets the accent bar + tint —
    never bold text alone (Foundation 2.3)."""
    active = rx.State.router.page.path == screen.route
    return rx.link(
        rx.hstack(
            rx.icon(
                screen.icon,
                size=16,
                color=rx.cond(active, t.Color.ACCENT.value, t.Color.TEXT_SECONDARY.value),
            ),
            rx.text(
                screen.label,
                font_size="13px",
                font_weight=rx.cond(active, "700", "500"),
                color=rx.cond(active, t.Color.ACCENT.value, t.Color.TEXT_PRIMARY.value),
            ),
            spacing="2",
            align="center",
            width="100%",
        ),
        href=screen.route,
        width="100%",
        padding="7px 12px",
        border_radius="8px",
        text_decoration="none",
        background=rx.cond(active, "#EAF0FB", "transparent"),
        border_left=rx.cond(
            active,
            f"3px solid {t.Color.ACCENT.value}",
            "3px solid transparent",
        ),
        _hover={"background": "#F1F4FA"},
        transition="background 120ms ease",
    )


def _area_block(area: NavArea) -> rx.Component:
    """One collapsible nav area: header row + its visible screens."""
    is_open = NavState.active_area == area.area
    return rx.vstack(
        rx.hstack(
            rx.text(
                area.area,
                font_size="11px",
                font_weight="700",
                color=t.Color.TEXT_SECONDARY.value,
                letter_spacing="0.06em",
                text_transform="uppercase",
            ),
            rx.spacer(),
            rx.icon(
                "chevron-down",
                size=14,
                color=t.Color.TEXT_MUTED.value,
                transform=rx.cond(is_open, "rotate(0deg)", "rotate(-90deg)"),
                transition="transform 150ms ease",
            ),
            width="100%",
            align="center",
            padding="6px 4px",
            cursor="pointer",
            on_click=NavState.set_area(area.area),
        ),
        rx.cond(
            is_open,
            rx.vstack(
                rx.foreach(area.screens, _nav_link),
                spacing="1",
                width="100%",
            ),
            rx.fragment(),
        ),
        spacing="1",
        width="100%",
    )


def _brand() -> rx.Component:
    return rx.hstack(
        rx.box(
            rx.text(
                "S",
                color="#FFFFFF",
                font_weight="700",
                font_size="16px",
            ),
            width="30px",
            height="30px",
            border_radius="9px",
            background=t.Color.ACCENT.value,
            display="flex",
            align_items="center",
            justify_content="center",
            box_shadow="0 2px 6px rgba(59,111,219,.35)",
            flex_shrink="0",
        ),
        rx.vstack(
            rx.text(
                "Setu",
                font_size="15px",
                font_weight="700",
                color=t.Color.TEXT_PRIMARY.value,
                line_height="1.1",
                white_space="nowrap",
            ),
            rx.text(
                "Reconciliation",
                font_size="11px",
                color=t.Color.TEXT_MUTED.value,
                line_height="1.2",
                white_space="nowrap",
            ),
            spacing="0",
            align="start",
            min_width="0",
        ),
        spacing="2",
        align="center",
        width="100%",
    )


def _identity_footer() -> rx.Component:
    """Signed-in identity + sign out, pinned to the sidebar bottom."""
    return rx.vstack(
        c.divider(),
        rx.hstack(
            rx.box(
                rx.text(
                    AuthState.initials,
                    color="#FFFFFF",
                    font_size="11px",
                    font_weight="700",
                ),
                width="28px",
                height="28px",
                border_radius="999px",
                background=t.Color.TEXT_PRIMARY.value,
                display="flex",
                align_items="center",
                justify_content="center",
                flex_shrink="0",
            ),
            rx.vstack(
                rx.text(
                    AuthState.display_name,
                    font_size="12px",
                    font_weight="600",
                    color=t.Color.TEXT_PRIMARY.value,
                    line_height="1.2",
                ),
                rx.text(
                    AuthState.role_name,
                    font_size="11px",
                    color=t.Color.TEXT_SECONDARY.value,
                    line_height="1.2",
                ),
                spacing="0",
                align="start",
                flex="1",
                overflow="hidden",
            ),
            rx.tooltip(
                rx.icon_button(
                    rx.icon("log-out", size=15),
                    variant="ghost",
                    color_scheme="gray",
                    size="1",
                    on_click=AuthState.logout,
                ),
                content="Sign out",
            ),
            width="100%",
            align="center",
            spacing="2",
        ),
        spacing="3",
        width="100%",
        padding_top="10px",
    )


def _sidebar() -> rx.Component:
    return rx.vstack(
        _brand(),
        rx.box(height="1px", width="100%", background=t.Color.BORDER.value, margin="6px 0"),
        rx.vstack(
            rx.foreach(AuthState.nav_areas, _area_block),
            spacing="4",
            width="100%",
        ),
        rx.spacer(),
        _identity_footer(),
        spacing="3",
        width=SIDEBAR_WIDTH,
        min_width=SIDEBAR_WIDTH,
        height="100vh",
        position="sticky",
        top="0",
        padding="20px 14px",
        background=t.Color.SURFACE.value,
        border_right=f"1px solid {t.Color.BORDER.value}",
        overflow_y="auto",
        align="start",
    )


def _session_warning() -> rx.Component:
    """The inactivity warning, shown as a floating bar when the session is
    within the warn window. Explicit, with a "stay signed in" action."""
    return rx.cond(
        AuthState.show_session_warning,
        rx.box(
            rx.hstack(
                rx.icon("clock", size=16, color=t.Color.AI_ON.value),
                rx.text(
                    "Your session will expire soon due to inactivity. Save any work you need to keep.",
                    font_size="13px",
                    color=t.Color.AI_ON.value,
                ),
                rx.spacer(),
                rx.button(
                    "Stay signed in",
                    size="1",
                    variant="soft",
                    color_scheme="amber",
                    on_click=AuthState.extend_session,
                ),
                width="100%",
                align="center",
                spacing="2",
            ),
            width="100%",
            background="#FDF4E3",
            border="1px solid #F0D9AE",
            border_radius="10px",
            padding="10px 14px",
            margin_bottom="16px",
        ),
        rx.fragment(),
    )


def shell(content: rx.Component) -> rx.Component:
    """Wrap a page's content in the app shell."""
    return rx.hstack(
        _sidebar(),
        rx.box(
            rx.box(
                _session_warning(),
                content,
                max_width="1200px",
                width="100%",
                margin="0 auto",
                padding="32px 32px 64px 32px",
            ),
            flex="1",
            min_width="0",
            background=t.Color.PAGE_BACKGROUND.value,
        ),
        spacing="0",
        align="start",
        width="100%",
        min_height="100vh",
    )