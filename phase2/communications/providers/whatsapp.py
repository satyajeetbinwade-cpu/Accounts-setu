"""WhatsApp provider — Twilio / WATI / Interakt with a pluggable interface.

Supports:
- Twilio WhatsApp Business API
- WATI (WhatsApp Team Inbox) — popular in India
- Interakt — popular in India

To use in production, set the provider config in the database:
  provider_type: 'twilio', 'wati', or 'interakt'

Twilio config_json:
  {
    "account_sid": "ACxxxxx",
    "auth_token": "xxxxx",
    "from_number": "whatsapp:+14155238886"
  }

WATI config_json:
  {
    "api_url": "https://api.wati.io/api/v1",
    "token": "xxxxx"
  }

Interakt config_json:
  {
    "api_url": "https://api.interakt.ai/v1",
    "api_key": "xxxxx"
  }
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Optional


def send_whatsapp(
    config: dict,
    *,
    to_phone: str,
    message_body: str,
) -> tuple[bool, Optional[str]]:
    """Send a WhatsApp message via the configured provider.

    Returns (success, error_message).
    """
    provider_type = config.get("provider_type", "twilio")

    if provider_type == "twilio":
        return _send_twilio(config, to_phone, message_body)
    elif provider_type == "wati":
        return _send_wati(config, to_phone, message_body)
    elif provider_type == "interakt":
        return _send_interakt(config, to_phone, message_body)
    else:
        return False, f"Unknown WhatsApp provider: {provider_type}"


def _send_twilio(config: dict, to_phone: str, message_body: str) -> tuple[bool, Optional[str]]:
    """Send via Twilio's WhatsApp Business API using HTTP POST."""
    account_sid = config.get("account_sid", "")
    auth_token = config.get("auth_token", "")
    from_number = config.get("from_number", "whatsapp:+14155238886")

    # Format phone number
    if not to_phone.startswith("whatsapp:"):
        to_phone = f"whatsapp:{to_phone}"

    import base64
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    data = urllib.parse.urlencode({
        "To": to_phone,
        "From": from_number,
        "Body": message_body,
    }).encode()

    req = urllib.request.Request(url, data=data)
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = resp.read().decode()
            result = json.loads(response_body)
            if resp.status in (200, 201):
                return True, result.get("sid")  # provider_ref = message SID
            return False, result.get("message", "Twilio API error")
    except urllib.error.HTTPError as e:
        return False, f"Twilio HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, str(e)


def _send_wati(config: dict, to_phone: str, message_body: str) -> tuple[bool, Optional[str]]:
    """Send via WATI API."""
    api_url = config.get("api_url", "https://api.wati.io/api/v1").rstrip("/")
    token = config.get("token", "")

    # Format phone: remove any non-digit chars
    phone = "".join(c for c in to_phone if c.isdigit())

    url = f"{api_url}/sendSessionMessage/{phone}"
    payload = json.dumps({
        "messageText": message_body,
    }).encode()

    req = urllib.request.Request(url, data=payload)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return True, result.get("id")
    except urllib.error.HTTPError as e:
        return False, f"WATI HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, str(e)


def _send_interakt(config: dict, to_phone: str, message_body: str) -> tuple[bool, Optional[str]]:
    """Send via Interakt API."""
    api_url = config.get("api_url", "https://api.interakt.ai/v1").rstrip("/")
    api_key = config.get("api_key", "")

    phone = "".join(c for c in to_phone if c.isdigit())

    url = f"{api_url}/send-message"
    payload = json.dumps({
        "phoneNumber": phone,
        "type": "text",
        "text": {"body": message_body},
    }).encode()

    req = urllib.request.Request(url, data=payload)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return True, result.get("id")
    except urllib.error.HTTPError as e:
        return False, f"Interakt HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return False, str(e)
