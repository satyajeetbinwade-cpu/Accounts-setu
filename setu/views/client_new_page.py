"""F2 — New Client (standalone screen at ``/clients/new``).

Its own page, not a form rendered inline above the Client Roster table: the
roster now contains zero form fields.

One GST registration = one client, so there is exactly ONE GSTIN field and no
"State (for the primary GSTIN)" input — the state belongs in the free-text
address. At least one of PAN or TAN is required; the primary contact block
(email/phone/address) is OPTIONAL. The inline message and the disabled
Save button both come from the SERVICE's own validator, so the hint can never
disagree with what the save enforces.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.client_state import NewClientState
from setu.views import shell


def _field(label: str, value, on_change, *, placeholder: str = "") -> rx.Component:
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.input(value=value, on_change=on_change, placeholder=placeholder, width="100%"),
        spacing="1",
        align="start",
        width="100%",
    )


def _form_card() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Client details", style=t.TEXT["card_title"]),
            rx.grid(
                _field("Legal name *", NewClientState.legal_name, NewClientState.set_legal_name),
                _field("Assigned team", NewClientState.assigned_team, NewClientState.set_assigned_team),
                columns="2",
                spacing="4",
                width="100%",
            ),
            rx.text(
                "At least one of PAN or TAN is required — fill both if you have them.",
                style=t.TEXT["micro"],
            ),
            rx.grid(
                _field("PAN", NewClientState.pan, NewClientState.set_pan, placeholder="AABCM1234K"),
                _field("TAN", NewClientState.tan, NewClientState.set_tan, placeholder="MUMA12345B"),
                columns="2",
                spacing="4",
                width="100%",
            ),
            _field(
                "GSTIN",
                NewClientState.gstin,
                NewClientState.set_gstin,
                placeholder="27AABCM1234K1Z9",
            ),
            c.divider(),
            rx.text("Primary contact (optional)", style=t.TEXT["card_title"]),
            rx.grid(
                _field("Email", NewClientState.email, NewClientState.set_email),
                _field("Phone", NewClientState.phone, NewClientState.set_phone),
                columns="2",
                spacing="4",
                width="100%",
            ),
            _field(
                "Address (include the state — it isn't a separate field any more)",
                NewClientState.address,
                NewClientState.set_address,
            ),
            rx.cond(
                NewClientState.validation_message != "",
                c.inline_reason(NewClientState.validation_message),
                rx.fragment(),
            ),
            rx.hstack(
                rx.button(
                    "Create client",
                    on_click=NewClientState.submit,
                    disabled=~NewClientState.ready,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Cancel",
                    on_click=NewClientState.cancel,
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                ),
                spacing="2",
            ),
            spacing="4",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def client_new_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.back_button("← Back to Client Roster", NewClientState.cancel),
            c.page_header(
                "New Client",
                "Onboard one client. Each GST registration is its own client — there are no branches.",
            ),
            rx.cond(NewClientState.error != "", c.inline_reason(NewClientState.error), rx.fragment()),
            _form_card(),
            spacing="4",
            width="100%",
            align="start",
        )
    )
