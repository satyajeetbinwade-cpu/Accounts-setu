"""Database operations for the Communications module.

Pattern matches src/clients/db.py — raw SQL, no business logic.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------

def list_providers(conn: sqlite3.Connection, *, channel: Optional[str] = None) -> list[dict[str, Any]]:
    if channel:
        rows = conn.execute(
            "SELECT * FROM followup_providers WHERE channel = ? ORDER BY provider_id",
            (channel,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM followup_providers ORDER BY channel, provider_id").fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(followup_providers)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def upsert_provider(
    conn: sqlite3.Connection,
    provider_id: Optional[int],
    *,
    channel: str,
    provider_type: str,
    label: Optional[str],
    config_json: str,
) -> int:
    if provider_id:
        conn.execute(
            """UPDATE followup_providers SET channel=?, provider_type=?, label=?,
               config_json=?, updated_at=datetime('now') WHERE provider_id=?""",
            (channel, provider_type, label, config_json, provider_id),
        )
        return provider_id
    cur = conn.execute(
        "INSERT INTO followup_providers (channel, provider_type, label, config_json) VALUES (?, ?, ?, ?)",
        (channel, provider_type, label, config_json),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def list_templates(conn: sqlite3.Connection, *, channel: Optional[str] = None) -> list[dict[str, Any]]:
    if channel and channel != "both":
        rows = conn.execute(
            "SELECT * FROM followup_templates WHERE channel IN (?, 'both') AND is_active=1 ORDER BY name",
            (channel,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM followup_templates WHERE is_active=1 ORDER BY name"
        ).fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(followup_templates)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def get_template(conn: sqlite3.Connection, name: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM followup_templates WHERE name = ? AND is_active=1", (name,)
    ).fetchone()
    if not row:
        return None
    cols = [d[0] for d in conn.execute("PRAGMA table_info(followup_templates)").fetchall()]
    return dict(zip(cols, row))


# ---------------------------------------------------------------------------
# Communication log
# ---------------------------------------------------------------------------

def insert_log(
    conn: sqlite3.Connection,
    *,
    client_id: int,
    channel: str,
    template_name: Optional[str],
    recipient_email: Optional[str],
    recipient_phone: Optional[str],
    recipient_name: Optional[str],
    subject: Optional[str],
    message_body: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO communication_log
           (client_id, channel, template_name, recipient_email, recipient_phone,
            recipient_name, subject, message_body)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_id, channel, template_name, recipient_email, recipient_phone,
         recipient_name, subject, message_body),
    )
    conn.commit()
    return cur.lastrowid


def update_log_status(
    conn: sqlite3.Connection,
    log_id: int,
    status: str,
    *,
    provider_ref: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    if status in ("sent", "read"):
        conn.execute(
            """UPDATE communication_log SET status=?, provider_ref=?, sent_at=datetime('now')
               WHERE log_id=?""",
            (status, provider_ref, log_id),
        )
    else:
        conn.execute(
            "UPDATE communication_log SET status=?, error_message=? WHERE log_id=?",
            (status, error_message, log_id),
        )
    conn.commit()


def list_logs(
    conn: sqlite3.Connection,
    *,
    client_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if client_id:
        rows = conn.execute(
            "SELECT * FROM communication_log WHERE client_id=? ORDER BY created_at DESC LIMIT ?",
            (client_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM communication_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(communication_log)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Client follow-up preferences
# ---------------------------------------------------------------------------

def get_followup_prefs(conn: sqlite3.Connection, client_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.*, c.name AS contact_name, c.role_title
           FROM client_followup_prefs p
           LEFT JOIN contact_directory c ON c.contact_id = p.contact_id
           WHERE p.client_id = ? AND p.is_active=1""",
        (client_id,),
    ).fetchall()
    cols = ["pref_id", "client_id", "contact_id", "email", "phone",
            "preferred_channel", "is_active", "created_at", "updated_at",
            "contact_name", "role_title"]
    return [dict(zip(cols, r)) for r in rows]


def upsert_followup_pref(
    conn: sqlite3.Connection,
    client_id: int,
    *,
    contact_id: Optional[int],
    email: Optional[str],
    phone: Optional[str],
    preferred_channel: str,
) -> int:
    existing = conn.execute(
        "SELECT pref_id FROM client_followup_prefs WHERE client_id=? AND contact_id=?",
        (client_id, contact_id),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE client_followup_prefs SET email=?, phone=?, preferred_channel=?,
               updated_at=datetime('now') WHERE pref_id=?""",
            (email, phone, preferred_channel, existing[0]),
        )
        conn.commit()
        return existing[0]
    cur = conn.execute(
        """INSERT INTO client_followup_prefs
           (client_id, contact_id, email, phone, preferred_channel)
           VALUES (?, ?, ?, ?, ?)""",
        (client_id, contact_id, email, phone, preferred_channel),
    )
    conn.commit()
    return cur.lastrowid
