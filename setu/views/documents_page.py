"""F3 — Document & Data Repository.

Scoped vault (upload → classify → file), document detail (versions, evidence
links, reassign, soft-delete), Unified Review Queue, Recently Deleted. Uses
only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.documents_state import DocumentsState
from setu.views import shell

_HUB = ["Document Vault", "Unified Review Queue", "Recently Deleted"]

_ACCEPT = {
    "image/jpeg": [".jpg", ".jpeg"],
    "image/png": [".png"],
    "application/pdf": [".pdf"],
    "application/msword": [".doc"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
    "text/csv": [".csv"],
}


def _hub_button(label: str) -> rx.Component:
    active = DocumentsState.section == label
    return rx.button(
        label,
        on_click=DocumentsState.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _upload_widget() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Upload document", style=t.TEXT["card_title"]),
            rx.text(
                "Images (JPG/PNG), documents (DOC/DOCX/PDF), spreadsheets (XLS/XLSX/CSV). No size cap.",
                style=t.TEXT["micro"],
            ),
            rx.input(
                value=DocumentsState.upload_period,
                on_change=DocumentsState.set_upload_period,
                placeholder="Period (optional), e.g. 2026-08",
                width="240px",
            ),
            rx.upload(
                rx.vstack(
                    rx.icon("upload", size=22, color=t.Color.NEUTRAL.value),
                    rx.text("Drag files here or click to browse", style=t.TEXT["label"]),
                    spacing="2",
                    align="center",
                    padding="24px",
                ),
                id="f3_upload",
                accept=_ACCEPT,
                multiple=True,
                border=f"1px dashed {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
                background=t.Color.SURFACE.value,
            ),
            rx.hstack(
                rx.button(
                    "Upload",
                    on_click=DocumentsState.handle_upload(rx.upload_files(upload_id="f3_upload")),
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Clear",
                    on_click=rx.clear_selected_files("f3_upload"),
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                ),
                spacing="2",
            ),
            rx.cond(DocumentsState.upload_error != "", c.inline_reason(DocumentsState.upload_error), rx.fragment()),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _doc_row(doc) -> rx.Component:
    return rx.hstack(
        rx.text(
            doc.filename,
            on_click=DocumentsState.open_document(doc.document_id),
            flex="3",
            font_size="13px",
            font_weight="600",
            color=t.Color.TEXT_PRIMARY.value,
            cursor="pointer",
            text_align="left",
            overflow="hidden",
            text_overflow="ellipsis",
            white_space="nowrap",
            _hover={"color": t.Color.ACCENT.value, "text_decoration": "underline"},
        ),
        rx.text(doc.doc_type, style=t.TEXT["body"], flex="2"),
        rx.text(rx.cond(doc.period != "", doc.period, "—"), style=t.TEXT["body"], flex="2"),
        rx.box(
            rx.cond(
                doc.review_status == "pending_review",
                c.confidence_badge("ai", pct=doc.classification_pct, label="Awaiting review"),
                rx.text("Filed", style=t.TEXT["micro"]),
            ),
            flex="2",
        ),
        width="100%",
        align="center",
        padding="8px 4px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _vault() -> rx.Component:
    return rx.vstack(
        rx.cond(
            DocumentsState.selected_document_id != 0,
            _document_detail(),
            rx.vstack(
                rx.hstack(
                    rx.text("Client:", style=t.TEXT["micro"]),
                    rx.select(
                        DocumentsState.client_options.map(lambda o: o.legal_name),
                        on_change=lambda v: DocumentsState.set_vault_client(
                            DocumentsState.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                        ),
                        placeholder="Select a client",
                        width="280px",
                    ),
                    spacing="2",
                    align="center",
                ),
                rx.cond(DocumentsState.can_upload, _upload_widget(), rx.fragment()),
                c.card(
                    rx.vstack(
                        rx.hstack(
                            rx.select(
                                ["All", "GSTR-2B", "Form 26AS", "Tally Export", "IMS Export", "TDS Certificate", "Bank Statement", "Invoice", "Other"],
                                value=DocumentsState.filter_type,
                                on_change=DocumentsState.set_filter_type,
                                width="200px",
                            ),
                            rx.input(
                                value=DocumentsState.filter_period,
                                on_change=DocumentsState.set_filter_period,
                                placeholder="Period",
                                width="140px",
                            ),
                            rx.input(
                                value=DocumentsState.filter_search,
                                on_change=DocumentsState.set_filter_search,
                                placeholder="Search filename / type",
                                flex="1",
                            ),
                            spacing="3",
                            width="100%",
                        ),
                        rx.cond(
                            DocumentsState.documents.length() > 0,
                            rx.vstack(
                                rx.hstack(
                                    rx.text("File", style=t.TEXT["label"], flex="3"),
                                    rx.text("Type", style=t.TEXT["label"], flex="2"),
                                    rx.text("Period", style=t.TEXT["label"], flex="2"),
                                    rx.text("Status", style=t.TEXT["label"], flex="2"),
                                    width="100%",
                                    padding="0 4px 6px 4px",
                                    border_bottom=f"1px solid {t.Color.BORDER.value}",
                                ),
                                rx.foreach(DocumentsState.documents, _doc_row),
                                spacing="0",
                                width="100%",
                            ),
                            c.empty_state("No documents filed for this client yet.", icon="folder-open"),
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                spacing="4",
                width="100%",
                align="start",
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


def _document_detail() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back to vault",
            variant="ghost",
            color_scheme="gray",
            size="1",
            on_click=DocumentsState.back_to_vault,
        ),
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.vstack(
                        rx.text(DocumentsState.detail_filename, font_size="17px", font_weight="700"),
                        rx.text(
                            f"Client: {DocumentsState.detail_client} · Type: {DocumentsState.detail_type} · Period: {DocumentsState.detail_period}",
                            style=t.TEXT["label"],
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.spacer(),
                    rx.cond(
                        DocumentsState.detail_review_status == "pending_review",
                        c.confidence_badge("ai", pct=DocumentsState.detail_pct, label="Awaiting classification review"),
                        rx.fragment(),
                    ),
                    rx.cond(
                        DocumentsState.detail_is_deleted,
                        c.accent_pill("Deleted — see Recently Deleted to restore"),
                        rx.fragment(),
                    ),
                    width="100%",
                    align="start",
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # Used in — evidence links
        c.card(
            rx.vstack(
                rx.text("Used in", style=t.TEXT["card_title"]),
                rx.cond(
                    DocumentsState.evidence.length() > 0,
                    rx.vstack(
                        rx.foreach(DocumentsState.evidence, lambda e: rx.text(f"• {e.target_label}", style=t.TEXT["body"])),
                        spacing="1",
                        align="start",
                    ),
                    rx.text("Not yet used in any record.", style=t.TEXT["micro"]),
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # Version history
        c.card(
            rx.vstack(
                rx.text("Version history", style=t.TEXT["card_title"]),
                rx.cond(
                    DocumentsState.versions.length() > 0,
                    rx.vstack(
                        rx.foreach(
                            DocumentsState.versions,
                            lambda v: rx.hstack(
                                rx.text(f"v{v.version_number}", style=t.TEXT["body"], flex="1"),
                                rx.text(v.filename, style=t.TEXT["body"], flex="4"),
                                rx.text(f"{v.uploaded_by} · {v.uploaded_at}", style=t.TEXT["micro"], flex="3"),
                                width="100%",
                                padding="6px 0",
                                border_bottom=f"1px solid {t.Color.BORDER.value}",
                            ),
                        ),
                        spacing="0",
                        width="100%",
                    ),
                    rx.text("No versions recorded.", style=t.TEXT["micro"]),
                ),
                rx.cond(
                    DocumentsState.can_upload,
                    rx.vstack(
                        rx.upload(
                            rx.text("Upload new version", style=t.TEXT["label"]),
                            id="f3_newver",
                            accept=_ACCEPT,
                            multiple=False,
                            border=f"1px dashed {t.Color.BORDER.value}",
                            border_radius="10px",
                            padding="14px",
                            width="100%",
                        ),
                        rx.button(
                            "Save new version",
                            on_click=DocumentsState.add_version(rx.upload_files(upload_id="f3_newver")),
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_PRIMARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                        spacing="2",
                        align="start",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # Reassign
        c.card(
            rx.vstack(
                rx.text("Reassign to a different client", style=t.TEXT["card_title"]),
                rx.cond(
                    DocumentsState.can_reassign,
                    rx.vstack(
                        rx.select(
                            DocumentsState.client_options.map(lambda o: o.legal_name),
                            on_change=lambda v: DocumentsState.set_reassign_client(
                                DocumentsState.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                            ),
                            placeholder="Correct client",
                            width="280px",
                        ),
                        rx.cond(
                            DocumentsState.reassign_dirty,
                            rx.vstack(
                                rx.text("Reason for this reassignment (required before saving)", style=t.TEXT["label"]),
                                rx.input(
                                    value=DocumentsState.reassign_reason,
                                    on_change=DocumentsState.set_reassign_reason,
                                    placeholder="e.g. uploaded under the wrong client during intake",
                                    width="100%",
                                ),
                                rx.button(
                                    "Save reassignment",
                                    on_click=DocumentsState.save_reassign,
                                    disabled=DocumentsState.reassign_reason == "",
                                    background=t.Color.ACCENT.value,
                                    color="#FFFFFF",
                                ),
                                rx.cond(
                                    DocumentsState.reassign_reason == "",
                                    c.inline_reason("A reason is required before this change can be saved."),
                                    rx.fragment(),
                                ),
                                spacing="2",
                                align="start",
                                width="100%",
                            ),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="start",
                        width="100%",
                    ),
                    c.inline_reason("Requires Manager-level permission or above."),
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # Soft-delete / restore
        c.card(
            rx.vstack(
                rx.text("Document actions", style=t.TEXT["card_title"]),
                rx.cond(
                    DocumentsState.detail_is_deleted,
                    rx.vstack(
                        c.deactivate_button(
                            "Restore",
                            on_click=DocumentsState.restore_document(DocumentsState.selected_document_id),
                            disabled=~DocumentsState.can_delete,
                        ),
                        rx.cond(
                            ~DocumentsState.can_delete,
                            c.inline_reason("Requires Partner or Manager"),
                            rx.fragment(),
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.cond(
                        DocumentsState.can_delete,
                        rx.popover.root(
                            rx.popover.trigger(
                                c.delete_button("Delete"),
                            ),
                            rx.popover.content(
                                rx.vstack(
                                    rx.text("Reason for deletion (optional)", style=t.TEXT["label"]),
                                    rx.input(
                                        value=DocumentsState.delete_reason,
                                        on_change=DocumentsState.set_delete_reason,
                                        width="100%",
                                    ),
                                    rx.button(
                                        "Confirm delete",
                                        on_click=DocumentsState.soft_delete,
                                        background=t.Color.DANGER.value,
                                        color="#FFFFFF",
                                    ),
                                    spacing="2",
                                    align="start",
                                    width="240px",
                                ),
                                side="bottom",
                            ),
                        ),
                        rx.vstack(
                            c.delete_button("Delete", disabled=True),
                            c.inline_reason("Requires Partner or Manager"),
                            spacing="1",
                            align="start",
                        ),
                    ),
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        spacing="4",
        width="100%",
        align="start",
    )


def _queue_row(e) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text(f"Document #{e.document_id} · {e.doc_type}", style=t.TEXT["card_title"]),
                    rx.text(
                        rx.cond(e.period != "", f"Client: {e.client_label} · Period: {e.period}", f"Client: {e.client_label}"),
                        style=t.TEXT["micro"],
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.spacer(),
                c.accent_pill(e.reason),
                width="100%",
                align="start",
            ),
            rx.cond(
                DocumentsState.can_resolve,
                rx.vstack(
                    rx.hstack(
                        rx.select(
                            ["(keep as-is)", "GSTR-2B", "Form 26AS", "Tally Export", "IMS Export", "TDS Certificate", "Bank Statement", "Invoice", "Other"],
                            value=DocumentsState.queue_type[e.entry_id.to_string()],
                            on_change=lambda v: DocumentsState.set_queue_type(e.entry_id, v),
                            width="220px",
                        ),
                        rx.input(
                            value=DocumentsState.queue_notes[e.entry_id.to_string()],
                            on_change=lambda v: DocumentsState.set_queue_notes(e.entry_id, v),
                            placeholder="Resolution notes (optional)",
                            flex="1",
                        ),
                        rx.button(
                            "Resolve",
                            on_click=DocumentsState.resolve_queue(e.entry_id),
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        spacing="3",
                        width="100%",
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                c.inline_reason("Requires Manager-level permission or above to resolve."),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _review_queue() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                c.stat(DocumentsState.queue.length().to_string(), "items awaiting resolution"),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            DocumentsState.queue.length() > 0,
            rx.vstack(rx.foreach(DocumentsState.queue, _queue_row), spacing="3", width="100%"),
            c.empty_state("Nothing waiting on human review right now.", icon="check-check"),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


def _deleted_row(d) -> rx.Component:
    return rx.hstack(
        rx.text(d.filename, style=t.TEXT["body"], flex="3"),
        rx.text(d.doc_type, style=t.TEXT["body"], flex="2"),
        rx.text(d.deleted_at, style=t.TEXT["micro"], flex="2"),
        rx.text(d.deleted_by, style=t.TEXT["micro"], flex="2"),
        rx.button(
            "Restore",
            on_click=DocumentsState.restore_document(d.document_id),
            disabled=~DocumentsState.can_delete,
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_PRIMARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="8px",
        ),
        width="100%",
        align="center",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _recently_deleted() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Recently deleted", style=t.TEXT["card_title"]),
            rx.text("Soft-delete only — always recoverable, evidence links still resolve.", style=t.TEXT["label"]),
            rx.cond(
                DocumentsState.deleted.length() > 0,
                rx.vstack(rx.foreach(DocumentsState.deleted, _deleted_row), spacing="0", width="100%"),
                c.empty_state("No deleted documents.", icon="trash-2"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def documents_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("Document & Data Repository", "Every document, organized, searchable, and evidence-linkable."),
            rx.cond(DocumentsState.flash != "", c.info_banner(DocumentsState.flash), rx.fragment()),
            rx.cond(DocumentsState.error != "", c.inline_reason(DocumentsState.error), rx.fragment()),
            rx.hstack(
                *[_hub_button(label) for label in _HUB],
                spacing="2",
                wrap="wrap",
                padding="4px",
                background=t.Color.SURFACE.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
            ),
            rx.match(
                DocumentsState.section,
                ("Document Vault", _vault()),
                ("Unified Review Queue", _review_queue()),
                ("Recently Deleted", _recently_deleted()),
                _vault(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )