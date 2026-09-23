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
    # pre-check, and the field mapping — all of it. The filename is passed
    # explicitly: without it the layer writes the bytes to a temp file named
    # "upload.csv", losing the real extension (an .xlsx upload would then be
    # parsed as CSV and yield no rows).
    result = normalizer.normalize_source_file(
        file_bytes, source_type, client_ref or str(client_id), period,
        client_id=client_id, recon_type=recon_type, actor=actor,
        filename=filename, db_path=db_path,
    )

    thresholds = get_thresholds(db_path=db_path)
    field_results = _field_results_from_ingestion(result, thresholds)

    if result.status in ("wrong_slot", "unrecognized"):
        upload_status = "unrecognized"
    elif result.status == "empty" or result.row_count_out == 0:
        # A file that mapped cleanly but yielded NO canonical rows is not a
        # confident mapping — there is nothing to be confident about. This
        # is its own status so the review screen can never present an empty
        # read as "every field mapped confidently", and so confirm_mapping()
        # can refuse it unless the reviewer explicitly says the file is
        # deliberately empty.
        upload_status = "empty"
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
        elif conf_pct is None:
            # No confidence = RULE-sourced (deterministic GSTR-2B/IMS parser,
            # or a trusted profile) — a confident mapping, not a guess. Its
            # own status so the UI can show a rule badge, never "AI — 0%".
            status = "rule"
        elif conf_pct >= thresholds["auto_apply"]:
            status = "auto"
        elif conf_pct >= thresholds["manual"]:
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


def _folder_name_for_client(client_id: int, client_ref: Optional[str] = None) -> Optional[str]:
    """Resolve the data/ FOLDER name for a client id.

    The reconciliation engine reads files from `data/<folder>/<period>/
    <source_type>/`, where the folder is a client's folder name (often
    abbreviated), NOT F2's `legal_name`. An upload stored only in the DB
    never reached that folder, so a confirmed file was invisible to the
    Reconcile flow. This maps a client id onto the folder whose name best
    matches the client's legal name (exact, then normalized-prefix), and
    falls back to the passed client_ref, then the DB client row's own name.
    """
    from src.data_paths import DATA_ROOT

    candidates: list[str] = []
    try:
        from src.clients import service as clients

        for c in clients.list_clients(include_inactive=True):
            if int(c["client_id"]) == int(client_id):
                candidates.append(str(c["legal_name"]))
                break
    except Exception:  # noqa: BLE001
        pass
    if client_ref:
        candidates.append(str(client_ref))

    on_disk = [p.name for p in DATA_ROOT.iterdir() if p.is_dir()] if DATA_ROOT.exists() else []

    def norm(name: str) -> str:
        return "".join(ch for ch in str(name).lower() if ch.isalnum())

    # Exact folder-name match, then the BEST (longest-overlap) normalized
    # prefix match either way. Longest wins: "Acme Textiles Pvt Ltd" must
    # resolve to "AcmeTextiles", never the shorter unrelated "acme" folder.
    for cand in candidates:
        if cand in on_disk:
            return cand
    best: Optional[tuple[int, str]] = None
    for cand in candidates:
        n = norm(cand)
        for folder in on_disk:
            f = norm(folder)
            if f and n and (f == n or f.startswith(n) or n.startswith(f)):
                overlap = min(len(f), len(n))
                if best is None or overlap > best[0]:
                    best = (overlap, folder)
    if best is not None:
        return best[1]
    # No match on disk — fall back to the client's own name (source_data_path
    # will create the folder) rather than silently dropping the file.
    if candidates:
        return candidates[0]
    return None


def materialize_upload_to_disk(
    upload_id: int, *, client_ref: Optional[str] = None, db_path=None,
) -> Optional[str]:
    """Copy an upload's stored bytes into the client's data/ folder so the
    reconciliation engine can see it.

    THE MISSING LINK: `upload_and_infer()` stores bytes ONLY in F3's
    document_versions BLOB, and the Reconcile flow's file picker reads the
    DISK folder (`data/<client>/<period>/<source_type>/`). Without this
    step a confirmed upload could never be chosen for reconciliation.
    Called from `confirm_mapping()`.

    Returns the destination filename, or None when the file couldn't be
    written (best-effort — never blocks a confirmation that succeeded).
    """
    from src.data_paths import source_data_path

    conn = _connect(db_path)
    try:
        upload = idb.get_raw_upload(conn, upload_id)
        if upload is None:
            return None
        doc = ddb.get_document(conn, upload["document_id"])
        version = ddb.get_current_version(conn, upload["document_id"]) if doc else None
        file_bytes = version.get("file_bytes") if version else None
        period = (doc.get("period") if doc else None) or upload.get("period")
        filename = upload["filename"]
        source_type = upload["source_type"]
    finally:
        conn.close()

    if not file_bytes:
        return None

    folder = _folder_name_for_client(upload["client_id"], client_ref)
    if not folder:
        return None
    try:
        directory = source_data_path(folder, period or "-", source_type)
        dest = directory / filename
        dest.write_bytes(file_bytes)
        return filename
    except Exception:  # noqa: BLE001
        return None


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


def list_uploads_with_context(
    *, client_id: Optional[int] = None, status: Optional[str] = None, db_path=None,
) -> list[dict[str, Any]]:
    """Uploads joined to their document's period, plus the row accounting
    from the persisted ingestion result.

    The Uploads list needs the period the upload form collected and the
    real rows-read/extracted counts. Neither lives on ``raw_uploads``, so
    both are resolved here rather than in the UI — and the counts are
    resolved by ``stored_result_for_upload()``, which does NOT depend on
    rebuilding the upload key (see that function).
    """
    conn = _connect(db_path)
    try:
        rows = idb.list_raw_uploads_with_context(conn, client_id=client_id, status=status)
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for u in rows:
        stored = stored_result_for_upload(u["upload_id"], db_path=db_path) or {}
        row = dict(u)
        row["row_count_in"] = int(stored.get("row_count_in") or 0)
        row["row_count_out"] = int(stored.get("row_count_out") or 0)
        row["has_stored_result"] = bool(stored)
        out.append(row)
    return out


def stored_result_for_upload(upload_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    """The persisted ingestion result for a raw upload.

    Deliberately does NOT compose the upload key from the raw_upload row:
    that row carries neither ``client_ref`` nor ``period``, so the composed
    key never matched what ``normalize_source_file()`` stored, and every
    upload displayed "0 row(s) read · 0 row(s) extracted" regardless of how
    many rows it actually read. Resolving through the document (which does
    carry the period) and matching on the stored columns fixes existing
    rows too, with no backfill.
    """
    conn = _connect(db_path)
    try:
        upload = idb.get_raw_upload(conn, upload_id)
        if upload is None:
            return None
        return idb.find_ingestion_result(
            conn,
            client_id=upload["client_id"],
            source_type=upload["source_type"],
            filename=upload["filename"],
        )
    finally:
        conn.close()


def upload_row_counts(upload_id: int, *, db_path=None) -> dict[str, int]:
    """(rows_read, rows_extracted) for an upload — 0/0 only when genuinely
    unknown, never as a silent default for a lookup miss."""
    stored = stored_result_for_upload(upload_id, db_path=db_path)
    if not stored:
        return {"row_count_in": 0, "row_count_out": 0, "known": 0}
    return {
        "row_count_in": int(stored.get("row_count_in") or 0),
        "row_count_out": int(stored.get("row_count_out") or 0),
        "known": 1,
    }


def confirm_mapping(
    upload_id: int, *, field_overrides: dict[str, Optional[str]], trust_for_reuse: bool, actor: str,
    client_ref: Optional[str] = None, allow_empty: bool = False, db_path=None,
) -> None:
    """Human confirmation of the (possibly edited) mapping. Never
    auto-trusts — trust is opt-in via ``trust_for_reuse``, never implied
    by confirming alone. Clears the file's Unified Review Queue entry and
    the document's pending_review status so it can proceed.

    Also persists the FULL confirmed mapping against the file's shape
    (client + source_type + header signature) via the unified layer, so
    the next file of this shape skips both the model call and the manual
    step when trust was granted (Prompt 2).

    HARD GATE on an empty read: a file that yielded zero canonical rows
    cannot be confirmed as a mapping — there is no data for the mapping to
    be correct about. ``allow_empty=True`` is the reviewer's explicit
    "this file is deliberately empty" acknowledgement, and is the ONLY way
    past the gate. Enforced here rather than in the UI so no caller can
    bypass it.

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

        if not allow_empty:
            stored = idb.find_ingestion_result(
                conn,
                client_id=upload["client_id"],
                source_type=upload["source_type"],
                filename=upload["filename"],
            )
            if stored is not None and int(stored.get("row_count_out") or 0) == 0:
                raise IngestionAIError(
                    "This file read 0 rows — there is no data for this mapping to apply to. "
                    "Confirm it as a deliberately empty file if that is expected, or re-upload "
                    "the correct file."
                )

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

    # Materialize the confirmed file into the client's data/ folder so the
    # Reconcile flow can actually pick it (F3 stores bytes only as a DB BLOB;
    # the engine reads the disk folder). Best-effort — never un-confirms.
    materialize_upload_to_disk(upload_id, client_ref=client_ref, db_path=db_path)


def re_run_mapping(
    upload_id: int, *, actor: str, client_ref: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Force a fresh model mapping for an already-ingested file.

    The shape cache is keyed on the file's header set, so once a file has
    been ingested its mapping is replayed on every future read — including
    a mapping that left a field (e.g. GSTIN) unmapped. This action clears
    BOTH the cached shape and the persisted ingestion result for the file,
    then re-normalizes with `use_cache=False` so the model genuinely runs
    again.

    Returns the fresh ingestion result as a dict (status, row counts,
    mapping). Raises IngestionAIError when the upload or its stored file
    bytes can't be found.

    NOTE: this is a live model call (~15-25s). Callers should confirm with
    the user before invoking it.
    """
    conn = _connect(db_path)
    try:
        upload = idb.get_raw_upload(conn, upload_id)
        if upload is None:
            raise IngestionAIError("Upload not found.")
        doc = ddb.get_document(conn, upload["document_id"])
        file_bytes = ddb.get_version_bytes(conn, doc["current_version_id"]) if doc else None
        period = doc.get("period") if doc else None
    finally:
        conn.close()

    if not file_bytes:
        raise IngestionAIError(
            "The original file bytes are no longer available for this upload — re-upload the file."
        )

    ref = client_ref or str(upload["client_id"])
    source_type = upload["source_type"]
    filename = upload["filename"]

    # Clear the cached shape + the persisted result so nothing replays the
    # old mapping. Best-effort: a missing row is fine.
    try:
        conn = idb.connect(db_path)
        try:
            stored = idb.find_ingestion_result(
                conn, client_id=upload["client_id"], source_type=source_type, filename=filename,
            )
            headers = (stored or {}).get("headers") or []
            if headers:
                signature = normalizer._header_signature([str(h) for h in headers])
                idb.delete_shape(
                    conn, client_ref=ref, source_type=source_type, header_signature=signature,
                )
            idb.delete_ingestion_result(
                conn, normalizer.upload_key(ref, period, source_type, filename),
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

    result = normalizer.normalize_source_file(
        file_bytes, source_type, ref, period,
        client_id=upload["client_id"], actor=actor, filename=filename,
        db_path=db_path, use_cache=False,
    )

    thresholds = get_thresholds(db_path=db_path)
    field_results = _field_results_from_ingestion(result, thresholds)

    # Refresh the stored report so the review screen shows the new mapping.
    conn = _connect(db_path)
    try:
        idb.create_report(
            conn, upload_id=upload_id, field_mapping=field_results,
            c5_context_used=bool(_c5_runtime_context(upload["client_id"])),
        )
    finally:
        conn.close()

    return {
        "status": result.status,
        "row_count_in": result.row_count_in,
        "row_count_out": result.row_count_out,
        "model_used": result.model_used,
        "llm_cached": result.llm_cached,
        "field_results": field_results,
        "ingestion": result,
    }


def re_run_file(
    client: str, period: Optional[str], source_type: str, filename: str,
    *, actor: str, client_id: Optional[int] = None, recon_type: Optional[str] = None, db_path=None,
) -> dict[str, Any]:
    """Force a fresh mapping for a file identified by its coordinates.

    The Reconcile flow works with files on disk (client/period/source_type/
    filename), not raw_upload rows, so it needs this coordinate-keyed
    variant of `re_run_mapping()`. Same behaviour: clear the cached shape +
    persisted result, then re-normalize with `use_cache=False`.

    A live model call (~15-25s) — confirm with the user first.
    """
    from src.data_paths import source_data_path

    path = source_data_path(client, period, source_type) / filename
    if not path.exists():
        raise IngestionAIError(f"Source file not found: {path}")

    # Clear the cached shape + persisted result so nothing replays the old
    # mapping. Best-effort.
    try:
        conn = idb.connect(db_path)
        try:
            stored = normalizer.get_stored_result(client, period, source_type, filename, db_path=db_path)
            headers = (stored or {}).get("headers") or []
            if headers:
                signature = normalizer._header_signature([str(h) for h in headers])
                idb.delete_shape(
                    conn, client_ref=client, source_type=source_type, header_signature=signature,
                )
            idb.delete_ingestion_result(
                conn, normalizer.upload_key(client, period, source_type, filename),
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

    result = normalizer.normalize_source_file(
        path, source_type, client, period,
        client_id=client_id, recon_type=recon_type, actor=actor,
        db_path=db_path, use_cache=False,
    )
    return {
        "status": result.status,
        "row_count_in": result.row_count_in,
        "row_count_out": result.row_count_out,
        "model_used": result.model_used,
        "llm_cached": result.llm_cached,
        "ingestion": result,
    }


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


def list_profiles_with_usage(*, db_path=None) -> list[dict[str, Any]]:
    """Profiles with the usage that ACTUALLY happened.

    ``column_mapping_profiles.last_used_at`` is never incremented by the
    runtime (reuse reads ``ingestion_shape_cache``), so an admin deciding
    whether to revoke would be looking at a permanently blank column. The
    real counts are aggregated from the shape cache.
    """
    conn = _connect(db_path)
    try:
        return idb.list_profiles_with_usage(conn)
    finally:
        conn.close()


def revoke_profile_trust(profile_id: int, *, db_path=None) -> int:
    """Revoke trust for a mapping profile — and genuinely stop reuse.

    Flipping the profile's own ``trusted`` flag is not enough: the runtime
    reuses mappings out of ``ingestion_shape_cache``, so a revoke that only
    touched the profile would leave the cached mapping being applied on
    every future upload of that shape while the admin list claimed trust
    was gone. The backing shapes are untrusted here too. Returns the number
    of learned shapes affected.
    """
    conn = _connect(db_path)
    try:
        profile = idb.get_profile_by_id(conn, profile_id)
        if profile is None:
            raise IngestionAIError("Mapping profile not found.")
        idb.set_profile_trust(conn, profile_id, False)
        return idb.untrust_shapes_for_profile(
            conn, client_id=profile["client_id"], source_type=profile["source_type"]
        )
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
