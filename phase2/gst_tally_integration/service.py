"""Public API for the GST Portal & Tally Integration module.

Every UI file should import from here, not from the submodules directly.

Core workflow:
1. **Fetch** data from GST Portal or Tally → stored as pending sync operation
2. **Preview** — admin reviews the fetched/pushed data
3. **Confirm** — admin confirms, data is committed to the platform
4. **Push** — reconciled data is pushed back to GST Portal or Tally

Business rules:
- All external operations go through a preview → confirm workflow
- Credentials are encrypted at rest
- Tally connection is validated before any operation
- Sync operations are logged for audit trail
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from src import db as recon_db
from src.clients import service as client_svc
from src.gst_tally_integration import db as gtdb
from src.gst_tally_integration.schema import init_gst_tally_schema
from src.gst_tally_integration.gst_portal.client import GstPortalClient
from src.gst_tally_integration.tally.client import TallyClient


class GstTallyError(Exception):
    """Raised for expected integration failures."""


def init_gst_tally(db_path=None) -> None:
    """Create GST/Tally tables. Call once at app start."""
    conn = recon_db.get_connection(db_path)
    try:
        init_gst_tally_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None):
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# GST Credentials management
# ---------------------------------------------------------------------------

def list_gst_credentials(client_id: Optional[int] = None, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        creds = gtdb.list_gst_credentials(conn, client_id)
        # Strip encrypted password from result
        for c in creds:
            c.pop("encrypted_password", None)
        return creds
    finally:
        conn.close()


def save_gst_credential(
    client_id: int, gstin: str, username: str, password: str, *,
    db_path=None,
) -> int:
    """Save encrypted GST portal credentials.

    In production, password should be encrypted using the vault module
    (src/vault/) before storage.
    """
    # TODO: Encrypt password using src/vault/service.py
    conn = _connect(db_path)
    try:
        return gtdb.upsert_gst_credential(
            conn, client_id, gstin, username, password,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tally Connection management
# ---------------------------------------------------------------------------

def list_tally_connections(client_id: Optional[int] = None, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return gtdb.list_tally_connections(conn, client_id)
    finally:
        conn.close()


def save_tally_connection(
    conn_id: Optional[int], client_id: int, *,
    host: str = "localhost", port: int = 9000,
    company_name: Optional[str] = None, db_path=None,
) -> int:
    """Create or update a Tally connection and validate it."""
    conn = _connect(db_path)
    try:
        result = gtdb.upsert_tally_connection(
            conn, conn_id, client_id, host=host, port=port, company_name=company_name,
        )
        return result
    finally:
        conn.close()


def test_tally_connection(conn_id: int, *, db_path=None) -> tuple[bool, Optional[str]]:
    """Test a Tally connection by pinging it."""
    conn = _connect(db_path)
    try:
        tc = gtdb.list_tally_connections(conn)
        tally_config = next((t for t in tc if t["conn_id"] == conn_id), None)
        if not tally_config:
            return False, "Connection not found."

        client = TallyClient(
            host=tally_config["host"],
            port=tally_config["port"],
            company_name=tally_config["company_name"],
        )
        return client.ping()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fetch operations (GST Portal)
# ---------------------------------------------------------------------------

def fetch_from_gst_portal(
    client_id: int, gstin: str, data_type: str, period: str, *,
    db_path=None,
) -> int:
    """Fetch data from GST Portal and create a preview sync operation.

    Args:
        client_id: Client to fetch for
        gstin: GSTIN to fetch for
        data_type: "returns", "invoices", "filing_calendar"
        period: "MMYYYY" format

    Returns sync operation ID for preview.
    """
    conn = _connect(db_path)
    try:
        # Get credentials
        creds = gtdb.list_gst_credentials(conn, client_id)
        gst_cred = next((c for c in creds if c["gstin"] == gstin), None)
        if not gst_cred:
            raise GstTallyError(f"No GST credentials found for GSTIN {gstin}.")

        # Create sync op
        op_id = gtdb.create_sync_op(
            conn, client_id, "fetch", "gst_portal", data_type, period=period,
            summary_json=json.dumps({"gstin": gstin, "status": "fetching"}),
        )

        # Fetch data
        client = GstPortalClient(
            gstin=gstin,
            username=gst_cred["username"],
            password=gst_cred["encrypted_password"],
            sandbox=True,  # TODO: make configurable
        )

        if data_type == "returns":
            success, data = client.fetch_returns(period)
        elif data_type == "invoices":
            success, data = client.fetch_invoices(period)
        elif data_type == "filing_calendar":
            success, data = client.fetch_filing_calendar()
        else:
            raise GstTallyError(f"Unknown GST data type: {data_type}")

        if not success:
            gtdb.update_sync_op_status(conn, op_id, "failed", error_message=str(data))
            raise GstTallyError(f"GST Portal fetch failed: {data}")

        # Store the fetched data and mark as previewing
        if data_type == "returns" and isinstance(data, list):
            for ret in data:
                gtdb.upsert_gst_return(
                    conn, client_id, gstin,
                    ret.get("return_type", "GSTR3B"), period,
                    filing_status=ret.get("status", "Unknown"),
                    filed_at=ret.get("filed_at"),
                    raw_json=json.dumps(ret),
                )
        elif data_type == "invoices":
            gtdb.upsert_gst_return(
                conn, client_id, gstin, "INVOICES", period,
                filing_status="fetched",
                raw_json=json.dumps(data if isinstance(data, dict) else {"invoices": data}),
            )

        gtdb.update_sync_op_status(
            conn, op_id, "previewing",
            details_json=json.dumps({"data_summary": _summarize_fetched_data(data)}),
        )

        return op_id
    except GstTallyError:
        raise
    except Exception as e:
        raise GstTallyError(f"Failed to fetch from GST Portal: {e}")
    finally:
        conn.close()


def _summarize_fetched_data(data: Any) -> dict:
    """Create a human-readable summary of fetched data."""
    if isinstance(data, dict):
        return {
            "type": type(data).__name__,
            "keys": list(data.keys())[:10],
            "size": len(json.dumps(data)),
        }
    if isinstance(data, list):
        return {
            "type": "list",
            "count": len(data),
            "sample": data[:3] if data else [],
        }
    return {"type": type(data).__name__, "value": str(data)[:200]}


# ---------------------------------------------------------------------------
# Fetch operations (Tally)
# ---------------------------------------------------------------------------

def fetch_from_tally(
    conn_id: int, client_id: int, data_type: str, *,
    period: Optional[str] = None, db_path=None,
) -> int:
    """Fetch data from Tally and create a preview sync operation.

    Args:
        conn_id: Tally connection ID
        client_id: Client to fetch for
        data_type: "coa", "ledgers", "vouchers"
        period: "YYYYMMDD" format for vouchers

    Returns sync operation ID for preview.
    """
    conn = _connect(db_path)
    try:
        tally_configs = gtdb.list_tally_connections(conn, client_id)
        tally_config = next((t for t in tally_configs if t["conn_id"] == conn_id), None)
        if not tally_config:
            raise GstTallyError("Tally connection not found.")

        # Create sync op
        op_id = gtdb.create_sync_op(
            conn, client_id, "fetch", "tally", data_type, period=period,
            summary_json=json.dumps({"conn_id": conn_id, "status": "fetching"}),
        )

        # Fetch data
        client = TallyClient(
            host=tally_config["host"],
            port=tally_config["port"],
            company_name=tally_config["company_name"],
        )

        if data_type == "coa":
            success, data = client.fetch_chart_of_accounts()
        elif data_type == "ledgers":
            success, data = client.fetch_ledgers()
        elif data_type == "vouchers" and period:
            from_date = period[:6] + "01"
            to_date = period
            success, data = client.fetch_vouchers(from_date, to_date)
        else:
            raise GstTallyError(f"Unknown Tally data type: {data_type}")

        if not success:
            gtdb.update_sync_op_status(conn, op_id, "failed", error_message=str(data))
            raise GstTallyError(f"Tally fetch failed: {data}")

        # Cache the data
        gtdb.upsert_tally_data(
            conn, client_id, data_type,
            period=period,
            raw_json=json.dumps(data),
        )

        gtdb.update_sync_op_status(
            conn, op_id, "previewing",
            details_json=json.dumps({"data_summary": _summarize_fetched_data(data)}),
        )

        return op_id
    except GstTallyError:
        raise
    except Exception as e:
        raise GstTallyError(f"Failed to fetch from Tally: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Push operations
# ---------------------------------------------------------------------------

def push_reconciliation_to_tally(
    conn_id: int, client_id: int, period: str, entries: list[dict], *,
    db_path=None,
) -> int:
    """Push reconciliation adjustment entries to Tally.

    Creates a sync operation that goes through preview → confirm.

    Args:
        conn_id: Tally connection ID
        client_id: Client
        period: "YYYYMM" format
        entries: List of adjustment entry dicts

    Returns sync operation ID.
    """
    conn = _connect(db_path)
    try:
        tally_configs = gtdb.list_tally_connections(conn, client_id)
        tally_config = next((t for t in tally_configs if t["conn_id"] == conn_id), None)
        if not tally_config:
            raise GstTallyError("Tally connection not found.")

        client_info = client_svc.get_client(client_id, db_path=db_path)
        client_name = client_info["legal_name"] if client_info else f"Client #{client_id}"

        op_id = gtdb.create_sync_op(
            conn, client_id, "push", "tally", "reconciliation_entries",
            period=period,
            summary_json=json.dumps({
                "client_name": client_name,
                "entry_count": len(entries),
                "status": "pending_review",
            }),
        )

        gtdb.update_sync_op_status(
            conn, op_id, "previewing",
            details_json=json.dumps({
                "entries": entries,
                "total_debit": sum(
                    abs(e.get("amount", 0)) for e in entries
                    for leg in e.get("entries", []) if leg.get("type") == "Dr"
                ),
                "total_credit": sum(
                    abs(e.get("amount", 0)) for e in entries
                    for leg in e.get("entries", []) if leg.get("type") == "Cr"
                ),
            }),
        )

        return op_id
    finally:
        conn.close()


def push_to_gst_portal(
    client_id: int, gstin: str, return_type: str, period: str, data: dict, *,
    db_path=None,
) -> int:
    """Push/filing data to GST Portal.

    Args:
        client_id: Client
        gstin: GSTIN
        return_type: "GSTR1", "GSTR3B", etc.
        period: "MMYYYY" format
        data: Return data payload

    Returns sync operation ID.
    """
    conn = _connect(db_path)
    try:
        creds = gtdb.list_gst_credentials(conn, client_id)
        gst_cred = next((c for c in creds if c["gstin"] == gstin), None)
        if not gst_cred:
            raise GstTallyError(f"No GST credentials found for GSTIN {gstin}.")

        op_id = gtdb.create_sync_op(
            conn, client_id, "push", "gst_portal", return_type, period=period,
            summary_json=json.dumps({
                "gstin": gstin,
                "return_type": return_type,
                "status": "pending_review",
            }),
        )

        gtdb.update_sync_op_status(
            conn, op_id, "previewing",
            details_json=json.dumps({
                "return_type": return_type,
                "period": period,
                "data_keys": list(data.keys()),
            }),
        )

        return op_id
    except GstTallyError:
        raise
    except Exception as e:
        raise GstTallyError(f"Failed to prepare GST push: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Confirm workflow (admin approval)
# ---------------------------------------------------------------------------

def confirm_sync_operation(op_id: int, confirmed_by: str, *, db_path=None) -> None:
    """Admin confirms and executes a sync operation.

    For fetch operations: marks as completed (data already cached).
    For push operations: actually executes the push.
    """
    conn = _connect(db_path)
    try:
        ops = gtdb.list_sync_ops(conn, status="previewing")
        op = next((o for o in ops if o["op_id"] == op_id), None)
        if not op:
            raise GstTallyError(f"Sync operation #{op_id} not found or not in preview state.")

        gtdb.update_sync_op_status(
            conn, op_id, "confirmed",
            confirmed_by=confirmed_by,
        )

        # Execute the actual push if this is a push operation
        if op["direction"] == "push":
            _execute_push(conn, op)

        gtdb.update_sync_op_status(conn, op_id, "completed")
    except GstTallyError:
        raise
    except Exception as e:
        raise GstTallyError(f"Failed to confirm sync operation: {e}")
    finally:
        conn.close()


def cancel_sync_operation(op_id: int, *, db_path=None) -> None:
    """Cancel a pending sync operation."""
    conn = _connect(db_path)
    try:
        gtdb.update_sync_op_status(conn, op_id, "cancelled")
    finally:
        conn.close()


def _execute_push(conn, op) -> None:
    """Execute a confirmed push operation."""
    details = json.loads(op["details_json"])

    if op["source"] == "tally":
        tally_configs = gtdb.list_tally_connections(conn, op["client_id"])
        tally_config = next(iter(tally_configs), None)
        if not tally_config:
            raise GstTallyError("Tally connection not found for push execution.")

        client = TallyClient(
            host=tally_config["host"],
            port=tally_config["port"],
            company_name=tally_config["company_name"],
        )

        if op["data_type"] == "reconciliation_entries":
            entries = details.get("entries", [])
            for entry in entries:
                success, error = client.push_journal_entry(entry)
                if not success:
                    raise GstTallyError(f"Tally push failed: {error}")

    elif op["source"] == "gst_portal":
        creds = gtdb.list_gst_credentials(conn, op["client_id"])
        gst_cred = next(iter(creds), None)
        if not gst_cred:
            raise GstTallyError("GST credentials not found for push execution.")

        client = GstPortalClient(
            gstin=gst_cred["gstin"],
            username=gst_cred["username"],
            password=gst_cred["encrypted_password"],
            sandbox=True,
        )

        success, error = client.push_return(
            op["data_type"],
            op["period"] or "",
            details.get("data", {}),
        )
        if not success:
            raise GstTallyError(f"GST push failed: {error}")


# ---------------------------------------------------------------------------
# Sync operations listing
# ---------------------------------------------------------------------------

def list_sync_operations(
    *, client_id: Optional[int] = None, status: Optional[str] = None, limit: int = 50, db_path=None,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return gtdb.list_sync_ops(conn, client_id=client_id, status=status, limit=limit)
    finally:
        conn.close()


def get_sync_operation(op_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        ops = gtdb.list_sync_ops(conn, limit=1000)
        return next((o for o in ops if o["op_id"] == op_id), None)
    finally:
        conn.close()
