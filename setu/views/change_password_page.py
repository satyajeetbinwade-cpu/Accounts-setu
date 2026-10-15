"""Forced password change — the screen a seeded or Admin-reset credential
must pass through before the account can be used.

Reached two ways: straight after signing in with a password somebody else
issued, or from the banner in the app shell. It shows no credential material
and keeps no password in state — the values arrive in the form payload, are
checked server-side by ``src.auth.service.change_own_password``, and are gone.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import tokens as t
from setu.state import AuthState


def _field(
    label: str,
    name: str,
    *,
    hint: str = "",
    auto_complete: str = "off",
) -> rx.Component:
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.input(
            name=name,
            type="password",
            placeholder="",
            width="100%",
            size="3",
            custom_attrs={"autoComplete": auto_complete},
        ),
        rx.cond(
            hint != "",
            rx.text(hint, font_size="11px", color=t.Color.TEXT_SECONDARY.value),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        width="100%",
    )


def change_password_page() -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.vstack(
                rx.text(
                    "Choose a new password",
                    font_size="22px",
                    font_weight="700",
                    color=t.Color.TEXT_PRIMARY.value,
                ),
                rx.text(
                    "This account was set up with a password someone else issued. "
                    "Replace it to continue.",
                    font_size="13px",
                    color=t.Color.TEXT_SECONDARY.value,
                ),
                spacing="1",
                align="start",
                width="100%",
            ),
            rx.cond(
                AuthState.cp_error != "",
                rx.box(
                    rx.text(
                        AuthState.cp_error,
                        font_size="13px",
                        color=t.Color.DANGER.value,
                    ),
                    background="#FDECEC",
                    border="1px solid #F5C2C2",
                    border_radius="10px",
                    padding="10px 14px",
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.form(
                rx.vstack(
                    _field("Current password", "current_password"),
                    _field(
                        "New password",
                        "new_password",
                        hint="At least 8 characters.",
                        auto_complete="new-password",
                    ),
                    _field("Confirm new password", "confirm_password", auto_complete="new-password"),
                    rx.button(
                        "Save and continue",
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
                on_submit=AuthState.submit_password_change,
                reset_on_submit=True,
                width="100%",
            ),
            rx.button(
                "Sign out",
                on_click=AuthState.logout,
                variant="ghost",
                size="2",
                color=t.Color.TEXT_SECONDARY.value,
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
        ),
        width="100%",
        min_height="100vh",
        background=t.Color.PAGE_BACKGROUND.value,
        padding="24px",
    )
