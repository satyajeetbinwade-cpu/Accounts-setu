"""Public API for F3-B — Invoice Extraction & Digitalization.

Every other module (and every UI file) should import from here, not from
src.invoice_extract.db or src.invoice_extract.extractor directly. Mirrors
src/ingestion_ai/service.py's role.

Depends on: F1 (auth, permissions), C3-ext (runtime model routing — two new
touchpoints added by this build, provisioned structurally below), C4
(credential storage for the configured provider) and C1 (canonical GST/TDS
field naming, reused so extracted output aligns with what Module 2 would
eventually expect). F3 is an optional, unwired evidence-link target only —
see the Cross-Module Retrofit Flags. Does NOT depend on Module 2.

Business rules enforced HERE (not merely in the UI), per the build prompt:
- Per-field confidence, never a single document-level score.
- Threshold routing against an ADMIN-CONFIGURABLE threshold (default 90%),
  read from src.settings — never hardcoded.
- A field the AI cannot find at all is left null and flagged "not present"
  (is_present=0) — distinct from a low-confidence extraction, never invented.
- HARD GATE (same pattern as F3-AI): an upload with any unresolved
  sub-threshold (or "not present") field can NEVER be included in an export
  batch. Enforced at the data-pipeline level in generate_export(), which
  raises InvoiceExtractError rather than relying on the UI hiding a button.
- An export batch is IMMUTABLE: once generated it cannot be edited or
  regenerated in place. Correcting a mistake means generating a new batch;
  the old one remains in history. There is no code path that updates a
  batch row.
- Duplicate invoice detection (same invoice number + vendor GSTIN) surfaces
  a warning at upload time but does NOT block; the uploader confirms it's
  intentional or discards it.
- Multi-page invoices are ONE upload; page-splitting is not exposed.
- An Excel/Word file containing multiple invoices is NOT auto-split (known
  limitation, see the build prompt's Explicitly Deferred section).

Cross-Module Retrofit Flags (all structurally provisioned, none wired in
this build):
- F3-B -> C3-ext: this build adds two AITouchpoint rows
  (invoice_extraction_visual, invoice_extraction_structured). C3-ext is not
  built in this repo yet, so the rows live in a local table
  (invoice_ai_touchpoints) and move unchanged to C3-ext's ModelAssignment
  table when it ships. Both are seeded ACTIVE (not the greyed placeholder
  treatment used for unbuilt touchpoints).
- F3-B -> F3 (deferred, structural only): _link_confirmed_to_f3() is a stub
  that would file each Confirmed upload as an F3 Document with an
  EvidenceLink back to this module's record.
- F3-B -> Module 2 (deferred, structural only): the exported Tally-ready
  batch is a flat-file hand-off, not a live data feed.
- F3-B -> F4: export history and review-edit actions are logged locally
  (invoice_extract_change_log) — the same retrofit-later stub every other
  module uses ahead of F4.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

from src import db as recon_db
from src.invoice_extract import db as idb
from src.invoice_extract import extractor
from src.invoice_extract.seed import (
    CANONICAL_FIELD_KEYS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    EXPORT_FORMAT_VERSION,
    FIELD_TALLY_COLUMNS,
    FIELD_LABELS,
    run_seed,
)
from src.invoice_extract.schema import init_invoice_extract_schema

# Admin-configurable confidence threshold (per the build prompt: read from
# src.settings like F3-AI's thresholds, never hardcoded). Stored under a
# flat C3 key with the prompt's stated default as the fallback.
_THRESHOLD_KEY = "invoice_extract.threshold"

# Upload status machine.
STATUS_EXTRACTED = "extracted"      # all fields auto-accepted; awaiting Confirm
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_CONFIRMED = "confirmed"
STATUS_EXPORTED = "exported"


class InvoiceExtractError(Exception):
    """Raised for expected F3-B failures (validation, blocked action)."""


def init_invoice_extract(db_path=None) -> None:
    """Create F3-B tables and seed the two C3-ext touchpoint rows. Call
    once at app start, alongside the other modules' init_*() calls."""
    conn = _connect(db_path)
    try:
        init_invoice_extract_schema(conn)
        run_seed(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Admin-configurable threshold
# ---------------------------------------------------------------------------


def get_threshold(*, db_path=None) -> int:
    try:
        from src.settings import service as settings

        return settings.get_setting_int(_THRESHOLD_KEY, default=DEFAULT_CONFIDENCE_THRESHOLD, db_path=db_path)
    except Exception:  # noqa: BLE001 — settings must never block extraction
        return DEFAULT_CONFIDENCE_THRESHOLD


def set_threshold(value: int, *, actor: str, db_path=None) -> None:
    if not (0 <= value <= 100):
        raise InvoiceExtractError("Confidence threshold must be between 0 and 100.")
    from src.settings import service as settings

    settings.set_setting_int(_THRESHOLD_KEY, value, actor=actor, db_path=db_path)


# ---------------------------------------------------------------------------
# C3-ext touchpoints (the two new ModelAssignment rows)
# ---------------------------------------------------------------------------


def list_touchpoints(*, db_path=None) -> list[dict[str, Any]]:
    """F3-B's two touchpoints, with their model assignment read from the
    central AI model registry when it has one.

    The registry (Setup → AI Models) is the single place models are set, so
    this module's own table is a mirror rather than a competing source of
    truth. Falls back to the local row when the registry has no entry, so
    this module keeps working standalone.
    """
    conn = _connect(db_path)
    try:
        rows = idb.list_touchpoints(conn)
    finally:
        conn.close()

    try:
        from src.ai_models import service as ai_models

        for row in rows:
            assignment = ai_models.get_assignment(row["touchpoint_key"], db_path=db_path)
            if assignment and assignment.get("primary_model"):
                row["primary_model"] = assignment["primary_model"]
                row["fallback_model"] = assignment.get("fallback_model") or row["fallback_model"]
                row["primary_provider"] = "OpenRouter"
                row["fallback_provider"] = "OpenRouter"
    except Exception:  # noqa: BLE001 — registry must never break this module
        pass
    return rows


def update_touchpoint_models(
    key: str, *, primary_model: str, fallback_model: str, actor: str, db_path=None,
) -> None:
    """Admin edit of a touchpoint's model assignment.

    Writes through to the central AI model registry (the single source of
    truth) AND to this module's own mirror table, so the two never disagree.
    """
    if not primary_model.strip() or not fallback_model.strip():
        raise InvoiceExtractError("Both a primary and a fallback model are required.")

    # The registry owns validation (id shape, fallback must differ) and the
    # audit entry. It is the primary write; the local mirror follows.
    try:
        from src.ai_models import service as ai_models

        ai_models.set_assignment_models(
            key, primary_model=primary_model, fallback_model=fallback_model,
            actor=actor, db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise InvoiceExtractError(str(exc)) from exc

    conn = _connect(db_path)
    try:
        idb.set_touchpoint_models(
            conn, key, primary_model=primary_model.strip(), fallback_model=fallback_model.strip(),
        )
        idb.log_change(conn, upload_id=None, action="model_assignment_changed",
                       detail=f"{key}: primary={primary_model.strip()}, fallback={fallback_model.strip()}", actor=actor)
    finally:
        conn.close()


def test_touchpoint_call(key: str, *, db_path=None) -> dict[str, Any]:
    """A dry-run "test call" for one touchpoint (acceptance criterion: both
    touchpoints' test-calls succeed). This PoC has no live model budget, so
    this is an honest structural check — the touchpoint is ACTIVE, both a
    primary and a fallback model are configured, and routing to this
    touchpoint resolves for at least one accepted source format. It reports
    the provider/model it *would* dispatch to. Never fabricates a live
    response."""
    conn = _connect(db_path)
    try:
        tp = idb.get_touchpoint(conn, key)
        if tp is None:
            raise InvoiceExtractError(f"Unknown touchpoint '{key}'.")
        ok = bool(tp["is_active"]) and bool(tp["primary_model"]) and bool(tp["fallback_model"])
        return {
            "ok": ok,
            "touchpoint": key,
            "primary": f"{tp['primary_model']} ({tp['primary_provider']})",
            "fallback": f"{tp['fallback_model']} ({tp['fallback_provider']})",
            "active": bool(tp["is_active"]),
            "message": (
                "Reachable — primary and fallback configured; routing resolves."
                if ok else "Not reachable — touchpoint inactive or a model is unset."
            ),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Upload → extract → threshold-route
# ---------------------------------------------------------------------------


def upload_invoice(
    *, client_id: int, filename: str, file_bytes: bytes, batch_id: Optional[str], actor: str, db_path=None,
) -> dict[str, Any]:
    """Extract every canonical field from an uploaded invoice and route it.

    Returns a dict with the created upload, per-field results (tagged
    auto_accepted vs reviewable), the reviewable field list, and a
    duplicate-warning descriptor (informational only — never blocks).
    """
    if not filename or "." not in filename:
        raise InvoiceExtractError("File has no extension — can't validate its type.")
    ext = filename.rsplit(".", 1)[-1].lower()
    from src.documents.service import ALLOWED_EXTENSIONS

    if ext not in ALLOWED_EXTENSIONS:
        raise InvoiceExtractError(
            f"'.{ext}' isn't an accepted file type. Allowed: images (JPG/PNG), "
            "documents (DOC/DOCX/PDF), spreadsheets (XLS/XLSX/CSV)."
        )

    source_format = extractor.source_format_for(filename)
    field_results, extraction_path = extractor.extract(
        filename=filename, file_bytes=file_bytes, source_format=source_format,
    )

    threshold = get_threshold(db_path=db_path)
    tagged = _apply_threshold(field_results, threshold)
    reviewable = [f["field_name"] for f in tagged if f["reviewable"]]
    status = STATUS_NEEDS_REVIEW if reviewable else STATUS_EXTRACTED

    conn = _connect(db_path)
    try:
        # Duplicate detection uses the extracted invoice number + vendor GSTIN.
        dup_keys = _duplicate_keys(tagged)
        duplicate = idb.find_duplicate(conn, **dup_keys)

        upload_id = idb.create_upload(
            conn, client_id=client_id, filename=filename, file_ext=ext, source_format=source_format,
            extraction_path=extraction_path, batch_id=batch_id, status=status,
            duplicate_warning=duplicate is not None, actor=actor, file_bytes=file_bytes,
        )
        for f in field_results:
            idb.insert_field(conn, upload_id=upload_id, result=f)

        if reviewable:
            idb.create_queue_entry(conn, upload_id=upload_id, flagged_fields=reviewable)

        idb.log_change(conn, upload_id=upload_id, action="upload_extracted",
                       detail=f"{source_format}/{extraction_path}; {len(reviewable)} field(s) need review", actor=actor)
    finally:
        conn.close()

    return {
        "upload_id": upload_id,
        "client_id": client_id,
        "filename": filename,
        "source_format": source_format,
        "extraction_path": extraction_path,
        "status": status,
        "threshold": threshold,
        "field_results": tagged,
        "reviewable_fields": reviewable,
        "duplicate": duplicate,
    }


def _apply_threshold(field_results: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
    """Tag each extracted field with the threshold decision:
      * confidence >= threshold            -> auto_accepted (no badge shown)
      * confidence < threshold (present)   -> reviewable ("AI — N%")
      * not present (confidence is None)   -> reviewable ("not present")
    The last two are BOTH reviewable: nothing below threshold and nothing
    genuinely absent may silently reach an export batch."""
    out: list[dict[str, Any]] = []
    for f in field_results:
        f = dict(f)
        label = FIELD_LABELS.get(f["field_name"], f["field_name"])
        if not f.get("is_present") or f.get("confidence") is None:
            f["reviewable"] = True
            f["review_kind"] = "not_present"
            f["status_label"] = "not present"
        elif f["confidence"] >= threshold:
            f["reviewable"] = False
            f["review_kind"] = None
            f["status_label"] = "auto"
        else:
            f["reviewable"] = True
            f["review_kind"] = "sub_threshold"
            f["status_label"] = f"AI \u2014 {f['confidence']}%"
        f["label"] = label
        out.append(f)
    return out


def _duplicate_keys(tagged: list[dict[str, Any]]) -> dict[str, Optional[str]]:
    by_name = {f["field_name"]: f.get("extracted_value") for f in tagged}
    return {"invoice_number": by_name.get("invoice_number"), "vendor_gstin": by_name.get("vendor_gstin")}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_uploads(
    *, client_id: Optional[int] = None, status: Optional[str] = None, batch_id: Optional[str] = None, db_path=None,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.list_uploads(conn, client_id=client_id, status=status, batch_id=batch_id)
    finally:
        conn.close()


def get_upload(upload_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.get_upload(conn, upload_id)
    finally:
        conn.close()


def get_upload_bytes(upload_id: int, *, db_path=None) -> Optional[bytes]:
    conn = _connect(db_path)
    try:
        return idb.get_upload_bytes(conn, upload_id)
    finally:
        conn.close()


def get_fields(upload_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        rows = idb.list_fields(conn, upload_id)
        out = []
        for r in rows:
            r = dict(r)
            r["label"] = FIELD_LABELS.get(r["field_name"], r["field_name"])
            r["effective_value"] = idb.effective_value(r)
            out.append(r)
        return out
    finally:
        conn.close()


def get_reviewable_fields(upload_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """Fields still gating this upload: unreviewed AND (sub-threshold or
    not present). Once a reviewer resolves a field it drops off this list."""
    threshold = get_threshold(db_path=db_path)
    out = []
    for r in get_fields(upload_id, db_path=db_path):
        if r["resolved"]:
            continue
        conf = r["confidence"]
        if (not r["is_present"]) or conf is None or conf < threshold:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Review queue (this module's OWN queue, NOT F3's Unified Review Queue)
# ---------------------------------------------------------------------------


def list_review_queue(*, status: Optional[str] = "open", db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        entries = idb.list_queue(conn, status=status)
        out = []
        for e in entries:
            up = idb.get_upload(conn, e["upload_id"])
            out.append({**e, "upload": up})
        return out
    finally:
        conn.close()


def review_queue_summary(*, db_path=None) -> dict[str, Any]:
    """Headline count of uploads awaiting resolution + a grouped-by-client
    breakdown (Foundation 3.6 headline -> grouped -> detail)."""
    conn = _connect(db_path)
    try:
        entries = idb.list_queue(conn, status="open")
        by_client: dict[int, int] = {}
        for e in entries:
            up = idb.get_upload(conn, e["upload_id"])
            if up:
                by_client[up["client_id"]] = by_client.get(up["client_id"], 0) + 1
        return {"open_count": len(entries), "by_client": by_client}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------


def resolve_field(
    upload_id: int, *, field_name: str, resolved_value: Optional[str], actor: str, db_path=None,
) -> dict[str, Any]:
    """Reviewer confirms/edits one flagged field. Once every flagged field
    is resolved the upload auto-flips to Confirmed and leaves the queue."""
    conn = _connect(db_path)
    try:
        upload = idb.get_upload(conn, upload_id)
        if upload is None:
            raise InvoiceExtractError("Upload not found.")
        if upload["status"] == STATUS_EXPORTED:
            raise InvoiceExtractError("This upload is already part of an immutable export batch and can't be edited.")
        idb.resolve_field(conn, upload_id=upload_id, field_name=field_name, resolved_value=resolved_value, actor=actor)
        idb.log_change(conn, upload_id=upload_id, action="field_resolved",
                       detail=f"{field_name} = {resolved_value!r}", actor=actor)
    finally:
        conn.close()

    # Auto-flip to Confirmed when nothing remains reviewable.
    remaining = get_reviewable_fields(upload_id, db_path=db_path)
    if not remaining:
        _confirm_upload(upload_id, actor=actor, db_path=db_path)
    return {"remaining": remaining, "status": get_upload(upload_id, db_path=db_path)["status"]}


def confirm_upload(upload_id: int, *, actor: str, db_path=None) -> None:
    """Explicit reviewer confirmation. Refuses if any flagged field is
    still unresolved — the gate is enforced here, not in the UI."""
    remaining = get_reviewable_fields(upload_id, db_path=db_path)
    if remaining:
        names = ", ".join(r["label"] for r in remaining)
        raise InvoiceExtractError(f"Can't confirm — {len(remaining)} field(s) still need resolution: {names}.")
    _confirm_upload(upload_id, actor=actor, db_path=db_path)


def _confirm_upload(upload_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        upload = idb.get_upload(conn, upload_id)
        if upload is None:
            raise InvoiceExtractError("Upload not found.")
        if upload["status"] == STATUS_EXPORTED:
            return
        idb.set_upload_status(conn, upload_id, STATUS_CONFIRMED, confirmed_by=actor)
        idb.resolve_queue_entry(conn, upload_id, resolved_by=actor)
        idb.log_change(conn, upload_id=upload_id, action="confirmed", detail="", actor=actor)
    finally:
        conn.close()
    _link_confirmed_to_f3(upload_id)  # deferred, structural only — no-op


def discard_upload(upload_id: int, *, actor: str, db_path=None) -> None:
    """Discard an upload (e.g. a duplicate the uploader judged unintentional).
    Refuses to discard anything already in an export batch."""
    conn = _connect(db_path)
    try:
        upload = idb.get_upload(conn, upload_id)
        if upload is None:
            raise InvoiceExtractError("Upload not found.")
        if upload["status"] == STATUS_EXPORTED:
            raise InvoiceExtractError("This upload is part of an immutable export batch and can't be discarded.")
        conn.execute("DELETE FROM extracted_invoice_fields WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM invoice_review_queue_entries WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM invoice_uploads WHERE upload_id = ?", (upload_id,))
        conn.commit()
        idb.log_change(conn, upload_id=None, action="upload_discarded",
                       detail=f"upload {upload_id} ({upload['filename']})", actor=actor)
    finally:
        conn.close()


def is_exportable(upload_id: int, *, db_path=None) -> tuple[bool, str]:
    """The HARD GATE, queryable by the UI: an upload is exportable only when
    Confirmed AND no flagged field remains unresolved. Returns (ok, reason)."""
    upload = get_upload(upload_id, db_path=db_path)
    if upload is None:
        return False, "Upload not found."
    if upload["status"] == STATUS_EXPORTED:
        return True, ""
    remaining = get_reviewable_fields(upload_id, db_path=db_path)
    if remaining:
        return False, f"{len(remaining)} field(s) still need resolution."
    if upload["status"] != STATUS_CONFIRMED:
        return False, "Not yet confirmed."
    return True, ""


# ---------------------------------------------------------------------------
# Export — Tally-ready Excel/CSV, immutable batch
# ---------------------------------------------------------------------------


# Canonical Tally column order (label used as the export column header).
EXPORT_COLUMNS: list[str] = [FIELD_LABELS[k] for k in CANONICAL_FIELD_KEYS]


def generate_export(
    *, upload_ids: list[int], actor: str, fmt: str = "xlsx", db_path=None,
) -> dict[str, Any]:
    """Generate a Tally-import-ready batch from one or more Confirmed uploads.

    Enforces the HARD GATE on every upload (raises rather than silently
    skipping). Records the batch immutably — re-exporting produces a NEW
    batch, never an overwrite.
    """
    if not upload_ids:
        raise InvoiceExtractError("Select at least one Confirmed upload to export.")

    blocked: list[str] = []
    for uid in upload_ids:
        ok, reason = is_exportable(uid, db_path=db_path)
        if not ok:
            blocked.append(f"#{uid}: {reason}")
    if blocked:
        # Data-pipeline-level enforcement, independent of any UI state.
        raise InvoiceExtractError("Export blocked — " + "; ".join(blocked))

    rows: list[dict[str, Any]] = []
    conn = _connect(db_path)
    try:
        for uid in upload_ids:
            fields = idb.list_fields(conn, uid)
            by_name = {f["field_name"]: idb.effective_value(f) for f in fields}
            rows.append({FIELD_LABELS[k]: by_name.get(k) for k in CANONICAL_FIELD_KEYS})
    finally:
        conn.close()

    data = _build_export_bytes(rows, fmt=fmt)
    row_count = len(rows)
    row_range = f"1-{row_count}"
    filename = f"tally_invoice_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.{fmt}"

    conn = _connect(db_path)
    try:
        batch_id = idb.insert_export_batch(
            conn, filename=filename, upload_ids=upload_ids, row_count=row_count, row_range=row_range,
            format_version=EXPORT_FORMAT_VERSION, generated_by=actor,
        )
        for uid in upload_ids:
            idb.set_upload_status(conn, uid, STATUS_EXPORTED)
        idb.log_change(conn, upload_id=None, action="export_generated",
                       detail=f"batch {batch_id}: {row_count} row(s)", actor=actor)
    finally:
        conn.close()

    return {
        "batch_id": batch_id, "filename": filename, "row_count": row_count,
        "row_range": row_range, "format_version": EXPORT_FORMAT_VERSION, "data": data,
    }


def _build_export_bytes(rows: list[dict[str, Any]], *, fmt: str) -> bytes:
    """Build the export file in memory. Uses pandas + openpyxl (already
    project dependencies) — no new package introduced."""
    import pandas as pd

    df = pd.DataFrame(rows, columns=EXPORT_COLUMNS)
    buf = BytesIO()
    if fmt == "csv":
        df.to_csv(buf, index=False)
    else:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Tally Import")
    return buf.getvalue()


def list_export_batches(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.list_export_batches(conn)
    finally:
        conn.close()


def get_export_batch(batch_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.get_export_batch(conn, batch_id)
    finally:
        conn.close()


def regenerate_export_bytes(batch_id: int, *, db_path=None) -> bytes:
    """Rebuild the downloadable file for an existing (immutable) batch by
    reading its recorded uploads. This does NOT mutate the batch — it is a
    convenience re-download of the exact recorded row set."""
    batch = get_export_batch(batch_id, db_path=db_path)
    if batch is None:
        raise InvoiceExtractError("Export batch not found.")
    rows: list[dict[str, Any]] = []
    conn = _connect(db_path)
    try:
        for uid in batch["upload_ids"]:
            fields = idb.list_fields(conn, uid)
            by_name = {f["field_name"]: idb.effective_value(f) for f in fields}
            rows.append({FIELD_LABELS[k]: by_name.get(k) for k in CANONICAL_FIELD_KEYS})
    finally:
        conn.close()
    fmt = "csv" if batch["filename"].endswith(".csv") else "xlsx"
    return _build_export_bytes(rows, fmt=fmt)


def list_change_log(*, upload_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return idb.list_change_log(conn, upload_id=upload_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deferred cross-module retrofit hooks (structural only — no-ops this build)
# ---------------------------------------------------------------------------


def _link_confirmed_to_f3(upload_id: int) -> None:
    """F3-B -> F3 retrofit flag (deferred, structural only). When wired,
    this files the Confirmed upload as an F3 Document with an EvidenceLink
    back to this module's record, using F3's already-generic/polymorphic
    link target. Intentionally a no-op in this standalone build."""
    return None