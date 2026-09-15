"""Low-level CRUD for the C3 settings tables. Mirrors src/clients/db.py's
style: functions take a connection, do one thing, and commit individually.

src/settings/service.py is the only module that should import this one
directly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_to_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Flat platform settings
# ---------------------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str, actor: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at,
                                       updated_by = excluded.updated_by
        """,
        (key, value, _now(), actor),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Notification preferences (platform default + per-user override)
# ---------------------------------------------------------------------------


def list_notification_prefs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM notification_preferences ORDER BY kind, pref_key"
    )


def get_notification_pref(conn: sqlite3.Connection, pref_key: str) -> Optional[dict[str, Any]]:
    rows = _rows_to_dicts(conn, "SELECT * FROM notification_preferences WHERE pref_key = ?", (pref_key,))
    return rows[0] if rows else None


def set_firm_default(conn: sqlite3.Connection, pref_key: str, enabled: bool) -> None:
    conn.execute(
        "UPDATE notification_preferences SET firm_default = ? WHERE pref_key = ?",
        (int(enabled), pref_key),
    )
    conn.commit()


def set_firm_mandatory(conn: sqlite3.Connection, pref_key: str, mandatory: bool) -> None:
    conn.execute(
        "UPDATE notification_preferences SET firm_mandatory = ? WHERE pref_key = ?",
        (int(mandatory), pref_key),
    )
    conn.commit()


def upsert_notification_pref(
    conn: sqlite3.Connection, pref_key: str, label: str, kind: str,
    firm_default: bool = False, firm_mandatory: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO notification_preferences
            (pref_key, label, kind, firm_default, firm_mandatory)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(pref_key) DO UPDATE SET
            label = excluded.label,
            kind = excluded.kind,
            firm_default = excluded.firm_default,
            firm_mandatory = excluded.firm_mandatory
        """,
        (pref_key, label, kind, int(firm_default), int(firm_mandatory)),
    )
    conn.commit()


def list_user_overrides(conn: sqlite3.Connection, user_id: int) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT pref_key, enabled FROM notification_user_overrides WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {k: bool(v) for k, v in rows}


def set_user_override(conn: sqlite3.Connection, user_id: int, pref_key: str, enabled: bool) -> None:
    conn.execute(
        """
        INSERT INTO notification_user_overrides (user_id, pref_key, enabled, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, pref_key) DO UPDATE SET
            enabled = excluded.enabled, updated_at = excluded.updated_at
        """,
        (user_id, pref_key, int(enabled), _now()),
    )
    conn.commit()


def clear_user_override(conn: sqlite3.Connection, user_id: int, pref_key: str) -> None:
    conn.execute(
        "DELETE FROM notification_user_overrides WHERE user_id = ? AND pref_key = ?",
        (user_id, pref_key),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Credential / API connections (masked reference + live status only)
# ---------------------------------------------------------------------------


def list_connections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM credential_connections ORDER BY service"
    )


def get_connection(conn: sqlite3.Connection, connection_id: int) -> Optional[dict[str, Any]]:
    rows = _rows_to_dicts(
        conn, "SELECT * FROM credential_connections WHERE connection_id = ?", (connection_id,)
    )
    return rows[0] if rows else None


def add_connection(
    conn: sqlite3.Connection, service: str, masked_ref: str, status: str = "connected",
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO credential_connections (service, masked_ref, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (service, masked_ref, status, now, now),
    )
    conn.commit()
    return cur.lastrowid


def set_connection_status(conn: sqlite3.Connection, connection_id: int, status: str) -> None:
    conn.execute(
        "UPDATE credential_connections SET status = ?, updated_at = ? WHERE connection_id = ?",
        (status, _now(), connection_id),
    )
    conn.commit()


def remove_connection(conn: sqlite3.Connection, connection_id: int) -> None:
    conn.execute("DELETE FROM credential_connections WHERE connection_id = ?", (connection_id,))
    conn.commit()