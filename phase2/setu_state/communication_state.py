"""Reflex state for the Communications module (follow-ups).

Follows the same pattern as setu/state/client_state.py.
"""

from __future__ import annotations

import reflex as rx
from typing import Optional

from src.communications import service as comms
from src.communications.models import FollowUpRecipient, FollowUpRequest
from src.clients import service as client_svc


class CommunicationState(rx.State):
    """State for sending follow-ups and managing communication preferences."""

    # ---- Client selection ----
    selected_client_id: int = 0
    selected_client_name: str = ""
    clients: list[dict] = []

    # ---- Recipient info (editable at send-time) ----
    recipient_email: str = ""
    recipient_phone: str = ""
    recipient_contact_name: str = ""

    # ---- Follow-up content ----
    available_templates: list[dict] = []
    selected_template: str = "missing_invoice"
    period: str = ""
    recon_type: str = "GST"
    missing_count: str = ""
    missing_details: str = ""
    custom_message: str = ""
    subject_override: str = ""
    channel: str = "email"  # email, whatsapp, both

    # ---- Preview ----
    preview_subject: str = ""
    preview_body: str = ""

    # ---- Log ----
    communication_log: list[dict] = []
    log_count: int = 0

    # ---- UI state ----
    flash: str = ""
    is_sending: bool = False
    show_send_form: bool = True

    def load_clients(self):
        """Load clients list for the follow-up page."""
        self.clients = client_svc.list_clients(include_inactive=False)

    def select_client(self, client_id: int):
        """Select a client and load their contact info."""
        self.selected_client_id = client_id
        client = client_svc.get_client(client_id)
        if client:
            self.selected_client_name = client["legal_name"]

        # Load follow-up preferences
        prefs = comms.get_client_followup_prefs(client_id)
        if prefs:
            pref = prefs[0]
            self.recipient_email = pref.get("email") or ""
            self.recipient_phone = pref.get("phone") or ""
            self.recipient_contact_name = pref.get("contact_name") or ""
            self.channel = pref.get("preferred_channel", "email")

        # Load templates
        self.available_templates = comms.list_templates()

        # Load communication log
        self._load_log(client_id)

    def _load_log(self, client_id: Optional[int] = None):
        cid = client_id or self.selected_client_id
        if cid:
            self.communication_log = comms.list_logs(client_id=cid, limit=20)
        else:
            self.communication_log = []

    def set_recipient_email(self, value: str):
        self.recipient_email = value

    def set_recipient_phone(self, value: str):
        self.recipient_phone = value

    def set_recipient_contact_name(self, value: str):
        self.recipient_contact_name = value

    def set_selected_template(self, value: str):
        self.selected_template = value
        self._update_preview()

    def set_period(self, value: str):
        self.period = value
        self._update_preview()

    def set_recon_type(self, value: str):
        self.recon_type = value
        self._update_preview()

    def set_missing_count(self, value: str):
        self.missing_count = value

    def set_missing_details(self, value: str):
        self.missing_details = value

    def set_custom_message(self, value: str):
        self.custom_message = value

    def set_subject_override(self, value: str):
        self.subject_override = value

    def set_channel(self, value: str):
        self.channel = value

    def _update_preview(self):
        """Update the preview of the message."""
        context = self._build_context()
        template = next(
            (t for t in self.available_templates if t["name"] == self.selected_template),
            None,
        )
        if template:
            from src.communications.templates import render_template, render_subject
            self.preview_subject = render_subject(
                template.get("subject_template"), context
            ) or ""
            self.preview_body = render_template(template["body_template"], context)
        else:
            self.preview_subject = self.subject_override
            self.preview_body = self.custom_message

    def _build_context(self) -> dict:
        return {
            "contact_name": self.recipient_contact_name or self.selected_client_name,
            "period": self.period or "[period]",
            "recon_type": self.recon_type,
            "missing_count": self.missing_count or "N",
            "missing_details": self.missing_details or "[details not provided]",
            "subject": self.subject_override or "Follow-up",
            "custom_message": self.custom_message or "[custom message]",
        }

    def refresh_preview(self):
        """Manually refresh the preview."""
        self._update_preview()

    def send_followup(self):
        """Send the follow-up message."""
        if not self.selected_client_id:
            self.flash = "Please select a client first."
            return
        if not self.recipient_email and self.channel in ("email", "both"):
            self.flash = "Recipient email is required for email channel."
            return
        if not self.recipient_phone and self.channel in ("whatsapp", "both"):
            self.flash = "Recipient phone is required for WhatsApp channel."
            return

        self.is_sending = True
        try:
            request = FollowUpRequest(
                recipient=FollowUpRecipient(
                    client_id=self.selected_client_id,
                    client_name=self.selected_client_name,
                    email=self.recipient_email or None,
                    phone=self.recipient_phone or None,
                    contact_name=self.recipient_contact_name or None,
                ),
                channel=self.channel,
                template_name=self.selected_template,
                context=self._build_context(),
                subject_override=self.subject_override or None,
                message_override=None,
            )

            log_id = comms.send_followup(request)
            self.flash = f"Follow-up sent successfully (log #{log_id})."
            self._load_log()
        except Exception as e:
            self.flash = f"Failed to send: {e}"
        finally:
            self.is_sending = False

    def save_contact_prefs(self):
        """Save the current contact info as the client's follow-up preferences."""
        if not self.selected_client_id:
            return
        try:
            comms.save_followup_pref(
                self.selected_client_id,
                email=self.recipient_email or None,
                phone=self.recipient_phone or None,
                preferred_channel=self.channel,
            )
            self.flash = "Follow-up contact preferences saved."
        except Exception as e:
            self.flash = f"Failed to save preferences: {e}"

    def toggle_send_form(self):
        self.show_send_form = not self.show_send_form

    def clear_flash(self):
        self.flash = ""
