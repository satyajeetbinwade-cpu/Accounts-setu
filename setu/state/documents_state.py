"""F3 state — Document & Data Repository.

Scoped vault (upload → classify → file), document detail (versions, evidence
links, reassign, soft-delete), Unified Review Queue, Recently Deleted. Every
handler calls ``src.documents.service``.

File uploads use Reflex's ``rx.upload``; the handler is async because
``UploadFile.read()`` is a coroutine.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.documents import service as documents
from setu.state.auth_state import AuthState
from setu.state.shared_upload import SharedUploadState, _human_size


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class DocumentRow:
    document_id: int
    filename: str
    doc_type: str
    period: str
    review_status: str
    classification_pct: int
    is_deleted: bool
    client_label: str
    uploaded_at: str
    file_size: int
    # Reflex exposes dataclass FIELDS as vars — the formatted size can't be
    # computed by a Python helper in the view (Var truthiness raises), so it
    # is materialised here at construction.
    file_size_label: str
    is_duplicate: bool


@dataclass
class QueueRow:
    entry_id: int
    document_id: int
    reason: str
    doc_type: str
    client_label: str
    period: str
    flag_kind: str       # "classify" | "mapping" | "shape" | "other"
    flag_label: str      # human label for the flag-type pill


@dataclass
class DeletedRow:
    document_id: int
    filename: str
    doc_type: str
    deleted_at: str
    deleted_by: str


@dataclass
class VersionRow:
    version_number: int
    filename: str
    uploaded_by: str
    uploaded_at: str


@dataclass
class EvidenceRow:
    target_label: str


class DocumentsState(SharedUploadState):
    """Document & Data Repository state."""

    section: str = "Document Vault"

    client_options: list[ClientOption] = []
    vault_client_id: int = 0
    # True when the vault is rendered INSIDE another screen (F2's client
    # profile Documents tab) rather than the standalone /documents route.
    # The embedded render hides the client picker — the client is fixed by
    # the profile the user is already looking at.
    embedded: bool = False

    # vault filters
    filter_type: str = "All"
    filter_period: str = ""
    filter_search: str = ""
    documents: list[DocumentRow] = []

    # upload
    upload_period: str = ""
    upload_error: str = ""

    # detail
    selected_document_id: int = 0
    detail_filename: str = ""
    detail_client: str = ""
    detail_client_id: int = 0
    detail_type: str = ""
    detail_period: str = ""
    detail_review_status: str = ""
    detail_pct: int = 0
    detail_is_deleted: bool = False
    detail_notes: str = ""
    detail_notes_draft: str = ""
    versions: list[VersionRow] = []
    evidence: list[EvidenceRow] = []

    # reassign
    reassign_client_id: int = 0
    reassign_reason: str = ""

    # delete
    delete_reason: str = ""
    delete_confirm: bool = False

    # queue
    queue: list[QueueRow] = []
    queue_type: dict[str, str] = {}
    queue_notes: dict[str, str] = {}
    queue_filter_client: str = "All"
    queue_filter_type: str = "All types"
    queue_filter_flag: str = "All flags"
    queue_open_entry_id: int = 0     # the queue item currently open (file-by-file)
    queue_progress: dict[str, int] = {}

    # discard (queue resolution option)
    discard_confirm: bool = False

    # recently deleted
    deleted: list[DeletedRow] = []

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def can_view(self) -> bool:
        return "documents.view" in self._codes()

    @rx.var
    def can_upload(self) -> bool:
        return "documents.upload" in self._codes()

    @rx.var
    def vault_client_names(self) -> list[str]:
        return ["All clients"] + [o.legal_name for o in self.client_options]

    @rx.var
    def vault_client_name(self) -> str:
        if self.vault_client_id == 0:
            return "All clients"
        for o in self.client_options:
            if o.client_id == self.vault_client_id:
                return o.legal_name
        return "All clients"

    @rx.event
    def set_vault_client_by_name(self, name: str):
        if name == "All clients":
            self.vault_client_id = 0
        else:
            for o in self.client_options:
                if o.legal_name == name:
                    self.vault_client_id = o.client_id
                    break
        self.selected_document_id = 0
        self._load_all()

    @rx.var
    def can_delete(self) -> bool:
        return "documents.delete" in self._codes()

    @rx.var
    def can_reassign(self) -> bool:
        return "documents.reassign" in self._codes()

    @rx.var
    def can_resolve(self) -> bool:
        return "documents.review_queue.resolve" in self._codes()

    @rx.var
    def reassign_dirty(self) -> bool:
        return self.reassign_client_id != 0 and self.reassign_client_id != self._detail_client_id()

    @rx.var
    def reassign_client_name(self) -> str:
        for o in self.client_options:
            if o.client_id == self.reassign_client_id:
                return o.legal_name
        return self.detail_client

    @rx.var
    def detail_period_label(self) -> str:
        return self.detail_period.strip() if self.detail_period and self.detail_period.strip() else "Not set"

    def _detail_client_id(self) -> int:
        if not self.selected_document_id:
            return 0
        doc = documents.get_document(self.selected_document_id)
        return doc["client_id"] if doc else 0

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if (deny := self._gate("documents.view")):
            return rx.redirect(deny)
        self.embedded = False
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self._load_all()

    @rx.event
    def load_for_client(self, client_id: int):
        """Load the vault scoped to ONE client, for embedding inside F2's
        client profile Documents tab. No redirect: the caller (ClientState)
        has already passed its own permission gate, and the embedded view
        renders an inline reason instead when documents.view is absent."""
        self.embedded = True
        self.section = "Document Vault"
        self.selected_document_id = 0
        self.vault_client_id = int(client_id or 0)
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self._load_all()

    def _load_all(self) -> None:
        self._load_documents()
        self._load_queue()
        self._load_deleted()
        if self.selected_document_id:
            self._load_detail()

    def _load_documents(self) -> None:
        rows = documents.list_documents(
            client_id=self.vault_client_id or None,
            doc_type=None if self.filter_type == "All" else self.filter_type,
            period=self.filter_period or None,
            search=self.filter_search or None,
        )
        dups = documents.find_duplicate_filenames()
        self.documents = [
            DocumentRow(
                document_id=d["document_id"],
                filename=d.get("latest_filename") or f"Document #{d['document_id']}",
                doc_type=d["doc_type"],
                period=d.get("period") or "",
                review_status=d["review_status"],
                classification_pct=int(d.get("classification_pct") or 0),
                is_deleted=bool(d.get("is_deleted")),
                client_label=(clients.get_client(d["client_id"]) or {}).get("legal_name", "—"),
                uploaded_at=str(d.get("uploaded_at") or d.get("created_at") or "").replace("T", " ").split(".")[0],
                file_size=int(d.get("file_size") or 0),
                file_size_label=_human_size(int(d.get("file_size") or 0)),
                is_duplicate=d.get("latest_filename", "") in dups,
            )
            for d in rows
        ]

    def _load_detail(self) -> None:
        doc = documents.get_document(self.selected_document_id)
        if doc is None:
            self.selected_document_id = 0
            return
        client = clients.get_client(doc["client_id"])
        versions = documents.list_versions(doc["document_id"])
        latest = versions[0] if versions else None
        self.detail_filename = latest["filename"] if latest else "(no version)"
        self.detail_client = client["legal_name"] if client else "—"
        self.detail_client_id = doc["client_id"]
        self.detail_type = doc["doc_type"]
        self.detail_period = doc.get("period") or ""
        self.detail_review_status = doc["review_status"]
        self.detail_pct = int(doc.get("classification_pct") or 0)
        self.detail_is_deleted = bool(doc.get("is_deleted"))
        self.detail_notes = doc.get("notes") or ""
        self.detail_notes_draft = doc.get("notes") or ""
        self.versions = [
            VersionRow(
                version_number=v["version_number"],
                filename=v["filename"],
                uploaded_by=v["uploaded_by"],
                uploaded_at=str(v["uploaded_at"]).replace("T", " ").split(".")[0],
            )
            for v in versions
        ]
        self.evidence = [
            EvidenceRow(target_label=link["target_label"]) for link in documents.used_in(doc["document_id"])
        ]
        self.reassign_client_id = doc["client_id"]

    def _load_queue(self) -> None:
        self.queue_progress = documents.queue_progress()
        self.queue = [
            QueueRow(
                entry_id=e["entry_id"],
                document_id=e["document_id"],
                reason=e["reason"],
                doc_type=e.get("doc_type") or "—",
                client_label=(clients.get_client(e["client_id"]) or {}).get("legal_name", "—")
                if e.get("client_id")
                else "—",
                period=e.get("period") or "",
                flag_kind=_flag_kind(e["reason"]),
                flag_label=_flag_label(e["reason"]),
            )
            for e in documents.list_queue("pending")
        ]
        for e in self.queue:
            self.queue_type.setdefault(str(e.entry_id), "(keep as-is)")
            self.queue_notes.setdefault(str(e.entry_id), "")

    def _load_deleted(self) -> None:
        self.deleted = [
            DeletedRow(
                document_id=d["document_id"],
                filename=d.get("latest_filename") or f"Document #{d['document_id']}",
                doc_type=d["doc_type"],
                deleted_at=str(d.get("deleted_at") or "").replace("T", " ").split(".")[0],
                deleted_by=d.get("deleted_by") or "",
            )
            for d in documents.list_recently_deleted(client_id=self.vault_client_id or None)
        ]

    # ------------------------------------------------------------------
    # Nav + filters
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def set_vault_client(self, client_id: int):
        self.vault_client_id = client_id
        self.selected_document_id = 0
        self._load_all()

    def set_filter_type(self, v: str):
        self.filter_type = v
        self._load_documents()

    def set_filter_period(self, v: str):
        self.filter_period = v
        self._load_documents()

    def set_filter_search(self, v: str):
        self.filter_search = v
        self._load_documents()

    def set_upload_period(self, v: str):
        self.upload_period = v

    # ------------------------------------------------------------------
    # Drop-zone staging → then upload with per-file progress. The drop zone
    # calls the shared ``stage`` handler (FilePreviewChip); this handler runs
    # each staged file through Queued → Uploading → Processing → Done.
    # ------------------------------------------------------------------
    def _set_stage(self, key: str, stage: str, *, pct: int = 0, label: str = "") -> None:
        from setu.state.shared_upload import UploadProgressRow

        rows = []
        for r in self.upload_progress:
            if r.key == key:
                rows.append(UploadProgressRow(key=key, filename=r.filename, stage=stage, pct=pct, label=label))
            else:
                rows.append(r)
        self.upload_progress = rows

    @rx.event
    async def upload_pending(self):
        """Upload everything currently staged through the distinct
        queued/uploading/processing/done states (async generator)."""
        import asyncio

        if not self.pending:
            self.upload_error = "Choose at least one file to upload."
            return
        if not self.vault_client_id:
            self.upload_error = "Select a specific client to upload into (not 'All clients')."
            return

        staged = list(self.pending)
        self.upload_progress = []
        from setu.state.shared_upload import UploadProgressRow

        for pf in staged:
            self.upload_progress = self.upload_progress + [
                UploadProgressRow(key=pf.key, filename=pf.filename, stage="queued", pct=0, label="Queued")
            ]
        yield

        for i, pf in enumerate(staged):
            self._set_stage(pf.key, "uploading", pct=10)
            yield
            await asyncio.sleep(0.03)
            self._set_stage(pf.key, "uploading", pct=60)
            yield
            self._set_stage(pf.key, "processing", label="Classifying…")
            yield
            try:
                result = documents.upload_document(
                    client_id=self.vault_client_id,
                    filename=pf.filename,
                    file_bytes=self._pending_bytes(pf.key),
                    period=self.upload_period or None,
                    actor=self.username,
                )
                if result["routed_to_queue"]:
                    self._set_stage(pf.key, "needs_review", label="Routed to Review Queue")
                else:
                    self._set_stage(pf.key, "done", pct=100, label=f"Filed as {result['doc_type']}")
            except documents.DocumentError as exc:
                self._set_stage(pf.key, "failed", label=str(exc))
            yield

        self.clear_pending()
        self.upload_period = ""
        self._load_all()

    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        """Legacy single-shot handler kept for the Upload button; stages then
        uploads directly via the shared path."""
        self.upload_error = ""
        self.flash = ""
        if not files:
            return
        for f in files:
            data = await f.read()
            try:
                result = documents.upload_document(
                    client_id=self.vault_client_id,
                    filename=f.filename or "upload",
                    file_bytes=data,
                    period=self.upload_period or None,
                    actor=self.username,
                )
            except documents.DocumentError as exc:
                self.upload_error = str(exc)
                continue
            if result["routed_to_queue"]:
                self.flash = (
                    f"Couldn't classify '{f.filename}' with confidence — routed to the "
                    f"Unified Review Queue. Best guess: {result['doc_type']} ({result['confidence']}%)."
                )
            else:
                self.flash = f"Filed '{f.filename}' as {result['doc_type']} ({result['confidence']}%)."
        self.upload_period = ""
        self._load_all()

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------
    @rx.event
    def open_document(self, document_id: int):
        self.selected_document_id = document_id
        self.reassign_reason = ""
        self.delete_reason = ""
        self._load_detail()

    @rx.event
    def back_to_vault(self):
        self.selected_document_id = 0
        self._load_documents()

    @rx.event
    async def add_version(self, files: list[rx.UploadFile]):
        if not files or not self.selected_document_id:
            return
        f = files[0]
        data = await f.read()
        try:
            documents.add_version(
                self.selected_document_id,
                filename=f.filename or "upload",
                file_bytes=data,
                actor=self.username,
            )
            self.flash = "New version saved — evidence links and history preserved."
        except documents.DocumentError as exc:
            self.error = str(exc)
        self._load_detail()

    def set_reassign_client(self, client_id: int):
        self.reassign_client_id = client_id

    @rx.event
    def set_reassign_client_by_name(self, name: str):
        for o in self.client_options:
            if o.legal_name == name:
                self.reassign_client_id = o.client_id
                break

    def set_reassign_reason(self, v: str):
        self.reassign_reason = v

    @rx.event
    def save_reassign(self):
        if not self.reassign_reason.strip():
            return
        try:
            documents.reassign_document(
                self.selected_document_id,
                self.reassign_client_id,
                reason=self.reassign_reason,
                actor=self.username,
            )
            self.flash = "Document reassigned — version history and evidence links preserved."
            self.reassign_reason = ""
        except documents.DocumentError as exc:
            self.error = str(exc)
        self._load_detail()

    def set_delete_reason(self, v: str):
        self.delete_reason = v

    @rx.event
    def soft_delete(self):
        documents.soft_delete_document(
            self.selected_document_id, reason=self.delete_reason or None, actor=self.username,
        )
        self.flash = "Document soft-deleted — recoverable from Recently Deleted."
        self.delete_reason = ""
        self._load_all()

    @rx.event
    def restore_document(self, document_id: int):
        documents.restore_document(document_id, actor=self.username)
        self.flash = "Document restored."
        self._load_all()

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
    def set_queue_type(self, entry_id: int, v: str):
        self.queue_type[str(entry_id)] = v

    def set_queue_notes(self, entry_id: int, v: str):
        self.queue_notes[str(entry_id)] = v

    @rx.event
    def resolve_queue(self, entry_id: int):
        corrected = self.queue_type.get(str(entry_id), "(keep as-is)")
        documents.resolve_queue_entry(
            entry_id,
            resolved_doc_type=None if corrected == "(keep as-is)" else corrected,
            notes=self.queue_notes.get(str(entry_id)) or None,
            actor=self.username,
        )
        self.flash = "Resolved and filed."
        self._load_all()

    @rx.event
    def file_as_is(self):
        """Resolution option: file the document under its current type."""
        entry_id = self.queue_open_entry_id
        if not entry_id:
            return
        documents.resolve_queue_entry(
            entry_id,
            resolved_doc_type=None,
            notes=self.queue_notes.get(str(entry_id)) or None,
            actor=self.username,
        )
        self.flash = "Resolved and filed."
        self.queue_open_entry_id = 0
        self._load_all()

    @rx.event
    def reclassify_and_file(self):
        """Resolution option: reclassify the document type, then file it."""
        entry_id = self.queue_open_entry_id
        if not entry_id:
            return
        corrected = self.queue_type.get(str(entry_id), "(keep as-is)")
        if corrected != "(keep as-is)":
            documents.resolve_queue_entry(
                entry_id,
                resolved_doc_type=corrected,
                notes=self.queue_notes.get(str(entry_id)) or None,
                actor=self.username,
            )
        else:
            documents.resolve_queue_entry(
                entry_id,
                resolved_doc_type=None,
                notes=self.queue_notes.get(str(entry_id)) or None,
                actor=self.username,
            )
        self.flash = "Resolved and filed."
        self.queue_open_entry_id = 0
        self._load_all()

    @rx.event
    def confirm_discard(self):
        self.discard_confirm = True

    @rx.event
    def cancel_discard(self):
        self.discard_confirm = False

    @rx.event
    def discard_queue(self, entry_id: int):
        """The 'discard' resolution option — removes the document outright."""
        for e in self.queue:
            if e.entry_id == entry_id:
                documents.discard_document(e.document_id, actor=self.username)
                break
        self.flash = "Document discarded."
        self.discard_confirm = False
        self.queue_open_entry_id = 0
        self._load_all()

    # ------------------------------------------------------------------
    # Queue filters (client / doc type / failure reason)
    # ------------------------------------------------------------------
    def set_queue_filter_client(self, v: str):
        self.queue_filter_client = v

    def set_queue_filter_type(self, v: str):
        self.queue_filter_type = v

    def set_queue_filter_flag(self, v: str):
        self.queue_filter_flag = _FLAG_LABEL_TO_KIND.get(v, "All flags")

    @rx.var
    def queue_client_options(self) -> list[str]:
        seen = ["All"]
        for e in self.queue:
            if e.client_label not in seen:
                seen.append(e.client_label)
        return seen

    @rx.var
    def filtered_queue(self) -> list[QueueRow]:
        out: list[QueueRow] = []
        for e in self.queue:
            if self.queue_filter_client != "All" and e.client_label != self.queue_filter_client:
                continue
            if self.queue_filter_type != "All types" and e.doc_type != self.queue_filter_type:
                continue
            if self.queue_filter_flag != "All flags" and e.flag_kind != self.queue_filter_flag:
                continue
            out.append(e)
        return out

    # ------------------------------------------------------------------
    # Queue file-by-file flow (open a single item as a detail page)
    # ------------------------------------------------------------------
    @rx.event
    def open_queue_item(self, entry_id: int):
        self.queue_open_entry_id = entry_id
        self.discard_confirm = False

    @rx.event
    def close_queue_item(self):
        self.queue_open_entry_id = 0
        self._load_queue()

    def set_discard_confirm(self, v: bool):
        self.discard_confirm = v

    # ------------------------------------------------------------------
    # Queue detail helpers (the currently-open file's metadata)
    # ------------------------------------------------------------------
    @rx.var
    def open_queue_entry(self) -> QueueRow | None:
        for e in self.queue:
            if e.entry_id == self.queue_open_entry_id:
                return e
        return None

    @rx.var
    def queue_entry_title(self) -> str:
        e = self.open_queue_entry
        if e is None:
            return "Document"
        return f"Document #{e.document_id} · {e.doc_type}"

    @rx.var
    def queue_entry_reason(self) -> list[str]:
        e = self.open_queue_entry
        if e is None:
            return ["No item selected."]
        parts = [f"Flagged: {e.reason}"]
        if e.client_label != "—":
            parts.append(f"Client: {e.client_label}")
        if e.period:
            parts.append(f"Period: {e.period}")
        return parts

    @rx.var
    def queue_entry_flag_kind(self) -> str:
        e = self.open_queue_entry
        return e.flag_kind if e else "other"

    @rx.var
    def queue_entry_type_value(self) -> str:
        return self.queue_type.get(str(self.queue_open_entry_id), "(keep as-is)")

    @rx.var
    def queue_entry_notes_value(self) -> str:
        return self.queue_notes.get(str(self.queue_open_entry_id), "")

    # ------------------------------------------------------------------
    # Document notes (distinct from queue resolution notes)
    # ------------------------------------------------------------------
    def set_detail_notes_draft(self, v: str):
        self.detail_notes_draft = v

    @rx.event
    def save_notes(self):
        try:
            documents.set_document_notes(self.selected_document_id, self.detail_notes_draft)
            self.detail_notes = self.detail_notes_draft
            self.flash = "Note saved."
        except documents.DocumentError as exc:
            self.error = str(exc)

    # ------------------------------------------------------------------
    # Reassign — pre-fill current client
    # ------------------------------------------------------------------
    @rx.event
    def set_document_reassign_client(self, client_id: int):
        self.reassign_client_id = client_id

    @rx.event
    def confirm_delete(self):
        self.delete_confirm = True

    @rx.event
    def cancel_delete(self):
        self.delete_confirm = False

    @rx.event
    def confirm_soft_delete(self):
        documents.soft_delete_document(
            self.selected_document_id, reason=self.delete_reason or None, actor=self.username,
        )
        self.flash = "Document soft-deleted — recoverable from Recently Deleted."
        self.delete_reason = ""
        self.delete_confirm = False
        self._load_all()

    # ------------------------------------------------------------------
    # File viewer (shared)
    # ------------------------------------------------------------------
    @rx.event
    def open_document_viewer(self):
        """Build the shared viewer from the current version's bytes."""
        import base64

        v = documents.get_current_version(self.selected_document_id)
        if v is None:
            self.viewer_open = False
            return
        data = v.get("file_bytes")
        filename = v.get("filename") or self.detail_filename
        if not data:
            self.viewer_open = False
            return

        from setu.state.shared_upload import (
            VIEWER_DOWNLOAD,
            VIEWER_IMAGE,
            VIEWER_PDF,
            VIEWER_TABLE,
            VIEWER_TEXT,
            pdf_page_pngs,
            sniff_file,
        )

        info = sniff_file(filename, data)
        self.viewer_title = filename
        self.viewer_open = True
        self.viewer_kind = info["kind"]
        self.viewer_mime = info.get("mime", "")
        self.viewer_page = 1
        self.viewer_download_name = filename

        if info["kind"] == VIEWER_IMAGE:
            self.viewer_image_src = f"data:{info['mime']};base64,{base64.b64encode(data).decode('ascii')}"
            self.viewer_page_count = 0
        elif info["kind"] == VIEWER_PDF:
            pages = pdf_page_pngs(data)
            self.viewer_pdf_pages = pages
            self.viewer_page_count = len(pages)
            self.viewer_image_src = pages[0] if pages else ""
            if not pages:
                # Fall back to download if rasterisation failed
                self.viewer_kind = VIEWER_DOWNLOAD
        elif info["kind"] in (VIEWER_TABLE, VIEWER_TEXT):
            self._load_viewer_tabular_or_text(filename, data, info["kind"])
        else:
            self.viewer_kind = VIEWER_DOWNLOAD

        # Always prepare a download link as the fallback affordance
        mime = _mime_for(filename)
        self.viewer_download_mime = mime
        self.viewer_download_b64 = base64.b64encode(data).decode("ascii")

    def _load_viewer_tabular_or_text(self, filename: str, data: bytes, kind: str) -> None:
        self.viewer_table_headers = []
        self.viewer_table_rows = []
        self.viewer_text = ""
        if kind == "table":
            try:
                import io

                import pandas as pd

                if filename.lower().endswith(".csv"):
                    df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
                else:
                    df = pd.read_excel(io.BytesIO(data), dtype=str)
                self.viewer_table_headers = [str(c) for c in df.columns]
                self.viewer_table_rows = [
                    ["" if v is None else str(v) for v in row]
                    for row in df.head(60).itertuples(index=False)
                ]
            except Exception:  # noqa: BLE001
                self.viewer_kind = "download"
            return
        # text (docx / fallback)
        try:
            from src.invoice_extract import extractor

            text = extractor._docx_text(data, filename)
            self.viewer_text = (text or "").strip()[:8000]
        except Exception:  # noqa: BLE001
            self.viewer_text = ""


def _mime_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel", "csv": "text/csv", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
    }.get(ext, "application/octet-stream")


def _flag_kind(reason: str) -> str:
    r = (reason or "").lower()
    if "couldn't classify" in r:
        return "classify"
    if "unrecognized shape" in r:
        return "shape"
    if "need mapping" in r or "couldn't map" in r:
        return "mapping"
    return "other"


def _flag_label(reason: str) -> str:
    kind = _flag_kind(reason)
    if kind == "classify":
        return "Couldn't classify"
    if kind == "shape":
        return "Couldn't map — unrecognized shape"
    if kind == "mapping":
        return "Couldn't map — fields need mapping"
    return "Other"


_FLAG_LABEL_TO_KIND = {
    "All flags": "All flags",
    "Couldn't classify": "classify",
    "Couldn't map — fields need mapping": "mapping",
    "Couldn't map — unrecognized shape": "shape",
    "Other": "other",
}