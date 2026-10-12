"""Phase-3 service: the deterministic rules layer over a Phase-2 extraction.

    python -m src.invoice_extract.rules show <document_id>

WHAT THIS LAYER IS
------------------
Everything here can be computed from the stored values with NO model in the
loop: derived figures, pass/fail checks, applicability, direction and client
aliases. It computes no confidence scores (Phase 4) and it NEVER writes to
``invoice_doc_fields`` — the extracted values are read-only input.

``run_rules(extraction_id)`` is idempotent: it replaces its OWN rows for that
extraction (derived values, checks, ``source = 'rule'`` applicability rows,
direction) and leaves everything Phase 2 stored — and every reviewer override —
exactly as it was. Running it twice produces the same rows.

DIRECTION IS NEVER DEFAULTED
----------------------------
A direction of ``undetermined`` or ``likely_*`` is a QUESTION. Until a person
answers, the document is excluded from every register. Confirming sets
``sales`` / ``purchase`` with ``confirmed_by``; only then may a reviewer also
remember the recipient's name as an alias, and that alias is a visible,
revocable row.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from src.invoice_extract import applicability as applicability_mod
from src.invoice_extract import checks as checks_mod
from src.invoice_extract import direction as direction_mod
from src.invoice_extract.doc_context import DocContext, RuleConfig
from src.invoice_extract.rules_schema import (
    CHECK_FAIL,
    DIRECTION_LIKELY_PURCHASE,
    DIRECTION_LIKELY_SALES,
    DIRECTION_PURCHASE,
    DIRECTION_SALES,
    DIRECTION_UNDETERMINED,
    LEVEL_NOT_APPLICABLE,
    LEVEL_OPTIONAL,
    LEVEL_REQUIRED,
)
from src.invoice_extract.service import InvoiceExtractError, _connect

# --- derived keys ---------------------------------------------------------
DERIVED_ROUND_OFF = "round_off"
DERIVED_LINE_RATE = "line_rate"
DERIVED_ISSUER_STATE_CODE = "issuer_state_code"
DERIVED_EXPECTED_TAX_TYPE = "expected_tax_type"

DERIVED_LABELS: dict[str, str] = {
    DERIVED_ROUND_OFF: "Round off implied by the printed block",
    DERIVED_LINE_RATE: "Rate implied by tax ÷ taxable",
    DERIVED_ISSUER_STATE_CODE: "Issuer's state code (from the issuer GSTIN)",
    DERIVED_EXPECTED_TAX_TYPE: "Tax type the two states imply",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict_row(cur: sqlite3.Cursor, row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {c[0]: row[i] for i, c in enumerate(cur.description)}


# ---------------------------------------------------------------------------
# Configuration (C1)
# ---------------------------------------------------------------------------


def load_config(
    client_id: Optional[int] = None, *, invoice_date: Optional[str] = None, db_path=None,
) -> RuleConfig:
    """The C1 configuration one document's rules run against.

    Every value is the firm default with the usual per-client override, resolved
    through C1's own resolver — this module never reads a rule table directly.
    The rate slabs are the ones in force on the INVOICE DATE, so an invoice from
    before the 2025-09-22 slab change is judged against the slabs of its own
    time.
    """
    from src.rules import service as rules_service

    def number(key: str, default: str) -> Decimal:
        raw = rules_service.get_effective_value(key, client_id=client_id, db_path=db_path)
        try:
            return Decimal(str(raw).strip())
        except (InvalidOperation, TypeError, ValueError):
            return Decimal(default)

    def words(key: str, default: str) -> tuple[str, ...]:
        raw = rules_service.get_effective_value(key, client_id=client_id, db_path=db_path)
        text = raw if raw is not None else default
        return tuple(w.strip().lower() for w in str(text).split(",") if w.strip())

    prefix = rules_service.get_effective_value(
        "gst.service_sac_prefix", client_id=client_id, db_path=db_path,
    )
    state_rows = {row["code"]: row for row in rules_service.list_state_codes(db_path=db_path)}
    slabs = tuple(rules_service.gst_rate_slabs(invoice_date, db_path=db_path)) if invoice_date else ()
    return RuleConfig(
        total_tolerance=number("gst.total_tolerance_rupees", "0.05"),
        tax_tolerance=number("gst.tax_tolerance_rupees", "1.00"),
        service_sac_prefix=str(prefix if prefix is not None else "99").strip(),
        legal_form_suffixes=words("gst.client_legal_form_suffixes", ""),
        individual_stopwords=frozenset(words("gst.individual_stopwords", "")),
        slabs=slabs,
        state_codes=state_rows,
    )


# ---------------------------------------------------------------------------
# Loading one document into a DocContext
# ---------------------------------------------------------------------------


def _load_document(conn: sqlite3.Connection, document_id: int) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT d.document_id, d.upload_id, d.ordinal, d.category, d.label, u.client_id "
        "FROM invoice_documents d LEFT JOIN invoice_uploads u ON u.upload_id = d.upload_id "
        "WHERE d.document_id = ?",
        (document_id,),
    )
    row = _dict_row(cur, cur.fetchone())
    if row is None:
        raise InvoiceExtractError(f"Unknown document {document_id}.")
    return row


def _client_row(conn: sqlite3.Connection, client_id: Optional[int]) -> Optional[dict[str, Any]]:
    if client_id is None:
        return None
    cur = conn.execute(
        "SELECT client_id, legal_name, pan, gstin FROM end_clients WHERE client_id = ?",
        (client_id,),
    )
    return _dict_row(cur, cur.fetchone())


def _aliases(conn: sqlite3.Connection, client_id: Optional[int]) -> tuple[dict[str, Any], ...]:
    if client_id is None:
        return ()
    cur = conn.execute(
        "SELECT alias_id, alias_text, normalised_text FROM client_aliases "
        "WHERE client_id = ? AND revoked_at IS NULL ORDER BY alias_id",
        (client_id,),
    )
    return tuple(_dict_row(cur, row) for row in cur.fetchall())


def build_context(
    document_id: int, *, db_path=None, conn: Optional[sqlite3.Connection] = None,
) -> DocContext:
    """Everything the pure rules need for one document."""
    from src.rules import service as rules_service

    own = conn is None
    conn = conn or _connect(db_path)
    try:
        document = _load_document(conn, document_id)
        extraction_id = _active_extraction_id(conn, document_id)
        fields: dict[str, dict[str, Any]] = {}
        annotations: list[dict[str, Any]] = []
        derived: dict[str, str] = {}
        if extraction_id is not None:
            cur = conn.execute(
                "SELECT field_key, status, value_a, value_b, reads_agree, text_layer_match, "
                "is_handwritten, page, x, y, w, h, in_region FROM invoice_doc_fields "
                "WHERE extraction_id = ?",
                (extraction_id,),
            )
            for row in cur.fetchall():
                record = _dict_row(cur, row)
                assert record is not None
                fields[str(record["field_key"])] = record
            cur = conn.execute(
                "SELECT page, x, y, w, h, text_verbatim, kind_guess FROM invoice_doc_annotations "
                "WHERE extraction_id = ?",
                (extraction_id,),
            )
            annotations = [_dict_row(cur, row) for row in cur.fetchall()]
            for key, value in conn.execute(
                "SELECT key, value FROM invoice_doc_derived WHERE extraction_id = ?",
                (extraction_id,),
            ).fetchall():
                derived[str(key)] = "" if value is None else str(value)

        client = _client_row(conn, document.get("client_id"))
        invoice_date = None
        if "invoice_date" in fields:
            from src.invoice_extract import normalisers as nz

            record = fields["invoice_date"]
            invoice_date = nz.normalise_date(record.get("value_a") or record.get("value_b"))

        config = load_config(
            document.get("client_id"), invoice_date=invoice_date, db_path=db_path,
        )
        place_text = None
        if "place_of_supply" in fields:
            record = fields["place_of_supply"]
            place_text = record.get("value_a") or record.get("value_b")
        place_state = rules_service.resolve_state(place_text, db_path=db_path) if place_text else None

        return DocContext(
            document_id=int(document["document_id"]),
            extraction_id=extraction_id,
            category=str(document.get("category") or ""),
            upload_id=document.get("upload_id"),
            client_id=document.get("client_id"),
            fields=fields,
            derived=derived,
            annotations=tuple(annotations),
            client=client,
            aliases=_aliases(conn, document.get("client_id")),
            config=config,
            place_state=place_state,
        )
    finally:
        if own:
            conn.close()


def _active_extraction_id(conn: sqlite3.Connection, document_id: int) -> Optional[int]:
    row = conn.execute(
        "SELECT extraction_id FROM invoice_doc_extractions WHERE document_id = ? AND is_active = 1 "
        "ORDER BY extraction_id DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    return int(row[0]) if row else None


# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------


def compute_derived(ctx: DocContext) -> list[dict[str, Any]]:
    """Values CALCULATED from printed ones. Never written back."""
    out: list[dict[str, Any]] = []

    # ---- round off: only when the document does not print one
    if ctx.has_value("round_off"):
        pass
    else:
        total = ctx.number("invoice_total") if ctx.has_value("invoice_total") else None
        taxable, how = ctx.taxable()
        if total is not None and taxable is not None:
            tax = ctx.tax_total()
            value = total - taxable - tax
            out.append({
                "key": DERIVED_ROUND_OFF,
                "value": str(value),
                "formula": f"invoice total {total} - taxable {taxable} - tax {tax} (taxable {how})",
                "inputs": {
                    "invoice_total": str(total), "taxable": str(taxable),
                    "tax": str(tax), "taxable_source": how,
                },
            })

    # ---- line rate: only when no rate is printed
    _rate_key, printed_rate = ctx.printed_rate()
    if printed_rate is None:
        taxable, how = ctx.taxable()
        tax = ctx.tax_total()
        taxable_amounts = ctx.tax_amounts()
        if taxable is not None and taxable != 0 and taxable_amounts:
            percent = (tax / taxable * 100).quantize(Decimal("0.01"))
            out.append({
                "key": DERIVED_LINE_RATE,
                "value": str(percent),
                "formula": f"tax {tax} / taxable {taxable} x 100 (taxable {how})",
                "inputs": {
                    "tax": str(tax), "taxable": str(taxable), "taxable_source": how,
                    "heads": {k: str(v) for k, v in taxable_amounts.items()},
                },
            })

    # ---- issuer state code, from a structurally valid GSTIN
    from src.invoice_extract.gst_identifiers import gstin_valid

    issuer = ctx.value("issuer_gstin")
    if ctx.has_value("issuer_gstin") and issuer:
        result = gstin_valid(issuer, known_state_codes=ctx.config.known_state_codes or None)
        if result.ok and result.state_code:
            state = ctx.config.state_codes.get(result.state_code) or {}
            out.append({
                "key": DERIVED_ISSUER_STATE_CODE,
                "value": result.state_code,
                "formula": f"first 2 characters of the issuer GSTIN {result.normalised}",
                "inputs": {
                    "gstin": result.normalised, "state_code": result.state_code,
                    "state_name": state.get("name"), "utgst": bool(state.get("utgst")),
                },
            })

    # ---- expected tax type, from the two states
    expectation, reason = applicability_mod._tax_expectation(ctx)
    if expectation in ("igst", "intra"):
        heads = ["igst"] if expectation == "igst" else ["cgst", "sgst"]
        second = ctx.config.state_codes.get((ctx.place_state or {}).get("code") or "")
        label = "IGST" if expectation == "igst" else (
            "CGST + UTGST" if second and second.get("utgst") else "CGST + SGST"
        )
        out.append({
            "key": DERIVED_EXPECTED_TAX_TYPE,
            "value": label,
            "formula": reason,
            "inputs": {
                "heads": heads, "utgst_applies": bool(second and second.get("utgst")),
                "reason": reason,
            },
        })
    return out


def _derived_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {str(row["key"]): str(row["value"]) for row in rows}


# ---------------------------------------------------------------------------
# run_rules — the one entry point
# ---------------------------------------------------------------------------


def run_rules(
    extraction_id: int, *, actor: str = "rules:run", db_path=None,
) -> dict[str, Any]:
    """Compute derived values, checks, applicability and direction.

    One transaction, idempotent, safe to re-run after any edit: it REPLACES the
    rows it produced last time (``invoice_doc_derived``, ``invoice_doc_checks``,
    the ``source = 'rule'`` applicability rows, ``invoice_doc_direction``) and
    keeps the reviewer's ``source = 'user'`` overrides and any confirmed
    direction. It never writes to ``invoice_doc_fields``.
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT document_id FROM invoice_doc_extractions WHERE extraction_id = ?",
            (extraction_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise InvoiceExtractError(f"Unknown extraction {extraction_id}.")
        document_id = int(row[0])
    finally:
        conn.close()

    # Pass 1: derived values, computed from the printed values alone.
    ctx = build_context(document_id, db_path=db_path)
    derived = compute_derived(ctx)
    # Pass 2: everything else, with the derived values available to the checks
    # (derived_rate_on_valid_slab reads the derived rate). The context is
    # rebuilt as a frozen copy — nothing is mutated in place.
    ctx = replace(ctx, derived=_derived_map(derived))
    results = checks_mod.run_all(ctx)
    levels = applicability_mod.compute(ctx)
    missing = applicability_mod.missing_required(ctx, levels)
    decided = direction_mod.decide(ctx)

    now = _now()
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM invoice_doc_derived WHERE extraction_id = ?", (extraction_id,))
            for row in derived:
                conn.execute(
                    "INSERT INTO invoice_doc_derived (extraction_id, document_id, key, value, "
                    "formula, inputs_json, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        extraction_id, document_id, row["key"], row.get("value"),
                        row["formula"], json.dumps(row.get("inputs") or {}, default=str), now,
                    ),
                )

            conn.execute("DELETE FROM invoice_doc_checks WHERE extraction_id = ?", (extraction_id,))
            for result in results:
                conn.execute(
                    "INSERT INTO invoice_doc_checks (extraction_id, document_id, check_key, status, "
                    "detail, inputs_json, fields_implicated_json, computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        extraction_id, document_id, result.check_key, result.status, result.detail,
                        json.dumps(result.inputs, default=str),
                        json.dumps(list(result.fields_implicated), default=str), now,
                    ),
                )

            # Applicability: recompute the RULE rows and keep the USER rows.
            conn.execute(
                "DELETE FROM invoice_doc_applicability WHERE document_id = ? AND source = 'rule'",
                (document_id,),
            )
            for row in levels:
                # DO NOTHING on conflict, NOT OR REPLACE: a row that survived the
                # delete above is a reviewer's override, and a rule row must never
                # overwrite one.
                conn.execute(
                    "INSERT INTO invoice_doc_applicability (document_id, field_key, level, "
                    "reason, source, overridden_by, override_reason, at) VALUES (?, ?, ?, ?, "
                    "'rule', NULL, NULL, ?) ON CONFLICT(document_id, field_key) DO NOTHING",
                    (document_id, row.field_key, row.level, row.reason, now),
                )

            # Direction: never overwrite a human's confirmation.
            existing = conn.execute(
                "SELECT direction, confirmed_by FROM invoice_doc_direction WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            confirmed = bool(existing and existing[1])
            if not confirmed:
                conn.execute(
                    "INSERT OR REPLACE INTO invoice_doc_direction (document_id, direction, basis, "
                    "match_json, confirmed_by, confirmed_at, scope) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                    (
                        document_id, decided.direction, decided.basis,
                        json.dumps(decided.match, default=str), decided.scope,
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "extraction_id": extraction_id,
        "document_id": document_id,
        "derived": derived,
        "checks": [result.as_json() for result in results],
        "checks_failed": [result.check_key for result in results if result.failed],
        "applicability": [row.__dict__ for row in levels],
        "missing_required": missing,
        "direction": decided.direction,
        "direction_basis": decided.basis,
        "needs_a_human": decided.needs_a_human,
    }


def run_rules_for_document(document_id: int, *, actor: str = "rules:run", db_path=None) -> dict[str, Any]:
    """Run the rules for the document's ACTIVE extraction."""
    conn = _connect(db_path)
    try:
        extraction_id = _active_extraction_id(conn, document_id)
    finally:
        conn.close()
    if extraction_id is None:
        raise InvoiceExtractError(f"Document {document_id} has no active extraction to run rules on.")
    return run_rules(extraction_id, actor=actor, db_path=db_path)


def init_rules_v1(db_path=None) -> int:
    """Boot step: make sure the Phase-3 tables exist.

    Deliberately does NOT recompute anything at boot — recomputing every
    document on every restart would be wasted work, and anything already
    computed stays valid because the rules only read values the extraction
    wrote. Returns the number of documents currently missing a rules pass, so
    the caller can report it rather than guess.
    """
    conn = _connect(db_path)
    try:
        from src.invoice_extract.rules_schema import ensure_rules_schema

        ensure_rules_schema(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM invoice_doc_extractions e WHERE e.is_active = 1 "
            "AND NOT EXISTS (SELECT 1 FROM invoice_doc_checks c WHERE c.extraction_id = e.extraction_id)"
        ).fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_review_model
# ---------------------------------------------------------------------------


def get_review_model(document_id: int, *, db_path=None) -> dict[str, Any]:
    """The sections the review screen needs — no confidence scores yet.

    ``needs_you`` lists missing REQUIRED fields first (a real gap outranks a
    failed check), then failed checks, then a question about direction when the
    direction is not settled.
    """
    conn = _connect(db_path)
    try:
        document = _load_document(conn, document_id)
        extraction_id = _active_extraction_id(conn, document_id)
        if extraction_id is None:
            raise InvoiceExtractError(f"Document {document_id} has no active extraction.")

        cur = conn.execute(
            "SELECT check_key, status, detail, inputs_json, fields_implicated_json "
            "FROM invoice_doc_checks WHERE extraction_id = ?", (extraction_id,),
        )
        check_rows = [_dict_row(cur, row) for row in cur.fetchall()]
        check_rows.sort(key=lambda r: [k for k, _ in checks_mod.CHECKS].index(r["check_key"])
                        if r["check_key"] in [k for k, _ in checks_mod.CHECKS] else 99)

        cur = conn.execute(
            "SELECT field_key, level, reason, source, overridden_by, override_reason "
            "FROM invoice_doc_applicability WHERE document_id = ? ORDER BY field_key",
            (document_id,),
        )
        level_rows = [_dict_row(cur, row) for row in cur.fetchall()]

        cur = conn.execute(
            "SELECT key, value, formula, inputs_json FROM invoice_doc_derived "
            "WHERE extraction_id = ? ORDER BY derived_id", (extraction_id,),
        )
        derived_rows = [_dict_row(cur, row) for row in cur.fetchall()]

        cur = conn.execute(
            "SELECT direction, basis, match_json, confirmed_by, confirmed_at, scope "
            "FROM invoice_doc_direction WHERE document_id = ?", (document_id,),
        )
        direction_row = _dict_row(cur, cur.fetchone())

        cur = conn.execute(
            "SELECT field_key, status, value_a, value_b, reads_agree FROM invoice_doc_fields "
            "WHERE extraction_id = ?", (extraction_id,),
        )
        fields = {str(r["field_key"]): r for r in (_dict_row(cur, row) for row in cur.fetchall())}
    finally:
        conn.close()

    # Missing required fields, using the EFFECTIVE level (an override wins).
    effective: dict[str, str] = {}
    for row in level_rows:
        effective[str(row["field_key"])] = str(row["level"])
    missing: list[dict[str, Any]] = []
    for key, level in effective.items():
        if level != LEVEL_REQUIRED:
            continue
        record = fields.get(key)
        has_value = bool(record and (record.get("value_a") or record.get("value_b")))
        if not has_value:
            missing.append({
                "field_key": key,
                "status": (record or {}).get("status") or "not_extracted",
                "reason": next(
                    (r["reason"] for r in level_rows if r["field_key"] == key), "",
                ),
                "level_source": next(
                    (r["source"] for r in level_rows if r["field_key"] == key), "rule",
                ),
            })

    needs_you: list[dict[str, Any]] = [
        {"kind": "missing_required", **item} for item in missing
    ]
    needs_you += [
        {
            "kind": "check_failed", "check_key": row["check_key"], "detail": row["detail"],
            "fields_implicated": json.loads(row["fields_implicated_json"] or "[]"),
        }
        for row in check_rows if row["status"] == CHECK_FAIL
    ]
    direction = str((direction_row or {}).get("direction") or DIRECTION_UNDETERMINED)
    if direction not in (DIRECTION_SALES, DIRECTION_PURCHASE):
        needs_you.append({
            "kind": "direction_unsettled", "direction": direction,
            "basis": (direction_row or {}).get("basis"),
            "match": json.loads((direction_row or {}).get("match_json") or "{}"),
        })

    settled = [
        {"field_key": key, "value": record.get("value_a") or record.get("value_b")}
        for key, record in sorted(fields.items())
        if effective.get(key) == LEVEL_REQUIRED and (record.get("value_a") or record.get("value_b"))
        and not key.startswith("line.")
    ]
    return {
        "document_id": document_id,
        "extraction_id": extraction_id,
        "category": document.get("category"),
        "index": document.get("ordinal"),
        "client_id": document.get("client_id"),
        "needs_you": needs_you,
        "missing_required": missing,
        "settled": settled,
        "not_applicable": [
            {"field_key": row["field_key"], "reason": row["reason"]}
            for row in level_rows if row["level"] == LEVEL_NOT_APPLICABLE
        ],
        "optional": [
            {"field_key": row["field_key"], "reason": row["reason"]}
            for row in level_rows if row["level"] == LEVEL_OPTIONAL
        ],
        "derived": [
            {
                "key": row["key"], "label": DERIVED_LABELS.get(str(row["key"]), str(row["key"])),
                "value": row["value"], "formula": row["formula"],
                "inputs": json.loads(row["inputs_json"] or "{}"),
            }
            for row in derived_rows
        ],
        "checks": [
            {
                "check_key": row["check_key"], "label": checks_mod.CHECK_LABELS.get(row["check_key"], ""),
                "status": row["status"], "detail": row["detail"],
                "inputs": json.loads(row["inputs_json"] or "{}"),
                "fields_implicated": json.loads(row["fields_implicated_json"] or "[]"),
            }
            for row in check_rows
        ],
        "direction": {
            "direction": direction,
            "basis": (direction_row or {}).get("basis"),
            "match": json.loads((direction_row or {}).get("match_json") or "{}"),
            "confirmed_by": (direction_row or {}).get("confirmed_by"),
            "confirmed_at": (direction_row or {}).get("confirmed_at"),
            "needs_a_human": direction in (
                DIRECTION_LIKELY_SALES, DIRECTION_LIKELY_PURCHASE, DIRECTION_UNDETERMINED,
            ),
        },
        "aliases": [dict(a) for a in _aliases_public(document.get("client_id"), db_path=db_path)],
    }


def _aliases_public(client_id: Optional[int], *, db_path=None) -> tuple[dict[str, Any], ...]:
    if client_id is None:
        return ()
    conn = _connect(db_path)
    try:
        return _aliases(conn, client_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Applicability overrides
# ---------------------------------------------------------------------------


def override_applicability(
    document_id: int, field_key: str, level: str, *, actor: str, reason: str, db_path=None,
) -> None:
    """A reviewer's override of one field's level. Never changes a value.

    The reason is required: an override without one is an unexplained change to
    what the review screen asks for. The row survives every re-run of the rules.
    """
    if level not in (LEVEL_REQUIRED, LEVEL_OPTIONAL, LEVEL_NOT_APPLICABLE):
        raise InvoiceExtractError(f"'{level}' is not a level (required | optional | not_applicable).")
    if not str(reason or "").strip():
        raise InvoiceExtractError("An override needs a reason.")
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO invoice_doc_applicability (document_id, field_key, level, reason, source, "
            "overridden_by, override_reason, at) VALUES (?, ?, ?, ?, 'user', ?, ?, ?) "
            "ON CONFLICT(document_id, field_key) DO UPDATE SET level = excluded.level, "
            "reason = excluded.reason, source = 'user', overridden_by = excluded.overridden_by, "
            "override_reason = excluded.override_reason, at = excluded.at",
            (
                document_id, field_key, level, f"reviewer override: {reason}",
                actor, reason, _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Direction confirmation + aliases
# ---------------------------------------------------------------------------


def confirm_direction(
    document_id: int, direction: str, *, actor: str, alias_text: Optional[str] = None,
    remember_alias: bool = False, db_path=None,
) -> dict[str, Any]:
    """A person settles the direction. Optionally remember the name as an alias.

    Never called automatically, and it never touches an extracted value — it
    records WHO decided and WHEN, which is what makes the register trustworthy.
    """
    if direction not in (DIRECTION_SALES, DIRECTION_PURCHASE):
        raise InvoiceExtractError("A confirmation must be 'sales' or 'purchase'.")
    conn = _connect(db_path)
    try:
        document = _load_document(conn, document_id)
    finally:
        conn.close()

    remembered: Optional[dict[str, Any]] = None
    if remember_alias:
        text = alias_text or _likely_alias_text(document_id, db_path=db_path)
        ctx = build_context(document_id, db_path=db_path)
        allowed, why = direction_mod.can_be_remembered_as_alias(ctx, text)
        if not allowed:
            raise InvoiceExtractError(why)
        remembered = remember_client_alias(
            document.get("client_id"), str(text), actor=actor, db_path=db_path,
        )

    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "UPDATE invoice_doc_direction SET direction = ?, confirmed_by = ?, confirmed_at = ? "
            "WHERE document_id = ?",
            (direction, actor, _now(), document_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"document_id": document_id, "direction": direction, "confirmed_by": actor,
            "alias": remembered}


def _likely_alias_text(document_id: int, *, db_path=None) -> Optional[str]:
    ctx = build_context(document_id, db_path=db_path)
    return ctx.value("recipient_name")


def remember_client_alias(
    client_id: Optional[int], alias_text: str, *, actor: str, db_path=None,
) -> dict[str, Any]:
    """Store a client alias. ONLY ever called from a reviewer's confirmation."""
    if client_id is None:
        raise InvoiceExtractError("An alias needs a client.")
    text = str(alias_text or "").strip()
    if not text:
        raise InvoiceExtractError("An alias needs the name to remember.")
    normalised = direction_mod.normalise_party_name(text)
    if not normalised:
        raise InvoiceExtractError(f"{text!r} is not a usable name.")
    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT alias_id FROM client_aliases WHERE client_id = ? AND normalised_text = ? "
            "AND revoked_at IS NULL",
            (client_id, normalised),
        ).fetchone()
        if existing:
            return {"alias_id": int(existing[0]), "alias_text": text, "created": False}
        conn.execute(
            "INSERT INTO client_aliases (client_id, alias_text, normalised_text, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (client_id, text, normalised, actor, _now()),
        )
        alias_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        return {"alias_id": alias_id, "alias_text": text, "created": True}
    finally:
        conn.close()


def revoke_alias(alias_id: int, *, actor: str, db_path=None) -> None:
    """Revoke an alias. A revoked alias stays as history and stops matching."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT revoked_at FROM client_aliases WHERE alias_id = ?", (alias_id,),
        ).fetchone()
        if row is None:
            raise InvoiceExtractError(f"Unknown alias {alias_id}.")
        if row[0]:
            return
        conn.execute(
            "UPDATE client_aliases SET revoked_by = ?, revoked_at = ? WHERE alias_id = ?",
            (actor, _now(), alias_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_aliases(client_id: Optional[int] = None, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        if client_id is None:
            cur = conn.execute(
                "SELECT alias_id, client_id, alias_text, normalised_text, created_by, created_at, "
                "revoked_by, revoked_at FROM client_aliases ORDER BY alias_id"
            )
        else:
            cur = conn.execute(
                "SELECT alias_id, client_id, alias_text, normalised_text, created_by, created_at, "
                "revoked_by, revoked_at FROM client_aliases WHERE client_id = ? ORDER BY alias_id",
                (client_id,),
            )
        return [_dict_row(cur, row) for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_review(model: dict[str, Any]) -> None:
    print(f"Document {model['document_id']} ({model['category']}, index {model['index']}) "
          f"— client {model['client_id']}")
    print()
    print("NEEDS YOU")
    if not model["needs_you"]:
        print("  nothing — no missing required field, no failed check, direction settled")
    for item in model["needs_you"]:
        if item["kind"] == "missing_required":
            print(f"  [missing] {item['field_key']} (status {item['status']}) — {item['reason']}")
        elif item["kind"] == "check_failed":
            print(f"  [failed ] {item['check_key']}: {item['detail']}")
            if item["fields_implicated"]:
                print(f"             implicated: {', '.join(item['fields_implicated'])}")
        else:
            print(f"  [decide ] direction {item['direction']}: {item['basis']}")
    print()
    print("CHECKS")
    for check in model["checks"]:
        mark = {"pass": "PASS", "fail": "FAIL", "not_applicable": "n/a "}.get(check["status"], "?")
        print(f"  [{mark}] {check['check_key']}: {check['detail']}")
    print()
    print("DERIVED")
    for row in model["derived"]:
        print(f"  {row['key']} = {row['value']}   ({row['formula']})")
    if not model["derived"]:
        print("  none")
    print()
    print("DIRECTION")
    direction = model["direction"]
    print(f"  {direction['direction']} — {direction['basis']}")
    print()
    print("NOT APPLICABLE")
    for row in model["not_applicable"]:
        print(f"  {row['field_key']}: {row['reason']}")
    if not model["not_applicable"]:
        print("  none")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.invoice_extract.rules",
        description="Invoice Extraction Phase 3 — the deterministic rules layer.",
    )
    parser.add_argument("cmd", choices=["show"], help="show the review model for a document")
    parser.add_argument("document_id", type=int)
    parser.add_argument("--db", default=None, help="database path (default: the app database)")
    parser.add_argument("--rerun", action="store_true", help="recompute the rules before showing")
    parser.add_argument("--json", action="store_true", help="print the whole model as JSON")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        if args.rerun:
            run_rules_for_document(args.document_id, actor="cli:rules", db_path=args.db)
        if args.json:
            print(json.dumps(get_review_model(args.document_id, db_path=args.db), indent=2, default=str))
        else:
            _print_review(get_review_model(args.document_id, db_path=args.db))
        return 0
    except InvoiceExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
