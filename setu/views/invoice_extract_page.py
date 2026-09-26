"""F3-B — Invoice Extraction & Digitalization.

Upload (4 formats) → review screen (source preview | editable field table) →
Review Queue (headline → grouped → detail) → Export (immutable batches) →
Model Assignment (Admin). Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.invoice_extract_state import FORMAT_CHIPS, InvoiceExtractState
from setu.views import shell

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


def _hub() -> list[str]:
    return ["Upload", "Review Queue", "Export", "Model Assignment (Admin)"]


def _hub_button(label: str) -> rx.Component:
    active = InvoiceExtractState.section == label
    return rx.button(
        label,
        on_click=InvoiceExtractState.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _status_cell(row) -> rx.Component:
    return rx.match(
        row.status,
        ("needs_review", rx.vstack(c.pill(f"Needs review — {row.flagged_count} flagged", variant="danger"), spacing="1", align="start")),
        ("confirmed", c.confidence_badge("rule", label="Confirmed")),
        ("exported", c.accent_pill("Exported")),
        rx.text(row.status_label, style=t.TEXT["micro"]),
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _upload_row(row) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(row.filename, style=t.TEXT["body"], font_weight="600"),
            rx.hstack(
                rx.cond(
                    row.ai_used,
                    c.pill(f"AI · {row.ai_model}", variant="ai"),
                    c.pill("Offline parser", variant="placeholder"),
                ),
                rx.cond(
                    row.batch_id != "",
                    rx.text(f"Batch: {row.batch_id}", style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                spacing="2",
                align="center",
            ),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.text(row.path_label, style=t.TEXT["micro"]),
        rx.vstack(
            _status_cell(row),
            rx.cond(
                row.duplicate_warning,
                rx.text("⚠ Possible duplicate", style=t.TEXT["micro"]),
                rx.fragment(),
            ),
            spacing="1",
            align="start",
        ),
        rx.button(
            "Open",
            on_click=InvoiceExtractState.open_upload(row.upload_id),
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_PRIMARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="8px",
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _upload_section() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                c.section_title(
                    "Upload invoices",
                    "Any format — images (JPG/PNG), scanned or native PDF, Word and Excel/CSV. "
                    "An AI layer reads the document and maps it onto the standard field set; "
                    "images and scanned PDFs go to the vision model, text-based files to the "
                    "structured model.",
                ),
                rx.hstack(
                    *[c.pill(label, variant="placeholder") for label in FORMAT_CHIPS],
                    spacing="2",
                    wrap="wrap",
                ),
                rx.hstack(
                    rx.vstack(
                        rx.text("Client", style=t.TEXT["label"]),
                        rx.select(
                            InvoiceExtractState.client_names,
                            value=InvoiceExtractState.upload_client_name,
                            on_change=InvoiceExtractState.set_upload_client_by_name,
                            width="240px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.vstack(
                        rx.text("Batch name (optional)", style=t.TEXT["label"]),
                        rx.input(
                            value=InvoiceExtractState.batch_name,
                            on_change=InvoiceExtractState.set_batch_name,
                            placeholder="Groups uploads into one export run",
                            width="300px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    spacing="3",
                    align="end",
                    wrap="wrap",
                ),
                rx.upload(
                    rx.vstack(
                        rx.icon("upload", size=22, color=t.Color.NEUTRAL.value),
                        rx.text("Drag and drop invoices here", style=t.TEXT["label"]),
                        spacing="2",
                        align="center",
                        padding="24px",
                    ),
                    id="f3b_upload",
                    accept=_ACCEPT,
                    multiple=True,
                    on_drop=InvoiceExtractState.stage,
                    border=f"1px dashed {t.Color.BORDER.value}",
                    border_radius="12px",
                    width="100%",
                    background=t.Color.SURFACE.value,
                ),
                rx.cond(
                    InvoiceExtractState.pending.length() > 0,
                    rx.vstack(
                        rx.text("Ready to extract", style=t.TEXT["label"]),
                        rx.vstack(
                            rx.foreach(
                                InvoiceExtractState.pending,
                                lambda pf: c.file_preview_chip(
                                    pf,
                                    on_remove=InvoiceExtractState.remove_pending(pf.key),
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
                rx.cond(
                    InvoiceExtractState.upload_progress.length() > 0,
                    rx.vstack(
                        rx.foreach(
                            InvoiceExtractState.upload_progress,
                            lambda r: c.upload_progress_row(r),
                        ),
                        spacing="0",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                rx.hstack(
                    rx.button(
                        "Upload & extract",
                        on_click=InvoiceExtractState.upload_pending,
                        disabled=InvoiceExtractState.pending.length() == 0,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    rx.button(
                        "Clear",
                        on_click=InvoiceExtractState.clear_pending,
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                    ),
                    spacing="2",
                ),
                rx.cond(
                    InvoiceExtractState.upload_error != "",
                    c.inline_reason(InvoiceExtractState.upload_error),
                    rx.fragment(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        c.card(
            rx.vstack(
                c.section_title("Uploads"),
                rx.cond(
                    InvoiceExtractState.uploads.length() > 0,
                    rx.vstack(rx.foreach(InvoiceExtractState.uploads, _upload_row), spacing="0", width="100%"),
                    c.empty_state("No invoices uploaded yet for this client.", icon="file-text"),
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


# ---------------------------------------------------------------------------
# Review Queue
# ---------------------------------------------------------------------------


def _client_count(col) -> rx.Component:
    return rx.button(
        f"{col.label} — {col.count}",
        on_click=InvoiceExtractState.goto_queue_client(col.client_id),
        variant="soft",
        background="transparent",
        color=t.Color.TEXT_PRIMARY.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="10px",
        _hover={"background": "#EEF2F8", "border_color": t.Color.ACCENT.value},
    )


def _queue_row(row) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(row.filename, style=t.TEXT["body"], font_weight="600"),
            rx.text(f"{row.client_label} · {row.path_label}", style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        c.inline_reason(f"{row.flagged_count} field(s) flagged"),
        rx.button(
            "Review",
            on_click=InvoiceExtractState.open_upload(row.upload_id),
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_PRIMARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="8px",
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _review_queue_section() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                c.stat(
                    InvoiceExtractState.queue_open_count.to_string(),
                    "upload(s) awaiting resolution — this module's own queue, not F3's Unified Review Queue",
                    accent=True,
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            InvoiceExtractState.queue_by_client.length() > 0,
            rx.vstack(
                rx.text("By client", style=t.TEXT["label"], font_weight="700"),
                rx.hstack(
                    rx.foreach(InvoiceExtractState.queue_by_client, _client_count),
                    spacing="2",
                    wrap="wrap",
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            rx.fragment(),
        ),
        rx.cond(
            InvoiceExtractState.queue_entries.length() > 0,
            c.card(
                rx.vstack(
                    rx.foreach(InvoiceExtractState.queue_entries, _queue_row),
                    spacing="0",
                    width="100%",
                ),
                width="100%",
            ),
            c.empty_state(
                "Nothing awaiting review — every upload is either auto-accepted or already confirmed.",
                icon="circle_check",
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Review screen
# ---------------------------------------------------------------------------


def _field_row(f) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.hstack(
                    rx.text(f.label, style=t.TEXT["body"], font_weight="600"),
                    rx.button(
                        rx.icon("locate", size=13),
                        on_click=InvoiceExtractState.highlight_field(f.field_name),
                        size="1",
                        variant="ghost",
                        background="transparent",
                        color=t.Color.ACCENT.value,
                        title="Locate on the source document",
                    ),
                    spacing="1",
                    align="center",
                ),
                rx.cond(
                    f.source_location != "",
                    rx.text(f"source: {f.source_location}", style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                spacing="1",
                align="start",
                flex="1",
            ),
            rx.hstack(
                rx.cond(
                    f.resolved,
                    c.confidence_badge("rule", label="Resolved"),
                    rx.cond(
                        f.is_derived,
                        c.confidence_badge("rule", label="Derived"),
                        rx.cond(
                            f.not_present,
                            c.confidence_badge("ai", label="not present"),
                            rx.cond(
                                f.auto_accepted,
                                c.confidence_badge("ai", label=f"Auto-accepted — {f.confidence}%"),
                                c.confidence_badge("ai", pct=f.confidence),
                            ),
                        ),
                    ),
                ),
                rx.cond(
                    f.resolved | f.auto_accepted | f.is_derived,
                    rx.button(
                        "Edit",
                        on_click=InvoiceExtractState.start_edit_field(f.field_name),
                        size="1",
                        variant="ghost",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                    ),
                    rx.fragment(),
                ),
                spacing="2",
                align="center",
            ),
            width="100%",
            align="center",
            spacing="3",
        ),
        rx.cond(
            f.flagged | (InvoiceExtractState.editing_field == f.field_name),
            rx.box(
                rx.vstack(
                    rx.cond(
                        f.has_suggestion,
                        rx.hstack(
                            c.confidence_badge("ai", label=f"Suggested ({f.suggestion_label})"),
                            rx.text(
                                "Calculated from fields already accepted — confirm or override.",
                                style=t.TEXT["micro"],
                            ),
                            spacing="2",
                            align="center",
                        ),
                        rx.fragment(),
                    ),
                    rx.input(
                        value=f.input_value,
                        on_change=InvoiceExtractState.set_field_input(f.field_name),
                        placeholder="Enter the correct value (or leave blank if genuinely absent)",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.button(
                            "Resolve",
                            on_click=InvoiceExtractState.resolve_field(f.field_name),
                            size="1",
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.cond(
                            InvoiceExtractState.editing_field == f.field_name,
                            rx.button(
                                "Cancel",
                                on_click=InvoiceExtractState.cancel_edit_field,
                                size="1",
                                variant="ghost",
                                background="transparent",
                                color=t.Color.TEXT_SECONDARY.value,
                            ),
                            rx.fragment(),
                        ),
                        spacing="2",
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                background="#FDF4E3",
                border="1px solid #F0D9AE",
                border_radius="10px",
                padding="10px 12px",
                width="100%",
            ),
            rx.cond(
                f.is_derived,
                rx.text(
                    "derived — " + f.derived_label + " · " + f.effective_value,
                    style=t.TEXT["micro"],
                    color=t.Color.ACCENT.value,
                ),
                rx.text(
                    f"value: {rx.cond(f.effective_value != '', f.effective_value, '—')}",
                    style=t.TEXT["micro"],
                ),
            ),
        ),
        spacing="2",
        align="start",
        width="100%",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _field_section(title: str, section_key: str) -> rx.Component:
    """One review-screen group (Header / Line items / Totals). Renders only
    the fields whose ``section`` matches AND that are still open — resolved
    fields drop out of here and appear in the Resolved checklist below."""
    return rx.vstack(
        rx.text(title, style=t.TEXT["label"], font_weight="700", color=t.Color.TEXT_SECONDARY.value),
        rx.vstack(
            rx.foreach(
                InvoiceExtractState.fields,
                lambda f: rx.cond(
                    (f.section == section_key) & ~f.resolved,
                    _field_row(f),
                    rx.fragment(),
                ),
            ),
            spacing="0",
            width="100%",
        ),
        spacing="2",
        align="start",
        width="100%",
    )


def _resolved_checklist_row(f) -> rx.Component:
    """One row of the Resolved checklist: a tick, the field's human label,
    and its confirmed value as the summary. No edit controls inline — the
    value is already settled."""
    return rx.hstack(
        rx.icon("circle_check", size=15, color=t.Color.RULE.value),
        rx.text(f.label, style=t.TEXT["body"], font_weight="600", flex="1"),
        rx.text(
            rx.cond(f.effective_value != "", f.effective_value, "—"),
            style=t.TEXT["micro"],
            color=t.Color.TEXT_SECONDARY.value,
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="6px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _resolved_checklist() -> rx.Component:
    """The Resolved checklist — every resolved field, flat, below the open
    sections. Renders nothing when nothing is resolved yet."""
    return rx.cond(
        InvoiceExtractState.resolved_count > 0,
        c.card(
            rx.vstack(
                c.section_title(
                    "Resolved",
                    f"{InvoiceExtractState.resolved_count} field(s) confirmed — "
                    "these are settled and no longer need review.",
                ),
                rx.vstack(
                    rx.foreach(
                        InvoiceExtractState.fields,
                        lambda f: rx.cond(f.resolved, _resolved_checklist_row(f), rx.fragment()),
                    ),
                    spacing="0",
                    width="100%",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _preview() -> rx.Component:
    return rx.match(
        InvoiceExtractState.preview_kind,
        (
            "image",
            rx.image(
                src=InvoiceExtractState.preview_image_src,
                width="100%",
                border_radius="10px",
                alt=InvoiceExtractState.detail_filename,
            ),
        ),
        (
            "table",
            rx.el.div(
                rx.el.table(
                    rx.el.thead(
                        rx.el.tr(
                            rx.foreach(
                                InvoiceExtractState.preview_table_headers,
                                lambda h: rx.el.th(
                                    h,
                                    style={
                                        "text_align": "left",
                                        "font_size": "11px",
                                        "padding": "4px 8px",
                                        "border_bottom": f"1px solid {t.Color.BORDER.value}",
                                        "color": t.Color.TEXT_SECONDARY.value,
                                    },
                                ),
                            ),
                        ),
                    ),
                    rx.el.tbody(
                        rx.foreach(
                            InvoiceExtractState.preview_table_rows,
                            lambda row: rx.el.tr(
                                rx.foreach(
                                    row,
                                    lambda cell: rx.el.td(
                                        cell,
                                        style={
                                            "font_size": "12px",
                                            "padding": "4px 8px",
                                            "white_space": "nowrap",
                                            "color": t.Color.TEXT_PRIMARY.value,
                                        },
                                    ),
                                ),
                            ),
                        ),
                    ),
                    style={"border_collapse": "collapse", "width": "100%"},
                ),
                width="100%",
                overflow="auto",
                max_height="420px",
            ),
        ),
        (
            "text",
            rx.text_area(
                value=InvoiceExtractState.preview_text,
                read_only=True,
                height="360px",
                width="100%",
                font_family="monospace",
                font_size="12px",
            ),
        ),
        rx.text("Source preview unavailable for this format.", style=t.TEXT["micro"]),
    )


def _review_screen() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back",
            on_click=InvoiceExtractState.back_to_list,
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_SECONDARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="9px",
            size="2",
        ),
        c.card(
            rx.vstack(
                rx.text(InvoiceExtractState.detail_filename, style=t.TEXT["card_title"]),
                rx.text(
                    f"{InvoiceExtractState.detail_client} · {InvoiceExtractState.detail_path_label} · "
                    f"status: {InvoiceExtractState.detail_status_label}",
                    style=t.TEXT["micro"],
                ),
                rx.cond(
                    InvoiceExtractState.detail_ai_used,
                    rx.hstack(
                        c.confidence_badge(
                            "ai",
                            label=f"AI-extracted · {InvoiceExtractState.detail_ai_model}",
                        ),
                        rx.cond(
                            InvoiceExtractState.detail_ai_latency_ms > 0,
                            rx.text(
                                f"{InvoiceExtractState.detail_ai_latency_ms} ms",
                                style=t.TEXT["micro"],
                            ),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="center",
                    ),
                    rx.cond(
                        InvoiceExtractState.detail_ai_error != "",
                        c.inline_reason(
                            f"AI extraction unavailable — {InvoiceExtractState.detail_ai_error}. "
                            "Values below came from the offline parser."
                        ),
                        c.pill("Offline parser", variant="placeholder"),
                    ),
                ),
                rx.cond(
                    InvoiceExtractState.detail_duplicate,
                    rx.text(
                        "⚠ Possible duplicate — same invoice number + vendor GSTIN as an earlier upload.",
                        style=t.TEXT["micro"],
                    ),
                    rx.fragment(),
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            InvoiceExtractState.can_review,
            rx.vstack(
                c.card(
                    rx.vstack(
                        c.section_title("Source document"),
                        c.file_viewer_panel(
                            title_var=InvoiceExtractState.detail_filename,
                            kind_var=InvoiceExtractState.viewer_kind,
                            image_src_var=InvoiceExtractState.viewer_image_src,
                            page_var=InvoiceExtractState.viewer_page,
                            page_count_var=InvoiceExtractState.viewer_page_count,
                            table_headers_var=InvoiceExtractState.viewer_table_headers,
                            table_rows_var=InvoiceExtractState.viewer_table_rows,
                            text_var=InvoiceExtractState.viewer_text,
                            download_name_var=InvoiceExtractState.viewer_download_name,
                            download_href_var="data:" + InvoiceExtractState.viewer_download_mime + ";base64," + InvoiceExtractState.viewer_download_b64,
                            on_close=InvoiceExtractState.close_viewer,
                            on_page_prev=InvoiceExtractState.viewer_page_prev,
                            on_page_next=InvoiceExtractState.viewer_page_next,
                            zoom_var=InvoiceExtractState.viewer_zoom,
                            on_zoom_in=InvoiceExtractState.viewer_zoom_in,
                            on_zoom_out=InvoiceExtractState.viewer_zoom_out,
                            on_zoom_reset=InvoiceExtractState.viewer_zoom_reset,
                            highlight_active_var=InvoiceExtractState.highlight_active,
                            highlight_x_var=InvoiceExtractState.highlight_x,
                            highlight_y_var=InvoiceExtractState.highlight_y,
                            highlight_w_var=InvoiceExtractState.highlight_w,
                            highlight_h_var=InvoiceExtractState.highlight_h,
                            highlight_page_var=InvoiceExtractState.highlight_page,
                            highlight_row_index_var=InvoiceExtractState.highlight_row_index,
                            highlight_snippet_var=InvoiceExtractState.highlight_snippet,
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                c.card(
                    rx.vstack(
                        c.section_title(
                            "Extracted fields",
                            f"Confidence threshold: {InvoiceExtractState.detail_threshold}% — "
                            "fields below it (or absent) are flagged.",
                        ),
                        _field_section("Header", "header"),
                        _field_section("Line items", "line_items"),
                        _field_section("Totals block", "totals"),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                _resolved_checklist(),
                spacing="4",
                width="100%",
                align="start",
            ),
            c.info_banner(
                "You can upload invoices, but resolving flagged fields needs the review permission."
            ),
        ),
        rx.cond(
            InvoiceExtractState.detail_status == "exported",
            rx.vstack(
                c.accent_pill("Exported — immutable batch"),
                rx.text(
                    "This upload is part of an immutable export batch. Correcting it means "
                    "generating a new batch.",
                    style=t.TEXT["micro"],
                ),
                spacing="2",
                align="start",
            ),
            rx.vstack(
                rx.cond(
                    InvoiceExtractState.remaining_count > 0,
                    c.inline_reason(
                        f"{InvoiceExtractState.remaining_count} field(s) still need resolution "
                        "before this upload can be confirmed."
                    ),
                    rx.cond(
                        InvoiceExtractState.detail_status != "confirmed",
                        rx.button(
                            "Confirm upload",
                            on_click=InvoiceExtractState.confirm_upload,
                            size="3",
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        c.confidence_badge("rule", label="Confirmed"),
                    ),
                ),
                rx.hstack(
                    rx.dialog.root(
                        rx.dialog.trigger(
                            rx.button(
                                "Discard upload",
                                variant="soft",
                                background="#FDEBEA",
                                color=t.Color.DANGER.value,
                                border=f"1px solid {t.Color.DANGER.value}",
                                border_radius="9px",
                            ),
                        ),
                        rx.dialog.content(
                            rx.vstack(
                                rx.text("Discard this upload?", style=t.TEXT["card_title"]),
                                rx.text(
                                    "This permanently removes the upload and every extracted "
                                    "field. It cannot be undone.",
                                    style=t.TEXT["micro"],
                                ),
                                rx.dialog.close(
                                    rx.button(
                                        "Discard upload",
                                        on_click=InvoiceExtractState.discard_upload,
                                        background=t.Color.DANGER.value,
                                        color="#FFFFFF",
                                    ),
                                ),
                                spacing="3",
                                align="start",
                            ),
                        ),
                    ),
                    rx.dialog.root(
                        rx.dialog.trigger(
                            rx.button(
                                "Re-extract with AI",
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_PRIMARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="9px",
                            ),
                        ),
                        rx.dialog.content(
                            rx.vstack(
                                rx.text("Re-extract with AI?", style=t.TEXT["card_title"]),
                                rx.text(
                                    "This replaces every extracted value, including any manual "
                                    "resolutions already made on this upload. The upload returns "
                                    "to the review flow.",
                                    style=t.TEXT["micro"],
                                ),
                                rx.dialog.close(
                                    rx.button(
                                        "Re-extract",
                                        on_click=InvoiceExtractState.re_extract_upload,
                                        background=t.Color.ACCENT.value,
                                        color="#FFFFFF",
                                    ),
                                ),
                                spacing="3",
                                align="start",
                            ),
                        ),
                    ),
                    spacing="3",
                    align="center",
                ),
                spacing="3",
                align="start",
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _confirmed_row(row) -> rx.Component:
    return rx.hstack(
        rx.checkbox(
            checked=InvoiceExtractState.export_selected.contains(row.upload_id.to_string()),
            on_change=InvoiceExtractState.toggle_export_select(row.upload_id.to_string()),
        ),
        rx.vstack(
            rx.text(row.filename, style=t.TEXT["body"], font_weight="600"),
            rx.text(f"{row.path_label}", style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _batch_row(b) -> rx.Component:
    return rx.hstack(
        rx.text(f"#{b.batch_id}", style=t.TEXT["label"], font_weight="700"),
        rx.text(b.filename, style=t.TEXT["body"]),
        rx.text(b.generated_at, style=t.TEXT["micro"]),
        rx.text(f"{b.row_count} rows", style=t.TEXT["label"]),
        rx.button(
            "Download",
            on_click=InvoiceExtractState.download_batch(b.batch_id, b.filename, b.fmt),
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_PRIMARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="8px",
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _export_section() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                c.section_title(
                    "Generate purchase-register export",
                    "Only Confirmed uploads can be exported — the hard gate is enforced at the "
                    "data-pipeline level.",
                ),
                rx.vstack(
                    rx.text("Client", style=t.TEXT["label"]),
                    rx.select(
                        InvoiceExtractState.client_names,
                        value=InvoiceExtractState.export_client_name,
                        on_change=InvoiceExtractState.set_export_client_by_name,
                        width="240px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.cond(
                    InvoiceExtractState.confirmed_uploads.length() > 0,
                    rx.vstack(
                        rx.vstack(
                            rx.foreach(InvoiceExtractState.confirmed_uploads, _confirmed_row),
                            spacing="0",
                            width="100%",
                        ),
                        rx.hstack(
                            rx.button(
                                "xlsx",
                                on_click=InvoiceExtractState.set_export_fmt("xlsx"),
                                size="1",
                                variant="soft",
                                background=rx.cond(InvoiceExtractState.export_fmt == "xlsx", t.Color.ACCENT.value, "transparent"),
                                color=rx.cond(InvoiceExtractState.export_fmt == "xlsx", "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                            ),
                            rx.button(
                                "csv",
                                on_click=InvoiceExtractState.set_export_fmt("csv"),
                                size="1",
                                variant="soft",
                                background=rx.cond(InvoiceExtractState.export_fmt == "csv", t.Color.ACCENT.value, "transparent"),
                                color=rx.cond(InvoiceExtractState.export_fmt == "csv", "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                            ),
                            spacing="2",
                        ),
                        rx.cond(
                            InvoiceExtractState.can_export,
                            rx.button(
                                "Generate export",
                                on_click=InvoiceExtractState.generate_export,
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                            ),
                            c.inline_reason("Generating an export needs the export permission."),
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    rx.text(
                        "No Confirmed uploads for this client yet. Resolve every flagged field, "
                        "then confirm.",
                        style=t.TEXT["label"],
                    ),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        c.card(
            rx.vstack(
                c.section_title(
                    "Export history",
                    "Immutable by design — re-exporting produces a new batch, never an overwrite. "
                    "No edit action exists.",
                ),
                rx.cond(
                    InvoiceExtractState.batches.length() > 0,
                    rx.vstack(rx.foreach(InvoiceExtractState.batches, _batch_row), spacing="0", width="100%"),
                    c.empty_state("No export batches generated yet.", icon="archive"),
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


# ---------------------------------------------------------------------------
# Model assignment (admin)
# ---------------------------------------------------------------------------


def _touchpoint_card(tp) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text(tp.label, style=t.TEXT["card_title"]),
            rx.text(f"touchpoint key: {tp.touchpoint_key}", style=t.TEXT["micro"]),
            rx.hstack(
                rx.vstack(
                    rx.text(f"Primary — {tp.primary_model}", style=t.TEXT["body"], font_weight="600"),
                    rx.text(tp.primary_provider, style=t.TEXT["micro"]),
                    spacing="0",
                    align="start",
                ),
                rx.vstack(
                    rx.text(f"Fallback — {tp.fallback_model}", style=t.TEXT["body"], font_weight="600"),
                    spacing="0",
                    align="start",
                ),
                spacing="5",
                wrap="wrap",
            ),
            rx.hstack(
                c.confidence_badge("rule", label="Active"),
                rx.button(
                    "Test call",
                    on_click=InvoiceExtractState.test_touchpoint(tp.touchpoint_key),
                    size="1",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_PRIMARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                spacing="3",
                align="center",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _model_admin_section() -> rx.Component:
    return rx.vstack(
        rx.text(
            "Two touchpoint rows added by this build. Both are ACTIVE — this module ships "
            "functional AI extraction from day one.",
            style=t.TEXT["micro"],
        ),
        rx.vstack(rx.foreach(InvoiceExtractState.touchpoints, _touchpoint_card), spacing="3", width="100%"),
        c.card(
            rx.vstack(
                c.section_title(
                    "Model assignments are read-only here",
                    "Primary and fallback models are edited in one place only — Setup → AI Models. "
                    "Changing them there takes effect here immediately.",
                ),
                rx.button(
                    rx.hstack(
                        rx.icon("cpu", size=15),
                        rx.text("Edit in AI Model Config"),
                        spacing="2",
                        align="center",
                    ),
                    on_click=rx.redirect("/ai-models"),
                    variant="soft",
                    background="transparent",
                    color=t.Color.ACCENT.value,
                    border=f"1px solid {t.Color.ACCENT.value}",
                    border_radius="9px",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            InvoiceExtractState.is_threshold_admin,
            c.card(
                rx.vstack(
                    c.section_title(
                        "Confidence threshold",
                        "Platform-wide auto-accept threshold. Fields below it (or absent) are flagged.",
                    ),
                    rx.hstack(
                        rx.vstack(
                            rx.text("Auto-accept threshold (%)", style=t.TEXT["label"]),
                            rx.input(
                                value=InvoiceExtractState.threshold.to_string(),
                                on_change=InvoiceExtractState.set_threshold,
                                width="120px",
                                type="number",
                            ),
                            spacing="1",
                            align="start",
                        ),
                        rx.button(
                            "Save threshold",
                            on_click=InvoiceExtractState.save_threshold,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        spacing="3",
                        align="end",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        c.divider(),
        rx.text("Deferred in this build", style=t.TEXT["label"], font_weight="700"),
        rx.text(
            "Auto-splitting a multi-invoice Excel/Word file into separate records — known "
            "limitation, not built.",
            style=t.TEXT["micro"],
        ),
        rx.hstack(c.placeholder_badge("Module F3"), spacing="2"),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def invoice_extract_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Invoice Extraction & Digitalization",
                "Upload invoices in any of four formats — AI extracts the canonical field set "
                "with a per-field confidence score; anything under the threshold is gated into review.",
            ),
            rx.cond(InvoiceExtractState.flash != "", c.info_banner(InvoiceExtractState.flash), rx.fragment()),
            rx.cond(InvoiceExtractState.error != "", c.inline_reason(InvoiceExtractState.error), rx.fragment()),
            rx.hstack(
                *[_hub_button(label) for label in _hub()],
                spacing="2",
                wrap="wrap",
                padding="4px",
                background=t.Color.SURFACE.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
            ),
            rx.cond(
                InvoiceExtractState.selected_upload_id != 0,
                _review_screen(),
                rx.match(
                    InvoiceExtractState.section,
                    ("Review Queue", _review_queue_section()),
                    ("Export", _export_section()),
                    ("Model Assignment (Admin)", _model_admin_section()),
                    _upload_section(),
                ),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )