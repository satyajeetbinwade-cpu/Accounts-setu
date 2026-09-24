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
                on_drop=DocumentsState.stage,
                border=f"1px dashed {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
                background=t.Color.SURFACE.value,
            ),
            # FilePreviewChip — staged files before upload (shared component).
            rx.cond(
                DocumentsState.pending.length() > 0,
                rx.vstack(
                    rx.text("Ready to upload", style=t.TEXT["label"]),
                    rx.vstack(
                        rx.foreach(
                            DocumentsState.pending,
                            lambda pf: c.file_preview_chip(
                                pf,
                                on_remove=DocumentsState.remove_pending(pf.key),
                            ),
                        ),
                        spacing="2",
                        width="100%",
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                rx.fragment(),
            ),
            # UploadProgressState — one distinct stage per file (shared).
            rx.cond(
                DocumentsState.upload_progress.length() > 0,
                rx.vstack(
                    rx.foreach(
                        DocumentsState.upload_progress,
                        lambda r: c.upload_progress_row(r),
                    ),
                    spacing="0",
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.hstack(
                rx.button(
                    "Upload",
                    on_click=DocumentsState.upload_pending,
                    disabled=DocumentsState.pending.length() == 0,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Clear",
                    on_click=DocumentsState.clear_pending,
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
        rx.hstack(
            rx.text(
                doc.filename,
                on_click=DocumentsState.open_document(doc.document_id),
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
            rx.cond(
                doc.is_duplicate,
                c.pill("Possible duplicate", variant="ai"),
                rx.fragment(),
            ),
            spacing="2",
            align="center",
            min_width="0",
            flex="3",
        ),
        rx.text(doc.client_label, style=t.TEXT["body"], flex="2"),
        rx.text(doc.doc_type, style=t.TEXT["body"], flex="2"),
        rx.text(rx.cond(doc.period != "", doc.period, "—"), style=t.TEXT["body"], flex="1"),
        rx.text(rx.cond(doc.uploaded_at != "", doc.uploaded_at, "—"), style=t.TEXT["micro"], flex="2"),
        rx.text(doc.file_size_label, style=t.TEXT["micro"], flex="1"),
        rx.box(
            rx.cond(
                doc.review_status == "pending_review",
                c.confidence_badge("ai", pct=doc.classification_pct, label="Awaiting review"),
                rx.text("Filed", style=t.TEXT["micro"]),
            ),
            flex="2",
        ),
        rx.icon("chevron-right", size=16, color=t.Color.TEXT_MUTED.value),
        width="100%",
        align="center",
        padding="8px 4px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
        cursor="pointer",
        on_click=DocumentsState.open_document(doc.document_id),
        _hover={"background": "#F6F8FB"},
    )


def _vault() -> rx.Component:
    return rx.vstack(
        rx.cond(
            DocumentsState.selected_document_id != 0,
            _document_detail(),
            rx.vstack(
                rx.cond(
                    DocumentsState.embedded,
                    rx.fragment(),
                    rx.hstack(
                        rx.text("Client:", style=t.TEXT["micro"]),
                        rx.select(
                            DocumentsState.vault_client_names,
                            value=DocumentsState.vault_client_name,
                            on_change=DocumentsState.set_vault_client_by_name,
                            placeholder="Select a client",
                            width="320px",
                        ),
                        spacing="2",
                        align="center",
                    ),
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
                                    rx.text("Client", style=t.TEXT["label"], flex="2"),
                                    rx.text("Type", style=t.TEXT["label"], flex="2"),
                                    rx.text("Period", style=t.TEXT["label"], flex="1"),
                                    rx.text("Uploaded", style=t.TEXT["label"], flex="2"),
                                    rx.text("Size", style=t.TEXT["label"], flex="1"),
                                    rx.text("Status", style=t.TEXT["label"], flex="2"),
                                    rx.box(width="16px"),
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
            on_click=DocumentsState.back_to_vault,
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_SECONDARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="9px",
            size="2",
            _hover={"background": "#EEF2F8", "color": t.Color.TEXT_PRIMARY.value},
        ),
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.vstack(
                        rx.text(DocumentsState.detail_filename, font_size="17px", font_weight="700"),
                        rx.hstack(
                            rx.text(f"Client: {DocumentsState.detail_client}", style=t.TEXT["label"]),
                            rx.text("·", style=t.TEXT["micro"]),
                            rx.text(f"Type: {DocumentsState.detail_type}", style=t.TEXT["label"]),
                            rx.text("·", style=t.TEXT["micro"]),
                            rx.text("Period:", style=t.TEXT["label"]),
                            rx.text(DocumentsState.detail_period_label, style=t.TEXT["label"], font_weight="700"),
                            spacing="1",
                            align="center",
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
        # FileViewerPanel — the primary place a reviewer sees the file (shared).
        c.file_viewer_panel(
            title_var=DocumentsState.detail_filename,
            kind_var=DocumentsState.viewer_kind,
            image_src_var=DocumentsState.viewer_image_src,
            page_var=DocumentsState.viewer_page,
            page_count_var=DocumentsState.viewer_page_count,
            table_headers_var=DocumentsState.viewer_table_headers,
            table_rows_var=DocumentsState.viewer_table_rows,
            text_var=DocumentsState.viewer_text,
            download_name_var=DocumentsState.viewer_download_name,
            download_href_var="data:" + DocumentsState.viewer_download_mime + ";base64," + DocumentsState.viewer_download_b64,
            on_close=DocumentsState.close_viewer,
            on_page_prev=DocumentsState.viewer_page_prev,
            on_page_next=DocumentsState.viewer_page_next,
            zoom_var=DocumentsState.viewer_zoom,
            on_zoom_in=DocumentsState.viewer_zoom_in,
            on_zoom_out=DocumentsState.viewer_zoom_out,
            on_zoom_reset=DocumentsState.viewer_zoom_reset,
        ),
        rx.hstack(
            rx.button(
                rx.hstack(rx.icon("eye", size=16), rx.text("View file"), spacing="2"),
                on_click=DocumentsState.open_document_viewer,
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_PRIMARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="9px",
                size="2",
            ),
            rx.text("Opens the document inline — no download needed.", style=t.TEXT["micro"]),
            spacing="2",
            align="center",
        ),
        # Optional note on the document itself (distinct from queue notes)
        c.card(
            rx.vstack(
                rx.text("Reviewer note", style=t.TEXT["card_title"]),
                rx.input(
                    value=DocumentsState.detail_notes_draft,
                    on_change=DocumentsState.set_detail_notes_draft,
                    placeholder="Add an optional note about this document…",
                    width="100%",
                ),
                rx.button(
                    "Save note",
                    on_click=DocumentsState.save_notes,
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_PRIMARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                    size="1",
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
                            value=DocumentsState.reassign_client_name,
                            on_change=DocumentsState.set_reassign_client_by_name,
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
                        rx.cond(
                            DocumentsState.delete_confirm,
                            rx.vstack(
                                c.warning_banner(
                                    "This soft-deletes the document. It can be restored from Recently Deleted."
                                ),
                                rx.text("Reason for deletion (optional)", style=t.TEXT["label"]),
                                rx.input(
                                    value=DocumentsState.delete_reason,
                                    on_change=DocumentsState.set_delete_reason,
                                    placeholder="Why is this being deleted?",
                                    width="100%",
                                ),
                                rx.hstack(
                                    rx.button(
                                        "Confirm delete",
                                        on_click=DocumentsState.confirm_soft_delete,
                                        background=t.Color.DANGER.value,
                                        color="#FFFFFF",
                                    ),
                                    rx.button(
                                        "Cancel",
                                        on_click=DocumentsState.cancel_delete,
                                        variant="soft",
                                        background="transparent",
                                        color=t.Color.TEXT_SECONDARY.value,
                                        border=f"1px solid {t.Color.BORDER.value}",
                                        border_radius="9px",
                                    ),
                                    spacing="2",
                                ),
                                spacing="2",
                                align="start",
                                width="100%",
                            ),
                            c.delete_button("Delete", on_click=DocumentsState.confirm_delete),
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


def _flag_pill(e) -> rx.Component:
    """Distinct visual per flag type — 'couldn't classify' (needs human
    judgment) vs 'couldn't map — N need mapping' vs 'couldn't map —
    unrecognized shape' (parser gap) are different problems."""
    return rx.match(
        e.flag_kind,
        ("classify", c.pill("Couldn't classify — needs judgment", variant="ai")),
        ("mapping", c.pill("Couldn't map — fields need mapping", variant="accent")),
        ("shape", c.pill("Couldn't map — unrecognized shape", variant="danger")),
        c.pill("Needs review", variant="placeholder"),
    )


def _queue_row(e) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(
                rx.cond(e.document_id > 0, f"Document #{e.document_id} · {e.doc_type}", e.doc_type),
                style=t.TEXT["body"],
                font_weight="600",
            ),
            rx.text(
                rx.cond(e.period != "", f"Client: {e.client_label} · Period: {e.period}", f"Client: {e.client_label}"),
                style=t.TEXT["micro"],
            ),
            spacing="1",
            align="start",
            flex="1",
            min_width="0",
        ),
        rx.text(e.reason, style=t.TEXT["micro"], flex="2", min_width="0"),
        _flag_pill(e),
        rx.button(
            "Open",
            on_click=DocumentsState.open_queue_item(e.entry_id),
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_PRIMARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="8px",
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
        _hover={"background": "#F6F8FB"},
        cursor="pointer",
        on_click=DocumentsState.open_queue_item(e.entry_id),
    )


def _detail_flag_pill() -> rx.Component:
    return rx.match(
        DocumentsState.queue_entry_flag_kind,
        ("classify", c.pill("Couldn't classify — needs judgment", variant="ai")),
        ("mapping", c.pill("Couldn't map — fields need mapping", variant="accent")),
        ("shape", c.pill("Couldn't map — unrecognized shape", variant="danger")),
        c.pill("Needs review", variant="placeholder"),
    )


def _queue_detail() -> rx.Component:
    """File-by-file resolution flow: the FileViewerPanel + why it was flagged
    + the resolution action together, reusing the Document Detail pattern."""
    return rx.vstack(
        rx.button(
            "← Back to review queue",
            on_click=DocumentsState.close_queue_item,
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_SECONDARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="9px",
            size="2",
            _hover={"background": "#EEF2F8", "color": t.Color.TEXT_PRIMARY.value},
        ),
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.vstack(
                        rx.text(
                            DocumentsState.queue_entry_title,
                            style=t.TEXT["card_title"],
                        ),
                        rx.foreach(
                            DocumentsState.queue_entry_reason,
                            lambda r: rx.text(r, style=t.TEXT["micro"]),
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.spacer(),
                    _detail_flag_pill(),
                    width="100%",
                    align="start",
                ),
                rx.cond(
                    DocumentsState.can_resolve,
                    rx.vstack(
                        rx.text("Resolution", style=t.TEXT["card_title"]),
                        rx.hstack(
                            rx.select(
                                ["(keep as-is)", "GSTR-2B", "Form 26AS", "Tally Export", "IMS Export", "TDS Certificate", "Bank Statement", "Invoice", "Other"],
                                value=DocumentsState.queue_entry_type_value,
                                on_change=DocumentsState.set_queue_type(DocumentsState.queue_open_entry_id),
                                width="240px",
                            ),
                            rx.text("Reclassify document type (remap fields → file)", style=t.TEXT["micro"]),
                            spacing="2",
                            align="center",
                        ),
                        rx.input(
                            value=DocumentsState.queue_entry_notes_value,
                            on_change=DocumentsState.set_queue_notes(DocumentsState.queue_open_entry_id),
                            placeholder="Resolution notes (optional)",
                            width="100%",
                        ),
                        rx.hstack(
                            rx.button(
                                "File (keep as-is)",
                                on_click=DocumentsState.file_as_is,
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_PRIMARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="9px",
                            ),
                            rx.button(
                                "Reclassify & file",
                                on_click=DocumentsState.reclassify_and_file,
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                            ),
                            rx.cond(
                                DocumentsState.discard_confirm,
                                rx.hstack(
                                    rx.button(
                                        "Confirm discard",
                                        on_click=DocumentsState.discard_queue(DocumentsState.queue_open_entry_id),
                                        background=t.Color.DANGER.value,
                                        color="#FFFFFF",
                                    ),
                                    rx.button(
                                        "Cancel",
                                        on_click=DocumentsState.cancel_discard,
                                        variant="soft",
                                        background="transparent",
                                        color=t.Color.TEXT_SECONDARY.value,
                                        border=f"1px solid {t.Color.BORDER.value}",
                                        border_radius="9px",
                                    ),
                                    spacing="2",
                                ),
                                rx.button(
                                    "Discard",
                                    on_click=DocumentsState.confirm_discard,
                                    variant="soft",
                                    background="#FDEBEA",
                                    color=t.Color.DANGER.value,
                                    border=f"1px solid {t.Color.DANGER.value}",
                                    border_radius="9px",
                                ),
                            ),
                            spacing="2",
                            align="center",
                        ),
                        rx.cond(
                            DocumentsState.discard_confirm,
                            c.warning_banner("Discard permanently removes this document — it cannot be restored."),
                            rx.fragment(),
                        ),
                        spacing="3",
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
        ),
        spacing="4",
        width="100%",
        align="start",
    )


def _review_queue() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                c.stat(DocumentsState.queue.length().to_string(), "items awaiting resolution"),
                rx.hstack(
                    rx.text(
                        f"{DocumentsState.queue_progress['resolved_today']} of {DocumentsState.queue_progress['resolved_total'] + DocumentsState.queue_progress['pending']} resolved today",
                        style=t.TEXT["micro"],
                    ),
                    rx.progress(
                        value=rx.cond(
                            (DocumentsState.queue_progress['resolved_total'] + DocumentsState.queue_progress['pending']) > 0,
                            (DocumentsState.queue_progress['resolved_total'] * 100) / (DocumentsState.queue_progress['resolved_total'] + DocumentsState.queue_progress['pending']),
                            0,
                        ),
                        width="200px",
                        size="1",
                    ),
                    spacing="2",
                    align="center",
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            DocumentsState.queue_open_entry_id != 0,
            _queue_detail(),
            rx.vstack(
                rx.hstack(
                    rx.select(
                        DocumentsState.queue_client_options,
                        value=DocumentsState.queue_filter_client,
                        on_change=DocumentsState.set_queue_filter_client,
                        width="220px",
                    ),
                    rx.select(
                        ["All types", "GSTR-2B", "Form 26AS", "Tally Export", "IMS Export", "TDS Certificate", "Bank Statement", "Invoice", "Other"],
                        value=DocumentsState.queue_filter_type,
                        on_change=DocumentsState.set_queue_filter_type,
                        width="200px",
                    ),
                    rx.select(
                        ["All flags", "Couldn't classify", "Couldn't map — fields need mapping", "Couldn't map — unrecognized shape", "Other"],
                        value=DocumentsState.queue_filter_flag,
                        on_change=DocumentsState.set_queue_filter_flag,
                        width="280px",
                    ),
                    spacing="3",
                    width="100%",
                    wrap="wrap",
                ),
                rx.cond(
                    DocumentsState.filtered_queue.length() > 0,
                    c.card(
                        rx.vstack(
                            rx.foreach(DocumentsState.filtered_queue, _queue_row),
                            spacing="0",
                            width="100%",
                        ),
                        width="100%",
                    ),
                    c.empty_state("Nothing waiting on human review right now.", icon="check-check"),
                ),
                spacing="3",
                width="100%",
                align="start",
            ),
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


def embedded_vault() -> rx.Component:
    """The scoped document vault, rendered INSIDE another screen (F2's
    client profile Documents tab). The client is fixed by the caller via
    ``DocumentsState.load_for_client``, so the client picker is hidden.
    Renders an inline reason instead of redirecting when the signed-in user
    lacks documents.view — the caller has already passed its own gate."""
    return rx.cond(
        DocumentsState.can_view,
        _vault(),
        c.inline_reason("You do not have permission to view documents."),
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