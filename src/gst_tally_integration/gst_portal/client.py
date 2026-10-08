"""GST Portal (GSTN) API client.

Provides methods to authenticate with the GST portal and fetch/push data.

GSTN APIs (Indian GST):
- Authentication: OTP-based or credentials-based
- Returns: GSTR1, GSTR3B, GSTR9
- Invoices: Fetch/Push via the GST system
- Filings: Status and history

Note: The actual GSTN API requires:
1. A registered GST practitioner account
2. API credentials from the GST portal (Suvidha Provider or direct)
3. Proper encryption as per GSTN specifications

This module provides the interface structure. Production use requires:
- GSTN API subscription
- Proper key management for encryption
- Compliance with GSTN API guidelines
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
import base64
from typing import Any, Optional


class GstPortalClient:
    """Client for interacting with the GST Portal (GSTN) APIs."""

    # GSTN API endpoints (Suvidha Provider / direct API)
    API_BASE_SANDBOX = "https://santam-sandbox.gst.gov.in"
    API_BASE_PRODUCTION = "https://api.gst.gov.in"

    def __init__(
        self,
        gstin: str,
        username: str,
        password: str,
        *,
        sandbox: bool = True,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        self.gstin = gstin
        self.username = username
        self.password = password
        self.base_url = self.API_BASE_SANDBOX if sandbox else self.API_BASE_PRODUCTION
        self.client_id = client_id
        self.client_secret = client_secret
        self._auth_token: Optional[str] = None
        self._session: Optional[dict] = None

    # -----------------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------------

    def authenticate(self) -> tuple[bool, Optional[str]]:
        """Authenticate with the GST portal.

        GSTN uses an OTP-based flow:
        1. POST /auth/otp/request — sends OTP to registered mobile
        2. POST /auth/otp/authenticate — verifies OTP and returns token

        For API-based auth (Suvidha Provider):
        1. POST /auth/api/authenticate — returns auth token

        Returns (success, error_message).
        """
        # Placeholder: real GSTN auth requires OTP handling
        # This would need the UI to prompt for OTP input
        try:
            payload = json.dumps({
                "username": self.username,
                "password": self.password,
                "gstin": self.gstin,
                "client_id": self.client_id or "",
                "client_secret": self.client_secret or "",
            }).encode()

            req = urllib.request.Request(
                f"{self.base_url}/auth/api/authenticate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                if result.get("status") == "SUCCESS":
                    self._auth_token = result.get("auth_token")
                    self._session = result
                    return True, None
                return False, result.get("error", "Authentication failed")
        except urllib.error.HTTPError as e:
            return False, f"GSTN HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, str(e)

    # -----------------------------------------------------------------------
    # Returns
    # -----------------------------------------------------------------------

    def fetch_returns(self, period: str) -> tuple[bool, Any]:
        """Fetch GST returns for a period.

        Args:
            period: Format "MMYYYY" (e.g., "082026" for Aug 2026)

        Returns (success, data_or_error).
        """
        if not self._auth_token:
            success, error = self.authenticate()
            if not success:
                return False, error

        try:
            url = f"{self.base_url}/returns/{self.gstin}/period/{period}"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self._auth_token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
                return True, data
        except urllib.error.HTTPError as e:
            return False, f"GSTN HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, str(e)

    def fetch_return_status(self, return_type: str, period: str) -> tuple[bool, Any]:
        """Check filing status of a specific return type for a period.

        Args:
            return_type: "GSTR1", "GSTR3B", etc.
            period: Format "MMYYYY"

        Returns (success, data_or_error).
        """
        if not self._auth_token:
            success, error = self.authenticate()
            if not success:
                return False, error

        try:
            url = f"{self.base_url}/returns/{self.gstin}/{return_type}/status/{period}"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {self._auth_token}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                return True, data
        except urllib.error.HTTPError as e:
            return False, f"GSTN HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, str(e)

    # -----------------------------------------------------------------------
    # Invoices
    # -----------------------------------------------------------------------

    def fetch_invoices(self, period: str, return_type: str = "GSTR1") -> tuple[bool, Any]:
        """Fetch invoices from GST portal for a period.

        Returns (success, data_or_error).
        """
        if not self._auth_token:
            success, error = self.authenticate()
            if not success:
                return False, error

        try:
            url = f"{self.base_url}/returns/{self.gstin}/{return_type}/invoices/{period}"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {self._auth_token}"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
                return True, data
        except urllib.error.HTTPError as e:
            return False, f"GSTN HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, str(e)

    # -----------------------------------------------------------------------
    # Push data to GST portal
    # -----------------------------------------------------------------------

    def push_return(self, return_type: str, period: str, data: dict) -> tuple[bool, Any]:
        """Push/file a return on the GST portal.

        Args:
            return_type: "GSTR1", "GSTR3B", etc.
            period: Format "MMYYYY"
            data: Return data payload

        Returns (success, result_or_error).
        """
        if not self._auth_token:
            success, error = self.authenticate()
            if not success:
                return False, error

        try:
            payload = json.dumps(data).encode()
            url = f"{self.base_url}/returns/{self.gstin}/{return_type}/file/{period}"
            req = urllib.request.Request(
                url, data=payload,
                headers={
                    "Authorization": f"Bearer {self._auth_token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode())
                return True, result
        except urllib.error.HTTPError as e:
            return False, f"GSTN HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, str(e)

    # -----------------------------------------------------------------------
    # Filing history
    # -----------------------------------------------------------------------

    def fetch_filing_calendar(self) -> tuple[bool, Any]:
        """Fetch the filing calendar/due dates for the GSTIN."""
        if not self._auth_token:
            success, error = self.authenticate()
            if not success:
                return False, error

        try:
            url = f"{self.base_url}/filing/{self.gstin}/calendar"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {self._auth_token}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                return True, data
        except urllib.error.HTTPError as e:
            return False, f"GSTN HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, str(e)
