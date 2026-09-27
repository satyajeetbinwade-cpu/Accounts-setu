"""Public API for the F2 Client Profile & Master Data module \u2014 every
other module (and every UI file) should import from here, not from
src.clients.db directly. Mirrors src/auth/service.py's shape.

Covers: DB init, EndClient/Contact/ChartOfAccounts/HistoricalSnapshot
CRUD, the business rules from the F2 build prompt as revised on
27-Sep-2026 (a flat EndClient \u2014 one GST registration = one client, no
branch sub-entity; legal name required and at least one of PAN/TAN
required, with the primary contact block OPTIONAL; PAN/TAN/GSTIN edits
gated to Manager+ with a captured reason; a GSTIN can't be cleared or
changed while open reconciliation work references it; soft-delete-only
client deactivation), and the F1 retrofit that provisions a shared
End-Client login from this module's data.
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
    """Every EndClient row, straight from the flat table.

    There is no branch dimension to join any more \u2014 `gstin` is a column on
    the client itself.
    """
    conn = _connect(db_path)
    try:
        return cdb.list_clients(conn, include_inactive=include_inactive)
    finally:
        conn.close()


def get_client(client_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return cdb.get_client(conn, client_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Validation \u2014 one place, so create and update can never drift.
# ---------------------------------------------------------------------------

# (column, label) for the fields that must be non-empty on a saved record.
# The primary contact block (email/phone/address) is deliberately OPTIONAL —
# it is collected when the client has it, but never blocks a save.
REQUIRED_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("legal_name", "Legal name"),
)

# Fields that require Manager+ AND a captured reason ("sensitive" in F4's
# vocabulary too). Maps the column to the label used in messages.
SENSITIVE_FIELDS: dict[str, str] = {"pan": "PAN", "tan": "TAN", "gstin": "GSTIN"}

# The Details form's unrestricted fields. The identity fields above are saved
# individually, each behind its own reason capture.
DETAIL_FIELDS: tuple[str, ...] = (
    "legal_name",
    "assigned_team",
    "primary_contact_email",
    "primary_contact_phone",
    "primary_contact_address",
)


def _clean(value: Any) -> Optional[str]:
    """Trim to None so "" and "   " never masquerade as a real value."""
    text = str(value).strip() if value is not None else ""
    return text or None


def validate_client_record(record: dict[str, Any]) -> None:
    """The save rules for a whole client record. Raises ClientError with a
    plain-language message naming exactly what to fix.

    Only two rules: a legal name, and at least one of PAN or TAN. The primary
    contact block is optional.
    """
    missing = [label for key, label in REQUIRED_TEXT_FIELDS if not _clean(record.get(key))]
    if missing:
        raise ClientError("Required field(s) missing: " + ", ".join(missing) + ".")
    if not _clean(record.get("pan")) and not _clean(record.get("tan")):
        raise ClientError("At least one of PAN or TAN is required.")


def _validate_single_field_save(before: dict[str, Any], field: str, new_value: Optional[str]) -> None:
    """The lighter check for a per-field save.

    A single-field save must never BLANK the legal name, and must never leave
    the record with neither PAN nor TAN. It is deliberately NOT asked to
    complete an otherwise-incomplete record — that is the Details form's job,
    and demanding it here would make an identity edit impossible on a client
    whose PAN and TAN were both empty.
    """
    if not new_value:
        for key, label in REQUIRED_TEXT_FIELDS:
            if key == field:
                raise ClientError(f"{label} can't be blank.")
        if field in ("pan", "tan"):
            other = "tan" if field == "pan" else "pan"
            if not _clean(before.get(other)):
                raise ClientError("At least one of PAN or TAN is required.")


def create_client(
    *, legal_name: str, pan: Optional[str] = None, tan: Optional[str] = None,
    gstin: Optional[str] = None, assigned_team: Optional[str] = None,
    primary_contact_email: Optional[str] = None, primary_contact_phone: Optional[str] = None,
    primary_contact_address: Optional[str] = None, actor: str, db_path=None,
) -> int:
    """Create a client. Each GST registration is its OWN client record \u2014
    there is no branch to attach, and no re-onboarding flow for a sibling
    registration (it is simply another client)."""
    record = {
        "legal_name": legal_name,
        "pan": pan,
        "tan": tan,
        "gstin": gstin,
        "assigned_team": assigned_team,
        "primary_contact_email": primary_contact_email,
        "primary_contact_phone": primary_contact_phone,
        "primary_contact_address": primary_contact_address,
    }
    validate_client_record(record)
    conn = _connect(db_path)
    try:
        return cdb.create_client(
            conn,
            legal_name=_clean(legal_name),
            pan=_clean(pan),
            tan=_clean(tan),
            gstin=_clean(gstin),
            assigned_team=_clean(assigned_team),
            primary_contact_email=_clean(primary_contact_email),
            primary_contact_phone=_clean(primary_contact_phone),
            primary_contact_address=_clean(primary_contact_address),
            created_by=actor,
        )
    finally:
        conn.close()


def update_client_field(
    client_id: int, field: str, value: Any, *, actor: str, reason: Optional[str] = None, db_path=None,
) -> None:
    """Save ONE field on the client record.

    - legal_name / assigned_team / the primary contact block: unrestricted,
      but the field can never be blanked.
    - pan / tan / gstin: Manager+ (clients.gstin_pan.edit) AND a captured
      reason \u2014 enforced here, not just in the UI.
    - gstin additionally cannot be cleared or changed while open
      reconciliation work references the client.
    """
    if field not in cdb.EDITABLE_CLIENT_FIELDS:
        raise ClientError(f"'{field}' is not an editable client field.")
    new_value = _clean(value)
    conn = _connect(db_path)
    try:
        before = cdb.get_client(conn, client_id)
        if before is None:
            raise ClientError(f"No such client_id {client_id}.")
        old_value = before.get(field)

        _validate_single_field_save(before, field, new_value)

        if field in SENSITIVE_FIELDS:
            _require_gstin_pan_permission(actor, db_path=db_path)
            if not _clean(reason):
                raise ClientError(
                    f"A reason is required before saving a {SENSITIVE_FIELDS[field]} change."
                )

        if field == "gstin" and _clean(old_value) != new_value:
            allowed, blocked_because = can_change_gstin(client_id, db_path=db_path)
            if not allowed:
                raise ClientError(blocked_because or "This GSTIN can't be changed right now.")

        cdb.update_client_field(conn, client_id, field, new_value)
        if field in SENSITIVE_FIELDS:
            cdb.log_edit(
                conn, client_id=client_id, field=field,
                old_value=old_value, new_value=new_value, reason=reason, changed_by=actor,
                db_path=db_path,
            )
    finally:
        conn.close()


def update_client_details(
    client_id: int, *, actor: str, db_path=None, **fields: Any,
) -> None:
    """Save the Details form's unrestricted fields in ONE action.

    The whole record is re-validated (legal name + at least one of PAN/TAN)
    against the stored row MERGED with the proposed values, so this form can
    never leave a client in a state it can no longer be saved from. The
    primary contact block is optional and may be blank.
    """
    proposed: dict[str, Any] = {}
    conn = _connect(db_path)
    try:
        before = cdb.get_client(conn, client_id)
        if before is None:
            raise ClientError(f"No such client_id {client_id}.")
        proposed = dict(before)
        for name in DETAIL_FIELDS:
            if name in fields:
                proposed[name] = _clean(fields[name])
        validate_client_record(proposed)
        for name in DETAIL_FIELDS:
            if name in fields:
                cdb.update_client_field(conn, client_id, name, proposed[name])
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
        raise ClientError("PAN, TAN and GSTIN edits require Manager-level permission or above.")


# ---------------------------------------------------------------------------
# GSTIN \u2014 the client-level business rule
# ---------------------------------------------------------------------------


def client_has_open_recon_work(client_id: int, *, db_path=None) -> bool:
    """True when open reconciliation work references this client.

    Carried over from the branch model, now scoped to the client record
    directly: a client's GSTIN can't be cleared or changed while the
    Reconciliation Engine (Module 2) still holds OPEN exceptions for it.
    """
    conn = _connect(db_path)
    try:
        return cdb.has_open_recon_work(conn, client_id)
    finally:
        conn.close()


def can_change_gstin(client_id: int, *, db_path=None) -> tuple[bool, Optional[str]]:
    """Returns (allowed, reason_if_blocked) \u2014 the disabled-with-inline-reason
    counterpart of the old branch Delete gate, rendered by Foundation 3.2/3.4."""
    if client_has_open_recon_work(client_id, db_path=db_path):
        return False, (
            "This GSTIN can't be cleared or changed while open reconciliation work "
            "references this client. Resolve those exceptions first."
        )
    return True, None


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
        return cdb.list_edit_log(conn, client_id, db_path=db_path)
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
    module's own data (contact record + the client's GSTIN) instead of the F1
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
