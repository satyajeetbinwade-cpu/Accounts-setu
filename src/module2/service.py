"""Public API for Module 2 — Reconciliation Engine (2A GST / 2B TDS / 2C Other).

Every other module (and every UI file) should import from here, not from
src.module2.db directly. Mirrors src/f5/service.py's shape.

REUSE-FIRST, NOT A REBUILD FROM SCRATCH
----------------------------------------
This module EXTENDS Phase 1's already-tested matching engines — Unit 3
(src/matching/gst_matcher.py) and Unit 4 (src/matching/tds_matcher.py) —
rather than writing new matching logic from zero. The matchers themselves
are untouched; Module 2 layers the production concerns on top of their
output:

  * fuzzy confidence scoring is already produced by the matchers
    (confidence_score / confidence_band) and is surfaced here as a VISIBLE
    confidence badge — never a bare "matched" flag;
  * the exception queue (ReconciliationException, the generic Flagged Item
    shape Module 8 will consume);
  * the IMS Accept/Reject/Pending recommendation layer (2A) — a thin,
    conservative, explainable rules layer on the match result;
  * the TDS six-step chain trace (2B);
  * the live eligible-credit figure (2A);
  * 2C (Other) — six additional matching-key configurations layered onto
    the SAME shared engine, not a separate engine.

Business rules enforced here (per the Module 2 build prompt), not just in
the UI:
- IMS recommendations are NEVER silently auto-submitted — they always route
  through human review and C2's explicit Send gate once approved.
- A fuzzy match below the confidence threshold always routes to "needs
  review" — never auto-classified as Matched.
- "Not in 26AS" (2B) is distinguished from a genuine error — it may simply
  be a timing lag.
- Materiality-based routing is SEPARATE per reconciliation type (2A/2B/2C),
  each firm-wide-default-with-per-client-override per C1's pattern.
- Section 17(5) blocked-credit and reverse-charge items get separate
  handling within 2A — a "matched" invoice is not automatically eligible.
- TDS rate/section lookup is DETERMINISTIC against C1, never decided
  independently by Module 2. The one judgment-adjacent AI step is
  classification FROM NARRATION TEXT; the LLM must never invent a rate.

Out of scope (reaffirmed): drafting or posting the fix (Module 3's job);
IMS Accept/Reject/Pending submission (Module 2 recommends, C2 executes once
approved); the portal data pull itself (C2's job).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Optional

from src import db as recon_db
from src.module2 import db as mdb
from src.module2.schema import init_module2_schema

# Sub-type -> recon_type mapping. 2A is GST, 2B is TDS, 2C is Other.
SUB_TYPE_RECON = {"2A": "GST", "2B": "TDS", "2C": "OTHER"}

# The six 2C sources, layered onto the SAME shared engine.
OTHER_SOURCES = [
    ("bank", "Bank"),
    ("vendor_ledger", "Vendor Ledger"),
    ("form26as", "Form 26AS"),
    ("opening_balances", "Opening Balances"),
    ("loan_sheet", "Loan Sheet"),
    ("salary", "Salary"),
]

# The six TDS chain stages (2B), in order.
TDS_CHAIN_STAGES = [
    ("applicability", "Applicability"),
    ("rate", "Rate"),
    ("deduction", "Deduction"),
    ("deposit", "Deposit"),
    ("return", "Return"),
    ("reflection", "26AS reflection"),
]

# Stage states (Foundation: complete=resolved-green, gap=escalated-coral,
# timing_lag=AI-suggested-amber).
STAGE_COMPLETE = "complete"
STAGE_GAP = "gap"
STAGE_TIMING_LAG = "timing_lag"

# Classifications that are NOT clean matches — every one becomes an exception.
EXCEPTION_CLASSIFICATIONS = ("Not in Books", "Not in Portal", "Amount Difference")

# IMS recommendation values (2A).
IMS_ACCEPT = "Accept"
IMS_REJECT = "Reject"
IMS_PENDING = "Pending"

# Priority values.
PRIORITY_LOW = "low"
PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
PRIORITY_ESCALATED = "escalated"


class Module2Error(Exception):
    """Raised for expected Module 2 failures (validation, blocked action)."""


# ---------------------------------------------------------------------------
# Init + connection
# ---------------------------------------------------------------------------


def init_module2(db_path=None) -> None:
    """Create Module 2 tables and seed the matching-key configurations. Call
    once at app start, alongside the other modules' init_*() calls."""
    conn = _connect(db_path)
    try:
        init_module2_schema(conn)
        _seed_matching_key_configs(conn, db_path)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# MatchingKeyConfig — genuinely data-driven (a new 2C source is addable via
# configuration, not a new code path)
# ---------------------------------------------------------------------------


def _seed_matching_key_configs(conn: sqlite3.Connection, db_path=None) -> None:
    """Seed the per-sub-type matching-key configurations. Additive-upsert —
    never clobbers an Admin's tuned values. Tolerance/key definitions are
    read from C1 at run time; the values here are the resolved defaults.

    ``db_path`` must be threaded into the C1 read: initialising a scratch DB
    (a test DB, or any non-default path) otherwise read C1's rules from the
    *default* DB and failed with "no such table: rules".
    """
    from src.rules import service as rules

    def _tol(key: str, default: float) -> float:
        v = rules.get_effective_number(key, default=default, db_path=db_path)
        return default if v is None else v

    configs: list[dict[str, Any]] = [
        {
            "sub_type": "2A", "source_key": "gst", "label": "GST (2A)",
            "match_keys": json.dumps(["gstin", "invoice_number", "invoice_date", "invoice_value"]),
            "tolerance_absolute": _tol("gst.amount_tolerance.absolute", 10.0),
            "tolerance_percent": _tol("gst.amount_tolerance.percent", 1.0),
            "date_tolerance_days": int(_tol("gst.date_tolerance_days", 5)),
            "fuzzy_threshold": _tol("gst.fuzzy_threshold", 85.0),
            "materiality_threshold": _tol("materiality.gst", 10000.0),
        },
        {
            "sub_type": "2B", "source_key": "tds", "label": "TDS (2B)",
            "match_keys": json.dumps(["pan", "section", "amount_paid_credited", "tax_deducted"]),
            "tolerance_absolute": _tol("tds.amount_tolerance.absolute", 10.0),
            "tolerance_percent": _tol("tds.amount_tolerance.percent", 1.0),
            "date_tolerance_days": int(_tol("tds.date_tolerance_days", 7)),
            "fuzzy_threshold": _tol("tds.fuzzy_threshold", 80.0),
            "materiality_threshold": _tol("materiality.tds", 5000.0),
        },
    ]
    # 2C — six additional matching-key configurations on the SAME engine.
    for source_key, label in OTHER_SOURCES:
        configs.append({
            "sub_type": "2C", "source_key": source_key, "label": f"{label} (2C)",
            "match_keys": json.dumps(["reference", "date", "amount"]),
            "tolerance_absolute": 10.0,
            "tolerance_percent": 1.0,
            "date_tolerance_days": 7,
            "fuzzy_threshold": 80.0,
            "materiality_threshold": _tol("materiality.other", 5000.0),
        })

    for cfg in configs:
        mdb.upsert_matching_key_config(conn, cfg)


def list_matching_key_configs(*, sub_type: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = mdb.list_matching_key_configs(conn, sub_type=sub_type)
        for r in rows:
            try:
                r["match_keys"] = json.loads(r["match_keys"])
            except (TypeError, ValueError):
                pass
        return rows
    finally:
        conn.close()


def materiality_threshold_for(sub_type: str, *, client_id: Optional[int] = None, db_path=None) -> float:
    """The materiality threshold in force for a sub-type, honouring C1's
    per-client override over the firm-wide default. Separate per sub-type
    (2A/2B/2C), never one universal number."""
    from src.rules import service as rules

    key = {"2A": "materiality.gst", "2B": "materiality.tds", "2C": "materiality.other"}.get(sub_type)
    if key is None:
        return 0.0
    v = rules.get_effective_number(key, client_id=client_id, default=0.0)
    return 0.0 if v is None else v


# ---------------------------------------------------------------------------
# C3-ext model routing — now genuinely routed through the central AI model
# registry (Setup → AI Models). Kept as a thin descriptor so callers that read
# ``routing`` keep working.
# ---------------------------------------------------------------------------


def _route_via_c3ext(touchpoint_key: str) -> dict[str, Any]:
    """Describe routing for a touchpoint. The provider + model are resolved at
    call time by ``src.ai_models.gateway`` from the registry, so this is
    informational only. Never raises."""
    return {
        "routed": True, "touchpoint": touchpoint_key,
        "reason": "Routed via the AI model registry (Setup → AI Models).",
    }


def _c5_runtime_context(touchpoint_key: str, client_id: Optional[int] = None) -> str:
    """C5 retrofit: retrieve the live instruction-library context for the
    touchpoint so it's genuinely included at runtime, not an empty slot.
    Best-effort — any C5 error yields '', never breaks the run."""
    try:
        from src.c5 import service as c5

        return c5.runtime_context(touchpoint_key, client_id=client_id)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------


def _load_record(raw: Any) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _record_value(record: Optional[dict[str, Any]], keys: tuple[str, ...]) -> float:
    """First present numeric value among ``keys`` in a record, else 0.0."""
    if not isinstance(record, dict):
        return 0.0
    for k in keys:
        v = record.get(k)
        if v is None or v == "":
            continue
        try:
            return abs(float(v))
        except (TypeError, ValueError):
            continue
    return 0.0


def _tax_of(record: Optional[dict[str, Any]]) -> float:
    """Total tax on a GST record (total_tax, else sum of components)."""
    if not isinstance(record, dict):
        return 0.0
    tt = record.get("total_tax")
    if tt is not None and tt != "":
        try:
            return abs(float(tt))
        except (TypeError, ValueError):
            pass
    return sum(
        _record_value(record, (c,)) for c in ("cgst", "sgst", "igst", "cess")
    )


def _item_value(record: Optional[dict[str, Any]], recon_type: str) -> float:
    """The rupee value of a record for materiality purposes."""
    if recon_type == "GST":
        return _record_value(record, ("invoice_value", "taxable_value")) or _tax_of(record)
    if recon_type == "OTHER":
        # 2C records use the generic reference/date/amount identity.
        return _record_value(record, ("amount", "invoice_value"))
    return _record_value(record, ("amount_paid_credited", "tax_deducted", "tax_deposited"))


def _is_reverse_charge(record: Optional[dict[str, Any]]) -> bool:
    if not isinstance(record, dict):
        return False
    dt = str(record.get("document_type", "")).strip().lower()
    return dt in ("reverse charge", "rcm")


def _is_blocked_credit(record: Optional[dict[str, Any]]) -> bool:
    """Section 17(5) blocked-credit marker. The matcher carries an
    eligibility marker through where present but does not interpret it; we
    read it here so a "matched" invoice is not automatically eligible."""
    if not isinstance(record, dict):
        return False
    for key in ("itc_eligible", "itc_eligibility", "blocked_credit"):
        v = record.get(key)
        if v is None:
            continue
        if isinstance(v, bool):
            return not v if key != "blocked_credit" else v
        s = str(v).strip().lower()
        if key == "blocked_credit":
            return s in ("true", "1", "yes", "blocked")
        return s in ("false", "0", "no", "blocked", "ineligible")
    return False


# ---------------------------------------------------------------------------
# Exception generation — the retrofit point that wires a matching run into
# the exception queue, the IMS layer, the TDS chain, eligible credit and F5.
# ---------------------------------------------------------------------------


def generate_exceptions_for_run(
    run_id: int, *, client_id: int, actor: str = "system", db_path=None,
) -> dict[str, Any]:
    """Read a completed run's match_results and materialize Module 2's
    production layer on top of it:

      * one ReconciliationException per non-clean match (the generic Flagged
        Item shape Module 8 will consume);
      * an IMS Accept/Reject/Pending recommendation per 2A exception;
      * a six-step TDS chain trace per 2B exception;
      * the live eligible-credit figure (2A);
      * the F5 recon-of-recon check (matched+unmatched+excluded vs ingested).

    Idempotent per run: re-running for the same run_id clears that run's
    prior exceptions first, so a re-generation never duplicates.
    """
    conn = _connect(db_path)
    try:
        run = conn.execute(
            "SELECT client, period, recon_type FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise Module2Error(f"Run {run_id} not found.")
        _client, period, recon_type = run
        sub_type = {"GST": "2A", "TDS": "2B", "OTHER": "2C"}.get(recon_type, "2C")

        # Clear this run's prior exceptions (idempotent re-generation).
        conn.execute("DELETE FROM reconciliation_exceptions WHERE run_id = ?", (run_id,))
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM match_results WHERE run_id = ? ORDER BY result_id", (run_id,)
        ).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM match_results WHERE 0").description]
        results = [dict(zip(cols, r)) for r in rows]

        materiality = materiality_threshold_for(sub_type, client_id=client_id, db_path=db_path)

        # Reviewer corrections made in the Review screen are applied on top of
        # the engine's records, so an edit reaches the exceptions and the
        # Action Center. Keyed by fingerprint (carried forward across runs).
        from src import queries as _queries

        override_map = _queries.get_field_overrides(_client, period, recon_type, db_path=db_path)

        created = 0
        ims_count = 0
        chain_count = 0
        for r in results:
            classification = r["classification"]
            if classification not in EXCEPTION_CLASSIFICATIONS:
                continue
            books = _load_record(r.get("books_record"))
            portal = _load_record(r.get("portal_record"))
            by_side = override_map.get(r.get("fingerprint"))
            if by_side:
                for side, field_map in by_side.items():
                    target = books if side == "books" else portal
                    if isinstance(target, dict):
                        for field, meta in field_map.items():
                            target[field] = meta.get("value")
            value = _item_value(books, recon_type) or _item_value(portal, recon_type)
            above = value >= materiality if materiality else False

            item_type = r.get("difference_type") or classification
            priority = _priority_for(classification, above, r.get("confidence_band"))

            exception_id = mdb.insert_exception(conn, {
                "client_id": client_id, "run_id": run_id, "result_id": r["result_id"],
                "fingerprint": r.get("fingerprint"), "sub_type": sub_type,
                "recon_type": recon_type, "item_type": item_type,
                "classification": classification, "difference_type": r.get("difference_type"),
                "confidence_score": r.get("confidence_score"),
                "confidence_band": r.get("confidence_band"),
                "confidence_source": "rule",
                "priority": priority,
                "status": "open",
                "evidence": json.dumps({
                    "books_record": books, "portal_record": portal,
                    "matched_record_ids": _load_record(r.get("matched_record_ids")),
                }, default=str),
                "match_reason": r.get("match_reason"),
                "materiality_threshold": materiality,
                "above_materiality": above,
            })
            created += 1

            # 2A — IMS recommendation (thin, conservative, explainable).
            if sub_type == "2A":
                rec = _ims_recommendation(classification, r.get("difference_type"), books, portal)
                mdb.insert_recommendation(conn, {
                    "exception_id": exception_id, "run_id": run_id,
                    "fingerprint": r.get("fingerprint"), "touchpoint": "ims_recommendation",
                    "recommendation": rec["recommendation"], "reasoning": rec["reasoning"],
                    "confidence_pct": rec.get("confidence_pct"),
                    "routing_stub": True,
                })
                conn.execute(
                    "UPDATE reconciliation_exceptions SET recommendation = ?, recommendation_reason = ? "
                    "WHERE exception_id = ?",
                    (rec["recommendation"], rec["reasoning"], exception_id),
                )
                conn.commit()
                ims_count += 1

            # 2B — six-step TDS chain trace.
            if sub_type == "2B":
                stages = _tds_chain(books, portal, r.get("difference_type"), classification)
                for i, st in enumerate(stages):
                    mdb.insert_chain_stage(conn, {
                        "exception_id": exception_id, "stage_key": st["key"],
                        "stage_label": st["label"], "stage_state": st["state"],
                        "detail": st.get("detail"), "sort_order": i,
                    })
                chain_count += 1

        # 2A — live eligible-credit figure.
        if sub_type == "2A":
            _compute_eligible_credit(conn, client_id=client_id, run_id=run_id, period=period, results=results)

        # F5 retrofit — recon-of-recon check.
        recon_check = _run_recon_of_recon(
            client_id=client_id, run_id=run_id, results=results, recon_type=recon_type, db_path=db_path,
        )

        mdb.log_change(
            conn, exception_id=None, entry_kind="audit_trail", action="generate_exceptions",
            detail=f"run {run_id}: {created} exception(s), {ims_count} IMS rec(s), {chain_count} TDS chain(s)",
            reason=None, actor=actor,
        )

        return {
            "run_id": run_id, "sub_type": sub_type, "exceptions_created": created,
            "ims_recommendations": ims_count, "tds_chains": chain_count,
            "recon_of_recon": recon_check,
        }
    finally:
        conn.close()


def _priority_for(classification: str, above_materiality: bool, band: Optional[str]) -> str:
    """Priority: above-materiality escalates; low-confidence fuzzy matches
    are high; everything else normal."""
    if above_materiality:
        return PRIORITY_ESCALATED
    if band == "Low":
        return PRIORITY_HIGH
    if classification == "Amount Difference":
        return PRIORITY_HIGH
    return PRIORITY_NORMAL


# ---------------------------------------------------------------------------
# IMS recommendation layer (2A) — thin, conservative, explainable rules on
# the match result. NEVER silently auto-submitted.
# ---------------------------------------------------------------------------


def _ims_recommendation(
    classification: str, difference_type: Optional[str],
    books: Optional[dict[str, Any]], portal: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """A conservative, explainable recommendation. The reasoning is always
    first-class text (never a tooltip). This is a RULE layer, not an AI
    call — the confidence is the rule engine's own."""
    if classification == "Matched":
        return {
            "recommendation": IMS_ACCEPT,
            "reasoning": "Books and portal agree on this transaction — safe to accept in IMS.",
            "confidence_pct": 98,
        }
    if classification == "Not in Books":
        return {
            "recommendation": IMS_REJECT,
            "reasoning": (
                "The portal shows this transaction but it is missing from the books. "
                "Rejecting in IMS avoids claiming credit that isn't supported by your records."
            ),
            "confidence_pct": 90,
        }
    if classification == "Not in Portal":
        return {
            "recommendation": IMS_PENDING,
            "reasoning": (
                "The books show this transaction but the portal does not. This may be a "
                "supplier filing lag — keep it Pending rather than rejecting, and re-check "
                "after the supplier files."
            ),
            "confidence_pct": 75,
        }
    # Amount Difference
    if difference_type == "Rounding":
        return {
            "recommendation": IMS_ACCEPT,
            "reasoning": "The difference is within rounding tolerance — safe to accept in IMS.",
            "confidence_pct": 92,
        }
    if difference_type == "Timing Difference":
        return {
            "recommendation": IMS_PENDING,
            "reasoning": (
                "Same transaction recorded in different periods — a timing difference. "
                "Keep it Pending until both periods are reconciled."
            ),
            "confidence_pct": 80,
        }
    return {
        "recommendation": IMS_PENDING,
        "reasoning": (
            f"Books and portal disagree ({difference_type or 'amount difference'}). "
            "This needs a human decision before any IMS action — keep it Pending."
        ),
        "confidence_pct": 60,
    }


# ---------------------------------------------------------------------------
# TDS six-step chain (2B) — applicability -> rate -> deduction -> deposit ->
# return -> 26AS reflection. Distinguishes a genuine gap from a timing lag.
# ---------------------------------------------------------------------------


def _tds_chain(
    books: Optional[dict[str, Any]], portal: Optional[dict[str, Any]],
    difference_type: Optional[str], classification: str,
) -> list[dict[str, Any]]:
    """Build the six-stage trace. A stage is complete / gap / timing_lag.
    A timing-lag read is itself an AI-assisted judgment call (amber token)."""
    books = books or {}
    portal = portal or {}

    section = str(books.get("section") or portal.get("section") or "").strip()
    amount = _record_value(books, ("amount_paid_credited",)) or _record_value(portal, ("amount_paid_credited",))
    deducted = _record_value(books, ("tax_deducted",)) or _record_value(portal, ("tax_deducted",))
    deposited = _record_value(books, ("tax_deposited",)) or _record_value(portal, ("tax_deposited",))
    has_portal = bool(portal)

    stages: list[dict[str, Any]] = []

    # 1. Applicability — is the section applicable (threshold crossed)?
    if section:
        stages.append({"key": "applicability", "label": "Applicability", "state": STAGE_COMPLETE,
                       "detail": f"Section {section} applies to this payment."})
    else:
        stages.append({"key": "applicability", "label": "Applicability", "state": STAGE_GAP,
                       "detail": "No TDS section recorded — applicability cannot be established."})

    # 2. Rate — deterministic against C1 (never decided by Module 2).
    if difference_type == "Rate Mismatch":
        stages.append({"key": "rate", "label": "Rate", "state": STAGE_GAP,
                       "detail": "The rate applied does not match the section rate in C1."})
    elif section:
        stages.append({"key": "rate", "label": "Rate", "state": STAGE_COMPLETE,
                       "detail": f"Rate applied is consistent with section {section} (C1)."})
    else:
        stages.append({"key": "rate", "label": "Rate", "state": STAGE_GAP,
                       "detail": "Rate cannot be validated without a section."})

    # 3. Deduction — was tax actually deducted?
    if deducted > 0:
        stages.append({"key": "deduction", "label": "Deduction", "state": STAGE_COMPLETE,
                       "detail": f"Tax deducted: ₹{deducted:,.2f}."})
    else:
        stages.append({"key": "deduction", "label": "Deduction", "state": STAGE_GAP,
                       "detail": "No tax was deducted on this payment."})

    # 4. Deposit — was the deducted tax deposited?
    if deducted > 0 and deposited >= deducted - 1:
        stages.append({"key": "deposit", "label": "Deposit", "state": STAGE_COMPLETE,
                       "detail": f"Tax deposited: ₹{deposited:,.2f}."})
    elif deducted > 0:
        stages.append({"key": "deposit", "label": "Deposit", "state": STAGE_GAP,
                       "detail": f"Deducted ₹{deducted:,.2f} but only ₹{deposited:,.2f} deposited."})
    else:
        stages.append({"key": "deposit", "label": "Deposit", "state": STAGE_GAP,
                       "detail": "Nothing to deposit — no deduction recorded."})

    # 5. Return — was the TDS return filed? No filing data in this build, so
    #    a missing portal record reads as a timing lag, not a hard gap.
    if has_portal:
        stages.append({"key": "return", "label": "Return", "state": STAGE_COMPLETE,
                       "detail": "Return data present on the portal side."})
    else:
        stages.append({"key": "return", "label": "Return", "state": STAGE_TIMING_LAG,
                       "detail": "No return data yet — may simply be a filing lag."})

    # 6. 26AS reflection — is the deduction reflected in Form 26AS?
    if has_portal:
        stages.append({"key": "reflection", "label": "26AS reflection", "state": STAGE_COMPLETE,
                       "detail": "Deduction is reflected in Form 26AS."})
    elif classification == "Not in Portal":
        stages.append({"key": "reflection", "label": "26AS reflection", "state": STAGE_TIMING_LAG,
                       "detail": "Not yet reflected in 26AS — this may be a timing lag, not an error."})
    else:
        stages.append({"key": "reflection", "label": "26AS reflection", "state": STAGE_GAP,
                       "detail": "Deduction is not reflected in 26AS."})

    return stages


# ---------------------------------------------------------------------------
# Eligible credit (2A) — live, recalculated on each run
# ---------------------------------------------------------------------------


def _compute_eligible_credit(
    conn: sqlite3.Connection, *, client_id: int, run_id: int, period: str,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute the live "eligible credit as things stand" figure. Section
    17(5) blocked-credit and reverse-charge items are handled separately — a
    "matched" invoice is not automatically eligible."""
    total_itc = 0.0
    matched_itc = 0.0
    at_risk_itc = 0.0
    blocked_itc = 0.0
    reverse_charge_itc = 0.0

    for r in results:
        books = _load_record(r.get("books_record"))
        portal = _load_record(r.get("portal_record"))
        # PORTAL-first, the same side the Review model and the report use for
        # a row's tax. Reading books-first made the eligible-credit figure
        # drift from the matched rows' portal tax by a row's books-side
        # rounding (SG/301: books 3,241.80 vs portal 3,240.00 — a ₹1.80 gap).
        tax = _tax_of(portal) or _tax_of(books)
        classification = r["classification"]

        total_itc += tax
        if classification == "Matched":
            matched_itc += tax
            if _is_blocked_credit(books) or _is_blocked_credit(portal):
                blocked_itc += tax
            if _is_reverse_charge(books) or _is_reverse_charge(portal):
                reverse_charge_itc += tax
        elif classification in ("Not in Portal", "Amount Difference"):
            at_risk_itc += tax

    # Eligible credit = matched ITC less blocked-credit ITC. Reverse-charge
    # ITC is tracked separately (claimable only once the tax is paid by the
    # recipient) and is NOT folded into the headline eligible figure.
    eligible = max(matched_itc - blocked_itc, 0.0)

    figure_id = mdb.insert_eligible_credit(conn, {
        "client_id": client_id, "run_id": run_id, "period": period,
        "total_itc_claimed": round(total_itc, 2), "matched_itc": round(matched_itc, 2),
        "at_risk_itc": round(at_risk_itc, 2), "blocked_credit_itc": round(blocked_itc, 2),
        "reverse_charge_itc": round(reverse_charge_itc, 2),
        "eligible_credit": round(eligible, 2),
    })
    return {
        "figure_id": figure_id, "total_itc_claimed": round(total_itc, 2),
        "matched_itc": round(matched_itc, 2), "at_risk_itc": round(at_risk_itc, 2),
        "blocked_credit_itc": round(blocked_itc, 2),
        "reverse_charge_itc": round(reverse_charge_itc, 2),
        "eligible_credit": round(eligible, 2),
    }


def eligible_credit_for_client(
    client_id: int, *, period: Optional[str] = None, db_path=None,
) -> Optional[dict[str, Any]]:
    """The most recent live eligible-credit figure for a client (2A)."""
    conn = _connect(db_path)
    try:
        return mdb.latest_eligible_credit(conn, client_id=client_id, period=period)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# F5 retrofit — recon-of-recon check
# ---------------------------------------------------------------------------


def _run_recon_of_recon(
    *, client_id: int, run_id: int, results: list[dict[str, Any]], recon_type: str, db_path=None,
) -> dict[str, Any]:
    """Wire F5's previously-unwired recon-of-recon data model: confirm
    matched + unmatched + excluded sums tie back to the ingested totals from
    both sides. Best-effort — never breaks exception generation."""
    try:
        from src.f5 import service as f5

        matched = unmatched = excluded = ingested = 0.0
        for r in results:
            books = _load_record(r.get("books_record"))
            portal = _load_record(r.get("portal_record"))
            value = _item_value(books, recon_type) or _item_value(portal, recon_type)
            classification = r["classification"]
            if classification == "Matched":
                matched += value
            elif classification in ("Not in Books", "Not in Portal"):
                unmatched += value
            else:
                excluded += value
            ingested += value

        return f5.record_recon_of_recon(
            client_id=client_id, run_id=run_id, matched_sum=round(matched, 2),
            unmatched_sum=round(unmatched, 2), excluded_sum=round(excluded, 2),
            ingested_total=round(ingested, 2), db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "ties_out": None}


# ---------------------------------------------------------------------------
# Exception queries + resolution (Accept / Reject / Escalate)
# ---------------------------------------------------------------------------


def list_exceptions(
    *, client_id: Optional[int] = None, run_id: Optional[int] = None,
    sub_type: Optional[str] = None, status: Optional[str] = None,
    classification: Optional[str] = None, db_path=None,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return mdb.list_exceptions(
            conn, client_id=client_id, run_id=run_id, sub_type=sub_type,
            status=status, classification=classification,
        )
    finally:
        conn.close()


def get_exception(exception_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        exc = mdb.get_exception(conn, exception_id)
        if exc is None:
            return None
        try:
            exc["evidence"] = json.loads(exc["evidence"]) if exc.get("evidence") else {}
        except (TypeError, ValueError):
            exc["evidence"] = {}
        return exc
    finally:
        conn.close()


def exception_summary(*, client_id: Optional[int] = None, run_id: Optional[int] = None, db_path=None) -> dict[str, Any]:
    """Counts by classification + by sub-type, for the headline→grouped→detail
    screen. Each group is clickable through to a pre-filtered detail view."""
    exceptions = list_exceptions(client_id=client_id, run_id=run_id, db_path=db_path)
    by_classification: dict[str, int] = {}
    by_sub_type: dict[str, int] = {}
    open_count = 0
    escalated = 0
    for e in exceptions:
        by_classification[e["classification"]] = by_classification.get(e["classification"], 0) + 1
        by_sub_type[e["sub_type"]] = by_sub_type.get(e["sub_type"], 0) + 1
        if e["status"] == "open":
            open_count += 1
        if e["priority"] == PRIORITY_ESCALATED:
            escalated += 1
    return {
        "total": len(exceptions), "open": open_count, "escalated": escalated,
        "by_classification": by_classification, "by_sub_type": by_sub_type,
    }


def resolve_exception(
    exception_id: int, *, resolution: str, note: Optional[str], actor: str, db_path=None,
) -> dict[str, Any]:
    """Accept / Reject / Escalate an exception. Escalation routes into
    Module 8's existing escalation/priority mechanism (no new escalation UI
    is built here) — until Module 8 exists, it sets priority='escalated'."""
    if resolution not in ("accepted", "rejected", "escalated"):
        raise Module2Error(f"Unknown resolution: {resolution}")
    conn = _connect(db_path)
    try:
        exc = mdb.get_exception(conn, exception_id)
        if exc is None:
            raise Module2Error("Exception not found.")

        status = "escalated" if resolution == "escalated" else "resolved"
        mdb.resolve_exception(
            conn, exception_id, resolution=resolution, note=note, actor=actor, status=status,
        )
        if resolution == "escalated":
            mdb.set_exception_priority(conn, exception_id, PRIORITY_ESCALATED)

        # Audit Trail Entry + Edit History Entry (local stub, retrofit to F4).
        mdb.log_change(
            conn, exception_id=exception_id, entry_kind="audit_trail",
            action=f"resolve:{resolution}", detail=note, reason=note, actor=actor,
        )
        mdb.log_change(
            conn, exception_id=exception_id, entry_kind="edit_history",
            action=f"status:{exc['status']}->{status}", detail=note, reason=note, actor=actor,
        )
        return mdb.get_exception(conn, exception_id) or {}
    finally:
        conn.close()


def recommendations_for_exception(exception_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return mdb.list_recommendations(conn, exception_id=exception_id)
    finally:
        conn.close()


def tds_chain_for_exception(exception_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return mdb.list_chain_stages(conn, exception_id)
    finally:
        conn.close()


def list_change_log(*, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return mdb.list_change_log(conn, limit=limit)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TDS classification from narration text — the ONE judgment-adjacent AI step.
# The LLM suggests a classification FROM NARRATION TEXT; the rate lookup
# itself is always deterministic against C1. The LLM must never invent a rate.
# This is the THIRD ACTIVE row in C3-extended's model-routing table.
# ---------------------------------------------------------------------------


def _tds_section_options(*, db_path=None) -> list[dict[str, Any]]:
    """The TDS sections C1 knows about, so the model is constrained to a real
    section and can never invent one."""
    try:
        from src.rules import service as rules

        out: list[dict[str, Any]] = []
        for r in rules.list_regulatory_rules(db_path=db_path):
            if r.get("domain") == "TDS" and r.get("section"):
                out.append({
                    "section": str(r["section"]),
                    "rate_or_rule": str(r.get("rate_or_rule") or ""),
                })
        return out
    except Exception:  # noqa: BLE001
        return []


def classify_tds_from_narration(
    narration: str, *, client_id: Optional[int] = None, db_path=None,
) -> dict[str, Any]:
    """Suggest a TDS section from free-text narration via the configured AI
    touchpoint (provider + model resolved by the AI gateway, with primary →
    fallback failover).

    The rate is ALWAYS looked up deterministically from C1 — the model never
    invents a rate. When both legs of the touchpoint are unavailable the call
    degrades to "AI unavailable — proceed manually." rather than fabricating a
    suggestion.
    """
    routing = _route_via_c3ext("tds_classification")
    c5_context = _c5_runtime_context("tds_classification", client_id)

    def _unavailable(reason: str, meta: Optional[dict[str, Any]] = None, detail: str = "") -> dict[str, Any]:
        return {
            "available": False, "section": None, "rate": None, "reasoning": reason,
            "detail": detail,
            "routing": routing, "c5_context_used": bool(c5_context),
            "provider": (meta or {}).get("provider"), "model": (meta or {}).get("model"),
            "latency_ms": (meta or {}).get("latency_ms"),
        }

    text = (narration or "").strip()
    if not text:
        return _unavailable("No narration to classify.")

    sections = _tds_section_options(db_path=db_path)
    allowed = {s["section"] for s in sections}
    section_lines = "\n".join(
        f"  - {s['section']}: {s['rate_or_rule']}" for s in sections
    ) or "  (none configured)"

    system_prompt = (
        "You are a TDS classification assistant for an Indian accounting firm. "
        "Given a free-text narration, choose the SINGLE most likely TDS section. "
        "You MUST choose from this list of sections configured in the platform:\n"
        f"{section_lines}\n"
        "Never invent a section, and NEVER state a rate — the rate is looked up "
        "separately from the platform's regulatory table. "
        'Respond with JSON only: {"section": "<code or null>", "confidence": <0-100>, '
        '"reasoning": "<one or two sentences>"}.'
    )
    user_prompt = f"{text}\n\n{c5_context}" if c5_context else text

    try:
        from src.ai_models import gateway

        parsed, meta = gateway.call_touchpoint_json(
            "tds_classification", system_prompt, user_prompt,
            max_tokens=400, temperature=0.0,
            actor="system:module2", client_id=client_id, db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001 — any failure degrades honestly
        return _unavailable("AI unavailable — proceed manually.", None, detail=str(exc))

    section = str(parsed.get("section") or "").strip() or None
    if section is not None and allowed and section not in allowed:
        # The model proposed a section C1 does not know — do not trust it.
        section = None
    reasoning = str(parsed.get("reasoning") or "").strip()

    if section is None:
        return _unavailable(
            reasoning or "AI could not determine a section — proceed manually.", meta,
        )

    # Rate lookup is ALWAYS deterministic against C1's section table.
    rate = _deterministic_section_rate(section, db_path=db_path)
    return {
        "available": True, "section": section, "rate": rate,
        "reasoning": reasoning or (
            f"Narration suggests section {section}. The rate shown is looked up "
            "deterministically from C1 — the model never sets a rate."
        ),
        "confidence": parsed.get("confidence"),
        "routing": routing, "c5_context_used": bool(c5_context),
        "provider": meta.get("provider"), "model": meta.get("model"),
        "latency_ms": meta.get("latency_ms"),
    }


def _deterministic_section_rate(section: str, *, db_path=None) -> Optional[float]:
    """Look up the section's default rate from C1's regulatory rules. Never
    decided independently by Module 2."""
    try:
        from src.rules import service as rules

        for r in rules.list_regulatory_rules(db_path=db_path):
            if r.get("domain") == "TDS" and str(r.get("section")) == section:
                raw = str(r.get("rate_or_rule", ""))
                # e.g. "2.0% (1.0% individual/HUF)" -> 2.0
                import re

                m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", raw)
                if m:
                    return float(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# Forward-module routing — NO-OP PLACEHOLDERS (Modules 1/3/8 don't exist yet)
# ---------------------------------------------------------------------------


def route_to_module8_stub(exception_id: int, *, db_path=None) -> dict[str, Any]:
    """Every Reconciliation Exception routes DIRECTLY into Module 8's queue
    (built next) as the interim single point of human action. Module 8 does
    not exist in this repo yet, so this is a documented no-op placeholder —
    the exception already lives in reconciliation_exceptions, which is the
    generic Flagged Item shape Module 8 will consume."""
    return {
        "routed": False, "target": "Module 8", "exception_id": exception_id,
        "reason": "Module 8 not yet built in this repo — exception is held in the generic Flagged Item table.",
    }


def route_to_module1_stub(exception_id: int, *, db_path=None) -> dict[str, Any]:
    """Routing to Module 1's Information Requests is a NO-OP PLACEHOLDER —
    not user-facing, since Module 1 doesn't exist."""
    return {"routed": False, "target": "Module 1", "exception_id": exception_id,
            "reason": "Module 1 not yet built in this repo."}


def route_to_module3_stub(exception_id: int, *, db_path=None) -> dict[str, Any]:
    """Routing to Module 3's Corrective Entry candidates is a NO-OP
    PLACEHOLDER — not user-facing, since Module 3 doesn't exist."""
    return {"routed": False, "target": "Module 3", "exception_id": exception_id,
            "reason": "Module 3 not yet built in this repo."}


def link_evidence_to_exception(
    exception_id: int, *, document_id: int, actor: str, db_path=None,
) -> int:
    """Link a document to an exception via F3's generic polymorphic
    evidence_links (target_type='reconciliation_exception')."""
    from src.documents import service as documents

    return documents.link_evidence(
        document_id=document_id, target_type="reconciliation_exception",
        target_id=exception_id, target_label=f"Reconciliation Exception #{exception_id}",
        actor=actor, db_path=db_path,
    )