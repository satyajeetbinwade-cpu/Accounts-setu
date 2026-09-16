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


@dataclass
class VersionRow:
    version_number: int
    filename: str
    uploaded_by: str
    uploaded_at: str


@dataclass
class EvidenceRow:
    target_label: str


@dataclass
class QueueRow:
    entry_id: int
    document_id: int
    reason: str
    doc_type: str
    client_label: str
    period: str


@dataclass
class DeletedRow:
    document_id: int
    filename: str
    doc_type: str
    deleted_at: str
    deleted_by: str


class DocumentsState(AuthState):
    """Document & Data Repository state."""

    section: str = "Document Vault"

    client_options: list[ClientOption] = []
    vault_client_id: int = 0

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
    detail_type: str = ""
    detail_period: str = ""
    detail_review_status: str = ""
    detail_pct: int = 0
    detail_is_deleted: bool = False
    versions: list[VersionRow] = []
    evidence: list[EvidenceRow] = []

    # reassign
    reassign_client_id: int = 0
    reassign_reason: str = ""

    # delete
    delete_reason: str = ""

    # queue
    queue: list[QueueRow] = []
    queue_type: dict[str, str] = {}
    queue_notes: dict[str, str] = {}

    # recently deleted
    deleted: list[DeletedRow] = []

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def can_upload(self) -> bool:
        return "documents.upload" in self._codes()

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
        if "documents.view" not in self._codes():
            return rx.redirect("/")
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        if not self.vault_client_id and self.client_options:
            self.vault_client_id = self.client_options[0].client_id
        self._load_all()

    def _load_all(self) -> None:
        self._load_documents()
        self._load_queue()
        self._load_deleted()
        if self.selected_document_id:
            self._load_detail()

    def _load_documents(self) -> None:
        if not self.vault_client_id:
            self.documents = []
            return
        rows = documents.list_documents(
            client_id=self.vault_client_id,
            doc_type=None if self.filter_type == "All" else self.filter_type,
            period=self.filter_period or None,
            search=self.filter_search or None,
        )
        self.documents = [
            DocumentRow(
                document_id=d["document_id"],
                filename=d.get("latest_filename") or f"Document #{d['document_id']}",
                doc_type=d["doc_type"],
                period=d.get("period") or "",
                review_status=d["review_status"],
                classification_pct=int(d.get("classification_pct") or 0),
                is_deleted=bool(d.get("is_deleted")),
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
        self.detail_type = doc["doc_type"]
        self.detail_period = doc.get("period") or ""
        self.detail_review_status = doc["review_status"]
        self.detail_pct = int(doc.get("classification_pct") or 0)
        self.detail_is_deleted = bool(doc.get("is_deleted"))
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
    # Upload (async — UploadFile.read() is a coroutine)
    # ------------------------------------------------------------------
    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
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