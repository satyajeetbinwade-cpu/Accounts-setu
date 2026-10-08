"""Data models for the Communications module."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FollowUpRecipient:
    """Contact details for a follow-up recipient — editable at send-time."""
    client_id: int
    client_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    contact_name: Optional[str] = None


@dataclass
class FollowUpRequest:
    """A follow-up to be sent."""
    recipient: FollowUpRecipient
    channel: str  # "email", "whatsapp", or "both"
    template_name: str  # "missing_invoice", "missing_entries", "custom"
    context: dict = field(default_factory=dict)
    # Context fields:
    #   period: str          — e.g. "2026-08"
    #   recon_type: str      — "GST" or "TDS"
    #   missing_count: int   — number of missing items
    #   missing_details: str — summary
    #   custom_message: str  — for template_name="custom"
    subject_override: Optional[str] = None
    message_override: Optional[str] = None


@dataclass
class CommunicationLogEntry:
    """Record of a sent communication."""
    log_id: int = 0
    client_id: int = 0
    channel: str = ""
    template_name: str = ""
    recipient_email: Optional[str] = None
    recipient_phone: Optional[str] = None
    subject: Optional[str] = None
    message_body: str = ""
    status: str = ""  # "sent", "failed", "pending"
    error_message: Optional[str] = None
    sent_at: Optional[str] = None
    created_at: str = ""


@dataclass
class ProviderConfig:
    """Configuration for a communication provider."""
    provider_id: int = 0
    channel: str = ""
    provider_type: str = ""
    config_json: str = "{}"
    is_active: bool = True
    created_at: str = ""
    updated_at: str = ""


@dataclass
class FollowUpTemplate:
    """Reusable message template."""
    template_id: int = 0
    name: str = ""
    channel: str = ""
    subject_template: Optional[str] = None
    body_template: str = ""
    variables: str = "[]"
    is_active: bool = True
    created_at: str = ""
