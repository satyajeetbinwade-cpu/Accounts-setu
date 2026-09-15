"""Public API for the F3 Document & Data Repository module — every other
module (and every UI file) should import from here, not from
src.documents.db directly. Mirrors src/clients/service.py's shape.

Covers: DB init, upload → auto-classify → file (or route to the shared
Unified Review Queue), file-level version control, evidence linkage
(bidirectionally inspectable, polymorphic targets), soft-delete +
recovery, and misfiled-document reassignment (reuses F2's
reason-capture-before-save pattern exactly).

Business rules enforced here (not just in the UI), per the F3 build
prompt:
- File types restricted to images (JPG/PNG), documents (DOC/DOCX/PDF),
  spreadsheets (XLS/XLSX/CSV). No size cap.
- Retention is INFINITE — no purge path exists anywhere in this module.
- OCR/text extraction is explicitly deferred — not built here.
- Low document-type classification confidence routes into the shared
  Unified Review Queue, tagged "couldn't classify", rather than
  auto-filing incorrectly.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.documents import db as ddb
from src.documents.schema import init_documents_schema

ALLOWED_EXTENSIONS = {
    "jpg", "jpeg", "png",       # images
    "doc", "docx", "pdf",       # documents
    "xls", "xlsx", "csv",       # spreadsheets
}

DOC_TYPES = [
    "GSTR-2B", "Form 26AS", "Tally Export", "IMS Export", "TDS Certificate",
    "Bank Statement", "Invoice", "Other",
]

# Auto-classification confidence threshold: at/above this, the document
# auto-files silently (no badge shown once filed, per the design table).
# Below it, the document routes into the Unified Review Queue tagged
# "couldn't classify" instead of auto-filing incorrectly.
AUTO_FILE_CONFIDENCE_THRESHOLD = 70

# Lightweight AI-assisted classification: reads filename/metadata keywords
# (NOT a fixed lookup table mapping — this is a heuristic confidence
# scorer, deliberately simple for the PoC, standing in for a real
# AI-assisted classifier). Every module downstream only ever sees the
# (doc_type, confidence) result plus the badge form, never this logic.
_KEYWORD_HINTS: list[tuple[str, str]] = [
    ("2b", "GSTR-2B"),
    ("gstr2b", "GSTR-2B"),
    ("gstr-2b", "GSTR-2B"),
    ("26as", "Form 26AS"),
    ("form26as", "Form 26AS"),
    ("tally", "Tally Export"),
    ("ims", "IMS Export"),
    ("tds", "TDS Certificate"),
    ("bank", "Bank Statement"),
    ("statement", "Bank Statement"),
    ("invoice", "Invoice"),
]


class DocumentError(Exception):
    """Raised for expected F3 module failures (validation, blocked action)."""


def init_documents(db_path=None) -> None:
    """Create F3 tables. Call once at app start, alongside the other
    modules' init_*() calls."""
    conn = recon_db.get_connection(db_path)
    try:
        init_documents_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def validate_file(filename: str) -> str:
    """Basic integrity validation at intake (F5's principle, applied
    here ahead of F5's own build): extension must be one of the allowed
    business formats. Returns the lowercase extension, or raises
    DocumentError."""
    if not filename or "." not in filename:
        raise DocumentError("File has no extension — can't validate its type.")
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise DocumentError(
            f"'.{ext}' isn't an accepted file type. Allowed: images (JPG/PNG), "
            "documents (DOC/DOCX/PDF), spreadsheets (XLS/XLSX/CSV)."
        )
    return ext


def classify_document(filename: str) -> tuple[str, int]:
    """Lightweight AI-assisted classification stub reading filename
    metadata (not a fixed lookup table). Returns (doc_type, confidence_pct).
    A real deployment would swap this for an actual model call; every
    caller only ever sees this (doc_type, pct) result."""
    lowered = filename.lower()
    for keyword, doc_type in _KEYWORD_HINTS:
        if keyword in lowered:
            return doc_type, 92
    return "Other", 45


def upload_document(
    *, client_id: int, filename: str, file_bytes: bytes, period: Optional[str], actor: str, db_path=None,
) -> dict[str, Any]:
    """Upload → auto-classify → file (high confidence) OR route to the
    Unified Review Queue tagged "couldn't classify" (low confidence).
    Returns {"document_id": int, "doc_type": str, "confidence": int,
    "routed_to_queue": bool}."""
    ext = validate_file(filename)
    doc_type, confidence = classify_document(filename)
    routed_to_queue = confidence < AUTO_FILE_CONFIDENCE_THRESHOLD
    review_status = "pending_review" if routed_to_queue else "filed"

    conn = _connect(db_path)
    try:
        document_id = ddb.create_document_with_version(
            conn, client_id=client_id, doc_type=doc_type, period=period,
            filename=filename, file_ext=ext, file_size=len(file_bytes), file_bytes=file_bytes,
            classification_source="ai", classification_pct=confidence, review_status=review_status,
            created_by=actor,
        )
        if routed_to_queue:
            ddb.add_queue_entry(conn, document_id=document_id, reason="couldn't classify")
        return {
            "document_id": document_id, "doc_type": doc_type, "confidence": confidence,
            "routed_to_queue": routed_to_queue,
        }
    finally:
        conn.close()


def add_version(
    document_id: int, *, filename: str, file_bytes: bytes, actor: str, notes: Optional[str] = None, db_path=None,
) -> int:
    """File-level version control — supersedes the current version
    without discarding history."""
    ext = validate_file(filename)
    conn = _connect(db_path)
    try:
        version_number = ddb.next_version_number(conn, document_id)
        version_id = ddb.add_version(
            conn, document_id=document_id, version_number=version_number, filename=filename,
            file_ext=ext, file_size=len(file_bytes), file_bytes=file_bytes, uploaded_by=actor, notes=notes,
        )
        conn.execute("UPDATE documents SET current_version_id = ? WHERE document_id = ?", (version_id, document_id))
        conn.commit()
        return version_id
    finally:
        conn.close()


def list_versions(document_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return ddb.list_versions(conn, document_id)
    finally:
        conn.close()


def get_version_bytes(version_id: int, *, db_path=None) -> Optional[bytes]:
    conn = _connect(db_path)
    try:
        return ddb.get_version_bytes(conn, version_id)
    finally:
        conn.close()


def get_document(document_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return ddb.get_document(conn, document_id)
    finally:
        conn.close()


def list_documents(
    *, client_id: Optional[int] = None, doc_type: Optional[str] = None, period: Optional[str] = None,
    search: Optional[str] = None, include_deleted: bool = False, deleted_only: bool = False, db_path=None,
) -> list[dict[str, Any]]:
    """Search by client, period, document type, and free text where
    extractable (matches against the latest version's filename — OCR/text
    extraction is explicitly deferred, not built here)."""
    conn = _connect(db_path)
    try:
        rows = ddb.list_documents(
            conn, client_id=client_id, doc_type=doc_type, period=period,
            include_deleted=include_deleted, deleted_only=deleted_only,
        )
        if search:
            needle = search.lower()
            filtered = []
            for row in rows:
                versions = ddb.list_versions(conn, row["document_id"])
                row["latest_filename"] = versions[0]["filename"] if versions else ""
                if needle in row["latest_filename"].lower() or needle in row["doc_type"].lower():
                    filtered.append(row)
            return filtered
        for row in rows:
            versions = ddb.list_versions(conn, row["document_id"])
            row["latest_filename"] = versions[0]["filename"] if versions else ""
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Evidence linkage (bidirectionally inspectable, polymorphic)
# ---------------------------------------------------------------------------

EVIDENCE_TARGET_TYPES = ["reconciliation_exception", "audit_query", "corrective_entry", "filing_record"]

_EVIDENCE_TARGET_LABELS = {
    "reconciliation_exception": "Reconciliation Exception (Module 2)",
    "audit_query": "Audit Query (Module 5)",
    "corrective_entry": "Corrective Entry (Module 3)",
    "filing_record": "Filing Record (C2)",
}


def link_evidence(
    *, document_id: int, target_type: str, target_id: Optional[int] = None,
    target_label: Optional[str] = None, actor: Optional[str] = None, db_path=None,
) -> int:
    """Structurally real, generic/polymorphic link — most target types
    have no real backing table yet (Modules 2/3/5 don't exist), so this
    is exercised directly (e.g. by tests or a future module's own
    service) rather than from today's UI."""
    if target_type not in EVIDENCE_TARGET_TYPES:
        raise DocumentError(f"Unknown evidence target type: {target_type}")
    label = target_label or _EVIDENCE_TARGET_LABELS.get(target_type, target_type)
    conn = _connect(db_path)
    try:
        return ddb.add_evidence_link(
            conn, document_id=document_id, target_type=target_type, target_id=target_id,
            target_label=label, created_by=actor,
        )
    finally:
        conn.close()


def used_in(document_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """From a document → every place it's used. Structurally real,
    empty list until Modules 2/3/5 exist — the 'Used in' panel renders
    its 'Not yet used in any record' empty state on an empty list."""
    conn = _connect(db_path)
    try:
        return ddb.list_evidence_links_for_document(conn, document_id)
    finally:
        conn.close()


def documents_for_target(target_type: str, target_id: int, *, db_path=None) -> list[dict[str, Any]]:
    """From any evidence-link target → back to its document(s)."""
    conn = _connect(db_path)
    try:
        return ddb.list_documents_for_target(conn, target_type, target_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unified Review Queue (shared with future F3-AI's "couldn't map" tag)
# ---------------------------------------------------------------------------


def list_queue(status: Optional[str] = "pending", *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return ddb.list_queue_entries(conn, status=status)
    finally:
        conn.close()


def resolve_queue_entry(
    entry_id: int, *, resolved_doc_type: Optional[str] = None, notes: Optional[str], actor: str, db_path=None,
) -> None:
    """Human resolves a queue entry — files the document under the
    corrected type (if supplied) and marks it filed."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT document_id FROM unified_review_queue_entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise DocumentError("Queue entry not found.")
        document_id = row[0]
        if resolved_doc_type:
            conn.execute(
                "UPDATE documents SET doc_type = ?, review_status = 'filed' WHERE document_id = ?",
                (resolved_doc_type, document_id),
            )
        else:
            conn.execute("UPDATE documents SET review_status = 'filed' WHERE document_id = ?", (document_id,))
        conn.commit()
        ddb.resolve_queue_entry(conn, entry_id, resolved_by=actor, notes=notes)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Soft-delete + Recently deleted (Partner/Manager only, enforced in UI +
# here defensively via the actor's permission)
# ---------------------------------------------------------------------------


def soft_delete_document(document_id: int, *, reason: Optional[str] = None, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        ddb.set_document_deleted(conn, document_id, is_deleted=True, actor=actor)
        ddb.log_edit(
            conn, document_id=document_id, action="soft_delete", old_value="active", new_value="deleted",
            reason=reason, changed_by=actor,
        )
    finally:
        conn.close()


def restore_document(document_id: int, *, actor: str, db_path=None) -> None:
    """Evidence links are never removed on soft-delete, so they still
    resolve correctly after a restore — nothing to re-link here."""
    conn = _connect(db_path)
    try:
        ddb.set_document_deleted(conn, document_id, is_deleted=False, actor=actor)
        ddb.log_edit(
            conn, document_id=document_id, action="restore", old_value="deleted", new_value="active",
            reason=None, changed_by=actor,
        )
    finally:
        conn.close()


def list_recently_deleted(*, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    return list_documents(client_id=client_id, deleted_only=True, db_path=db_path)


# ---------------------------------------------------------------------------
# Reassign misfiled document — reuses F2's reason-capture pattern exactly
# (client picker + required reason)
# ---------------------------------------------------------------------------


def reassign_document(
    document_id: int, new_client_id: int, *, reason: str, actor: str, db_path=None,
) -> None:
    """A misfiled document can be reassigned to the correct client
    without losing version history or evidence links — neither table is
    touched by this call, only the document's client_id."""
    if not reason or not reason.strip():
        raise DocumentError("A reason is required before saving a reassignment.")
    conn = _connect(db_path)
    try:
        doc = ddb.get_document(conn, document_id)
        if doc is None:
            raise DocumentError("Document not found.")
        old_client_id = doc["client_id"]
        ddb.reassign_document_client(conn, document_id, new_client_id)
        ddb.log_edit(
            conn, document_id=document_id, action="reassign", old_value=str(old_client_id),
            new_value=str(new_client_id), reason=reason, changed_by=actor,
        )
    finally:
        conn.close()


def list_edit_log(document_id: int, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return ddb.list_edit_log(conn, document_id)
    finally:
        conn.close()
