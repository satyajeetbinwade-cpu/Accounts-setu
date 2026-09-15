"""Low-level CRUD for the C4 vault tables. Mirrors src/settings/db.py's
style: functions take a connection, do one thing, and commit individually.

src/vault/service.py is the only module that should import this one
directly. No function here ever handles a plaintext secret — that only
happens in src/vault/crypto.py, called from service.py.
"""

from __future__ import annotations

import json
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
# Encrypted credentials
# ---------------------------------------------------------------------------


def list_credentials(conn: sqlite3.Connection, *, client_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Never selects the ciphertext column \u2014 callers that only need to
    render a row (service/label/masked_ref/status) should use this rather
    than get_credential(), which does return ciphertext for decrypt-time use."""
    if client_id is not None:
        return _rows_to_dicts(
            conn,
            """
            SELECT credential_id, service, label, masked_ref, status, client_id,
                   created_by, created_at, updated_at, is_active
            FROM encrypted_credentials
            WHERE is_active = 1 AND client_id = ?
            ORDER BY service
            """,
            (client_id,),
        )
    return _rows_to_dicts(
        conn,
        """
        SELECT credential_id, service, label, masked_ref, status, client_id,
               created_by, created_at, updated_at, is_active
        FROM encrypted_credentials
        WHERE is_active = 1
        ORDER BY service
        """,
    )


def get_credential(conn: sqlite3.Connection, credential_id: int) -> Optional[dict[str, Any]]:
    rows = _rows_to_dicts(
        conn, "SELECT * FROM encrypted_credentials WHERE credential_id = ?", (credential_id,)
    )
    return rows[0] if rows else None


def add_credential(
    conn: sqlite3.Connection, service: str, label: Optional[str], masked_ref: str,
    ciphertext: str, *, client_id: Optional[int], created_by: str, status: str = "connected",
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO encrypted_credentials
            (service, label, masked_ref, ciphertext, status, client_id,
             created_by, created_at, updated_at, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (service, label, masked_ref, ciphertext, status, client_id, created_by, now, now),
    )
    conn.commit()
    return cur.lastrowid


def rotate_credential(
    conn: sqlite3.Connection, credential_id: int, new_masked_ref: str, new_ciphertext: str,
) -> None:
    """Overwrite the ciphertext/masked_ref in place \u2014 the old value is
    never retained anywhere once this commits."""
    conn.execute(
        """
        UPDATE encrypted_credentials
        SET masked_ref = ?, ciphertext = ?, status = 'connected', updated_at = ?
        WHERE credential_id = ?
        """,
        (new_masked_ref, new_ciphertext, _now(), credential_id),
    )
    conn.commit()


def set_credential_status(conn: sqlite3.Connection, credential_id: int, status: str) -> None:
    conn.execute(
        "UPDATE encrypted_credentials SET status = ?, updated_at = ? WHERE credential_id = ?",
        (status, _now(), credential_id),
    )
    conn.commit()


def deactivate_credential(conn: sqlite3.Connection, credential_id: int) -> None:
    conn.execute(
        "UPDATE encrypted_credentials SET is_active = 0, updated_at = ? WHERE credential_id = ?",
        (_now(), credential_id),
    )
    conn.commit()


def record_rotation(conn: sqlite3.Connection, credential_id: int, rotated_by: str) -> None:
    conn.execute(
        "INSERT INTO credential_rotation_history (credential_id, rotated_by, rotated_at) VALUES (?, ?, ?)",
        (credential_id, rotated_by, _now()),
    )
    conn.commit()


def list_rotation_history(conn: sqlite3.Connection, credential_id: Optional[int] = None, limit: int = 200) -> list[dict[str, Any]]:
    if credential_id is not None:
        return _rows_to_dicts(
            conn,
            """
            SELECT h.id, h.credential_id, c.service, c.label, h.rotated_by, h.rotated_at
            FROM credential_rotation_history h
            JOIN encrypted_credentials c ON c.credential_id = h.credential_id
            WHERE h.credential_id = ?
            ORDER BY h.id DESC LIMIT ?
            """,
            (credential_id, limit),
        )
    return _rows_to_dicts(
        conn,
        """
        SELECT h.id, h.credential_id, c.service, c.label, h.rotated_by, h.rotated_at
        FROM credential_rotation_history h
        JOIN encrypted_credentials c ON c.credential_id = h.credential_id
        ORDER BY h.id DESC LIMIT ?
        """,
        (limit,),
    )


# ---------------------------------------------------------------------------
# Security events (the real C4 sink)
# ---------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection, event_type: str, *,
    actor: Optional[str] = None, client_id: Optional[int] = None, detail: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO vault_security_events (event_type, actor, client_id, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, actor, client_id, detail, _now()),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, *, client_id: Optional[int] = None, limit: int = 200) -> list[dict[str, Any]]:
    if client_id is not None:
        return _rows_to_dicts(
            conn,
            "SELECT * FROM vault_security_events WHERE client_id = ? ORDER BY id DESC LIMIT ?",
            (client_id, limit),
        )
    return _rows_to_dicts(
        conn, "SELECT * FROM vault_security_events ORDER BY id DESC LIMIT ?", (limit,)
    )


def event_counts_by_type_since(conn: sqlite3.Connection, since_iso: str) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT event_type, COUNT(*) FROM vault_security_events
        WHERE created_at >= ? GROUP BY event_type
        """,
        (since_iso,),
    ).fetchall()
    return {t: c for t, c in rows}


# ---------------------------------------------------------------------------
# DPDP deletion requests
# ---------------------------------------------------------------------------


def create_dpdp_request(
    conn: sqlite3.Connection, *, client_id: Optional[int], client_label: str, scope: str,
    legal_basis: str, deletable_items: list[str], exempt_items: list[str], submitted_by: str,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO dpdp_deletion_requests
            (client_id, client_label, scope, legal_basis, deletable_items, exempt_items,
             status, submitted_by, submitted_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (client_id, client_label, scope, legal_basis, json.dumps(deletable_items),
         json.dumps(exempt_items), submitted_by, now),
    )
    conn.commit()
    return cur.lastrowid


def list_dpdp_requests(conn: sqlite3.Connection, *, status: Optional[str] = None) -> list[dict[str, Any]]:
    if status is not None:
        rows = _rows_to_dicts(
            conn, "SELECT * FROM dpdp_deletion_requests WHERE status = ? ORDER BY request_id DESC", (status,)
        )
    else:
        rows = _rows_to_dicts(
            conn, "SELECT * FROM dpdp_deletion_requests ORDER BY request_id DESC"
        )
    for r in rows:
        r["deletable_items"] = json.loads(r["deletable_items"])
        r["exempt_items"] = json.loads(r["exempt_items"])
    return rows


def get_dpdp_request(conn: sqlite3.Connection, request_id: int) -> Optional[dict[str, Any]]:
    rows = _rows_to_dicts(
        conn, "SELECT * FROM dpdp_deletion_requests WHERE request_id = ?", (request_id,)
    )
    if not rows:
        return None
    r = rows[0]
    r["deletable_items"] = json.loads(r["deletable_items"])
    r["exempt_items"] = json.loads(r["exempt_items"])
    return r


def decide_dpdp_request(
    conn: sqlite3.Connection, request_id: int, *, status: str, decided_by: str, reason: str,
) -> None:
    now = _now()
    executed_at = now if status == "approved" else None
    conn.execute(
        """
        UPDATE dpdp_deletion_requests
        SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ?, executed_at = ?
        WHERE request_id = ?
        """,
        (status, decided_by, now, reason, executed_at, request_id),
    )
    conn.commit()
