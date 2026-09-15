"""Public API for the C1 Rules, Taxonomy & Regulatory Configuration module —
every other module (and every UI file) should import from here, not from
src.rules.db directly. Mirrors src/clients/service.py's shape.

Covers: DB init + seed, the Rules Workspace (five categories), effective-
dated Rule Versions at BOTH the firm-wide and per-client-override layers,
the pre-seeded Taxonomy Editor, the pre-seeded Regulatory Rules Table (with
staleness signal), the Statutory Due Dates category for C2, and the local
change-log stub (retrofit to F4 once built).

Business rules enforced here (per the C1 build prompt):
- Rule edits are FORWARD-ONLY: saving inserts a NEW effective-dated
  RuleVersion; it never rewrites a past version, so already-processed
  transactions keep their classification/calculation.
- Manager-level approval is sufficient for ALL rule types (no separate
  Partner-only tier); there is no dry-run gate this phase.
- A rule cannot be deleted if any historical Rule Version is still
  referenced by past transactions (structural hook — no consumer queries
  past versions yet, so deletion is currently always safe).
- Wide-blast-radius rules carry high_impact=True, which the UI uses to
  require the impact-confirmation banner before saving.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Optional

from src import db as recon_db
from src.rules import db as rdb
from src.rules.schema import init_rules_schema
from src.rules.seed import run_seed, SEED_AS_OF


class RulesError(Exception):
    """Raised for expected rules-module failures (validation, blocked action)."""


# ---------------------------------------------------------------------------
# Init + connection
# ---------------------------------------------------------------------------


def init_rules(db_path=None) -> None:
    """Create C1 tables and seed first-run data. Call once at app start,
    alongside db.init_db(), auth.init_auth(), clients.init_clients() and
    settings.init_settings()."""
    conn = _connect(db_path)
    try:
        init_rules_schema(conn)
        run_seed(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Rules Workspace — categories + rules
# ---------------------------------------------------------------------------


def list_categories(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return rdb.list_categories(conn)
    finally:
        conn.close()


def list_rules(category_key: Optional[str] = None, *, db_path=None) -> list[dict[str, Any]]:
    """Return all rules (optionally scoped to one category), each enriched
    with its current firm-wide value, its client-override count, and its
    value_type/unit/high_impact for rendering."""
    conn = _connect(db_path)
    try:
        cat_id = None
        if category_key is not None:
            cat = rdb.get_category_by_key(conn, category_key)
            if cat is not None:
                cat_id = cat["category_id"]
        rules = rdb.list_rules(conn, cat_id)
        for r in rules:
            fw = rdb.latest_rule_version(conn, r["rule_id"], "firm", None)
            r["firm_value"] = fw["value"] if fw else None
            r["firm_effective_from"] = fw["effective_from"] if fw else None
            r["override_count"] = rdb.count_client_overrides(conn, r["rule_id"])
            r["overridden_client_ids"] = rdb.list_overridden_client_ids(conn, r["rule_id"])
        return rules
    finally:
        conn.close()


def get_rule(key: str, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return rdb.get_rule_by_key(conn, key)
    finally:
        conn.close()


def get_effective_value(
    key: str, *, client_id: Optional[int] = None, db_path=None,
) -> Optional[str]:
    """Resolve the effective value of a rule for a given client: the client's
    own override if one exists, else the firm-wide default. Returns the raw
    stored string (or None). This is the resolver Module 2 reads tolerance /
    materiality thresholds through — the two independent layers (firm-wide +
    per-client) are collapsed here at read time, never written back."""
    conn = _connect(db_path)
    try:
        rule = rdb.get_rule_by_key(conn, key)
        if rule is None:
            return None
        if client_id is not None:
            override = rdb.latest_rule_version(conn, rule["rule_id"], "client", client_id)
            if override is not None:
                return override["value"]
        firm = rdb.latest_rule_version(conn, rule["rule_id"], "firm", None)
        return firm["value"] if firm else None
    finally:
        conn.close()


def get_effective_number(
    key: str, *, client_id: Optional[int] = None, default: Optional[float] = None, db_path=None,
) -> Optional[float]:
    """Numeric convenience wrapper over get_effective_value(). Returns
    ``default`` when the rule is absent or its value is not numeric."""
    raw = get_effective_value(key, client_id=client_id, db_path=db_path)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Rule version editing (forward-only, two independent layers)
# ---------------------------------------------------------------------------


def _resolve_value_type(value: str, value_type: str) -> str:
    """Normalize a string value to its canonical stored form for the given
    value_type. Returns the canonical string, or raises RulesError."""
    value = value.strip()
    if value_type == "number":
        try:
            return str(float(value))
        except ValueError:
            raise RulesError(f"'{value}' is not a valid number.")
    if value_type == "boolean":
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return "true"
        if v in ("false", "0", "no", "off"):
            return "false"
        raise RulesError(f"'{value}' is not a valid boolean (true/false).")
    if value_type == "date":
        # Accept ISO date; store as-is after a light validation.
        try:
            datetime.fromisoformat(value)
            return value
        except ValueError:
            raise RulesError(f"'{value}' is not a valid ISO date.")
    return value  # text — store verbatim


def edit_rule_value(
    rule_key: str, new_value: str, *, scope: str, client_id: Optional[int],
    actor: str, reason: Optional[str] = None, db_path=None,
) -> int:
    """Create a NEW effective-dated RuleVersion for the given scope, and
    return its version_id. Forward-only: the previous version is untouched.

    ``scope`` is 'firm' (client_id must be None) or 'client' (client_id
    required). This is the single-step edit path — Manager+ saving IS both
    propose and approve; no dry-run gate this phase.

    F4 retrofit: a rule VALUE is a high-sensitivity field ("rule
    thresholds" per the PRD), so a reason is now MANDATORY at BOTH the
    firm-wide and per-client-override layers — enforced at the F4 layer via
    the code-level FieldSensitivityFlag, and surfaced in the UI as the
    Reason-capture-before-save component (3.3)."""
    if scope not in ("firm", "client"):
        raise RulesError(f"Unknown scope: {scope}")
    if scope == "client" and client_id is None:
        raise RulesError("A client is required to edit a client override.")
    if scope == "firm" and client_id is not None:
        raise RulesError("A firm-wide edit must not carry a client_id.")
    if not (reason and reason.strip()):
        raise RulesError("A reason is required before saving a rule change.")

    conn = _connect(db_path)
    try:
        rule = rdb.get_rule_by_key(conn, rule_key)
        if rule is None:
            raise RulesError(f"Unknown rule key: {rule_key}")
        canonical = _resolve_value_type(new_value, rule["value_type"])
        before = rdb.latest_rule_version(conn, rule["rule_id"], scope, client_id)
        old_value = before["value"] if before else None

        version_id = rdb.insert_rule_version(
            conn, rule_id=rule["rule_id"], scope=scope, client_id=client_id,
            value=canonical, effective_from=_today(), created_by=actor,
        )
        rdb.log_change(
            conn, rule_id=rule["rule_id"], scope=scope, client_id=client_id,
            field="value", old_value=old_value, new_value=canonical,
            changed_by=actor, reason=reason.strip(),
        )
        return version_id
    finally:
        conn.close()


def rule_versions(rule_key: str, *, scope: Optional[str] = None, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    """All historical versions of a rule (firm-wide and/or per-client),
    newest first. Never mutated — these are the forward-only record."""
    conn = _connect(db_path)
    try:
        rule = rdb.get_rule_by_key(conn, rule_key)
        if rule is None:
            return []
        return rdb.list_rule_versions(conn, rule["rule_id"], scope=scope, client_id=client_id)
    finally:
        conn.close()


def is_high_impact(rule_key: str, *, db_path=None) -> bool:
    conn = _connect(db_path)
    try:
        rule = rdb.get_rule_by_key(conn, rule_key)
        return bool(rule and rule["high_impact"])
    finally:
        conn.close()


def can_delete_rule(rule_key: str, *, db_path=None) -> tuple[bool, Optional[str]]:
    """Returns (allowed, reason_if_blocked). A rule cannot be deleted if any
    historical Rule Version is still referenced by past transactions. No
    consumer queries past versions yet (Module 2 isn't built), so this is a
    structural hook that currently always returns True."""
    # Structural hook only — see the C1 prompt's business rules. Once a
    # consumer of past rule versions exists, wire the reference check here.
    return True, None


# ---------------------------------------------------------------------------
# Taxonomy Editor (pre-seeded)
# ---------------------------------------------------------------------------


def list_taxonomy(category: Optional[str] = None, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return rdb.list_taxonomy(conn, category)
    finally:
        conn.close()


def taxonomy_categories(*, db_path=None) -> list[str]:
    conn = _connect(db_path)
    try:
        rows = rdb.list_taxonomy(conn)
        return sorted({r["category"] for r in rows})
    finally:
        conn.close()


def rename_taxonomy_entry(entry_id: int, label: str, *, actor: str, db_path=None) -> None:
    """Safe label-only edit — no confirmation needed (C1 design table)."""
    if not label.strip():
        raise RulesError("Label is required.")
    conn = _connect(db_path)
    try:
        entry = rdb.get_taxonomy_entry(conn, entry_id)
        if entry is None:
            raise RulesError("Taxonomy entry not found.")
        rdb.rename_taxonomy_entry(conn, entry_id, label.strip())
        rdb.log_change(
            conn, rule_id=None, scope=None, client_id=None, field="taxonomy_label",
            old_value=entry["label"], new_value=label.strip(), changed_by=actor,
        )
    finally:
        conn.close()


def add_taxonomy_entry(category: str, code: str, label: str, *, actor: str, db_path=None) -> int:
    if not code.strip() or not label.strip():
        raise RulesError("Code and label are required.")
    conn = _connect(db_path)
    try:
        return rdb.add_taxonomy_entry(
            conn, category=category, code=code.strip(), label=label.strip(),
            description=None, sort_order=999,
        )
    finally:
        conn.close()


def can_delete_taxonomy_entry(entry_id: int, *, db_path=None) -> tuple[bool, Optional[str]]:
    """Taxonomy entries only rename or delete (no deactivate). Delete is
    blocked if the entry is still referenced. No consumer references
    taxonomy codes yet (Module 2/Module 8 not built), so this is a
    structural hook that currently always allows delete."""
    conn = _connect(db_path)
    try:
        entry = rdb.get_taxonomy_entry(conn, entry_id)
        if entry is None:
            return False, "Taxonomy entry not found."
    finally:
        conn.close()
    # Structural hook — wire the real reference check once FlaggedItem
    # consumers (Module 8) exist.
    return True, None


def delete_taxonomy_entry(entry_id: int, *, actor: str, db_path=None) -> None:
    allowed, reason = can_delete_taxonomy_entry(entry_id, db_path=db_path)
    if not allowed:
        raise RulesError(reason or "This taxonomy entry can't be deleted.")
    conn = _connect(db_path)
    try:
        entry = rdb.get_taxonomy_entry(conn, entry_id)
        rdb.delete_taxonomy_entry(conn, entry_id)
        rdb.log_change(
            conn, rule_id=None, scope=None, client_id=None, field="taxonomy_deleted",
            old_value=entry["code"] if entry else None, new_value=None, changed_by=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regulatory Rules Table (pre-seeded, staleness signal)
# ---------------------------------------------------------------------------

STALE_AFTER_MONTHS = 6  # staleness signal: "not reviewed in N months"


def list_regulatory_rules(*, db_path=None) -> list[dict[str, Any]]:
    """Return the regulatory table enriched with a per-row status:
      - 'seed'    -> "Seed data — unverified" (last_reviewed_at is NULL)
      - 'reviewed'-> "Reviewed [date]" (last_reviewed_at within N months)
      - 'stale'   -> "Stale — not reviewed in N months" (older than N months)
    """
    conn = _connect(db_path)
    try:
        rows = rdb.list_regulatory_rules(conn)
        for r in rows:
            r["status"] = _regulatory_status(r["last_reviewed_at"])
            r["is_seed"] = r["last_reviewed_at"] is None
        return rows
    finally:
        conn.close()


def _regulatory_status(last_reviewed_at: Optional[str]) -> str:
    if last_reviewed_at is None:
        return "seed"
    try:
        reviewed = datetime.fromisoformat(last_reviewed_at)
    except ValueError:
        return "seed"
    now = datetime.now(timezone.utc)
    reviewed_naive = reviewed.replace(tzinfo=timezone.utc) if reviewed.tzinfo is None else reviewed
    months = (now.year - reviewed_naive.year) * 12 + (now.month - reviewed_naive.month)
    if months >= STALE_AFTER_MONTHS:
        return "stale"
    return "reviewed"


def update_regulatory_rule(reg_rule_id: int, *, rate_or_rule: str, effective_from: str, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        before = rdb.get_regulatory_rule(conn, reg_rule_id)
        if before is None:
            raise RulesError("Regulatory rule not found.")
        rdb.update_regulatory_rule(conn, reg_rule_id, rate_or_rule=rate_or_rule, effective_from=effective_from)
        rdb.log_change(
            conn, rule_id=None, scope=None, client_id=None, field="regulatory_rate",
            old_value=before["rate_or_rule"], new_value=rate_or_rule, changed_by=actor,
        )
    finally:
        conn.close()


def mark_regulatory_rule_reviewed(reg_rule_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        rdb.mark_regulatory_rule_reviewed(conn, reg_rule_id)
        rdb.log_change(
            conn, rule_id=None, scope=None, client_id=None, field="regulatory_reviewed",
            old_value=None, new_value=_today(), changed_by=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Statutory Due Dates (new category for C2)
# ---------------------------------------------------------------------------


def list_statutory_due_dates(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return rdb.list_statutory_due_dates(conn)
    finally:
        conn.close()


def update_statutory_due_date(
    due_date_id: int, *, obligation: str, period: str, due_day: Optional[int],
    due_month: Optional[int], grace_days: int, description: Optional[str], actor: str, db_path=None,
) -> None:
    conn = _connect(db_path)
    try:
        rdb.upsert_statutory_due_date(
            conn, obligation=obligation, period=period, due_day=due_day,
            due_month=due_month, grace_days=grace_days, description=description,
            sort_order=0,
        )
        rdb.log_change(
            conn, rule_id=None, scope=None, client_id=None, field="statutory_due_date",
            old_value=None, new_value=obligation, changed_by=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Change history — real F4 entries
# ---------------------------------------------------------------------------


def list_change_log(*, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    """F4 retrofit: C1 edits are now real F4 Edit History entries, so this
    reads F4's cross-cutting log filtered to C1's own record types and
    normalizes it to the legacy row shape."""
    from src.f4 import service as f4  # local import avoids a hard circular dep

    c1_types = {"rule", "rule_config", "taxonomy"}
    rows = [r for r in f4.recent_history(limit=max(limit * 3, limit), db_path=db_path)
            if r["record_type"] in c1_types][:limit]
    return [
        {
            "field": r["field"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "reason": r["reason"],
            "changed_by": r["changed_by"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]