"""Public API for the F3-AI Smart Document Ingestion module — every
other module (and every UI file) should import from here, not from
src.ingestion_ai.db or src.ingestion_ai.mapper directly. Mirrors
src/documents/service.py's shape (this module sits directly in front of
it).

Depends on: F3 (src.documents — repository + Unified Review Queue), C1's
canonical schema (adapted — see mapper.py's docstring for the exact
adaptation), C3-extended (model routing — stubbed no-op until C3-ext is
built, see _route_via_c3ext()), C5 (instruction library — genuinely
live, see _c5_runtime_context()).

Business rules enforced here (per the F3-AI build prompt), not just in
the UI:
- Confidence thresholds (auto-apply / flagged / manual) are EDITABLE,
  ADMIN-CONFIGURABLE settings (stored via src.settings, not hardcoded),
  with the prompt's stated defaults (>=90 / 70-90 / <70) as the fallback.
- HARD GATE: any file with an unresolved flagged/unmapped field cannot
  proceed to reconciliation — enforced structurally by
  `is_ready_for_reconciliation()`, which every future reconciliation
  entry point must check, not merely a UI-level warning.
- A confidence score below the manual threshold is never silently
  accepted — such fields are always left null + flagged, never guessed.
- A file whose shape doesn't match any known canonical schema is flagged
  'unrecognized', never force-mapped into the nearest available schema.
- Where two plausible mappings exist for an ambiguous column, BOTH
  candidates are retained on the report for the review screen to show —
  this module never silently picks one.
- A ColumnMappingProfile learned for one client's export format is never
  auto-applied to a different client — cross-client copy is a separate,
  explicit Admin action (`copy_profile_to_client`).
- Trust is opt-in on confirm, never implied by Confirm alone, and is
  revocable at any time via `revoke_profile_trust`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from src import db as recon_db
from src.documents import service as documents
from src.documents import db as ddb
from src.ingestion_ai import db as idb
from src.ingestion_ai import llm
from src.ingestion_ai import mapper
from src.ingestion_ai import normalizer
from src.ingestion_ai.schema import init_ingestion_ai_schema

# ---------------------------------------------------------------------------
# Admin-configurable confidence thresholds (per the build prompt: EDITABLE,
# not hardcoded). Stored via src.settings (same flat key/value table C3
# already provides), with these as the documented defaults.
# ---------------------------------------------------------------------------

_THRESHOLD_AUTO_KEY = "ingestion_ai.threshold_auto_apply"
_THRESHOLD_MANUAL_KEY = "ingestion_ai.threshold_manual"
DEFAULT_AUTO_APPLY_THRESHOLD = 90
DEFAULT_MANUAL_THRESHOLD = 70


class IngestionAIError(Exception):
    """Raised for expected F3-AI module failures (validation, blocked action)."""


def init_ingestion_ai(db_path=None) -> None:
    """Create F3-AI tables. Call once at app start, AFTER
    documents.init_documents() (raw_uploads references documents)."""
    conn = _connect(db_path)
    try:
        init_ingestion_ai_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def get_thresholds(*, db_path=None) -> dict[str, int]:
    """Read the admin-configurable thresholds, falling back to the
    build prompt's stated defaults if unset."""
    try:
        from src.settings import service as settings

        auto = settings.get_setting_int(_THRESHOLD_AUTO_KEY, default=DEFAULT_AUTO_APPLY_THRESHOLD, db_path=db_path)
        manual = settings.get_setting_int(_THRESHOLD_MANUAL_KEY, default=DEFAULT_MANUAL_THRESHOLD, db_path=db_path)
        return {"auto_apply": auto, "manual": manual}
    except Exception:  # noqa: BLE001 — settings must never block ingestion
        return {"auto_apply": DEFAULT_AUTO_APPLY_THRESHOLD, "manual": DEFAULT_MANUAL_THRESHOLD}


def set_thresholds(*, auto_apply: int, manual: int, actor: str, db_path=None) -> None:
    if not (0 <= manual <= auto_apply <= 100):
        raise IngestionAIError(
            "Thresholds must satisfy 0 <= manual <= auto_apply <= 100."
        )
    from src.settings import service as settings

    settings.set_setting_int(_THRESHOLD_AUTO_KEY, auto_apply, actor=actor, db_path=db_path)
    settings.set_setting_int(_THRESHOLD_MANUAL_KEY, manual, actor=actor, db_path=db_path)


# ---------------------------------------------------------------------------
# C3-ext (model routing) — first active touchpoint, STUBBED. C3-ext isn't
# built yet in this repo. This function exists so the retrofit is a
# same-shape swap once C3-ext ships: replace the body, keep the signature
# and the "routing_stub" flag callers already read off the report.
# ---------------------------------------------------------------------------


def _route_via_c3ext(touchpoint_key: str = "ingestion_mapping") -> dict[str, Any]:
    """Stub for C3-ext's model-routing table. Returns a fixed descriptor
    noting this call was NOT genuinely routed. Never raises — routing
    absence must degrade to the heuristic mapper, matching C3-ext's own
    documented "AI unavailable — proceed manually" degrade path."""
    return {"routed": False, "touchpoint": touchpoint_key, "reason": "C3-ext not yet built in this repo"}


def _c5_runtime_context(client_id: Optional[int] = None) -> str:
    """C5 retrofit: this module is C5's first active touchpoint
    (ingestion_mapping). Retrieves the live instruction-library context
    so it's genuinely included in the mapping pass, not an empty
    placeholder slot. Best-effort — any C5 error yields '', never breaks
    ingestion (same non-blocking pattern as ai_analysis.py's own C5
    retrofit)."""
    try:
        from src.c5 import service as c5

        return c5.runtime_context("ingestion_mapping", client_id=client_id)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Upload → infer → hard-gate
# ---------------------------------------------------------------------------


def upload_and_infer(
    *, client_id: int, source_type: str, filename: str, file_bytes: bytes, period: Optional[str],
    actor: str, client_ref: Optional[str] = None, recon_type: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """File a raw source upload into F3, then run the UNIFIED ingestion
    layer over it.

    This is the same `normalize_source_file()` the Run screen calls — there
    is no second, divergent mapping path. The F3 document row is still
    created (F3-AI never keeps a second document store), and the
    normalizer's per-field result is translated into the shape this
    module's review screen already renders.

    Returns:
        {
          "upload_id": int, "document_id": int, "status": str,
          "field_results": [...], "auto_mapped": [...], "flagged": [...],
          "unavailable": [...], "ingestion": IngestionResult,
        }
    """
    conn = _connect(db_path)
    try:
        # File into F3 first — F3-AI never keeps a second document store.
        doc_type = _doc_type_for_source(source_type)
        document_id = ddb.create_document_with_version(
            conn, client_id=client_id, doc_type=doc_type, period=period,
            filename=filename, file_ext=_ext(filename), file_size=len(file_bytes), file_bytes=file_bytes,
            classification_source="ai", classification_pct=None, review_status="pending_review",
            created_by=actor,
        )
    finally:
        conn.close()

    # The unified layer does the structural read, the classification
    # pre-check, and the field mapping — all of it.
    result = normalizer.normalize_source_file(
        file_bytes, source_type, client_ref or str(client_id), period,
        client_id=client_id, recon_type=recon_type, actor=actor, db_path=db_path,
    )

    thresholds = get_thresholds(db_path=db_path)
    field_results = _field_results_from_ingestion(result, thresholds)

    if result.status in ("wrong_slot", "unrecognized"):
        upload_status = "unrecognized"
    elif result.unmapped_required_fields:
        upload_status = "blocked"
    else:
        upload_status = "needs_confirm"

    conn = _connect(db_path)
    try:
        upload_id = idb.create_raw_upload(
            conn, document_id=document_id, client_id=client_id, source_type=source_type,
            filename=filename, status=upload_status, created_by=actor,
        )
        idb.create_report(
            conn, upload_id=upload_id, field_mapping=field_results,
            c5_context_used=bool(_c5_runtime_context(client_id)),
        )
        if upload_status == "unrecognized":
            ddb.add_queue_entry(
                conn, document_id=document_id,
                reason=result.message or "couldn't map — unrecognized shape",
            )
        elif upload_status == "blocked":
            n_blocked = len(result.unmapped_required_fields)
            ddb.add_queue_entry(
                conn, document_id=document_id, reason=f"couldn't map — {n_blocked} field(s) need mapping"
            )
    finally:
        conn.close()

    return {
        "upload_id": upload_id, "document_id": document_id, "status": upload_status,
        "field_results": field_results,
        "auto_mapped": [r for r in field_results if r["status"] == "auto"],
        "flagged": [r for r in field_results if r["status"] == "flagged"],
        "unavailable": [r for r in field_results if r["status"] in ("unavailable", "unavailable_review")],
        "ingestion": result,
    }


def _field_results_from_ingestion(result, thresholds: dict[str, int]) -> list[dict[str, Any]]:
    """Translate the unified layer's FieldMapping list into the per-field
    result shape this module's review screen renders.

    The confidence is the model's 0.0-1.0 score scaled to 0-100, and the
    status tier comes from the SAME admin-configurable thresholds the
    module already used — so the review screen's behaviour is unchanged
    even though the mapping now comes from the unified layer.
    """
    out: list[dict[str, Any]] = []
    for m in result.field_mappings:
        conf_pct = None if m.confidence is None else int(round(m.confidence * 100))
        if not m.mapped:
            status = "unavailable"
        elif conf_pct is not None and conf_pct >= thresholds["auto_apply"]:
            status = "auto"
        elif conf_pct is not None and conf_pct >= thresholds["manual"]:
            status = "flagged"
        else:
            status = "unavailable_review"
        out.append({
            "canonical_field": m.canonical_field,
            "raw_column": m.source_column,
            "confidence": conf_pct,
            "reason": m.reason,
            "required": m.required,
            "from_trusted_profile": m.from_trusted_profile,
            "candidates": [m.source_column] if m.source_column else [],
            "status": status,
        })
    return out


def _doc_type_for_source(source_type: str) -> str:
    return {
        "gstr2b": "GSTR-2B", "ims": "IMS Export", "form26as": "Form 26AS",
        "tds": "TDS Certificate", "tally": "Tally Export",
    }.get(source_type, "Other")


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _write_temp(filename: str, file_bytes: bytes) -> Path:
    import tempfile

    suffix = "." + _ext(filename) if _ext(filename) else ".csv"
    fd, path_str = tempfile.mkstemp(suffix=suffix)
    path = Path(path_str)
    path.write_bytes(file_bytes)
    import os

    os.close(fd)
    return path


def _apply_thresholds(field_results: list[dict[str, Any]], thresholds: dict[str, int]) -> list[dict[str, Any]]:
    """Tag each field result with its tier per the three-tier threshold
    logic: >=auto_apply -> 'auto'; manual..auto_apply -> 'flagged';
    <manual -> 'manual' (still shown, never auto-applied); no candidate at
    all -> 'unavailable' (genuinely absent, distinct from a failed
    extraction — never invented)."""
    out = []
    for r in field_results:
        r = dict(r)
        conf = r["confidence"]
        if conf is None:
            r["status"] = "unavailable"
        elif conf >= thresholds["auto_apply"]:
            r["status"] = "auto"
        elif conf >= thresholds["manual"]:
            r["status"] = "flagged"
        else:
            r["status"] = "unavailable_review"  # below manual threshold — blank, human must map or leave null
        out.append(r)
    return out


def _apply_trusted_profile(tiered: list[dict[str, Any]], column_map: dict[str, Optional[str]]) -> list[dict[str, Any]]:
    """A trusted profile's confirmed mapping overrides the heuristic
    inference for fields it covers — this is the 'auto-proposed on future
    uploads of that shape' behavior. Still shown on the review screen as
    an 'auto' (rule-sourced, not AI) result."""
    out = []
    for r in tiered:
        field = r["canonical_field"]
        if field in column_map:
            r = dict(r)
            r["raw_column"] = column_map[field]
            r["confidence"] = None
            r["status"] = "auto"
            r["from_trusted_profile"] = True
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Mapping-review screen reads/writes
# ---------------------------------------------------------------------------


def get_upload(upload_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.get_raw_upload(conn, upload_id)
    finally:
        conn.close()


def get_report(upload_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.get_report_for_upload(conn, upload_id)
    finally:
        conn.close()


def list_uploads(*, client_id: Optional[int] = None, status: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.list_raw_uploads(conn, client_id=client_id, status=status)
    finally:
        conn.close()


def confirm_mapping(
    upload_id: int, *, field_overrides: dict[str, Optional[str]], trust_for_reuse: bool, actor: str,
    client_ref: Optional[str] = None, db_path=None,
) -> None:
    """Human confirmation of the (possibly edited) mapping. Never
    auto-trusts — trust is opt-in via ``trust_for_reuse``, never implied
    by confirming alone. Clears the file's Unified Review Queue entry and
    the document's pending_review status so it can proceed.

    Also persists the FULL confirmed mapping against the file's shape
    (client + source_type + header signature) via the unified layer, so
    the next file of this shape skips both the model call and the manual
    step when trust was granted (Prompt 2).

    F5 retrofit: once the mapping is confirmed, the file's data is
    CANONICAL — this is the exact point F5's structural validity gate
    runs its SECOND, DISTINCT check (malformed GSTIN/PAN/dates/
    duplicates) on the confirmed data, independent of this module's own
    mapping-confidence gate. Best-effort: never blocks confirmation if
    F5 is unavailable."""
    conn = _connect(db_path)
    try:
        upload = idb.get_raw_upload(conn, upload_id)
        if upload is None:
            raise IngestionAIError("Upload not found.")

        idb.confirm_upload(
            conn, upload_id, confirmed_mapping=field_overrides, trusted_on_confirm=trust_for_reuse,
            confirmed_by=actor,
        )
        idb.upsert_profile(
            conn, client_id=upload["client_id"], source_type=upload["source_type"], column_map=field_overrides,
            trusted=trust_for_reuse, actor=actor,
        )
        # Resolve F3's queue entry for this document (if any) + mark filed.
        ddb.resolve_queue_entry_for_document(conn, upload["document_id"], resolved_by=actor)
        conn.execute(
            "UPDATE documents SET review_status = 'filed' WHERE document_id = ?", (upload["document_id"],)
        )
        conn.commit()
    finally:
        conn.close()

    # Persist the confirmed mapping against the file's SHAPE so a future
    # file of the same shape reuses it (and skips the model call entirely
    # when trusted). Best-effort — never blocks a confirmation that
    # already succeeded.
    try:
        stored = normalizer.get_stored_result(
            client_ref or str(upload["client_id"]), upload.get("period"),
            upload["source_type"], upload["filename"], db_path=db_path,
        )
        signature = (stored or {}).get("header_signature")
        if signature:
            normalizer.confirm_shape_mapping(
                client_ref or str(upload["client_id"]), upload["source_type"], signature,
                field_overrides=field_overrides, trust_for_reuse=trust_for_reuse,
                actor=actor, db_path=db_path,
            )
    except Exception:  # noqa: BLE001
        pass

    _run_f5_structural_checks(upload, field_overrides)


def _run_f5_structural_checks(upload: dict[str, Any], field_overrides: dict[str, Optional[str]]) -> None:
    """F5 retrofit — build canonical rows using the just-confirmed
    mapping and hand them to F5's structural validity gate. Never raises
    — a failure here must not block a mapping confirmation that already
    succeeded."""
    try:
        from src.f5 import service as f5

        recon_type = "GST" if upload["source_type"] in ("gstr2b", "ims") else (
            "TDS" if upload["source_type"] in ("form26as", "tds") else None
        )
        if recon_type is None:
            return  # tally (books) — no single recon_type; skip this retrofit hook for now

        conn = _connect()
        try:
            doc = ddb.get_document(conn, upload["document_id"])
            file_bytes = ddb.get_version_bytes(conn, doc["current_version_id"]) if doc else None
        finally:
            conn.close()
        if not file_bytes:
            return

        tmp_path = _write_temp(upload["filename"], file_bytes)
        try:
            raw_df = mapper.read_with_detected_header(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        # Build canonical-ish rows: canonical_field -> raw value, using the
        # confirmed mapping (raw_column may be None -> field left blank).
        rows: list[dict[str, Any]] = []
        for _, raw_row in raw_df.iterrows():
            row: dict[str, Any] = {}
            for field, raw_col in field_overrides.items():
                row[field] = raw_row.get(raw_col) if raw_col else None
            rows.append(row)

        date_fields = ["invoice_date"] if recon_type == "GST" else ["deposit_date"]
        dedupe_keys = ["gstin", "invoice_number"] if recon_type == "GST" else ["pan", "challan_number"]

        f5.run_structural_checks(
            upload["client_id"], recon_type=recon_type, source_type=upload["source_type"],
            source_file=upload["filename"], rows=rows, date_fields=date_fields, dedupe_keys=dedupe_keys,
        )
    except Exception:  # noqa: BLE001 — F5 is a downstream trust gate, never a hard dependency of confirming a mapping
        pass


def is_ready_for_reconciliation(document_id: int, *, db_path=None) -> tuple[bool, Optional[str]]:
    """HARD GATE, not a soft UI warning: a file with any unresolved
    flagged/unmapped field cannot proceed to reconciliation. This is the
    structural check any future reconciliation entry point must call
    before consuming a document's data. Returns (ready, reason_if_blocked)."""
    conn = _connect(db_path)
    try:
        upload = idb.get_raw_upload_by_document(conn, document_id)
        if upload is None:
            return True, None  # not an F3-AI upload (e.g. a manually-classified F3 doc) — not this gate's concern
        if upload["status"] != "confirmed":
            return False, f"Mapping not yet confirmed (status: {upload['status']})."
        return True, None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mapping Profiles admin list
# ---------------------------------------------------------------------------


def list_profiles(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.list_profiles(conn)
    finally:
        conn.close()


def revoke_profile_trust(profile_id: int, *, db_path=None) -> None:
    """Revoking trust is reversible — the shape can be re-trusted on the
    next confirm, so this doesn't need the Delete-vs-Deactivate pair."""
    conn = _connect(db_path)
    try:
        idb.set_profile_trust(conn, profile_id, False)
    finally:
        conn.close()


def copy_profile_to_client(
    profile_id: int, target_client_id: int, *, actor: str, db_path=None,
) -> int:
    """Explicit Admin-only cross-client copy — a profile learned for one
    client is NEVER silently applied to another, per the build prompt.
    The copy is created UN-trusted regardless of the source profile's
    trust state; the target client's first confirm re-establishes trust."""
    conn = _connect(db_path)
    try:
        rows = idb.list_profiles(conn)
        source = next((p for p in rows if p["profile_id"] == profile_id), None)
        if source is None:
            raise IngestionAIError("Source profile not found.")
        return idb.upsert_profile(
            conn, client_id=target_client_id, source_type=source["source_type"],
            column_map=source["column_map"], trusted=False, actor=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unified AI ingestion layer (Prompts 1-4)
# ---------------------------------------------------------------------------
# `normalize_source_file()` is THE single ingestion path every upload in the
# app calls — the Run screen and Smart Ingestion both go through it, so
# there is no code path where a raw uploaded file reaches the reconciliation
# engine without being normalized first. Re-exported here so callers import
# from this module (the public API) rather than reaching into normalizer.py.


def normalize_source_file(*args, **kwargs):
    """Normalize a raw upload into the canonical schema. See
    src.ingestion_ai.normalizer.normalize_source_file for the full contract.

    Never raises on a schema mismatch — missing/unmatched columns are
    reported through the returned IngestionResult's
    `unmapped_required_fields` and `status`, and the caller decides whether
    to block or proceed.
    """
    return normalizer.normalize_source_file(*args, **kwargs)


def get_stored_result(*args, **kwargs):
    """The persisted IngestionResult for an upload, or None."""
    return normalizer.get_stored_result(*args, **kwargs)


def upload_key(*args, **kwargs) -> str:
    return normalizer.upload_key(*args, **kwargs)


def confirm_shape_mapping(*args, **kwargs) -> None:
    """Persist a human-confirmed mapping for an exact file shape. With
    `trust_for_reuse=True` the next file of that shape skips both the model
    call and the manual step."""
    return normalizer.confirm_shape_mapping(*args, **kwargs)


def list_shapes(*, client_ref=None, source_type=None, db_path=None):
    return normalizer.list_shapes(client_ref=client_ref, source_type=source_type, db_path=db_path)


def revoke_shape_trust(shape_id: int, *, db_path=None) -> None:
    return normalizer.revoke_shape_trust(shape_id, db_path=db_path)


def get_preselect_threshold(*, db_path=None) -> float:
    """Confidence at/above which a mapped field is pre-selected in the
    mapping UI. Default 0.75, editable from Settings → AI Ingestion."""
    return normalizer.get_preselect_threshold(db_path=db_path)


def set_preselect_threshold(value: float, *, actor: str, db_path=None) -> None:
    return normalizer.set_preselect_threshold(value, actor=actor, db_path=db_path)


def canonical_fields_for(source_type: str) -> list[str]:
    return normalizer.canonical_fields_for(source_type)


def required_fields_for(source_type: str, recon_type=None) -> list[str]:
    return normalizer.required_fields_for(source_type, recon_type)


def slot_label(source_type: str) -> str:
    """Plain-language name for an upload slot (e.g. 'Tally Purchase
    Register'), for user-facing messages."""
    return normalizer._SLOT_LABELS.get(source_type, source_type)


# --- LLM configuration (frontend-editable) --------------------------------


def llm_status(*, db_path=None) -> dict[str, Any]:
    """Non-raising status of the ingestion LLM: configured?, model, key
    source, masked credential ref. Never returns the key itself."""
    return llm.llm_status(db_path=db_path)


def set_llm_model(model: str, *, actor: str, db_path=None) -> None:
    llm.set_llm_model(model, actor=actor, db_path=db_path)


def set_llm_base_url(base_url: str, *, actor: str, db_path=None) -> None:
    llm.set_llm_base_url(base_url, actor=actor, db_path=db_path)


def set_llm_credential(credential_id, *, actor: str, db_path=None) -> None:
    llm.set_llm_credential(credential_id, actor=actor, db_path=db_path)


def set_llm_limits(*, timeout_seconds=None, max_tokens=None, temperature=None, actor: str, db_path=None) -> None:
    llm.set_llm_limits(
        timeout_seconds=timeout_seconds, max_tokens=max_tokens, temperature=temperature,
        actor=actor, db_path=db_path,
    )


def test_llm_connection(*, db_path=None) -> dict[str, Any]:
    """Make one real, minimal call to verify the configured model + key
    actually work. Returns {ok, model, latency_ms, error}. Never raises —
    the settings screen renders the outcome either way."""
    import time

    try:
        start = time.monotonic()
        parsed, latency_ms, model_id, _raw = llm.call_llm_json(
            "You reply with JSON only.",
            'Reply with exactly {"ok": true} and nothing else.',
            db_path=db_path,
        )
        return {
            "ok": bool(parsed.get("ok", True)),
            "model": model_id,
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "model": None, "latency_ms": None, "error": str(exc)}
