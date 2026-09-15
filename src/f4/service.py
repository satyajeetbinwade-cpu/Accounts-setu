"""Public API for the F4 Universal Edit & Version History service \u2014 every
other module (and every UI file) should import from here, not from
src.f4.db directly. Mirrors src/rules/service.py's shape.

This is a cross-cutting service every prior module calls into: any editable
record can be changed with a reason, the old value is preserved, and
there's ONE reusable per-record History view.

Scope \u2014 genuine FIELD-LEVEL EDIT HISTORY only (per the build prompt's
resolution of the Open Conflict, following the locked Information
Architecture distinction between Audit Trail and Edit History):

- This service records "a record's stored value changed by a person, with a
  reason".
- It does NOT record SECURITY / OPERATIONAL EVENTS \u2014 failed logins,
  permission escalations, credential access, AI fallbacks, validation-block
  overrides, or filing sends. Those remain permanently in C4's
  SecurityEvent sink and are never migrated here. (F4 logs the EDIT, not
  the underlying business decision.)

Design notes:
- Entries are IMMUTABLE once written (no update/delete path in db.py). A
  same-minute correction is a new entry, never an overwrite.
- FieldSensitivityFlag is CODE-LEVEL, set here at build time (see
  FIELD_SENSITIVITY / MANDATORY_REASON_FIELDS), on the PRD's named fields.
  It is explicitly NOT an Admin-configurable setting.
- F4 never decides whether an edit is allowed \u2014 that's F1's permission
  gate, which happens before F4 is ever invoked. This service assumes the
  caller has already passed its own permission check.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.f4 import db as f4db
from src.f4.schema import init_f4_schema


class F4Error(Exception):
    """Raised for expected F4-module failures (e.g. a missing mandatory reason)."""


# ---------------------------------------------------------------------------
# Field sensitivity \u2014 CODE-LEVEL flags (NOT an Admin setting)
# ---------------------------------------------------------------------------
# Per the F4 build prompt: the PRD's named fields (GSTIN, PAN, rule
# thresholds, bank details) require a mandatory reason before saving; all
# other fields are optional/skippable.
#
# Expressed two ways so callers can ask either "is this record+field
# mandatory?" (the usual case) or "is this a mandatory field name?" in
# isolation:
#   * MANDATORY_REASON_FIELDS \u2014 the PRD's named sensitive field NAMES.
#   * FIELD_SENSITIVITY \u2014 a per-(record_type, field) override map, so a
#     field that is sensitive in one module can be flagged precisely there
#     without over-matching elsewhere.
MANDATORY_REASON_FIELDS: frozenset[str] = frozenset(
    {
        "gstin", "pan",          # F2 identity fields
        "value",                 # C1 rule thresholds (the rule's stored value)
        "bank", "bank_account", "bank_details",  # bank details
    }
)

# (record_type, field) -> "mandatory" | "optional". Anything not listed
# falls back to MANDATORY_REASON_FIELDS membership by field name.
FIELD_SENSITIVITY: dict[tuple[str, str], str] = {
    ("client", "pan"): "mandatory",
    ("client", "gstin"): "mandatory",
    ("branch", "gstin"): "mandatory",
    # C1 rule VALUE edits (firm-wide + per-client override) are the "rule
    # thresholds" the PRD names \u2014 mandatory reason at BOTH layers.
    ("rule", "value"): "mandatory",
}


def init_f4(db_path=None) -> None:
    """Create F4 tables. Call once at app start, alongside the other
    modules' init_*() calls."""
    conn = _connect(db_path)
    try:
        init_f4_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# Sensitivity helpers
# ---------------------------------------------------------------------------


def sensitivity_for(record_type: str, field: str) -> str:
    """Return 'mandatory' or 'optional' for a given record+field, per the
    code-level FieldSensitivityFlag."""
    explicit = FIELD_SENSITIVITY.get((record_type, field))
    if explicit is not None:
        return explicit
    return "mandatory" if field in MANDATORY_REASON_FIELDS else "optional"


def requires_reason(record_type: str, field: str) -> bool:
    """True when this field is high-sensitivity and a reason is mandatory."""
    return sensitivity_for(record_type, field) == "mandatory"


# ---------------------------------------------------------------------------
# Universal edit-capture service
# ---------------------------------------------------------------------------


def record_edit(
    *,
    record_type: str,
    record_id: Any,
    field: str,
    old_value: Any,
    new_value: Any,
    reason: Optional[str] = None,
    actor: str,
    client_id: Optional[int] = None,
    db_path=None,
) -> int:
    """Capture one edit: old value -> new value -> reason -> who -> when.

    Callable from any module. Returns the new entry_id.

    Enforces the mandatory-reason rule for high-sensitivity fields: if
    ``requires_reason(record_type, field)`` is True and no reason is given,
    raises F4Error rather than silently recording an unexplained edit. For
    optional fields the reason is stored verbatim (or None).

    F4 does not check permissions \u2014 the caller's own gate (F1) must have
    already allowed the edit before calling this.
    """
    if requires_reason(record_type, field) and not (reason and reason.strip()):
        raise F4Error(
            f"A reason is required before saving a change to '{field}'. "
            "This is a high-sensitivity field."
        )
    conn = _connect(db_path)
    try:
        return f4db.insert_entry(
            conn,
            record_type=record_type,
            record_id=str(record_id),
            field=field,
            old_value=None if old_value is None else str(old_value),
            new_value=None if new_value is None else str(new_value),
            reason=reason.strip() if reason else None,
            changed_by=actor,
            client_id=client_id,
        )
    finally:
        conn.close()


def record_edits(edits: list[dict[str, Any]], *, actor: str, db_path=None) -> list[int]:
    """Capture a BULK edit as INDIVIDUAL per-record entries \u2014 never one
    vague combined line (F4 business rule). Each item in ``edits`` is a
    kwargs dict for :func:`record_edit` (without ``actor``); returns the
    list of entry_ids in the same order.
    """
    return [
        record_edit(actor=actor, db_path=db_path, **edit)
        for edit in edits
    ]


# ---------------------------------------------------------------------------
# Per-record History view
# ---------------------------------------------------------------------------


def history_for_record(
    record_type: str, record_id: Any, *, client_id: Optional[int] = None,
    firm_only: bool = False, db_path=None,
) -> list[dict[str, Any]]:
    """All entries for one record, newest first, each enriched with a
    plain-language sentence (see :func:`format_entry`) \u2014 ready for the
    reusable History panel."""
    conn = _connect(db_path)
    try:
        rows = f4db.list_entries_for_record(
            conn, record_type, str(record_id), client_id=client_id, firm_only=firm_only,
        )
    finally:
        conn.close()
    for r in rows:
        r["plain_language"] = format_entry(r)
    return rows


def history_for_client(client_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """All client-scoped entries for one client, newest first."""
    conn = _connect(db_path)
    try:
        rows = f4db.list_entries_for_client(conn, client_id)
    finally:
        conn.close()
    for r in rows:
        r["plain_language"] = format_entry(r)
    return rows


def recent_history(*, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = f4db.recent_entries(conn, limit=limit)
    finally:
        conn.close()
    return rows


def history_count_for_record(record_type: str, record_id: Any, *, db_path=None) -> int:
    """Cheap count used to label the "View History (N)" trigger without
    pulling every entry."""
    return len(history_for_record(record_type, record_id, db_path=db_path))


# ---------------------------------------------------------------------------
# Plain-language rendering (never a raw diff)
# ---------------------------------------------------------------------------

_MISSING = "(none)"


def format_entry(entry: dict[str, Any]) -> str:
    """One entry in plain language: "Changed from X to Y, by [name], on
    [date], because [reason]."

    No reason clause is rendered for optional-reason fields where none was
    given (F4 UX requirement). Never a raw diff.
    """
    old = entry.get("old_value")
    new = entry.get("new_value")
    old_txt = old if old not in (None, "") else _MISSING
    new_txt = new if new not in (None, "") else _MISSING
    sentence = f"Changed from {old_txt} to {new_txt}"
    sentence += f", by {entry.get('changed_by') or '?'}"
    sentence += f", on {_short_date(entry.get('created_at'))}"
    reason = entry.get("reason")
    if reason:
        sentence += f", because {reason}"
    return sentence + "."


def _short_date(value: Optional[str]) -> str:
    if not value:
        return "?"
    return value.split("T", 1)[0]