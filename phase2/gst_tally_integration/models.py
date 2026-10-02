"""Data models for GST Portal & Tally Integration."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GstCredentials:
    """Encrypted GST portal credentials for a client."""
    credential_id: int = 0
    client_id: int = 0
    gstin: str = ""
    username: str = ""
    encrypted_password: str = ""  # AES-encrypted
    is_active: bool = True
    last_synced_at: Optional[str] = None
    created_at: str = ""


@dataclass
class TallyConnection:
    """Tally connection configuration per client."""
    conn_id: int = 0
    client_id: int = 0
    host: str = "localhost"
    port: int = 9000
    company_name: str = ""
    is_active: bool = True
    last_synced_at: Optional[str] = None
    created_at: str = ""


@dataclass
class SyncOperation:
    """A sync operation (fetch or push)."""
    op_id: int = 0
    client_id: int = 0
    direction: str = ""  # "fetch" or "push"
    source: str = ""  # "gst_portal" or "tally"
    data_type: str = ""  # "returns", "invoices", "filings", "coa", "journal"
    status: str = "pending"  # pending, previewing, confirmed, completed, failed
    summary_json: str = "{}"
    details_json: str = "{}"
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    created_at: str = ""


@dataclass
class GstReturnData:
    """Fetched GST return data."""
    return_id: int = 0
    client_id: int = 0
    gstin: str = ""
    return_type: str = ""  # GSTR1, GSTR3B, GSTR9, etc.
    period: str = ""  # e.g. "2026-08"
    filing_status: str = ""
    filed_at: Optional[str] = None
    raw_json: str = "{}"
    synced_at: str = ""


@dataclass
class TallyData:
    """Fetched/pushed Tally data."""
    data_id: int = 0
    client_id: int = 0
    data_type: str = ""  # "coa", "voucher", "ledger", "journal"
    period: Optional[str] = None
    raw_xml: Optional[str] = None
    raw_json: str = "{}"
    synced_at: str = ""
