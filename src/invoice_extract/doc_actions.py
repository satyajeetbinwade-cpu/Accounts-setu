"""Document-level actions for the Invoice Extraction review screen.

A reviewer decides things about a DOCUMENT as well as about its fields: what
category it is, whether it belongs in this review at all, and whether they have
finished with it. This module is that API.

Three rules hold for every function here:

1. **Logged.** Every action appends a row to ``invoice_document_log`` with the
   actor, the before/after state and the human's REASON. The log is the record;
   nothing here is derivable from the current state of the document.
2. **Refused on a filed upload.** An upload in an immutable export batch may not
   be changed (see :mod:`src.invoice_extract.immutable`).
3. **No values, no pipeline.** Nothing here changes an extracted value, and
   nothing here starts a run. A category change that makes a document
   extractable marks its extraction as needing one and stops there.

Gated by ``invoice_extract.v2.enabled`` (default FALSE), because everything here
reads or writes v2 data. The legacy extraction path is untouched.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract.immutable import is_immutable_status, is_upload_immutable
from src.invoice_extract.review_schema import (
    ACTION_EXCLUDED,
    ACTION_INCLUDED,
    ACTION_REVIEW_CLEARED,
    ACTION_REVIEWED,
    ACTION_SET_CATEGORY,
    CLEARED_BY_CORRECTION,
    CLEARED_BY_REVIEWER,
    CLEARED_BY_RUN,
)
from src.invoice_extract.triage import DEFAULT_EXCLUDED_CATEGORY
from src.invoice_extract.service import InvoiceExtractError, _connect, _now

#: Every category a document may be put into. Deliberately the same vocabulary
#: triage categorises with — a reviewer re-classifies a document into the
#: platform's own categories, they do not invent one.
ALLOWED_CATEGORIES: tuple[str, ...] = (
    "tax_invoice",
    "no_gst_expense",
    "supporting",
    "ticket",
    DEFAULT_EXCLUDED_CATEGORY,
)

#: The categories the extraction pass actually reads. A document in any other
#: category is skipped WITH A REASON, never silently.
EXTRACTABLE_CATEGORIES: tuple[str, ...] = ("tax_invoice", "no_gst_expense")

#: The status a document's extraction is left in when its category changes.
STATUS_NEEDS_RUN = "queued"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_enabled(db_path=None) -> None:
    """Everything here touches v2 data, so the whole module is behind the flag.

    The gate itself lives next to the setting it guards
    (``extract.require_enabled``), so every v2 service refuses identically.
    """
    from src.invoice_extract import extract as extract_mod

    extract_mod.require_enabled(db_path=db_path)


def _dict_row(cur: sqlite3.Cursor, row) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {name: row[i] for i, name in enumerate([d[0] for d in cur.description])}


def _document(conn: sqlite3.Connection, document_id: int) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT d.*, u.status AS upload_status, u.client_id AS client_id "
        "FROM invoice_documents d "
        "LEFT JOIN invoice_uploads u ON u.upload_id = d.upload_id "
        "WHERE d.document_id = ?",
        (int(document_id),),
    )
    document = _dict_row(cur, cur.fetchone())
    if document is None:
        raise InvoiceExtractError(f"Unknown document {document_id}.")
    return document


def _refuse_if_filed(document: dict[str, Any]) -> None:
    if is_immutable_status(document.get("upload_status")):
        raise InvoiceExtractError(
            f"Upload {document.get('upload_id')} has been exported — it is part of an "
            "immutable export batch, so this document cannot be changed any more."
        )


def _log(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: str,
    document_id: Optional[int] = None,
    upload_id: Optional[int] = None,
    reason: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    field_key: Optional[str] = None,
) -> None:
    """Append one audit row. Never updates, never deletes."""
    conn.execute(
        "INSERT INTO invoice_document_log "
        "(document_id, upload_id, action, field_key, reason, before_json, after_json, "
        " actor, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_id, upload_id, action, field_key, reason,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            actor, _now(),
        ),
    )


def append_log(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: str,
    document_id: Optional[int] = None,
    upload_id: Optional[int] = None,
    reason: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
    field_key: Optional[str] = None,
) -> None:
    """Append one log row on the CALLER's connection — its own transaction.

    The extraction uses this to record what its post-read rule dropped, so the
    log row and the fields it describes commit together: an extraction can
    remove a false line, but never silently.
    """
    _log(
        conn, action=action, actor=actor, document_id=document_id,
        upload_id=upload_id, reason=reason, before=before, after=after,
        field_key=field_key,
    )


def _mark_extraction_needs_run(conn: sqlite3.Connection, document_id: int) -> int:
    """Put the document's ACTIVE extraction back to ``queued``.

    This marks a need; it does not start anything and it creates nothing. A
    document that has never been extracted has no row to mark — the next
    ``start_extraction`` will create one. Returns how many rows were marked.
    """
    cur = conn.execute(
        "UPDATE invoice_doc_extractions SET status = ?, error = NULL "
        "WHERE document_id = ? AND is_active = 1",
        (STATUS_NEEDS_RUN, int(document_id)),
    )
    return int(cur.rowcount or 0)


def _state(document: dict[str, Any]) -> dict[str, Any]:
    """The parts of a document a human can change — the before/after payload."""
    return {
        "category": document.get("category"),
        "category_source": document.get("category_source"),
        "excluded": bool(document.get("excluded")),
        "excluded_reason": document.get("excluded_reason"),
    }


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------


def set_category(
    document_id: int, category: str, *, actor: str, reason: str, db_path=None,
) -> dict[str, Any]:
    """Re-classify one document. The reason is required.

    Three deliberate behaviours:

    * ``category_source`` becomes ``'user'``: the platform's own categorisation
      is replaced, not merely annotated.
    * Moving INTO ``not_a_voucher`` excludes the document, with the reason
      recorded. Moving OUT of it does NOT re-include it — an exclusion is a
      human decision and a category change is not an un-exclusion. Call
      :func:`include_document` for that.
    * A move to an EXTRACTABLE category marks the document's active extraction
      as ``queued`` (it needs a run) but does not start one. Pick it up with
      ``extract.resume_extraction`` — or ``start_extraction(force=True)``, which
      supersedes instead of resuming.
    """
    _require_enabled(db_path)
    if not str(reason or "").strip():
        raise InvoiceExtractError("A category change needs a reason.")
    new_category = str(category or "").strip()
    if new_category not in ALLOWED_CATEGORIES:
        raise InvoiceExtractError(
            f"'{category}' is not a category ({', '.join(ALLOWED_CATEGORIES)})."
        )

    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        document = _document(conn, document_id)
        _refuse_if_filed(document)
        before = _state(document)

        excluded = int(before["excluded"])
        excluded_reason = document.get("excluded_reason")
        if new_category == DEFAULT_EXCLUDED_CATEGORY and not excluded:
            excluded = 1
            excluded_reason = f"category '{new_category}': {reason}"
        # Moving OUT of an excluded category deliberately does NOT clear
        # ``excluded``: only include_document() does that.

        conn.execute(
            "UPDATE invoice_documents SET category = ?, category_source = 'user', "
            "excluded = ?, excluded_reason = ? WHERE document_id = ?",
            (new_category, excluded, excluded_reason, int(document_id)),
        )

        marked = 0
        if new_category in EXTRACTABLE_CATEGORIES:
            marked = _mark_extraction_needs_run(conn, int(document_id))

        after = {
            "category": new_category, "category_source": "user",
            "excluded": bool(excluded), "excluded_reason": excluded_reason,
            "extractions_marked_needing_a_run": marked,
        }
        _log(
            conn, action=ACTION_SET_CATEGORY, actor=actor, document_id=int(document_id),
            upload_id=document.get("upload_id"), reason=reason, before=before, after=after,
        )
        conn.commit()
        return {"document_id": int(document_id), **after, "needs_a_run": marked > 0}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Exclude / include
# ---------------------------------------------------------------------------


def exclude_document(document_id: int, *, actor: str, reason: str, db_path=None) -> dict[str, Any]:
    """Take one document out of the review. The reason is required."""
    _require_enabled(db_path)
    if not str(reason or "").strip():
        raise InvoiceExtractError("Excluding a document needs a reason.")

    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        document = _document(conn, document_id)
        _refuse_if_filed(document)
        before = _state(document)
        if before["excluded"]:
            return {"document_id": int(document_id), "excluded": True, "changed": False,
                    "excluded_reason": document.get("excluded_reason")}

        conn.execute(
            "UPDATE invoice_documents SET excluded = 1, excluded_reason = ? WHERE document_id = ?",
            (reason, int(document_id)),
        )
        after = {**before, "excluded": True, "excluded_reason": reason}
        _log(
            conn, action=ACTION_EXCLUDED, actor=actor, document_id=int(document_id),
            upload_id=document.get("upload_id"), reason=reason, before=before, after=after,
        )
        conn.commit()
        return {"document_id": int(document_id), "excluded": True, "changed": True,
                "excluded_reason": reason}
    finally:
        conn.close()


def include_document(
    document_id: int, *, actor: str, reason: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Put an excluded document back into the review.

    Allowed for a document in ANY category — but re-including a document whose
    category is not extractable changes nothing about what gets extracted, and
    the returned ``note`` says so plainly rather than letting a screen imply
    otherwise.
    """
    _require_enabled(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        document = _document(conn, document_id)
        _refuse_if_filed(document)
        before = _state(document)
        note = None
        if document.get("category") not in EXTRACTABLE_CATEGORIES:
            note = (
                f"category '{document.get('category')}' is not extracted — re-including the "
                "document puts it back in the review, but nothing will be extracted from it."
            )
        if not before["excluded"]:
            return {"document_id": int(document_id), "excluded": False, "changed": False,
                    "note": note}

        conn.execute(
            "UPDATE invoice_documents SET excluded = 0, excluded_reason = NULL "
            "WHERE document_id = ?",
            (int(document_id),),
        )
        after = {**before, "excluded": False, "excluded_reason": None}
        _log(
            conn, action=ACTION_INCLUDED, actor=actor, document_id=int(document_id),
            upload_id=document.get("upload_id"),
            reason=reason or "re-included by a reviewer", before=before, after=after,
        )
        conn.commit()
        return {"document_id": int(document_id), "excluded": False, "changed": True, "note": note}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reviewed
# ---------------------------------------------------------------------------


def review_status(document_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    """The standing review for one document, or None."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM invoice_document_review WHERE document_id = ?", (int(document_id),),
        )
        return _dict_row(cur, cur.fetchone())
    finally:
        conn.close()


def is_reviewed(document_id: int, *, db_path=None) -> bool:
    """Is a review standing for this document? The cheap existence check.

    False for a database with no review tables yet — an un-migrated database
    has nothing signed off. Distinct from ``mark_reviewed``, which also refuses
    an upload that has been exported.
    """
    conn = _connect(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT 1 FROM invoice_document_review WHERE document_id = ?",
                (int(document_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None
    finally:
        conn.close()


def mark_reviewed(
    document_id: int, *, actor: str, note: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """A human says they have finished with this document.

    REFUSES while the review model still has items needing a decision, and on an
    upload that has been exported. Refusing is the point: a "reviewed" flag that
    can be set over an unresolved question is worth nothing.

    The formula version and threshold in force are snapshotted onto the row, so
    a later change to either cannot silently re-sign an earlier review.
    """
    _require_enabled(db_path)
    conn = _connect(db_path)
    try:
        document = _document(conn, document_id)
    finally:
        conn.close()
    _refuse_if_filed(document)

    extractable = (
        document.get("category") in EXTRACTABLE_CATEGORIES
        and not document.get("excluded")
    )
    if extractable:
        from src.invoice_extract import rules as rules_mod

        try:
            model = rules_mod.get_review_model(document_id, db_path=db_path)
        except InvoiceExtractError as exc:
            raise InvoiceExtractError(
                f"Document {document_id} cannot be reviewed yet: {exc}"
            ) from exc
        needs_you = model.get("needs_you") or []
        if needs_you:
            kinds = ", ".join(sorted({str(i.get("kind")) for i in needs_you}))
            raise InvoiceExtractError(
                f"Document {document_id} still has {len(needs_you)} item(s) needing a "
                f"decision ({kinds}) — settle them before marking it reviewed."
            )

    from src.invoice_extract import confidence as confidence_mod

    config = confidence_mod.load_config(client_id=document.get("client_id"), db_path=db_path)
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO invoice_document_review "
            "(document_id, reviewed_by, reviewed_at, note, reviewed_formula_version, "
            " reviewed_threshold) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(document_id) DO UPDATE SET reviewed_by = excluded.reviewed_by, "
            "reviewed_at = excluded.reviewed_at, note = excluded.note, "
            "reviewed_formula_version = excluded.reviewed_formula_version, "
            "reviewed_threshold = excluded.reviewed_threshold",
            (
                int(document_id), actor, _now(), note,
                config.formula_version, float(config.threshold),
            ),
        )
        _log(
            conn, action=ACTION_REVIEWED, actor=actor, document_id=int(document_id),
            upload_id=document.get("upload_id"), reason=note,
            after={"reviewed_by": actor, "reviewed_formula_version": config.formula_version,
                   "reviewed_threshold": float(config.threshold)},
        )
        conn.commit()
    finally:
        conn.close()
    return review_status(document_id, db_path=db_path) or {}


def unmark_reviewed(
    document_id: int, *, actor: str, reason: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Withdraw a review. The log keeps the history of when it stood."""
    _require_enabled(db_path)
    return clear_reviewed(
        document_id, actor=actor, triggered_by=CLEARED_BY_REVIEWER,
        reason=reason or "withdrawn by a reviewer", db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Field-level decisions (the review screen's Confirm / Not printed)
# ---------------------------------------------------------------------------

#: The two human decisions a field row can carry. Stored as field STATUSES
#: (``fv2.STATUS_CONFIRMED`` / ``fv2.STATUS_NOT_PRINTED_CONFIRMED``) plus a
#: small JSON in the field row's ``review_json`` column (migration 0008), so
#: the decision lives ON the row: every reader sees the same answer, and no
#: re-run of rules or confidence can write over it.
FIELD_ACTION_CONFIRMED = "confirmed"
FIELD_ACTION_NOT_PRINTED = "not_printed"
_FIELD_ACTIONS = (FIELD_ACTION_CONFIRMED, FIELD_ACTION_NOT_PRINTED)


def field_review_decision(field_row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The HUMAN decision stored on a field row, or None.

    The decision lives ON THE FIELD ROW (status + ``review_json``) — the
    ``invoice_field_corrections`` table is HISTORY and is deliberately never
    read back as a decision, so an old log row cannot count as one.
    """
    raw = field_row.get("review_json")
    if not raw or not isinstance(raw, str) or not raw.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("reviewed") not in _FIELD_ACTIONS:
        return None
    return data


def apply_field_decision(
    document_id: int, field_key: str, *, action: str, value: Optional[str],
    actor: str, db_path=None,
) -> dict[str, Any]:
    """Record ONE human decision about ONE field, in one transaction.

    * it stores the decision on the field row itself — status
      ``confirmed`` / ``not_printed_confirmed`` plus a JSON of the reviewed
      value, actor and time — creating the row when the field has none;
    * it writes the correction-log row (the audit trail), and
    * it unsigns any standing document review.

    Because the decision lives on ``invoice_doc_fields``, a later rules or
    confidence re-run can never overwrite or un-settle it (neither writes that
    table). An empty confirm is refused here as well — the screen's message is
    not the only guard.
    """
    if action not in _FIELD_ACTIONS:
        raise InvoiceExtractError(f"'{action}' is not a field decision.")
    text = (value or "").strip()
    if action == FIELD_ACTION_CONFIRMED and not text:
        raise InvoiceExtractError("Type the value, or use Not printed on the document.")

    from src.invoice_extract.confidence_schema import (
        ACTION_CONFIRMED as LOG_CONFIRMED,
        ACTION_MARKED_NOT_PRINTED as LOG_NOT_PRINTED,
    )

    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        document = _document(conn, document_id)
        _refuse_if_filed(document)

        row = conn.execute(
            "SELECT extraction_id FROM invoice_doc_extractions "
            "WHERE document_id = ? AND is_active = 1 "
            "ORDER BY extraction_id DESC LIMIT 1",
            (int(document_id),),
        ).fetchone()
        if row is None:
            raise InvoiceExtractError(
                "This document has no active extraction to record the decision against."
            )
        extraction_id = int(row[0])

        cur = conn.execute(
            "SELECT f.field_id, f.value_a, f.value_b, f.is_handwritten, "
            "s.score, s.threshold_used, s.formula_version, "
            "e.provider_a, e.model_a, e.provider_b, e.model_b "
            "FROM invoice_doc_fields f "
            "LEFT JOIN invoice_doc_field_scores s ON s.field_id = f.field_id "
            "LEFT JOIN invoice_doc_extractions e ON e.extraction_id = f.extraction_id "
            "WHERE f.extraction_id = ? AND f.field_key = ? "
            "ORDER BY f.field_id",
            (extraction_id, field_key),
        )
        # A field can have MORE THAN ONE row in an extraction (the demo seed
        # writes one per read pass). The review model reads the LAST row, so
        # the decision must land on ALL of them — updating only the first left
        # the screen showing the question it had just answered.
        field_rows = [_dict_row(cur, row) for row in cur.fetchall()]
        field = field_rows[-1] if field_rows else None

        now = _now()
        status = (fv2.STATUS_CONFIRMED if action == FIELD_ACTION_CONFIRMED
                  else fv2.STATUS_NOT_PRINTED_CONFIRMED)
        meta = {
            "reviewed": action,
            "value": text or None,
            "by": actor,
            "at": now,
        }
        value_before = (field or {}).get("value_a") or (field or {}).get("value_b")
        score = (field or {}).get("score")
        threshold = (field or {}).get("threshold_used")
        was_flagged = score is None or (
            threshold is not None and float(score) < float(threshold)
        )
        extraction_models = json.dumps({
            "read_a": {"provider": (field or {}).get("provider_a"), "model": (field or {}).get("model_a")},
            "read_b": {"provider": (field or {}).get("provider_b"), "model": (field or {}).get("model_b")},
        })

        conn.execute("BEGIN")
        try:
            if field is None:
                cur = conn.execute(
                    "INSERT INTO invoice_doc_fields "
                    "(extraction_id, document_id, field_key, status, review_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (extraction_id, int(document_id), field_key, status, json.dumps(meta)),
                )
                field_id = int(cur.lastrowid)
            else:
                field_id = int(field["field_id"])
                conn.execute(
                    "UPDATE invoice_doc_fields SET status = ?, review_json = ? "
                    "WHERE extraction_id = ? AND field_key = ?",
                    (status, json.dumps(meta), extraction_id, field_key),
                )
            conn.execute(
                "INSERT INTO invoice_field_corrections (field_id, document_id, client_id, "
                "field_key, value_before, value_after, action, reason_code, reason_text, actor, "
                "at, score_at_time, was_flagged, formula_version, document_category, "
                "extraction_models, is_handwritten) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    field_id, int(document_id), document.get("client_id"), field_key,
                    value_before, text or None,
                    (LOG_CONFIRMED if action == FIELD_ACTION_CONFIRMED else LOG_NOT_PRINTED),
                    ("correct" if action == FIELD_ACTION_CONFIRMED else "not_on_document"),
                    actor, now, score, 1 if was_flagged else 0,
                    (field or {}).get("formula_version"), document.get("category"),
                    extraction_models, 1 if (field or {}).get("is_handwritten") else 0,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    # A decision UN-SIGNS any standing review: the reviewer vouched for values
    # that have now changed. Same rule as every other correction.
    try:
        clear_reviewed(
            int(document_id), actor=actor, triggered_by="field_decision",
            reason=f"decision on {field_key} ({action})", field_key=field_key,
            db_path=db_path,
        )
    except Exception:  # noqa: BLE001 — the decision itself is already committed
        pass

    return {
        "document_id": int(document_id), "field_id": field_id, "field_key": field_key,
        "status": status, "action": action, "value": text or None,
    }


def accept_check(
    document_id: int, check_key: str, *, actor: str, db_path=None,
) -> dict[str, Any]:
    """A person accepted a failed check's figures AS READ.

    Stored INSIDE the check row's ``inputs_json`` (no schema change): the
    original status and detail are kept, so the strip can still say what the
    check found — the acceptance is what drops the card. First acceptance
    stands. A later rules run regenerates check rows and therefore clears
    acceptances; they vouch for the evidence in front of the reviewer.
    """
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        document = _document(conn, document_id)
        _refuse_if_filed(document)
        row = conn.execute(
            "SELECT extraction_id FROM invoice_doc_extractions "
            "WHERE document_id = ? AND is_active = 1 "
            "ORDER BY extraction_id DESC LIMIT 1",
            (int(document_id),),
        ).fetchone()
        if row is None:
            raise InvoiceExtractError("This document has no active extraction.")
        extraction_id = int(row[0])
        cur = conn.execute(
            "SELECT inputs_json FROM invoice_doc_checks "
            "WHERE extraction_id = ? AND check_key = ?",
            (extraction_id, check_key),
        )
        check = cur.fetchone()
        if check is None:
            raise InvoiceExtractError(f"This document has no '{check_key}' check to accept.")
        try:
            inputs = json.loads(check[0] or "{}")
        except (TypeError, ValueError):
            inputs = {}
        if not isinstance(inputs, dict):
            inputs = {}
        if not inputs.get("accepted_by"):
            inputs["accepted_by"] = actor
            inputs["accepted_at"] = _now()
            conn.execute(
                "UPDATE invoice_doc_checks SET inputs_json = ? "
                "WHERE extraction_id = ? AND check_key = ?",
                (json.dumps(inputs, default=str), extraction_id, check_key),
            )
            conn.commit()
        return {
            "document_id": int(document_id), "check_key": check_key,
            "accepted_by": inputs.get("accepted_by"), "accepted_at": inputs.get("accepted_at"),
        }
    finally:
        conn.close()


def clear_reviewed(
    document_id: int, *, actor: str, triggered_by: str, reason: Optional[str] = None,
    field_key: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Drop a standing review because the thing it vouched for has changed.

    Called by the extraction pass (a re-extraction) and by
    ``confidence.record_correction`` (a new human correction): after either, the
    earlier sign-off is about a document that no longer exists. It is recorded
    in the log rather than deleted silently.
    """
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            document = _document(conn, document_id)
            row = conn.execute(
                "SELECT reviewed_by, reviewed_at FROM invoice_document_review "
                "WHERE document_id = ?", (int(document_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            # A database created before migration 0006 has no review tables.
            # There is nothing to clear, and a correction must never fail for
            # that — the correction itself is already committed.
            return {"document_id": int(document_id), "cleared": False,
                    "note": "no review tables in this database yet"}
        if row is None:
            return {"document_id": int(document_id), "cleared": False}
        conn.execute(
            "DELETE FROM invoice_document_review WHERE document_id = ?", (int(document_id),),
        )
        _log(
            conn, action=ACTION_REVIEW_CLEARED, actor=actor, document_id=int(document_id),
            upload_id=document.get("upload_id"), field_key=field_key,
            reason=reason or f"cleared by {triggered_by}",
            before={"reviewed_by": row[0], "reviewed_at": row[1], "triggered_by": triggered_by},
        )
        conn.commit()
        return {"document_id": int(document_id), "cleared": True,
                "was_reviewed_by": row[0], "triggered_by": triggered_by}
    finally:
        conn.close()


def clear_reviewed_for_upload(
    upload_id: int, *, actor: str, triggered_by: str, reason: Optional[str] = None,
    document_ids: Optional[list[int]] = None, db_path=None,
) -> list[dict[str, Any]]:
    """Clear every standing review on an upload (or on the given documents)."""
    conn = _connect(db_path)
    try:
        if document_ids:
            ids = [int(d) for d in document_ids]
        else:
            ids = [
                int(r[0]) for r in conn.execute(
                    "SELECT document_id FROM invoice_documents WHERE upload_id = ? "
                    "AND is_active = 1", (int(upload_id),),
                ).fetchall()
            ]
    finally:
        conn.close()
    cleared: list[dict[str, Any]] = []
    for document_id in ids:
        result = clear_reviewed(
            document_id, actor=actor, triggered_by=triggered_by, reason=reason, db_path=db_path,
        )
        if result.get("cleared"):
            cleared.append(result)
    return cleared


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------


def list_document_log(
    document_id: Optional[int] = None, *, upload_id: Optional[int] = None,
    limit: int = 200, db_path=None,
) -> list[dict[str, Any]]:
    """The audit trail, newest first, for one document or one upload."""
    where: list[str] = []
    params: list[Any] = []
    if document_id is not None:
        where.append("document_id = ?")
        params.append(int(document_id))
    if upload_id is not None:
        where.append("upload_id = ?")
        params.append(int(upload_id))
    sql = "SELECT * FROM invoice_document_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY log_id DESC LIMIT ?"
    params.append(max(1, int(limit)))

    conn = _connect(db_path)
    try:
        cur = conn.execute(sql, tuple(params))
        return [
            {name: row[i] for i, name in enumerate([d[0] for d in cur.description])}
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


__all__ = [
    "ALLOWED_CATEGORIES",
    "EXTRACTABLE_CATEGORIES",
    "STATUS_NEEDS_RUN",
    "CLEARED_BY_CORRECTION",
    "CLEARED_BY_REVIEWER",
    "CLEARED_BY_RUN",
    "set_category",
    "exclude_document",
    "include_document",
    "mark_reviewed",
    "unmark_reviewed",
    "clear_reviewed",
    "clear_reviewed_for_upload",
    "review_status",
    "list_document_log",
    "is_upload_immutable",
]
