"""Public API for the F5 Data Integrity & Validation Layer — every other
module (and every UI file) should import from here, not from src.f5.db
directly. Mirrors src/rules/service.py's shape.

Covers: structural validity checks on already-mapped canonical data (a
SECOND, DISTINCT gate from F3-AI's mapping-confidence queue), the manual
control-total completeness check, per-client-per-source sync health
(REAL for the manual-upload path; dormant/stub for API sources until C2
exists), the reconciliation-of-the-reconciliation dormant data model +
calling interface (wired/tested only once Module 2 exists), the
override-with-reason flow (severity + role gated), Partner escalation on
persistent sync failure, and the local change-log stub (retrofit to F4).

Business rules enforced here (per the F5 build prompt), not just in the UI:
- A validation failure blocks only the affected data, never the client's
  entire processing (see run_structural_checks — it creates per-issue
  blocks scoped to one source file, never a client-wide block).
- Overriding a block always requires a reason + sufficient permission:
  Senior Accountant may override MINOR/COSMETIC issues only;
  GSTIN/PAN-level issues remain Manager+ only.
- Every override writes an Audit Trail Entry, and — since an override
  changes the block's own state — also an Edit History Entry (both rows
  in validation_change_log, local stub until F4 exists).
- Consecutive/persistent sync failures escalate directly to a Partner
  (escalation_events), not just passive dashboard visibility.
- F5 flags and blocks; it never corrects bad data, never second-guesses
  business-logic validation (C1's job), and never performs the API call
  itself (C2's job) — it only validates what's already been ingested.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from typing import Any, Optional

from src import db as recon_db
from src.f5 import db as fdb
from src.f5.schema import init_f5_schema

PERSISTENT_FAILURE_THRESHOLD = 3  # consecutive attempted_failed entries -> escalate to Partner


class F5Error(Exception):
    """Raised for expected F5-module failures (validation, blocked action)."""


def init_f5(db_path=None) -> None:
    """Create F5 tables. Call once at app start, alongside the other
    modules' init_*() calls."""
    conn = _connect(db_path)
    try:
        init_f5_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _now_iso() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Structural validity (SECOND, DISTINCT gate from F3-AI's mapping queue)
# ---------------------------------------------------------------------------

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][A-Z][0-9A-Z]$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")

# Which severity a given issue_type carries. GSTIN/PAN/TAN identity fields
# are ALWAYS gstin_pan-tier (Manager+ only to override); malformed dates
# and duplicates are minor by default (Senior+ can override).
_ISSUE_SEVERITY = {
    "malformed_gstin": "gstin_pan",
    "invalid_pan": "gstin_pan",
    "invalid_tan": "gstin_pan",
    "impossible_date": "minor",
    "duplicate": "minor",
}

_ISSUE_LABELS = {
    "malformed_gstin": "Malformed GSTIN",
    "invalid_pan": "Invalid PAN format",
    "invalid_tan": "Invalid TAN format",
    "impossible_date": "Impossible date",
    "duplicate": "Duplicate record",
}


def _is_valid_date(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return True  # blank is not F5's concern here (ingestion already flags blanks)
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return False
    # "Impossible" also covers far-future/far-past filing dates — a loose
    # sanity band, not a business-logic date-window check (that's C1's job).
    return date(2000, 1, 1) <= d <= date(2100, 12, 31)


def run_structural_checks(
    client_id: int, *, recon_type: str, source_type: str, source_file: str,
    rows: list[dict[str, Any]], gstin_field: str = "gstin", pan_field: str = "pan",
    date_fields: Optional[list[str]] = None, dedupe_keys: Optional[list[str]] = None,
    db_path=None,
) -> list[dict[str, Any]]:
    """Run structural validity checks on already-mapped CANONICAL rows for
    one source file. Different failure mode from F3-AI's mapping gate:
    F3-AI asks "did we identify what column means what"; this asks "is the
    value itself valid." Never merged into F3-AI's queue.

    Creates one ValidationBlock PER DISTINCT ISSUE (not per row) so a
    block always scopes to the affected data only, never the client's
    entire processing. Re-running clears prior OPEN blocks for this exact
    (client, source_type, source_file) first, so a fixed file doesn't
    leave a stale block behind.

    Returns the list of newly created blocks.
    """
    date_fields = date_fields or []
    dedupe_keys = dedupe_keys or []

    conn = _connect(db_path)
    try:
        fdb.clear_open_blocks_for_source(conn, client_id=client_id, source_type=source_type, source_file=source_file)

        issues: dict[tuple[str, str, str], list[str]] = {}
        # key = (issue_type, field_name, raw_value) -> list of row labels

        for idx, row in enumerate(rows):
            row_label = str(row.get("invoice_number") or row.get("challan_number") or f"row {idx + 1}")

            gstin = str(row.get(gstin_field) or "").strip().upper()
            if gstin and not _GSTIN_RE.match(gstin):
                issues.setdefault(("malformed_gstin", gstin_field, gstin), []).append(row_label)

            pan = str(row.get(pan_field) or "").strip().upper()
            if pan and not _PAN_RE.match(pan):
                issues.setdefault(("invalid_pan", pan_field, pan), []).append(row_label)

            for f in date_fields:
                val = str(row.get(f) or "").strip()
                if val and not _is_valid_date(val):
                    issues.setdefault(("impossible_date", f, val), []).append(row_label)

        # Duplicates: same dedupe_keys tuple appearing more than once.
        if dedupe_keys:
            seen: dict[tuple, list[str]] = {}
            for idx, row in enumerate(rows):
                key = tuple(str(row.get(k) or "").strip().upper() for k in dedupe_keys)
                if not any(key):
                    continue
                row_label = str(row.get("invoice_number") or row.get("challan_number") or f"row {idx + 1}")
                seen.setdefault(key, []).append(row_label)
            for key, labels in seen.items():
                if len(labels) > 1:
                    raw_desc = " / ".join(f"{k}={v}" for k, v in zip(dedupe_keys, key))
                    issues.setdefault(("duplicate", "+".join(dedupe_keys), raw_desc), []).extend(labels)

        created: list[dict[str, Any]] = []
        for (issue_type, field_name, raw_value), row_labels in issues.items():
            severity = _ISSUE_SEVERITY.get(issue_type, "minor")
            label = _ISSUE_LABELS.get(issue_type, issue_type)
            description = (
                f"{label}: '{raw_value}' in field '{field_name}' "
                f"({len(row_labels)} row(s) affected — e.g. {', '.join(row_labels[:3])}"
                f"{', …' if len(row_labels) > 3 else ''})."
            )
            block_id = fdb.insert_block(
                conn, client_id=client_id, recon_type=recon_type, source_type=source_type,
                source_file=source_file, issue_type=issue_type, severity=severity,
                field_name=field_name, raw_value=raw_value, description=description,
                row_count=len(row_labels),
            )
            fdb.log_change(
                conn, block_id=block_id, entry_kind="audit_trail", action="block_created",
                detail=description, reason=None, actor="system",
            )
            created.append(fdb.get_block(conn, block_id))
        return created
    finally:
        conn.close()


def list_blocks(*, client_id: Optional[int] = None, status: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_blocks(conn, client_id=client_id, status=status)
    finally:
        conn.close()


def get_block(block_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.get_block(conn, block_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Override flow — severity + role gated, reason required
# ---------------------------------------------------------------------------


def can_override_block(block: dict[str, Any], actor_role: str) -> tuple[bool, Optional[str]]:
    """(allowed, reason_if_blocked). Senior Accountant may override
    MINOR/COSMETIC issues only; GSTIN/PAN-level issues remain Manager+
    only (Manager, Partner, Admin)."""
    manager_plus = {"Manager", "Partner", "Admin"}
    senior_plus = manager_plus | {"Senior Accountant"}

    if block["severity"] == "gstin_pan":
        if actor_role not in manager_plus:
            return False, "Requires Manager or above."
        return True, None

    # cosmetic / minor
    if actor_role not in senior_plus:
        return False, "Requires Senior Accountant or above."
    return True, None


def override_block(block_id: int, *, actor: str, actor_role: str, reason: str, db_path=None) -> None:
    if not reason or not reason.strip():
        raise F5Error("A reason is required to override a validation block.")

    conn = _connect(db_path)
    try:
        block = fdb.get_block(conn, block_id)
        if block is None:
            raise F5Error("Validation block not found.")
        if block["status"] != "open":
            raise F5Error("This block has already been resolved.")

        allowed, deny_reason = can_override_block(block, actor_role)
        if not allowed:
            raise F5Error(deny_reason or "You don't have permission to override this block.")

        fdb.override_block(conn, block_id, actor=actor, reason=reason.strip())

        # Audit Trail Entry — every override.
        fdb.log_change(
            conn, block_id=block_id, entry_kind="audit_trail", action="override",
            detail=f"{block['issue_type']} on {block['source_file']}", reason=reason.strip(), actor=actor,
        )
        # Edit History Entry — the override changed the block's data
        # state (open -> overridden), so this also gets a data-state entry.
        fdb.log_change(
            conn, block_id=block_id, entry_kind="edit_history", action="status_change:open->overridden",
            detail=f"severity={block['severity']}", reason=reason.strip(), actor=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Completeness checks — manual control total (interim workflow)
# ---------------------------------------------------------------------------


def record_control_total(
    *, client_id: int, period: str, recon_type: str, source_type: str, source_file: str,
    control_count: int, control_amount: Optional[float], ingested_count: int,
    ingested_amount: Optional[float], entered_by: str, db_path=None,
) -> dict[str, Any]:
    """Compare a manually-entered source-system control total against
    what was actually ingested. Produces a REAL, working completeness
    result — 'match' if counts (and amounts, when both provided) agree,
    'mismatch' otherwise."""
    result = "match"
    if control_count != ingested_count:
        result = "mismatch"
    if control_amount is not None and ingested_amount is not None:
        if round(control_amount, 2) != round(ingested_amount, 2):
            result = "mismatch"

    conn = _connect(db_path)
    try:
        control_id = fdb.insert_control_total(
            conn, client_id=client_id, period=period, recon_type=recon_type, source_type=source_type,
            source_file=source_file, control_count=control_count, control_amount=control_amount,
            ingested_count=ingested_count, ingested_amount=ingested_amount, result=result,
            entered_by=entered_by,
        )
        fdb.log_change(
            conn, block_id=None, entry_kind="audit_trail", action="control_total_entered",
            detail=f"{source_type}/{source_file}: control={control_count}/{control_amount} "
                   f"ingested={ingested_count}/{ingested_amount} -> {result}",
            reason=None, actor=entered_by,
        )
        row = _row_by_id(conn, "manual_control_totals", "control_id", control_id)
        return row
    finally:
        conn.close()


def _row_by_id(conn: sqlite3.Connection, table: str, pk: str, value: int) -> dict[str, Any]:
    cur = conn.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (value,))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else {}


def list_control_totals(*, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_control_totals(conn, client_id=client_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sync health — real for manual-upload path, dormant/stub for API sources
# ---------------------------------------------------------------------------

SYNC_SOURCES = ["tally", "gst_portal", "traces", "bank"]

# Manual-upload sources actually exercised this build; anything else is a
# structural stub until C2's live connection exists.
_MANUAL_UPLOAD_SOURCES = {"tally", "gst_portal", "traces"}


def record_sync_health(
    *, client_id: int, source: str, status: str, detail: Optional[str] = None, db_path=None,
) -> None:
    """Record a REAL sync-health entry for the manual-upload path (source
    in _MANUAL_UPLOAD_SOURCES). For a genuinely API-fed source this would
    be dormant until C2 exists — this build only ever calls it from the
    manual-upload flow, so it's always real data, never fabricated.

    Also checks for PERSISTENT_FAILURE_THRESHOLD consecutive
    attempted_failed entries and escalates directly to Partner when hit
    (not just passive dashboard visibility)."""
    if status not in ("not_attempted", "attempted_failed", "succeeded"):
        raise F5Error(f"Unknown sync health status: {status}")

    conn = _connect(db_path)
    try:
        fdb.insert_sync_health(conn, client_id=client_id, source=source, status=status, detail=detail)

        if status == "attempted_failed":
            fails = fdb.consecutive_failures(conn, client_id, source)
            if fails >= PERSISTENT_FAILURE_THRESHOLD:
                message = (
                    f"Sync source '{source}' has failed {fails} consecutive times for this client — "
                    "escalated directly to Partner."
                )
                fdb.insert_escalation(conn, client_id=client_id, source=source, consecutive_fails=fails, message=message)
                _notify_partner_sync_escalation(client_id, source, fails, message)
    finally:
        conn.close()


def _notify_partner_sync_escalation(client_id: int, source: str, fails: int, message: str) -> None:
    """Ensures the firm-mandatory 'completeness / recon-of-recon failure'
    notification type exists via C3 (a Partner can't disable it — same
    escalation shape as C4's DPDP flow) — an upsert is enough to
    guarantee the type is registered; the actual escalation record lives
    in escalation_events (local stand-in for real push/email delivery).
    Best-effort: never breaks sync-health recording if C3 is unavailable."""
    try:
        from src.settings import db as sdb

        conn = recon_db.get_connection()
        try:
            sdb.upsert_notification_pref(
                conn, "sync_failure_escalation", "Completeness / recon-of-recon failure",
                "escalation", firm_default=True, firm_mandatory=True,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — never let a notification-seed failure break sync health
        pass


def sync_health_for_client(client_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """One row per source (latest status), always including every source
    in SYNC_SOURCES even if never checked (renders as 'not_attempted')."""
    conn = _connect(db_path)
    try:
        latest = {r["source"]: r for r in fdb.latest_sync_health(conn, client_id)}
    finally:
        conn.close()
    out = []
    for source in SYNC_SOURCES:
        row = latest.get(source)
        if row is None:
            out.append({
                "client_id": client_id, "source": source, "status": "not_attempted",
                "detail": None, "checked_at": None,
                "is_stub": source not in _MANUAL_UPLOAD_SOURCES,
            })
        else:
            row = dict(row)
            row["is_stub"] = source not in _MANUAL_UPLOAD_SOURCES
            out.append(row)
    return out


def sync_health_history(client_id: int, source: str, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.sync_health_history(conn, client_id, source)
    finally:
        conn.close()


def list_escalations(*, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_escalations(conn, client_id=client_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reconciliation-of-the-reconciliation — dormant data model + interface
# ---------------------------------------------------------------------------


def recon_of_recon_for_client(client_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """Always empty this build — Module 2 (build step 9) is what actually
    wires and tests the real matched+unmatched+excluded-vs-ingested-total
    check. The UI renders the dormant placeholder whenever this is empty,
    which is always, right now. No fabricated data is ever returned."""
    conn = _connect(db_path)
    try:
        return fdb.list_recon_of_recon(conn, client_id=client_id)
    finally:
        conn.close()


def record_recon_of_recon(
    *, client_id: int, run_id: Optional[int], matched_sum: Optional[float], unmatched_sum: Optional[float],
    excluded_sum: Optional[float], ingested_total: Optional[float], db_path=None,
) -> dict[str, Any]:
    """NOT called anywhere in this build — Module 2's own build step is
    the retrofit point that calls this for real. Present now so the
    calling interface is stable and doesn't need a signature change
    later."""
    ties_out = None
    if matched_sum is not None and unmatched_sum is not None and excluded_sum is not None and ingested_total is not None:
        ties_out = round(matched_sum + unmatched_sum + excluded_sum, 2) == round(ingested_total, 2)

    conn = _connect(db_path)
    try:
        result_id = fdb.insert_recon_of_recon(
            conn, client_id=client_id, run_id=run_id, matched_sum=matched_sum, unmatched_sum=unmatched_sum,
            excluded_sum=excluded_sum, ingested_total=ingested_total, ties_out=ties_out,
        )
        return _row_by_id(conn, "recon_of_recon_results", "result_id", result_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Change log (local stub — retrofit to F4)
# ---------------------------------------------------------------------------


def list_change_log(*, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_change_log(conn, limit=limit)
    finally:
        conn.close()
