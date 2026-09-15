"""Public API for the F2 Client Profile & Master Data module \u2014 every
other module (and every UI file) should import from here, not from
src.clients.db directly. Mirrors src/auth/service.py's shape.

Covers: DB init, EndClient/GSTINBranch/Contact/ChartOfAccounts/
HistoricalSnapshot CRUD, the business rules from the F2 build prompt
(branch delete gated on open recon work \u2014 structural hook only until
Module 2 exists; GSTIN/PAN edits gated to Manager+ with a captured
reason; soft-delete-only client deactivation), and the F1 retrofit that
provisions a shared End-Client login from this module's data.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.auth import service as auth
from src.clients import db as cdb
from src.clients.schema import init_clients_schema


class ClientError(Exception):
    """Raised for expected client-module failures (validation, blocked action)."""


def init_clients(db_path=None) -> None:
    """Create F2 tables. Call once at app start, alongside db.init_db()
    and auth.init_auth()."""
    conn = recon_db.get_connection(db_path)
    try:
        init_clients_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# EndClient
# ---------------------------------------------------------------------------


def list_clients(*, include_inactive: bool = True, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        clients = cdb.list_clients(conn, include_inactive=include_inactive)
        for c in clients:
            branches = cdb.list_branches(conn, c["client_id"], include_inactive=False)
            primary = next((b for b in branches if b["is_primary"]), None)
            c["primary_gstin"] = (primary or (branches[0] if branches else {})).get("gstin")
            c["branch_count"] = len(branches)
        return clients
    finally:
        conn.close()


def get_client(client_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.get_client(conn, client_id)
    finally:
        conn.close()


def create_client(
    *, legal_name: str, pan: Optional[str], assigned_team: Optional[str],
    initial_gstin: Optional[str] = None, initial_state: Optional[str] = None,
    actor: str, db_path=None,
) -> int:
    """Create a client profile. If an initial GSTIN is given, it becomes
    the primary branch \u2014 adding further branches later extends this
    same profile, no re-onboarding flow required."""
    if not legal_name:
        raise ClientError("Legal name is required.")
    conn = _connect(db_path)
    try:
        client_id = cdb.create_client(
            conn, legal_name=legal_name, pan=pan, assigned_team=assigned_team, created_by=actor,
        )
        if initial_gstin:
            cdb.create_branch(
                conn, client_id=client_id, gstin=initial_gstin, branch_name=None,
                address=None, state=initial_state, is_primary=True,
            )
        return client_id
    finally:
        conn.close()


def update_client_field(
    client_id: int, field: str, value: Any, *, actor: str, reason: Optional[str] = None, db_path=None,
) -> None:
    """Editing legal_name/assigned_team is unrestricted. Editing `pan`
    requires Manager+ (clients.gstin_pan.edit) and a captured reason \u2014
    enforced here, not just in the UI."""
    conn = _connect(db_path)
    try:
        before = cdb.get_client(conn, client_id)
        old_value = before.get(field) if before else None
        if field == "pan":
            _require_gstin_pan_permission(actor, db_path=db_path)
            if not reason:
                raise ClientError("A reason is required before saving a PAN change.")
            cdb.update_client_field(conn, client_id, field, value)
            cdb.log_edit(
                conn, client_id=client_id, branch_id=None, field=field,
                old_value=old_value, new_value=value, reason=reason, changed_by=actor,
            )
        else:
            cdb.update_client_field(conn, client_id, field, value)
    finally:
        conn.close()


def set_client_active(client_id: int, is_active: bool, *, actor: str, db_path=None) -> None:
    """Soft-delete only \u2014 deactivating preserves full history. There is
    no hard-delete path for an EndClient in this build."""
    conn = _connect(db_path)
    try:
        cdb.set_client_active(conn, client_id, is_active)
    finally:
        conn.close()


def _require_gstin_pan_permission(actor: str, *, db_path=None) -> None:
    """GSTIN/PAN edits require Manager-level permission or above (F2's
    business rule), enforced via F1's permission check."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT u.* , r.name AS role_name FROM users u JOIN roles r ON r.role_id = u.role_id "
            "WHERE u.username = ?",
            (actor,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ClientError("Could not resolve the current user for a permission check.")
    cols = ["user_id", "username", "display_name", "email", "password_salt", "password_hash",
            "role_id", "is_active", "is_end_client", "end_client_ref", "created_at",
            "last_login_at", "role_name"]
    user = dict(zip(cols, row))
    if not auth.has_permission(user, "clients.gstin_pan.edit", db_path=db_path):
        raise ClientError("GSTIN/PAN edits require Manager-level permission or above.")


# ---------------------------------------------------------------------------
# GSTINBranch
# ---------------------------------------------------------------------------


def list_branches(client_id: int, *, include_inactive: bool = True, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_branches(conn, client_id, include_inactive=include_inactive)
    finally:
        conn.close()


def get_branch(branch_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.get_branch(conn, branch_id)
    finally:
        conn.close()


def add_branch(
    client_id: int, *, gstin: str, branch_name: Optional[str], address: Optional[str],
    state: Optional[str], is_primary: bool = False, actor: str, db_path=None,
) -> int:
    """Adding a branch/GSTIN extends the existing profile \u2014 no
    re-onboarding flow required, per F2's business rule."""
    if not gstin:
        raise ClientError("GSTIN is required.")
    conn = _connect(db_path)
    try:
        return cdb.create_branch(
            conn, client_id=client_id, gstin=gstin, branch_name=branch_name,
            address=address, state=state, is_primary=is_primary,
        )
    finally:
        conn.close()


def update_branch_field(
    branch_id: int, field: str, value: Any, *, actor: str, reason: Optional[str] = None, db_path=None,
) -> None:
    """Editing status/branch_name/address/state is unrestricted. Editing
    `gstin` requires Manager+ and a captured reason (same rule as PAN)."""
    conn = _connect(db_path)
    try:
        before = cdb.get_branch(conn, branch_id)
        old_value = before.get(field) if before else None
        if field == "gstin":
            _require_gstin_pan_permission(actor, db_path=db_path)
            if not reason:
                raise ClientError("A reason is required before saving a GSTIN change.")
            cdb.update_branch_field(conn, branch_id, field, value)
            cdb.log_edit(
                conn, client_id=before.get("client_id") if before else None, branch_id=branch_id,
                field=field, old_value=old_value, new_value=value, reason=reason, changed_by=actor,
            )
        else:
            cdb.update_branch_field(conn, branch_id, field, value)
    finally:
        conn.close()


def set_branch_active(branch_id: int, is_active: bool, *, actor: str, db_path=None) -> None:
    """Deactivate is always available, regardless of open reconciliation
    work \u2014 only the hard-delete path below is gated."""
    conn = _connect(db_path)
    try:
        cdb.set_branch_active(conn, branch_id, is_active)
    finally:
        conn.close()


def branch_has_open_recon_work(branch_id: int, *, db_path=None) -> bool:
    """Structural constraint hook: a branch cannot be hard-deleted while
    open reconciliation work references it. Module 2 (the Reconciliation
    Engine) doesn't exist yet in this build, so there is currently no way
    for any GSTIN to have open recon work \u2014 this always returns False
    for now, and will activate correctly (return True where applicable)
    once Module 2 is built and can be queried here."""
    return False


def can_delete_branch(branch_id: int, *, db_path=None) -> tuple[bool, Optional[str]]:
    """Returns (allowed, reason_if_blocked)."""
    if branch_has_open_recon_work(branch_id, db_path=db_path):
        return False, "Open reconciliation work references this GSTIN/branch."
    # Deliberately disabled in THIS build regardless of the check above,
    # per F2's design table: "Delete ... disabled today (Module 2 doesn't
    # exist yet) with its inline reason visible, not hidden."
    return False, "Delete isn't available yet \u2014 the Reconciliation Engine (Module 2) doesn't exist yet."


def delete_branch(branch_id: int, *, actor: str, db_path=None) -> None:
    allowed, reason = can_delete_branch(branch_id, db_path=db_path)
    if not allowed:
        raise ClientError(reason or "This branch can't be deleted right now.")
    conn = _connect(db_path)
    try:
        cdb.delete_branch(conn, branch_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ContactDirectoryEntry (internal-reference only)
# ---------------------------------------------------------------------------


def list_contacts(client_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_contacts(conn, client_id)
    finally:
        conn.close()


def add_contact(
    client_id: int, *, name: str, role_title: Optional[str], email: Optional[str],
    phone: Optional[str], notes: Optional[str], actor: str, db_path=None,
) -> int:
    if not name:
        raise ClientError("Contact name is required.")
    conn = _connect(db_path)
    try:
        return cdb.create_contact(
            conn, client_id=client_id, name=name, role_title=role_title,
            email=email, phone=phone, notes=notes,
        )
    finally:
        conn.close()


def remove_contact(contact_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        cdb.delete_contact(conn, contact_id)
    finally:
        conn.close()


def update_contact_field(contact_id: int, field: str, value: Any, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        cdb.update_contact_field(conn, contact_id, field, value)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ChartOfAccounts
# ---------------------------------------------------------------------------


def _parse_bulk_text(text: str) -> list[tuple[str, str, str]]:
    """Parse tab- or comma-separated 'code, name, type' rows, one per
    line. Blank lines are skipped. Rows with fewer than 2 fields are
    skipped (need at least code + name)."""
    text = text.strip()
    if not text:
        return []
    delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[tuple[str, str, str]] = []
    for raw in reader:
        cells = [c.strip() for c in raw]
        if not cells or not cells[0]:
            continue
        code = cells[0]
        name = cells[1] if len(cells) > 1 else ""
        account_type = cells[2] if len(cells) > 2 else ""
        if not name:
            continue
        rows.append((code, name, account_type))
    return rows


def list_coa(client_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_coa(conn, client_id)
    finally:
        conn.close()


def bulk_import_coa(client_id: int, raw_text: str, *, actor: str, db_path=None) -> int:
    rows = _parse_bulk_text(raw_text)
    if not rows:
        raise ClientError("No valid rows found. Expected 'code, name, type' per line (tab or comma separated).")
    conn = _connect(db_path)
    try:
        return cdb.bulk_insert_coa(conn, client_id, rows)
    finally:
        conn.close()


def add_coa_row(client_id: int, code: str, name: str, account_type: str, *, actor: str, db_path=None) -> int:
    if not code or not name:
        raise ClientError("Code and name are required.")
    conn = _connect(db_path)
    try:
        return cdb.add_coa_row(conn, client_id, code, name, account_type)
    finally:
        conn.close()


def delete_coa_row(coa_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        cdb.delete_coa_row(conn, coa_id)
    finally:
        conn.close()


def coa_edit_warning() -> str:
    """HOOK ONLY (per F2's business rule): once C1's rule taxonomy
    exists, editing chart of accounts after C1 rules exist should surface
    a warning that dependent rules may need review. Wired for real once
    C1 is built \u2014 for now this is just the call-out point / message
    text the UI shows unconditionally as a heads-up, not a live check."""
    return (
        "Editing chart-of-accounts entries that regulatory/taxonomy rules depend on "
        "may require those rules to be reviewed. (Hook only \u2014 wires to real rule "
        "dependencies once Rules, Taxonomy & Regulatory Config (C1) is built.)"
    )


# ---------------------------------------------------------------------------
# HistoricalSnapshot (same paste/import pattern as chart of accounts)
# ---------------------------------------------------------------------------


def _parse_snapshot_text(text: str) -> list[tuple[str, str, str]]:
    """Parse 'line_item, amount, notes' rows, tab or comma separated."""
    text = text.strip()
    if not text:
        return []
    delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[tuple[str, str, str]] = []
    for raw in reader:
        cells = [c.strip() for c in raw]
        if not cells or not cells[0]:
            continue
        line_item = cells[0]
        amount = cells[1] if len(cells) > 1 else ""
        notes = cells[2] if len(cells) > 2 else ""
        rows.append((line_item, amount, notes))
    return rows


def list_snapshot_years(client_id: int, *, db_path=None) -> list[str]:
    conn = _connect(db_path)
    try:
        return cdb.list_snapshot_years(conn, client_id)
    finally:
        conn.close()


def list_snapshots(client_id: int, fiscal_year: Optional[str] = None, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_snapshots(conn, client_id, fiscal_year)
    finally:
        conn.close()


def bulk_import_snapshot(client_id: int, fiscal_year: str, raw_text: str, *, actor: str, db_path=None) -> int:
    if not fiscal_year:
        raise ClientError("Fiscal year is required.")
    rows = _parse_snapshot_text(raw_text)
    if not rows:
        raise ClientError("No valid rows found. Expected 'line item, amount, notes' per line (tab or comma separated).")
    conn = _connect(db_path)
    try:
        return cdb.bulk_insert_snapshot(conn, client_id, fiscal_year, rows)
    finally:
        conn.close()


def add_snapshot_row(
    client_id: int, fiscal_year: str, line_item: str, amount: str, notes: str, *, actor: str, db_path=None,
) -> int:
    if not fiscal_year or not line_item:
        raise ClientError("Fiscal year and line item are required.")
    conn = _connect(db_path)
    try:
        return cdb.add_snapshot_row(conn, client_id, fiscal_year, line_item, amount, notes)
    finally:
        conn.close()


def delete_snapshot_row(snapshot_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        cdb.delete_snapshot_row(conn, snapshot_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Edit log (read-only surfacing; local stub, retrofit target for F4)
# ---------------------------------------------------------------------------


def list_edit_log(client_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.list_edit_log(conn, client_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# F1 retrofit: provision the shared End-Client login from this module's
# contact + GSTIN data at client-creation time.
# ---------------------------------------------------------------------------


def provision_end_client_login(
    client_id: int, *, username: str, password: str, contact_id: Optional[int] = None,
    actor: str, db_path=None,
) -> int:
    """Create the shared End-Client login for a client, reading this
    module's own data (contact record + primary GSTIN) instead of the F1
    stub's manual entry. This is the retrofit F1's prompt called for:
    "wire F1's shared End-Client login provisioning to actually read this
    module's contact records and GSTIN data at client-creation time."
    """
    client = get_client(client_id, db_path=db_path)
    if client is None:
        raise ClientError(f"No such client_id {client_id}.")

    display_name = client["legal_name"]
    email = None
    if contact_id is not None:
        conn = _connect(db_path)
        try:
            contact = cdb.get_contact(conn, contact_id)
        finally:
            conn.close()
        if contact:
            display_name = f"{client['legal_name']} \u2014 {contact['name']}"
            email = contact.get("email")

    end_client_ref = str(client_id)
    user_id = auth.create_user(
        username=username, display_name=display_name, email=email, password=password,
        role_id=_end_client_role_id(db_path=db_path), actor=actor,
        is_end_client=True, end_client_ref=end_client_ref, db_path=db_path,
    )
    return user_id


def _end_client_role_id(*, db_path=None) -> int:
    for r in auth.list_roles(db_path=db_path):
        if r["name"] == "End-Client":
            return r["role_id"]
    raise ClientError("The 'End-Client' role is missing \u2014 has F1's seed run?")
