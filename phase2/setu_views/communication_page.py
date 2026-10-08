"""Communications page — send follow-ups and view communication history."""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from phase2.setu_state.communication_state import CommunicationState


def _client_selector() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Select Client", style=t.TEXT["card_title"]),
            rx.cond(
                CommunicationState.clients.length() > 0,
                rx.grid(
                    rx.foreach(
                        CommunicationState.clients,
                        lambda cl: rx.button(
                            cl["legal_name"],
                            on_click=CommunicationState.select_client(cl["client_id"]),
                            variant="soft",
                            color_scheme="gray",
                            background=rx.cond(
                                CommunicationState.selected_client_id == cl["client_id"],
                                t.Color.ACCENT.value,
                                "transparent",
                            ),
                            color=rx.cond(
                                CommunicationState.selected_client_id == cl["client_id"],
                                "#FFFFFF",
                                t.Color.TEXT_PRIMARY.value,
                            ),
                            width="100%",
                            justify="start",
                        ),
                    ),
                    columns="3",
                    spacing="2",
                    width="100%",
                ),
                c.empty_state("No clients loaded.", icon="building-2"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _recipient_form() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Recipient Details", style=t.TEXT["card_title"]),
            rx.text(
                "Contact info is editable at send-time. Changes here do NOT modify the client profile.",
                style=t.TEXT["micro"],
            ),
            rx.grid(
                rx.vstack(
                    rx.text("Email *", style=t.TEXT["label"]),
                    rx.input(
                        value=CommunicationState.recipient_email,
                        on_change=CommunicationState.set_recipient_email,
                        placeholder="client@example.com",
                        width="100%",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.vstack(
                    rx.text("Phone (with country code)", style=t.TEXT["label"]),
                    rx.input(
                        value=CommunicationState.recipient_phone,
                        on_change=CommunicationState.set_recipient_phone,
                        placeholder="+919876543210",
                        width="100%",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.vstack(
                    rx.text("Contact name", style=t.TEXT["label"]),
                    rx.input(
                        value=CommunicationState.recipient_contact_name,
                        on_change=CommunicationState.set_recipient_contact_name,
                        placeholder="Person to address",
                        width="100%",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.vstack(
                    rx.text("Channel", style=t.TEXT["label"]),
                    rx.select(
                        ["email", "whatsapp", "both"],
                        value=CommunicationState.channel,
                        on_change=CommunicationState.set_channel,
                        width="100%",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                columns="2",
                spacing="4",
                width="100%",
            ),
            rx.button(
                "Save as default for this client",
                on_click=CommunicationState.save_contact_prefs,
                variant="soft",
                color_scheme="gray",
                size="1",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _message_composer() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Message", style=t.TEXT["card_title"]),
            rx.vstack(
                rx.text("Template", style=t.TEXT["label"]),
                rx.select(
                    ["missing_invoice", "missing_entries", "followup_reminder", "custom"],
                    value=CommunicationState.selected_template,
                    on_change=CommunicationState.set_selected_template,
                    width="100%",
                ),
                spacing="1",
                align="start",
                width="100%",
            ),
            rx.cond(
                CommunicationState.selected_template == "custom",
                rx.vstack(
                    rx.vstack(
                        rx.text("Subject", style=t.TEXT["label"]),
                        rx.input(
                            value=CommunicationState.subject_override,
                            on_change=CommunicationState.set_subject_override,
                            placeholder="Follow-up subject",
                            width="100%",
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    rx.vstack(
                        rx.text("Message", style=t.TEXT["label"]),
                        rx.text_area(
                            value=CommunicationState.custom_message,
                            on_change=CommunicationState.set_custom_message,
                            placeholder="Type your message here...",
                            rows="6",
                            width="100%",
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    spacing="3",
                    width="100%",
                ),
                rx.vstack(
                    rx.grid(
                        rx.vstack(
                            rx.text("Period", style=t.TEXT["label"]),
                            rx.input(
                                value=CommunicationState.period,
                                on_change=CommunicationState.set_period,
                                placeholder="e.g. 2026-08",
                                width="100%",
                            ),
                            spacing="1",
                            align="start",
                            width="100%",
                        ),
                        rx.vstack(
                            rx.text("Recon type", style=t.TEXT["label"]),
                            rx.select(
                                ["GST", "TDS"],
                                value=CommunicationState.recon_type,
                                on_change=CommunicationState.set_recon_type,
                                width="100%",
                            ),
                            spacing="1",
                            align="start",
                            width="100%",
                        ),
                        columns="2",
                        spacing="4",
                        width="100%",
                    ),
                    rx.cond(
                        CommunicationState.selected_template == "missing_invoice",
                        rx.vstack(
                            rx.vstack(
                                rx.text("Missing count", style=t.TEXT["label"]),
                                rx.input(
                                    value=CommunicationState.missing_count,
                                    on_change=CommunicationState.set_missing_count,
                                    placeholder="e.g. 5",
                                    width="100%",
                                ),
                                spacing="1",
                                align="start",
                                width="100%",
                            ),
                        ),
                        rx.fragment(),
                    ),
                    rx.vstack(
                        rx.text("Details", style=t.TEXT["label"]),
                        rx.text_area(
                            value=CommunicationState.missing_details,
                            on_change=CommunicationState.set_missing_details,
                            placeholder="Describe what's missing or needs context...",
                            rows="4",
                            width="100%",
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    spacing="3",
                    width="100%",
                ),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _preview_panel() -> rx.Component:
    return rx.cond(
        CommunicationState.preview_subject != "" or CommunicationState.preview_body != "",
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.text("Preview", style=t.TEXT["card_title"]),
                    rx.spacer(),
                    rx.button(
                        "Refresh preview",
                        on_click=CommunicationState.refresh_preview,
                        size="1",
                        variant="soft",
                        color_scheme="gray",
                    ),
                    width="100%",
                    align="center",
                ),
                rx.cond(
                    CommunicationState.preview_subject != "",
                    rx.vstack(
                        rx.text("Subject:", style=t.TEXT["label"]),
                        rx.text(
                            CommunicationState.preview_subject,
                            font_weight="600",
                            style=t.TEXT["body"],
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                rx.box(height="1px", width="100%", background=t.Color.BORDER.value),
                rx.text("Body:", style=t.TEXT["label"]),
                rx.box(
                    rx.text(CommunicationState.preview_body, style=t.TEXT["body"]),
                    background="#FAFBFC",
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                    padding="12px",
                    white_space="pre-wrap",
                    width="100%",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _send_action() -> rx.Component:
    return rx.hstack(
        rx.button(
            rx.cond(
                CommunicationState.is_sending,
                rx.spinner(size="1", color="#FFFFFF"),
                rx.fragment(),
            ),
            rx.cond(
                CommunicationState.is_sending,
                "Sending...",
                "Send Follow-up",
            ),
            on_click=CommunicationState.send_followup,
            disabled=CommunicationState.is_sending,
            background=t.Color.ACCENT.value,
            color="#FFFFFF",
            border_radius="10px",
        ),
        rx.button(
            "Clear",
            variant="soft",
            color_scheme="gray",
            on_click=CommunicationState.toggle_send_form,
        ),
        spacing="3",
        align="center",
    )


def _communication_log() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Recent Communications", style=t.TEXT["card_title"]),
            rx.cond(
                CommunicationState.communication_log.length() > 0,
                rx.vstack(
                    rx.foreach(
                        CommunicationState.communication_log,
                        lambda log: rx.hstack(
                            rx.vstack(
                                rx.text(
                                    log.get("channel", "").upper() + " — " + (log.get("subject", "") or "(no subject)"),
                                    font_size="13px",
                                    font_weight="600",
                                ),
                                rx.text(
                                    f"To: {log.get('recipient_email') or log.get('recipient_phone', '')}",
                                    style=t.TEXT["micro"],
                                ),
                                spacing="1",
                                align="start",
                                flex="1",
                            ),
                            c.pill(log.get("status", "unknown"), variant="placeholder"),
                            rx.text(
                                log.get("created_at", ""),
                                style=t.TEXT["micro"],
                                white_space="nowrap",
                            ),
                            width="100%",
                            align="center",
                            padding="8px 0",
                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                            spacing="3",
                        ),
                    ),
                    spacing="0",
                    width="100%",
                ),
                c.empty_state("No communications sent yet.", icon="inbox"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def communication_page() -> rx.Component:
    return rx.vstack(
        c.page_header(
            "Client Follow-ups",
            "Send WhatsApp and email follow-ups to clients for missing invoices or entries.",
        ),
        rx.cond(CommunicationState.flash != "", c.info_banner(CommunicationState.flash), rx.fragment()),
        _client_selector(),
        rx.cond(
            CommunicationState.selected_client_id != 0,
            rx.vstack(
                _recipient_form(),
                _message_composer(),
                _preview_panel(),
                _send_action(),
                _communication_log(),
                spacing="4",
                width="100%",
                align="start",
            ),
            c.empty_state("Select a client above to compose a follow-up.", icon="mail"),
        ),
        spacing="5",
        width="100%",
        align="start",
        on_mount=CommunicationState.load_clients,
    )
