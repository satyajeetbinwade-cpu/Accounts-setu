"""Public API for the Communications module.

Every other module (and every UI file) should import from here, not from
src.communications.db directly. Mirrors src/clients/service.py's shape.

Covers: provider config, template management, sending follow-ups, log
retrieval, and per-client follow-up preference management.

Business rules:
- At least one active provider is required for a channel to send.
- Templates are seeded on init and can be overridden.
- Sending is logged in communication_log for audit trail.
- Contact info (phone/email) can be edited at send-time without
  persisting to the client profile.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.communications import db as cdb
from src.communications.schema import init_communications_schema
from src.communications.templates import (
    BUILTIN_TEMPLATES,
    render_subject,
    render_template,
)
from src.communications.providers.email import send_email
from src.communications.providers.whatsapp import send_whatsapp
from src.communications.models import (
    FollowUpRecipient,
    FollowUpRequest,
)


class CommunicationError(Exception):
    """Raised for expected communications failures."""


def init_communications(db_path=None) -> None:
    """Create communications tables and seed built-in templates.

    Call once at app start, alongside db.init_db(), auth.init_auth(),
    and the other module init functions.
    """
    conn = recon_db.get_connection(db_path)
    try:
        init_communications_schema(conn)
        _seed_templates(conn)
    finally:
        conn.close()


def _seed_templates(conn: sqlite3.Connection) -> None:
    """Insert built-in templates if they don't already exist."""
    for tmpl in BUILTIN_TEMPLATES:
        existing = conn.execute(
            "SELECT template_id FROM followup_templates WHERE name = ?",
            (tmpl["name"],),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO followup_templates
                   (name, channel, subject_template, body_template, variables)
                   VALUES (?, ?, ?, ?, ?)""",
                (tmpl["name"], tmpl["channel"], tmpl["subject_template"],
                 tmpl["body_template"], tmpl["variables"]),
            )
    conn.commit()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------

def list_providers(*, channel: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_providers(conn, channel=channel)
    finally:
        conn.close()


def save_provider(
    provider_id: Optional[int],
    *,
    channel: str,
    provider_type: str,
    label: Optional[str] = None,
    config: dict,
    db_path=None,
) -> int:
    """Create or update a provider configuration.

    Config is stored as JSON. Values like API keys should be encrypted
    in production.
    """
    conn = _connect(db_path)
    try:
        return cdb.upsert_provider(
            conn, provider_id,
            channel=channel,
            provider_type=provider_type,
            label=label,
            config_json=json.dumps(config),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Template management
# ---------------------------------------------------------------------------

def list_templates(*, channel: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_templates(conn, channel=channel)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sending follow-ups (the main business logic)
# ---------------------------------------------------------------------------

def send_followup(request: FollowUpRequest, *, db_path=None) -> int:
    """Send a follow-up message and log it.

    Returns the communication_log entry ID.

    Raises CommunicationError if:
    - No active provider is configured for the requested channel.
    - The recipient has no contact info for the requested channel.
    """
    if not request.recipient.email and not request.recipient.phone:
        raise CommunicationError(
            "Recipient has no email or phone number configured."
        )

    conn = _connect(db_path)
    try:
        channels_to_send = _resolve_channels(request.channel, request.recipient)
        template = cdb.get_template(conn, request.template_name) if request.template_name != "custom" else None

        log_ids = []
        for channel in channels_to_send:
            # Render the message
            context = dict(request.context)
            context["contact_name"] = request.recipient.contact_name or request.recipient.client_name

            if request.message_override:
                body = request.message_override
            elif template:
                body = render_template(template["body_template"], context)
            else:
                body = context.get("custom_message", "")

            subject = request.subject_override
            if not subject and template:
                rendered = render_subject(template.get("subject_template"), context)
                subject = rendered

            # Get channel-specific recipient info
            if channel == "email":
                to = request.recipient.email
            else:
                to = request.recipient.phone

            if not to:
                continue  # skip channel if no contact info

            # Log the pending message
            log_id = cdb.insert_log(
                conn,
                client_id=request.recipient.client_id,
                channel=channel,
                template_name=request.template_name,
                recipient_email=request.recipient.email if channel == "email" else None,
                recipient_phone=request.recipient.phone if channel == "whatsapp" else None,
                recipient_name=request.recipient.contact_name or request.recipient.client_name,
                subject=subject,
                message_body=body,
            )

            # Send via the active provider
            providers = cdb.list_providers(conn, channel=channel)
            active = [p for p in providers if p["is_active"]]

            if not active:
                cdb.update_log_status(conn, log_id, "failed",
                                      error_message=f"No active {channel} provider configured.")
                log_ids.append(log_id)
                continue

            config = json.loads(active[0]["config_json"])
            config["provider_type"] = active[0]["provider_type"]

            if channel == "email":
                success, ref_or_error = send_email(
                    config,
                    to_email=to,
                    to_name=request.recipient.contact_name,
                    subject=subject or "",
                    body_text=body,
                )
            else:
                success, ref_or_error = send_whatsapp(
                    config,
                    to_phone=to,
                    message_body=body,
                )

            if success:
                cdb.update_log_status(conn, log_id, "sent", provider_ref=ref_or_error)
            else:
                cdb.update_log_status(conn, log_id, "failed",
                                      error_message=ref_or_error)

            log_ids.append(log_id)

        if not log_ids:
            raise CommunicationError("No messages could be sent — check recipient contact info.")

        return log_ids[0]  # Return the first log ID for reference
    finally:
        conn.close()


def _resolve_channels(requested: str, recipient: FollowUpRecipient) -> list[str]:
    """Determine which channels to actually send on, given the request
    and available contact info."""
    if requested == "both":
        channels = []
        if recipient.email:
            channels.append("email")
        if recipient.phone:
            channels.append("whatsapp")
        return channels
    return [requested]


# ---------------------------------------------------------------------------
# Communication log
# ---------------------------------------------------------------------------

def list_logs(*, client_id: Optional[int] = None, limit: int = 50, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_logs(conn, client_id=client_id, limit=limit)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Client follow-up preferences
# ---------------------------------------------------------------------------

def get_client_followup_prefs(client_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.get_followup_prefs(conn, client_id)
    finally:
        conn.close()


def save_followup_pref(
    client_id: int,
    *,
    contact_id: Optional[int] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    preferred_channel: str = "email",
    db_path=None,
) -> int:
    """Save or update a client's follow-up contact preference.

    This is separate from the contact_directory — it can either link to
    an existing contact or store independent contact info for follow-ups.
    """
    conn = _connect(db_path)
    try:
        return cdb.upsert_followup_pref(
            conn, client_id,
            contact_id=contact_id,
            email=email,
            phone=phone,
            preferred_channel=preferred_channel,
        )
    finally:
        conn.close()
