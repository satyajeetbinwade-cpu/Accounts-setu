"""Database operations for GST/Tally Integration module.

Raw SQL, no business logic — matches src/clients/db.py pattern.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional


# ---------------------------------------------------------------------------
# GST credentials
# ---------------------------------------------------------------------------

def list_gst_credentials(conn: sqlite3.Connection, client_id: Optional[int] = None) -> list[dict[str, Any]]:
    if client_id:
        rows = conn.execute(
            "SELECT * FROM gst_credentials WHERE client_id=? AND is_active=1",
            (client_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM gst_credentials WHERE is_active=1").fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(gst_credentials)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def upsert_gst_credential(
    conn: sqlite3.Connection,
    client_id: int, gstin: str, username: str, encrypted_password: str,
) -> int:
    existing = conn.execute(
        "SELECT credential_id FROM gst_credentials WHERE client_id=? AND gstin=?",
        (client_id, gstin),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE gst_credentials SET username=?, encrypted_password=?,
               last_synced_at=datetime('now') WHERE credential_id=?""",
            (username, encrypted_password, existing[0]),
        )
        conn.commit()
        return existing[0]
    cur = conn.execute(
        "INSERT INTO gst_credentials (client_id, gstin, username, encrypted_password) VALUES (?, ?, ?, ?)",
        (client_id, gstin, username, encrypted_password),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Tally connections
# ---------------------------------------------------------------------------

def list_tally_connections(conn: sqlite3.Connection, client_id: Optional[int] = None) -> list[dict[str, Any]]:
    if client_id:
        rows = conn.execute(
            "SELECT * FROM tally_connections WHERE client_id=? AND is_active=1",
            (client_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tally_connections WHERE is_active=1").fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(tally_connections)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def upsert_tally_connection(
    conn: sqlite3.Connection,
    conn_id: Optional[int], client_id: int, *,
    host: str, port: int, company_name: Optional[str],
) -> int:
    if conn_id:
        conn.execute(
            """UPDATE tally_connections SET host=?, port=?, company_name=?,
               last_synced_at=datetime('now') WHERE conn_id=?""",
            (host, port, company_name, conn_id),
        )
        conn.commit()
        return conn_id
    cur = conn.execute(
        "INSERT INTO tally_connections (client_id, host, port, company_name) VALUES (?, ?, ?, ?)",
        (client_id, host, port, company_name),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Sync operations
# ---------------------------------------------------------------------------

def create_sync_op(
    conn: sqlite3.Connection,
    client_id: int, direction: str, source: str, data_type: str,
    *    period: Optional[str] = None,
    summary_json: str = "{}",
) -> int:
    cur = conn.execute(
        """INSERT INTO sync_operations
           (client_id, direction, source, data_type, period, summary_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (client_id, direction, source, data_type, period, summary_json),
    )
    conn.commit()
    return cur.lastrowid


def update_sync_op_status(
    conn: sqlite3.Connection,
    op_id: int, status: str, *,
    details_json: Optional[str] = None,
    error_message: Optional[str] = None,
    confirmed_by: Optional[str] = None,
) -> None:
    fields = ["status=?"]
    params = [status]
    if details_json is not None:
        fields.append("details_json=?")
        params.append(details_json)
    if error_message is not None:
        fields.append("error_message=?")
        params.append(error_message)
    if confirmed_by is not None:
        fields.append("confirmed_by=?")
        params.append(confirmed_by)
    if status == "confirmed":
        fields.append("confirmed_at=datetime('now')")
    if status == "completed":
        fields.append("completed_at=datetime('now')")
    params.append(op_id)
    conn.execute(f"UPDATE sync_operations SET {', '.join(fields)} WHERE op_id=?", params)
    conn.commit()


def list_sync_ops(
    conn: sqlite3.Connection,
    *, client_id: Optional[int] = None, status: Optional[str] = None, limit: int = 50,
) -> list[dict[str, Any]]:
    where = []
    params = []
    if client_id:
        where.append("client_id=?")
        params.append(client_id)
    if status:
        where.append("status=?")
        params.append(status)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM sync_operations {where_clause} ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(sync_operations)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# GST returns cache
# ---------------------------------------------------------------------------

def upsert_gst_return(
    conn: sqlite3.Connection,
    client_id: int, gstin: str, return_type: str, period: str, *,
    filing_status: Optional[str], filed_at: Optional[str], raw_json: str,
) -> None:
    conn.execute(
        """INSERT INTO gst_returns
           (client_id, gstin, return_type, period, filing_status, filed_at, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(client_id, gstin, return_type, period) DO UPDATE SET
               filing_status=excluded.filing_status,
               filed_at=excluded.filed_at,
               raw_json=excluded.raw_json,
               synced_at=datetime('now')""",
        (client_id, gstin, return_type, period, filing_status, filed_at, raw_json),
    )
    conn.commit()


def list_gst_returns(
    conn: sqlite3.Connection,
    client_id: int, *, gstin: Optional[str] = None, period: Optional[str] = None,
) -> list[dict[str, Any]]:
    where = ["client_id=?"]
    params = [client_id]
    if gstin:
        where.append("gstin=?")
        params.append(gstin)
    if period:
        where.append("period=?")
        params.append(period)
    rows = conn.execute(
        f"SELECT * FROM gst_returns WHERE {' AND '.join(where)} ORDER BY period DESC",
        params,
    ).fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(gst_returns)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Tally data cache
# ---------------------------------------------------------------------------

def upsert_tally_data(
    conn: sqlite3.Connection,
    client_id: int, data_type: str, *, period: Optional[str], raw_json: str, raw_xml: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO tally_data_cache (client_id, data_type, period, raw_json, raw_xml) VALUES (?, ?, ?, ?, ?)",
        (client_id, data_type, period, raw_json, raw_xml),
    )
    conn.commit()
    return cur.lastrowid


def list_tally_data(
    conn: sqlite3.Connection,
    client_id: int, *, data_type: Optional[str] = None, limit: int = 50,
) -> list[dict[str, Any]]:
    where = ["client_id=?"]
    params = [client_id]
    if data_type:
        where.append("data_type=?")
        params.append(data_type)
    rows = conn.execute(
        f"SELECT * FROM tally_data_cache WHERE {' AND '.join(where)} ORDER BY synced_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    cols = [d[0] for d in conn.execute("PRAGMA table_info(tally_data_cache)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]
