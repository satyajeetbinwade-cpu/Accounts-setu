"""Low-level CRUD for the F2 client tables. Mirrors src/auth/db.py's
style: functions take a connection, do one thing, and commit
individually (client profile actions are independent of any larger
transaction, same pattern as F1's user/role CRUD).

src/clients/service.py is the only module that should import this one
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


def _row_to_dict(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = _rows_to_dicts(conn, sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# EndClient
# ---------------------------------------------------------------------------


def list_clients(conn: sqlite3.Connection, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM end_clients"
    if not include_inactive:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY legal_name"
    return _rows_to_dicts(conn, sql)


def get_client(conn: sqlite3.Connection, client_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM end_clients WHERE client_id = ?", (client_id,))


def create_client(
    conn: sqlite3.Connection, *, legal_name: str, pan: Optional[str], assigned_team: Optional[str],
    created_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO end_clients (legal_name, pan, assigned_team, is_active, created_at, created_by)
        VALUES (?, ?, ?, 1, ?, ?)
        """,
        (legal_name, pan, assigned_team, _now(), created_by),
    )
    conn.commit()
    return cur.lastrowid


def update_client_field(conn: sqlite3.Connection, client_id: int, field: str, value: Any) -> None:
    if field not in ("legal_name", "pan", "assigned_team"):
        raise ValueError(f"Unknown editable client field: {field}")
    conn.execute(f"UPDATE end_clients SET {field} = ? WHERE client_id = ?", (value, client_id))
    conn.commit()


def set_client_active(conn: sqlite3.Connection, client_id: int, is_active: bool) -> None:
    """Soft-delete only \u2014 deactivation preserves full history."""
    conn.execute("UPDATE end_clients SET is_active = ? WHERE client_id = ?", (int(is_active), client_id))
    conn.commit()


# ---------------------------------------------------------------------------
# GSTINBranch
# ---------------------------------------------------------------------------


def list_branches(conn: sqlite3.Connection, client_id: int, *, include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM gstin_branches WHERE client_id = ?"
    if not include_inactive:
        sql += " AND is_active = 1"
    sql += " ORDER BY is_primary DESC, gstin"
    return _rows_to_dicts(conn, sql, (client_id,))


def get_branch(conn: sqlite3.Connection, branch_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM gstin_branches WHERE branch_id = ?", (branch_id,))


def create_branch(
    conn: sqlite3.Connection, *, client_id: int, gstin: str, branch_name: Optional[str],
    address: Optional[str], state: Optional[str], is_primary: bool = False,
) -> int:
    """Adding a branch extends the existing profile \u2014 no re-onboarding
    flow required, per F2's business rule."""
    if is_primary:
        conn.execute("UPDATE gstin_branches SET is_primary = 0 WHERE client_id = ?", (client_id,))
    cur = conn.execute(
        """
        INSERT INTO gstin_branches
            (client_id, gstin, branch_name, address, state, status, is_primary, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, 'Not Started', ?, 1, ?)
        """,
        (client_id, gstin, branch_name, address, state, int(is_primary), _now()),
    )
    conn.commit()
    return cur.lastrowid


def update_branch_field(conn: sqlite3.Connection, branch_id: int, field: str, value: Any) -> None:
    if field not in ("gstin", "branch_name", "address", "state", "status"):
        raise ValueError(f"Unknown editable branch field: {field}")
    conn.execute(f"UPDATE gstin_branches SET {field} = ? WHERE branch_id = ?", (value, branch_id))
    conn.commit()


def set_branch_active(conn: sqlite3.Connection, branch_id: int, is_active: bool) -> None:
    """Deactivation is always available regardless of open reconciliation
    work \u2014 only hard delete is gated (see branch_has_open_recon_work)."""
    conn.execute("UPDATE gstin_branches SET is_active = ? WHERE branch_id = ?", (int(is_active), branch_id))
    conn.commit()


def delete_branch(conn: sqlite3.Connection, branch_id: int) -> None:
    """Hard-delete. Caller (service layer) must confirm no open
    reconciliation work references this branch first \u2014 that check is
    structural now (Module 2 doesn't exist yet, so it always passes) and
    will activate correctly once Module 2 is built."""
    conn.execute("DELETE FROM gstin_branches WHERE branch_id = ?", (branch_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# ContactDirectoryEntry (internal-reference only, not linked to logins)
# ---------------------------------------------------------------------------


def list_contacts(conn: sqlite3.Connection, client_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM contact_directory WHERE client_id = ? ORDER BY name", (client_id,)
    )


def get_contact(conn: sqlite3.Connection, contact_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM contact_directory WHERE contact_id = ?", (contact_id,))


def create_contact(
    conn: sqlite3.Connection, *, client_id: int, name: str, role_title: Optional[str],
    email: Optional[str], phone: Optional[str], notes: Optional[str],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO contact_directory (client_id, name, role_title, email, phone, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (client_id, name, role_title, email, phone, notes, _now()),
    )
    conn.commit()
    return cur.lastrowid


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> None:
    conn.execute("DELETE FROM contact_directory WHERE contact_id = ?", (contact_id,))
    conn.commit()


def update_contact_field(conn: sqlite3.Connection, contact_id: int, field: str, value: Any) -> None:
    if field not in ("name", "role_title", "email", "phone", "notes"):
        raise ValueError(f"Unknown editable contact field: {field}")
    conn.execute(f"UPDATE contact_directory SET {field} = ? WHERE contact_id = ?", (value, contact_id))
    conn.commit()


# ---------------------------------------------------------------------------
# ChartOfAccounts (manual entry, bulk paste/import primary path)
# ---------------------------------------------------------------------------


def list_coa(conn: sqlite3.Connection, client_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn, "SELECT * FROM chart_of_accounts WHERE client_id = ? ORDER BY code", (client_id,)
    )


def bulk_insert_coa(conn: sqlite3.Connection, client_id: int, rows: list[tuple[str, str, str]]) -> int:
    now = _now()
    for code, name, account_type in rows:
        conn.execute(
            """
            INSERT INTO chart_of_accounts (client_id, code, name, account_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (client_id, code, name, account_type, now),
        )
    conn.commit()
    return len(rows)


def add_coa_row(conn: sqlite3.Connection, client_id: int, code: str, name: str, account_type: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO chart_of_accounts (client_id, code, name, account_type, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (client_id, code, name, account_type, _now()),
    )
    conn.commit()
    return cur.lastrowid


def delete_coa_row(conn: sqlite3.Connection, coa_id: int) -> None:
    conn.execute("DELETE FROM chart_of_accounts WHERE coa_id = ?", (coa_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# HistoricalSnapshot (same paste/import pattern as chart of accounts)
# ---------------------------------------------------------------------------


def list_snapshots(conn: sqlite3.Connection, client_id: int, fiscal_year: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM historical_snapshots WHERE client_id = ?"
    params: list[Any] = [client_id]
    if fiscal_year:
        sql += " AND fiscal_year = ?"
        params.append(fiscal_year)
    sql += " ORDER BY fiscal_year DESC, line_item"
    return _rows_to_dicts(conn, sql, tuple(params))


def list_snapshot_years(conn: sqlite3.Connection, client_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT fiscal_year FROM historical_snapshots WHERE client_id = ? ORDER BY fiscal_year DESC",
        (client_id,),
    ).fetchall()
    return [r[0] for r in rows]


def bulk_insert_snapshot(
    conn: sqlite3.Connection, client_id: int, fiscal_year: str, rows: list[tuple[str, str, str]]
) -> int:
    now = _now()
    for line_item, amount, notes in rows:
        conn.execute(
            """
            INSERT INTO historical_snapshots (client_id, fiscal_year, line_item, amount, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (client_id, fiscal_year, line_item, amount, notes, now),
        )
    conn.commit()
    return len(rows)


def add_snapshot_row(
    conn: sqlite3.Connection, client_id: int, fiscal_year: str, line_item: str, amount: str, notes: str
) -> int:
    cur = conn.execute(
        """
        INSERT INTO historical_snapshots (client_id, fiscal_year, line_item, amount, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (client_id, fiscal_year, line_item, amount, notes, _now()),
    )
    conn.commit()
    return cur.lastrowid


def delete_snapshot_row(conn: sqlite3.Connection, snapshot_id: int) -> None:
    conn.execute("DELETE FROM historical_snapshots WHERE snapshot_id = ?", (snapshot_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Edit history — real F4 entries
# ---------------------------------------------------------------------------
# F4 retrofit: this used to write F2's local `client_edit_log` stub. Per the
# F4 build prompt ("F2 — profile field edits, with GSTIN/PAN correctly
# flagged mandatory-reason. REPLACE the local stub"), it now records real,
# immutable F4 Edit History entries. Signature kept identical so existing
# call sites are unchanged; the local table is retained (empty) but unwritten.


def log_edit(
    conn: sqlite3.Connection, *, client_id: Optional[int], branch_id: Optional[int], field: str,
    old_value: Optional[str], new_value: Optional[str], reason: Optional[str], changed_by: str,
) -> None:
    from src.f4 import service as f4  # local import avoids a hard circular dep

    # Branch edits are client-scoped too (the client_id is passed through by
    # the caller for a branch edit); record against the specific record that
    # was actually edited.
    if branch_id is not None:
        f4.record_edit(
            record_type="branch", record_id=branch_id, client_id=client_id,
            field=field, old_value=old_value, new_value=new_value,
            reason=reason, actor=changed_by,
        )
    else:
        f4.record_edit(
            record_type="client", record_id=client_id, client_id=client_id,
            field=field, old_value=old_value, new_value=new_value,
            reason=reason, actor=changed_by,
        )


def list_edit_log(conn: sqlite3.Connection, client_id: int, limit: int = 100) -> list[dict[str, Any]]:
    """Read F2's client-scoped edits out of F4's cross-cutting log."""
    from src.f4 import service as f4  # local import avoids a hard circular dep

    rows = [
        r for r in f4.history_for_client(client_id)
        if r["record_type"] in ("client", "branch")
    ][:limit]
    return [
        {
            "client_id": client_id,
            "field": r["field"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "reason": r["reason"],
            "changed_by": r["changed_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
