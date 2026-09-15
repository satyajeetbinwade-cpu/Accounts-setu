"""Public API for the C2 Portal Connect & Filing module — every other
module (and every UI file) should import from here, not from
src.filing.db directly. Mirrors src/rules/service.py's shape.

Covers: the filing calendar (sourced from C1's Statutory Due Dates), the
explicit human Send action (Manager+ only, irreversible within Setu),
outcome recording with 'ambiguous' as a FIRST-CLASS state, acknowledgement
capture routed through F3 as linked evidence, the per-client/per-source
API-vs-manual toggle (dormant until C4 credentials exist), and the local
audit-trail stub (retrofit to F4).

Business rules enforced here (per the C2 build prompt), not just in the UI:
- MANAGER-LEVEL OR ABOVE IS REQUIRED FOR ALL SENDS — there is no lighter
  approval tier for smaller/routine filings in this build.
- A Send cannot be undone within Setu — correction is a fresh filing.
- A failed filing escalates to BOTH Manager and Partner.
- A partial/ambiguous failure is a FIRST-CLASS state, never folded into
  failed or pending; whether the portal actually received it must always
  be knowable and recorded.
- The API/manual toggle stays dormant/unavailable until a connected C4
  credential exists; manual upload is ALWAYS available regardless.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Optional

from src import db as recon_db
from src.filing import db as fdb
from src.filing.schema import init_filing_schema

# Source keys for the API/manual toggle. These map to C4 credential
# services and F5's sync sources. Only these two are filing-relevant.
SOURCE_KEYS = ["gst_portal", "traces"]

# Manager-or-above is required for ALL sends in this build.
SEND_AUTHORIZED_ROLES = {"Manager", "Partner", "Admin"}

# Final outcome states. 'ambiguous' is FIRST-CLASS (never folded into
# failed or pending), per the build prompt's near-emergency treatment.
OUTCOME_STATES = ("acknowledged", "failed", "ambiguous")


class FilingError(Exception):
    """Raised for expected filing-module failures (validation, blocked
    action, below-tier send attempt)."""


def init_filing(db_path=None) -> None:
    """Create C2 tables. Call once at app start, alongside the other
    modules' init_*() calls."""
    conn = _connect(db_path)
    try:
        init_filing_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Send authorization — Manager or above, no lighter tier
# ---------------------------------------------------------------------------


def can_send(actor_role: str) -> tuple[bool, Optional[str]]:
    """(allowed, reason_if_blocked). Manager, Partner and Admin may Send.
    Everything below Manager is blocked — there is deliberately no lighter
    tier in this build."""
    if actor_role in SEND_AUTHORIZED_ROLES:
        return True, None
    return False, "Requires Manager or above."


# ---------------------------------------------------------------------------
# Filing calendar — sourced from C1's Statutory Due Dates
# ---------------------------------------------------------------------------
# C2 depends on C1's statutory_due_dates (the "Statutory Due Dates" rule
# category). The calendar is materialized per client per return type per
# period. Due dates are computed from the obligation's due_day/due_month
# and the period. This module never mutates C1's table — it only reads it.


def _due_date_for(obligation: dict[str, Any], period_label: str) -> Optional[str]:
    """Compute the due date (ISO) for a given obligation + period label.
    ``period_label`` is expected to be a 'YYYY-MM' string (monthly/quarterly)
    or 'YYYY' (annual). Returns None when the obligation's period kind and
    the period label don't line up cleanly."""
    try:
        if obligation.get("period") == "annual":
            year = int(period_label[:4])
            month = obligation.get("due_month") or 3  # default to March if unset
            return date(year, month, (obligation.get("due_day") or 1)).isoformat()
        # monthly / quarterly: use the period's year + month (last month of
        # the period for quarterly).
        year = int(period_label[:4])
        month = int(period_label[5:7]) if len(period_label) >= 7 and period_label[4:5] == "-" else 1
        day = obligation.get("due_day") or 1
        return date(year, month, day).isoformat()
    except (ValueError, IndexError):
        return None


def build_calendar(client_id: int, *, periods: list[str], actor: str, db_path=None) -> list[dict[str, Any]]:
    """Materialize (or refresh) calendar entries for a client across the
    given periods, reading C1's statutory due dates. Returns the upserted
    entries. Idempotent (upsert by client/obligation/period)."""
    from src.rules import service as rules  # local import avoids a hard circular dep

    obligations = rules.list_statutory_due_dates(db_path=db_path)
    conn = _connect(db_path)
    try:
        entries: list[dict[str, Any]] = []
        for obligation in obligations:
            for period_label in periods:
                due = _due_date_for(obligation, period_label)
                if due is None:
                    continue
                entry_id = fdb.upsert_calendar_entry(
                    conn, client_id=client_id, obligation=obligation["obligation"],
                    period=period_label, due_date=due,
                )
                entries.append({
                    "entry_id": entry_id, "client_id": client_id,
                    "obligation": obligation["obligation"], "period": period_label,
                    "due_date": due,
                })
        return entries
    finally:
        conn.close()


def list_calendar(*, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        entries = fdb.list_calendar_entries(conn, client_id=client_id)
        today = date.today()
        for e in entries:
            try:
                d = date.fromisoformat(e["due_date"])
                e["days_until_due"] = (d - today).days
            except ValueError:
                e["days_until_due"] = None
        return entries
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Filing records — prepare -> send -> outcome -> acknowledge
# ---------------------------------------------------------------------------


def prepare_filing(
    *, client_id: int, obligation: str, period: str, package_document_id: Optional[int],
    actor: str, db_path=None,
) -> int:
    """Prepare a filing package. This phase's package is a GENERIC manual
    upload (any file) — a retrofit point once Module 2's GSTR-3B assembly
    exists. Returns the new filing_id."""
    if not obligation or not obligation.strip():
        raise FilingError("A return type (obligation) is required.")
    if not period or not period.strip():
        raise FilingError("A period is required.")
    conn = _connect(db_path)
    try:
        filing_id = fdb.create_filing_record(
            conn, client_id=client_id, obligation=obligation.strip(), period=period.strip(),
            package_document_id=package_document_id, created_by=actor,
        )
        fdb.log_audit(
            conn, filing_id=filing_id, entry_type="prepare", action="prepared",
            detail=f"{obligation} / {period}", actor=actor,
        )
        return filing_id
    finally:
        conn.close()


def attach_package(filing_id: int, *, package_document_id: int, actor: str, db_path=None) -> None:
    """Attach (or replace) the uploaded package document on a prepared filing."""
    conn = _connect(db_path)
    try:
        rec = fdb.get_filing_record(conn, filing_id)
        if rec is None:
            raise FilingError("Filing record not found.")
        if rec["status"] != "prepared":
            raise FilingError("Only a prepared filing can have its package changed.")
        fdb.attach_package_document(conn, filing_id, package_document_id)
        fdb.log_audit(
            conn, filing_id=filing_id, entry_type="prepare", action="package_attached",
            detail=f"document #{package_document_id}", actor=actor,
        )
    finally:
        conn.close()


def send_filing(filing_id: int, *, actor: str, actor_role: str, db_path=None) -> dict[str, Any]:
    """The explicit human Send action. Manager+ only, irreversible within
    Setu. Returns the updated filing record."""
    allowed, reason = can_send(actor_role)
    if not allowed:
        raise FilingError(reason or "You don't have permission to send filings.")

    conn = _connect(db_path)
    try:
        rec = fdb.get_filing_record(conn, filing_id)
        if rec is None:
            raise FilingError("Filing record not found.")
        if rec["status"] != "prepared":
            raise FilingError("Only a prepared filing can be sent.")
        if rec["package_document_id"] is None:
            raise FilingError("A filing package must be uploaded before sending.")

        sent_at = _now_iso()
        fdb.set_filing_sent(conn, filing_id, sent_by=actor, sent_at=sent_at)
        fdb.log_audit(
            conn, filing_id=filing_id, entry_type="send", action="sent",
            detail="Explicit human Send action (irreversible within Setu)", actor=actor,
        )
        # Retrofit the Send audit entry to C4's canonical sink as well.
        _log_send_to_vault(filing_id, actor, rec)
        return fdb.get_filing_record(conn, filing_id)
    finally:
        conn.close()


def record_outcome(
    filing_id: int, *, status: str, outcome_note: Optional[str], actor: str, db_path=None,
) -> dict[str, Any]:
    """Record the portal's outcome for a sent filing. 'ambiguous' is a
    FIRST-CLASS state and, like 'failed', fires a Manager+Partner
    escalation. 'acknowledged' closes the filing cleanly."""
    if status not in OUTCOME_STATES:
        raise FilingError(f"Unknown outcome: {status}")
    conn = _connect(db_path)
    try:
        rec = fdb.get_filing_record(conn, filing_id)
        if rec is None:
            raise FilingError("Filing record not found.")
        if rec["status"] != "sent":
            raise FilingError("Only a sent filing can have an outcome recorded.")

        fdb.set_filing_outcome(conn, filing_id, status=status, outcome_note=outcome_note)
        fdb.log_audit(
            conn, filing_id=filing_id, entry_type="outcome", action=status,
            detail=outcome_note, actor=actor,
        )
        if status in ("failed", "ambiguous"):
            _escalate(filing_id, status, rec, actor)
        return fdb.get_filing_record(conn, filing_id)
    finally:
        conn.close()


def confirm_acknowledgement(
    filing_id: int, *, acknowledgement_document_id: int, actor: str, db_path=None,
) -> dict[str, Any]:
    """Capture the portal's receipt through F3 as linked evidence, plus a
    manual confirmation step. Marks the filing acknowledged and links the
    document via F3's evidence_links."""
    conn = _connect(db_path)
    try:
        rec = fdb.get_filing_record(conn, filing_id)
        if rec is None:
            raise FilingError("Filing record not found.")
        if rec["status"] not in ("sent", "ambiguous", "failed"):
            raise FilingError("This filing is not in a state that can be acknowledged.")

        fdb.set_filing_acknowledgement(
            conn, filing_id, acknowledgement_document_id=acknowledgement_document_id,
            confirmed_by=actor,
        )
        fdb.log_audit(
            conn, filing_id=filing_id, entry_type="acknowledge", action="confirmed",
            detail=f"acknowledgement document #{acknowledgement_document_id}", actor=actor,
        )
        _link_evidence_document(acknowledgement_document_id, filing_id, actor)
        return fdb.get_filing_record(conn, filing_id)
    finally:
        conn.close()


def _escalate(filing_id: int, event_type: str, rec: dict[str, Any], actor: str) -> None:
    """Failed/ambiguous filing escalates to BOTH Manager and Partner. Records
    the escalation locally and registers a firm-mandatory C3 notification
    type (same escalation shape as C4's DPDP flow and F5's failures)."""
    conn = _connect()
    try:
        message = (
            f"{rec['obligation']} / {rec['period']} filing (client #{rec['client_id']}) "
            f"ended '{event_type}' — escalated to Manager + Partner."
        )
        fdb.insert_escalation(conn, filing_id=filing_id, event_type=event_type, message=message)
    finally:
        conn.close()
    _register_escalation_notification()


def _register_escalation_notification() -> None:
    """Ensure the firm-mandatory 'filing escalation' notification type exists
    via C3 (a Partner can't disable it). Best-effort: never breaks outcome
    recording if C3 is unavailable."""
    try:
        from src.settings import db as sdb

        conn = recon_db.get_connection()
        try:
            sdb.upsert_notification_pref(
                conn, "filing_escalation", "Filing failure / ambiguous outcome",
                "escalation", firm_default=True, firm_mandatory=True,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _log_send_to_vault(filing_id: int, actor: str, rec: dict[str, Any]) -> None:
    """Retrofit the Send audit entry to C4's canonical security-event sink
    (best-effort — C4 is the permanent home for these going forward)."""
    try:
        from src.vault import service as vault

        vault.log_security_event(
            "filing_sent", actor=actor, client_id=rec["client_id"],
            detail=f"filing #{filing_id}: {rec['obligation']} / {rec['period']}",
        )
    except Exception:  # noqa: BLE001
        pass


def _link_evidence_document(document_id: int, filing_id: int, actor: str) -> None:
    """Route the acknowledgement document through F3 as linked evidence
    (target_type = 'filing_record')."""
    try:
        from src.documents import service as documents

        documents.link_evidence(
            document_id=document_id, target_type="filing_record",
            target_id=filing_id, target_label=f"Filing #{filing_id}",
            actor=actor,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_filing(filing_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.get_filing_record(conn, filing_id)
    finally:
        conn.close()


def list_filings(*, client_id: Optional[int] = None, status: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_filing_records(conn, client_id=client_id, status=status)
    finally:
        conn.close()


def list_audit_log(filing_id: int, *, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_audit_log(conn, filing_id, limit=limit)
    finally:
        conn.close()


def list_escalations(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_escalations(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-client source toggle — dormant until C4 credentials exist
# ---------------------------------------------------------------------------


def source_availability(client_id: int, source: str, *, db_path=None) -> dict[str, Any]:
    """Whether the API-pull mode for a source is actually usable for this
    client. Reads LIVE from C4's credential status: a source stays
    unavailable until a connected credential exists. Manual upload is
    ALWAYS available regardless."""
    toggle = get_toggle(client_id, source, db_path=db_path)
    mode = toggle["mode"] if toggle else "manual"
    has_connected_credential = _has_connected_credential(client_id, source)
    return {
        "client_id": client_id,
        "source": source,
        "mode": mode,
        "api_available": has_connected_credential,
        "unavailable_reason": None if has_connected_credential else
            "Not yet available — coming once portal credentials are configured",
    }


def _has_connected_credential(client_id: int, source: str) -> bool:
    """Read LIVE from C4: is there a connected credential for this source
    (firm-wide or client-scoped)? Structural check, not a hardcoded stub."""
    try:
        from src.vault import service as vault

        service_name = "GST Portal" if source == "gst_portal" else "TRACES"
        # A credential is usable if a firm-wide (client_id NULL) or this
        # client's credential exists for the service and is connected.
        firm = vault.list_credentials(client_id=None, db_path=None)
        client_creds = vault.list_credentials(client_id=client_id, db_path=None)
        for c in firm + client_creds:
            if c.get("service") == service_name and c.get("status") == "connected":
                return True
        return False
    except Exception:  # noqa: BLE001 — treat as unavailable, never crash
        return False


def get_toggle(client_id: int, source: str, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.get_toggle(conn, client_id, source)
    finally:
        conn.close()


def set_toggle(client_id: int, source: str, mode: str, *, actor: str, db_path=None) -> None:
    """Persist the INTENDED mode. The mode is stored even though API mode
    stays dormant until C4 credentials exist — the field is structural."""
    if source not in SOURCE_KEYS:
        raise FilingError(f"Unknown source: {source}")
    if mode not in ("manual", "api"):
        raise FilingError(f"Unknown mode: {mode}")
    conn = _connect(db_path)
    try:
        fdb.upsert_toggle(conn, client_id=client_id, source=source, mode=mode)
    finally:
        conn.close()


def list_toggles(*, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return fdb.list_toggles(conn, client_id=client_id)
    finally:
        conn.close()
