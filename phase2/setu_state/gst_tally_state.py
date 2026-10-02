"""Reflex state for the GST Portal & Tally Integration module.

Manages the preview → confirm workflow for external data integration.
"""

from __future__ import annotations

import json
import reflex as rx
from typing import Optional

from src.clients import service as client_svc
from src.gst_tally_integration import service as gt_svc


class GstTallyState(rx.State):
    """State for GST Portal & Tally integration."""

    # ---- Client selection ----
    selected_client_id: int = 0
    selected_client_name: str = ""
    clients: list[dict] = []

    # ---- Branch/GSTIN selection ----
    client_branches: list[dict] = []
    selected_gstin: str = ""
    selected_branch_id: int = 0

    # ---- GST credentials ----
    gst_credentials: list[dict] = []
    cred_gstin: str = ""
    cred_username: str = ""
    cred_password: str = ""

    # ---- Tally connections ----
    tally_connections: list[dict] = []
    tally_host: str = "localhost"
    tally_port: str = "9000"
    tally_company: str = ""
    tally_conn_id: int = 0

    # ---- Sync operations ----
    sync_operations: list[dict] = []
    pending_ops: list[dict] = []
    selected_op: Optional[dict] = None
    op_details: dict = {}

    # ---- Fetch parameters ----
    fetch_period: str = ""
    fetch_data_type: str = "returns"  # returns, invoices, filing_calendar
    tally_data_type: str = "coa"  # coa, ledgers, vouchers

    # ---- Preview / Confirm ----
    preview_data: dict = {}
    preview_summary: str = ""

    # ---- Push to Tally ----
    push_entries: str = ""  # JSON text for entries
    push_period: str = ""

    # ---- UI state ----
    flash: str = ""
    is_loading: bool = False
    active_tab: str = "fetch_gst"

    def load_clients(self):
        self.clients = client_svc.list_clients(include_inactive=False)

    def select_client(self, client_id: int):
        self.selected_client_id = client_id
        client = client_svc.get_client(client_id)
        if client:
            self.selected_client_name = client["legal_name"]

        # Load branches
        self.client_branches = client_svc.list_branches(client_id, include_inactive=False)
        if self.client_branches:
            self.selected_gstin = self.client_branches[0]["gstin"]
            self.selected_branch_id = self.client_branches[0]["branch_id"]

        # Load credentials and connections
        self._refresh_creds_and_connections()

    def _refresh_creds_and_connections(self):
        if self.selected_client_id:
            self.gst_credentials = gt_svc.list_gst_credentials(self.selected_client_id)
            self.tally_connections = gt_svc.list_tally_connections(self.selected_client_id)

    def select_gstin(self, gstin: str):
        self.selected_gstin = gstin

    def set_fetch_period(self, value: str):
        self.fetch_period = value

    def set_fetch_data_type(self, value: str):
        self.fetch_data_type = value

    def set_tally_data_type(self, value: str):
        self.tally_data_type = value

    def set_active_tab(self, value: str):
        self.active_tab = value

    # ---- GST credentials ----

    def set_cred_gstin(self, value: str):
        self.cred_gstin = value

    def set_cred_username(self, value: str):
        self.cred_username = value

    def set_cred_password(self, value: str):
        self.cred_password = value

    def save_gst_credentials(self):
        if not self.selected_client_id or not self.cred_gstin or not self.cred_username:
            self.flash = "GSTIN, username, and password are required."
            return
        try:
            gt_svc.save_gst_credential(
                self.selected_client_id,
                self.cred_gstin,
                self.cred_username,
                self.cred_password,
            )
            self._refresh_creds_and_connections()
            self.flash = f"GST credentials saved for {self.cred_gstin}"
        except Exception as e:
            self.flash = f"Failed to save credentials: {e}"

    # ---- Tally connections ----

    def set_tally_host(self, value: str):
        self.tally_host = value

    def set_tally_port(self, value: str):
        self.tally_port = value

    def set_tally_company(self, value: str):
        self.tally_company = value

    def save_tally_connection(self):
        if not self.selected_client_id:
            self.flash = "Please select a client first."
            return
        try:
            conn_id = gt_svc.save_tally_connection(
                None,
                self.selected_client_id,
                host=self.tally_host,
                port=int(self.tally_port),
                company_name=self.tally_company or None,
            )
            self._refresh_creds_and_connections()
            self.tally_conn_id = conn_id
            self.flash = "Tally connection saved."
        except Exception as e:
            self.flash = f"Failed to save Tally connection: {e}"

    def test_tally_connection(self):
        if not self.tally_conn_id and not self.tally_connections:
            self.flash = "No Tally connection to test. Save one first."
            return
        conn_id = self.tally_conn_id or self.tally_connections[0]["conn_id"]
        try:
            success, error = gt_svc.test_tally_connection(conn_id)
            if success:
                self.flash = "✅ Tally connection successful!"
            else:
                self.flash = f"❌ Tally connection failed: {error}"
        except Exception as e:
            self.flash = f"Test failed: {e}"

    # ---- Fetch from GST Portal ----

    def fetch_gst_data(self):
        if not self.selected_client_id or not self.selected_gstin:
            self.flash = "Please select a client and GSTIN."
            return
        if not self.fetch_period:
            self.flash = "Please enter a period (MMYYYY format, e.g. 082026)."
            return

        self.is_loading = True
        try:
            op_id = gt_svc.fetch_from_gst_portal(
                self.selected_client_id,
                self.selected_gstin,
                self.fetch_data_type,
                self.fetch_period,
            )
            self.flash = f"Data fetched! Sync operation #{op_id} is ready for preview."
            self._load_pending_ops()
        except Exception as e:
            self.flash = f"Fetch failed: {e}"
        finally:
            self.is_loading = False

    # ---- Fetch from Tally ----

    def fetch_tally_data(self):
        if not self.selected_client_id:
            self.flash = "Please select a client."
            return
        conn_id = self.tally_conn_id or (
            self.tally_connections[0]["conn_id"] if self.tally_connections else None
        )
        if not conn_id:
            self.flash = "No Tally connection configured. Save one first."
            return

        period = None
        if self.tally_data_type == "vouchers" and self.fetch_period:
            period = self.fetch_period

        self.is_loading = True
        try:
            op_id = gt_svc.fetch_from_tally(
                conn_id,
                self.selected_client_id,
                self.tally_data_type,
                period=period,
            )
            self.flash = f"Tally data fetched! Sync operation #{op_id} is ready for preview."
            self._load_pending_ops()
        except Exception as e:
            self.flash = f"Tally fetch failed: {e}"
        finally:
            self.is_loading = False

    # ---- Preview / Confirm / Cancel ----

    def _load_pending_ops(self):
        self.pending_ops = gt_svc.list_sync_operations(status="previewing")
        self.sync_operations = gt_svc.list_sync_operations(limit=50)

    def preview_op(self, op_id: int):
        op = gt_svc.get_sync_operation(op_id)
        if op:
            self.selected_op = op
            self.op_details = json.loads(op["details_json"])
            self.preview_summary = json.dumps(self.op_details, indent=2)

    def confirm_op(self, op_id: int):
        from setu.state import AuthState
        username = getattr(AuthState, "username", "admin")
        try:
            gt_svc.confirm_sync_operation(op_id, confirmed_by=username)
            self.flash = f"Sync operation #{op_id} confirmed and executed."
            self._load_pending_ops()
            self.selected_op = None
        except Exception as e:
            self.flash = f"Confirmation failed: {e}"

    def cancel_op(self, op_id: int):
        try:
            gt_svc.cancel_sync_operation(op_id)
            self.flash = f"Sync operation #{op_id} cancelled."
            self._load_pending_ops()
            self.selected_op = None
        except Exception as e:
            self.flash = f"Cancel failed: {e}"

    def load_sync_operations(self):
        self._load_pending_ops()
        self.sync_operations = gt_svc.list_sync_operations(limit=50)

    # ---- Push to Tally ----

    def set_push_period(self, value: str):
        self.push_period = value

    def set_push_entries(self, value: str):
        self.push_entries = value

    def push_to_tally(self):
        if not self.selected_client_id:
            self.flash = "Please select a client."
            return
        if not self.push_entries:
            self.flash = "Please provide reconciliation entries in JSON format."
            return

        conn_id = self.tally_conn_id or (
            self.tally_connections[0]["conn_id"] if self.tally_connections else None
        )
        if not conn_id:
            self.flash = "No Tally connection configured."
            return

        try:
            entries = json.loads(self.push_entries)
            if isinstance(entries, dict):
                entries = [entries]  # single entry wrapped in list
            elif not isinstance(entries, list):
                raise ValueError("Entries must be a JSON list or object.")

            op_id = gt_svc.push_reconciliation_to_tally(
                conn_id,
                self.selected_client_id,
                self.push_period,
                entries,
            )
            self.flash = f"Push prepared! Sync operation #{op_id} is ready for admin review."
            self._load_pending_ops()
        except json.JSONDecodeError:
            self.flash = "Invalid JSON format for entries."
        except Exception as e:
            self.flash = f"Push failed: {e}"

    def clear_flash(self):
        self.flash = ""
