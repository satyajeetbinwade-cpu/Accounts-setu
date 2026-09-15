"""Low-level CRUD for the C1 rules tables. Mirrors src/clients/db.py's
style: functions take a connection, do one thing, and commit individually.

src/rules/service.py is the only module that should import this one
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
# Rule categories
# ---------------------------------------------------------------------------


def list_categories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM rule_categories ORDER BY sort_order, category_id")


def get_category_by_key(conn: sqlite3.Connection, key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM rule_categories WHERE key = ?", (key,))


def upsert_category(
    conn: sqlite3.Connection, key: str, label: str, description: Optional[str], sort_order: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO rule_categories (key, label, description, sort_order)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            label = excluded.label,
            description = excluded.description,
            sort_order = excluded.sort_order
        """,
        (key, label, description, sort_order),
    )
    conn.commit()
    row = conn.execute("SELECT category_id FROM rule_categories WHERE key = ?", (key,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def list_rules(conn: sqlite3.Connection, category_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM rules"
    params: tuple = ()
    if category_id is not None:
        sql += " WHERE category_id = ?"
        params = (category_id,)
    sql += " ORDER BY sort_order, rule_id"
    return _rows_to_dicts(conn, sql, params)


def get_rule_by_key(conn: sqlite3.Connection, key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM rules WHERE key = ?", (key,))


def upsert_rule(
    conn: sqlite3.Connection, *, category_id: int, key: str, label: str, value_type: str,
    unit: Optional[str], high_impact: bool, description: Optional[str], sort_order: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO rules (category_id, key, label, value_type, unit, high_impact, description, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            category_id = excluded.category_id,
            label = excluded.label,
            value_type = excluded.value_type,
            unit = excluded.unit,
            high_impact = excluded.high_impact,
            description = excluded.description,
            sort_order = excluded.sort_order
        """,
        (category_id, key, label, value_type, unit, int(high_impact), description, sort_order),
    )
    conn.commit()
    row = conn.execute("SELECT rule_id FROM rules WHERE key = ?", (key,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Rule versions (append-only, forward-only)
# ---------------------------------------------------------------------------


def insert_rule_version(
    conn: sqlite3.Connection, *, rule_id: int, scope: str, client_id: Optional[int],
    value: str, effective_from: str, created_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO rule_versions (rule_id, scope, client_id, value, effective_from, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (rule_id, scope, client_id, value, effective_from, created_by, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_rule_versions(
    conn: sqlite3.Connection, rule_id: int, scope: Optional[str] = None,
    client_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM rule_versions WHERE rule_id = ?"
    params: list[Any] = [rule_id]
    if scope is not None:
        sql += " AND scope = ?"
        params.append(scope)
    if client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    sql += " ORDER BY effective_from DESC, version_id DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def latest_rule_version(
    conn: sqlite3.Connection, rule_id: int, scope: str, client_id: Optional[int],
) -> Optional[dict[str, Any]]:
    rows = list_rule_versions(conn, rule_id, scope=scope, client_id=client_id)
    return rows[0] if rows else None


def count_client_overrides(conn: sqlite3.Connection, rule_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT client_id) FROM rule_versions WHERE rule_id = ? AND scope = 'client' AND client_id IS NOT NULL",
        (rule_id,),
    ).fetchone()
    return row[0]


def list_overridden_client_ids(conn: sqlite3.Connection, rule_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT client_id FROM rule_versions WHERE rule_id = ? AND scope = 'client' AND client_id IS NOT NULL",
        (rule_id,),
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Taxonomy entries
# ---------------------------------------------------------------------------


def list_taxonomy(conn: sqlite3.Connection, category: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM taxonomy_entries"
    params: tuple = ()
    if category is not None:
        sql += " WHERE category = ?"
        params = (category,)
    sql += " ORDER BY category, sort_order, code"
    return _rows_to_dicts(conn, sql, params)


def get_taxonomy_entry(conn: sqlite3.Connection, entry_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM taxonomy_entries WHERE entry_id = ?", (entry_id,))


def rename_taxonomy_entry(conn: sqlite3.Connection, entry_id: int, label: str) -> None:
    conn.execute("UPDATE taxonomy_entries SET label = ? WHERE entry_id = ?", (label, entry_id))
    conn.commit()


def delete_taxonomy_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM taxonomy_entries WHERE entry_id = ?", (entry_id,))
    conn.commit()


def add_taxonomy_entry(
    conn: sqlite3.Connection, *, category: str, code: str, label: str,
    description: Optional[str], sort_order: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO taxonomy_entries (category, code, label, description, sort_order)
        VALUES (?, ?, ?, ?, ?)
        """,
        (category, code, label, description, sort_order),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Regulatory rules table
# ---------------------------------------------------------------------------


def list_regulatory_rules(conn: sqlite3.Connection, domain: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM regulatory_rules"
    params: tuple = ()
    if domain is not None:
        sql += " WHERE domain = ?"
        params = (domain,)
    sql += " ORDER BY domain, section, name"
    return _rows_to_dicts(conn, sql, params)


def get_regulatory_rule(conn: sqlite3.Connection, reg_rule_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM regulatory_rules WHERE reg_rule_id = ?", (reg_rule_id,))


def upsert_regulatory_rule(
    conn: sqlite3.Connection, *, domain: str, section: Optional[str], name: str,
    rate_or_rule: str, effective_from: str, seed_as_of: str, notes: Optional[str],
) -> int:
    """Insert-or-update by (domain, section, name). NOTE: section is NULL for
    GST slabs, and SQLite UNIQUE constraints treat NULL as distinct from
    itself — so we do an explicit lookup rather than relying on ON CONFLICT
    (which would duplicate every NULL-section row on each seed run)."""
    existing = conn.execute(
        "SELECT reg_rule_id FROM regulatory_rules WHERE domain = ? AND section IS ? AND name = ?",
        (domain, section, name),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE regulatory_rules SET rate_or_rule = ?, effective_from = ?, seed_as_of = ?, notes = ? WHERE reg_rule_id = ?",
            (rate_or_rule, effective_from, seed_as_of, notes, existing[0]),
        )
        conn.commit()
        return existing[0]
    cur = conn.execute(
        """
        INSERT INTO regulatory_rules
            (domain, section, name, rate_or_rule, effective_from, seed_as_of, last_reviewed_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (domain, section, name, rate_or_rule, effective_from, seed_as_of, notes),
    )
    conn.commit()
    return cur.lastrowid


def update_regulatory_rule(
    conn: sqlite3.Connection, reg_rule_id: int, *, rate_or_rule: str, effective_from: str,
) -> None:
    conn.execute(
        "UPDATE regulatory_rules SET rate_or_rule = ?, effective_from = ? WHERE reg_rule_id = ?",
        (rate_or_rule, effective_from, reg_rule_id),
    )
    conn.commit()


def mark_regulatory_rule_reviewed(conn: sqlite3.Connection, reg_rule_id: int) -> None:
    conn.execute(
        "UPDATE regulatory_rules SET last_reviewed_at = ? WHERE reg_rule_id = ?",
        (_now(), reg_rule_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Statutory due dates
# ---------------------------------------------------------------------------


def list_statutory_due_dates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM statutory_due_dates ORDER BY sort_order, obligation")


def upsert_statutory_due_date(
    conn: sqlite3.Connection, *, obligation: str, period: str, due_day: Optional[int],
    due_month: Optional[int], grace_days: int, description: Optional[str], sort_order: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO statutory_due_dates
            (obligation, period, due_day, due_month, grace_days, description, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(obligation) DO UPDATE SET
            period = excluded.period,
            due_day = excluded.due_day,
            due_month = excluded.due_month,
            grace_days = excluded.grace_days,
            description = excluded.description,
            sort_order = excluded.sort_order
        """,
        (obligation, period, due_day, due_month, grace_days, description, sort_order),
    )
    conn.commit()
    row = conn.execute("SELECT due_date_id FROM statutory_due_dates WHERE obligation = ?", (obligation,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Change history — real F4 entries
# ---------------------------------------------------------------------------
# F4 retrofit: this used to write C1's local `rule_change_log` stub. Per the
# F4 build prompt ("C1 — rule changes, at BOTH the firm-wide and per-client-
# override layers ... REPLACE C1's local change log"), it now records real,
# immutable F4 Edit History entries. Signature kept identical so existing call
# sites are unchanged; the local table is retained (empty) but unwritten.
#
# record_type mapping: rule VALUE edits (the "rule thresholds" the PRD names
# as mandatory-reason) -> "rule"; every other C1 config edit (taxonomy label,
# regulatory rate, statutory due date) -> its own record_type, so the History
# view stays legible and only rule thresholds demand a mandatory reason.


def log_change(
    conn: sqlite3.Connection, *, rule_id: Optional[int], scope: Optional[str],
    client_id: Optional[int], field: str, old_value: Optional[str],
    new_value: Optional[str], changed_by: str, reason: Optional[str] = None,
    record_type: Optional[str] = None, record_id: Optional[str] = None,
) -> None:
    from src.f4 import service as f4  # local import avoids a hard circular dep

    if record_type is None:
        record_type = "rule" if field == "value" else ("taxonomy" if field.startswith("taxonomy") else "rule_config")
    if record_id is None:
        # Rule VALUE entries are per (rule_id, scope) so a firm-wide edit and a
        # per-client override each get their own History; client_id is also
        # stamped on the entry so the client-scoped view can filter on it.
        if field == "value" and rule_id is not None:
            record_id = f"{rule_id}:{scope or 'firm'}"
        elif rule_id is not None:
            record_id = str(rule_id)
        elif client_id is not None:
            record_id = str(client_id)
        else:
            record_id = scope or "firm"

    f4.record_edit(
        record_type=record_type, record_id=record_id, client_id=client_id,
        field=field, old_value=old_value, new_value=new_value,
        reason=reason, actor=changed_by,
    )


def list_change_log(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM rule_change_log ORDER BY id DESC LIMIT ?", (limit,))