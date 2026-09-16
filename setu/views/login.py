"""Login page — the F1 sign-in gate.

A full-page, centered sign-in card. Faithful to ``render_login_gate``: generic
failure message, an explicit "multiple failed attempts logged" notice from
attempt 4, the first-run admin hint, and the inactivity sign-out message.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import tokens as t
from setu.state import AuthState


def _field(label: str, name: str, *, type_: str = "text", placeholder: str = "") -> rx.Component:
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.input(
            name=name,
            type=type_,
            placeholder=placeholder,
            width="100%",
            size="3",
        ),
        spacing="1",
        align="start",
        width="100%",
    )


def _login_card() -> rx.Component:
    return rx.vstack(
        # Brand
        rx.vstack(
            rx.box(
                rx.text("S", color="#FFFFFF", font_weight="700", font_size="20px"),
                width="42px",
                height="42px",
                border_radius="12px",
                background=t.Color.ACCENT.value,
                display="flex",
                align_items="center",
                justify_content="center",
                box_shadow="0 4px 12px rgba(59,111,219,.35)",
            ),
            rx.text(
                "Setu",
                font_size="26px",
                font_weight="700",
                letter_spacing="-0.02em",
                color=t.Color.TEXT_PRIMARY.value,
            ),
            rx.text(
                "Reconciliation platform",
                style=t.TEXT["label"],
            ),
            spacing="2",
            align="center",
        ),
        rx.box(height="4px"),
        # Inactivity message (explicit, never a silent redirect)
        rx.cond(
            AuthState.signed_out_inactivity,
            rx.box(
                rx.text(
                    "You were signed out due to inactivity.",
                    font_size="13px",
                    color=t.Color.AI_ON.value,
                ),
                background="#FDF4E3",
                border="1px solid #F0D9AE",
                border_radius="10px",
                padding="10px 14px",
                width="100%",
            ),
            rx.fragment(),
        ),
        # Credentials
        rx.form(
            rx.vstack(
                _field("Username", "username", placeholder="your.username"),
                _field("Password", "password", type_="password", placeholder="••••••••"),
                rx.cond(
                    AuthState.login_error != "",
                    rx.vstack(
                        rx.text(
                            AuthState.login_error,
                            font_size="13px",
                            color=t.Color.TEXT_PRIMARY.value,
                        ),
                        rx.cond(
                            AuthState.show_failed_login_notice,
                            rx.text(
                                "⚠ Multiple failed attempts have been logged.",
                                font_size="11px",
                                color=t.Color.DANGER.value,
                            ),
                            rx.fragment(),
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                rx.button(
                    "Sign in",
                    type="submit",
                    width="100%",
                    size="3",
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                    border_radius="10px",
                    _hover={"background": "#315FC4"},
                ),
                spacing="4",
                width="100%",
            ),
            on_submit=AuthState.submit_login,
            reset_on_submit=False,
            width="100%",
        ),
        # First-run hint
        rx.box(
            rx.text(
                "First run? Default admin login is ",
                rx.text.span("admin / ChangeMe123", font_weight="700"),
                " — change this password immediately after signing in.",
                font_size="12px",
                color=t.Color.TEXT_SECONDARY.value,
            ),
            background="#EAF0FB",
            border="1px solid #D4E1F8",
            border_radius="10px",
            padding="10px 14px",
            width="100%",
        ),
        spacing="4",
        width="100%",
        max_width="380px",
        padding="36px 32px",
        background=t.Color.SURFACE.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="20px",
        box_shadow="0 8px 30px rgba(20,20,20,.08)",
        align="center",
    )


def login_page() -> rx.Component:
    return rx.center(
        _login_card(),
        width="100%",
        min_height="100vh",
        background=t.Color.PAGE_BACKGROUND.value,
        padding="24px",
    )