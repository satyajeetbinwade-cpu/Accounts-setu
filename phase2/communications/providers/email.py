"""Email provider — SendGrid SMTP with a pluggable interface.

Supports:
- SendGrid (API key via SMTP)
- Generic SMTP (any SMTP server)

To use in production, set the provider config in the database:
  provider_type: 'smtp' or 'sendgrid'
  config_json: {
    "host": "smtp.sendgrid.net",
    "port": 587,
    "username": "apikey",
    "password": "SG.xxxxx",
    "from_address": "noreply@setu-platform.com",
    "from_name": "Setu Accounts Team"
  }
"""

from __future__ import annotations

import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional


def send_email(
    config: dict,
    *,
    to_email: str,
    to_name: Optional[str],
    subject: str,
    body_text: str,
) -> tuple[bool, Optional[str]]:
    """Send an email via the configured provider.

    Returns (success, error_message).
    """
    host = config.get("host", "smtp.sendgrid.net")
    port = int(config.get("port", 587))
    username = config.get("username", "apikey")
    password = config.get("password", "")
    from_address = config.get("from_address", "noreply@setu-platform.com")
    from_name = config.get("from_name", "Setu Accounts Team")

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_address}>"
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(msg)
        return True, None
    except Exception as e:
        return False, str(e)
