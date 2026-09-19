"""F3-AI — Smart Document Ingestion.

Upload list, mapping-review screen (raw-file context + per-field confidence),
mapping profiles, confidence thresholds. Uses only Foundation components.

Upload staging and per-file progress reuse the SHARED FilePreviewChip and
UploadProgressState components (``c.file_preview_chip`` / ``c.upload_progress_row``)
that F3 (Document Vault) and F3-B (Invoice Extraction) already use — this
module does not fork them. It is spreadsheet/CSV only (OCR is explicitly out
of scope per its own copy), so no image/PDF preview rendering is added here.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.ingestion_ai_state import (
    LEAVE_UNMAPPED,
    STATUS_BLOCKED,
    STATUS_CONFIRMED,
    STATUS_EMPTY,
    STATUS_NEEDS_CONFIRM,
    STATUS_UNRECOGNIZED,
    IngestionAiState,
)
from setu.views import shell

_ACCEPT = {
    "text/csv": [".csv"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
}

_LEAVE_UNMAPPED = LEAVE_UNMAPPED


def _section_button(label: str) -> rx.Component:
    active = IngestionAiState.section == label
    return rx.button(
        label,
        on_click=IngestionAiState.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _back_button(label: str, on_click) -> rx.Component:
    """The standard, clearly visible back action — identical shape to
    Document Vault's "← Back to vault"."""
    return rx.button(
        label,
        on_click=on_click,
        variant="soft",
        background="transparent",
        color=t.Color.TEXT_SECONDARY.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="9px",
        size="2",
        _hover={"background": "#EEF2F8", "color": t.Color.TEXT_PRIMARY.value},
    )


# ---------------------------------------------------------------------------
# Upload + list
# ---------------------------------------------------------------------------


def _status_cell(u) -> rx.Component:
    """The three real failure modes are DIFFERENT problems and must look
    different, not merely read differently. Each branch builds the WHOLE
    pill with a literal variant — a matched Var passed as ``variant=``
    fails at compile time ("Cannot access a primitive map with a Var")."""
    return rx.match(
        u.status,
        (
            STATUS_NEEDS_CONFIRM,
            c.pill("Ready for review", variant="placeholder"),
        ),
        (
            STATUS_UNRECOGNIZED,
            c.pill("Unrecognized shape — needs manual review", variant="ai"),
        ),
        (
            STATUS_BLOCKED,
            c.pill(f"Blocked — {u.blocked_count} field(s) need mapping", variant="danger"),
        ),
        (
            STATUS_EMPTY,
            c.pill("Empty — 0 row(s) read", variant="danger"),
        ),
        (
            STATUS_CONFIRMED,
            c.confidence_badge("rule", label="Confirmed"),
        ),
        rx.text(u.status_label, style=t.TEXT["micro"]),
    )


def _upload_row(u) -> rx.Component:
    return rx.hstack(
        rx.checkbox(
            checked=IngestionAiState.selected_upload_ids.contains(u.upload_id),
            on_change=IngestionAiState.toggle_upload_select(u.upload_id),
            disabled=~u.bulk_eligible,
        ),
        rx.vstack(
            rx.text(u.filename, style=t.TEXT["body"], font_weight="600"),
            rx.text(u.source_label, style=t.TEXT["micro"]),
            spacing="0",
            align="start",
            flex="3",
            min_width="0",
        ),
        rx.text(u.client_label, style=t.TEXT["body"], flex="2"),
        rx.text(rx.cond(u.period != "", u.period, "—"), style=t.TEXT["body"], flex="1"),
        rx.text(
            rx.cond(
                u.counts_known,
                f"{u.row_count_in} read · {u.row_count_out} extracted",
                "counts unavailable",
            ),
            style=t.TEXT["micro"],
            flex="2",
        ),
        rx.box(_status_cell(u), flex="3"),
        rx.cond(
            IngestionAiState.can_review,
            rx.button(
                "Open",
                on_click=IngestionAiState.open_upload(u.upload_id),
                size="1",
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_PRIMARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="8px",
            ),
            rx.text("Uploaded, pending mapping review", style=t.TEXT["micro"]),
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _upload_zone() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Upload a raw source file", style=t.TEXT["card_title"]),
            rx.text(
                "OCR of scanned/image-based documents is out of scope — spreadsheets/CSV only.",
                style=t.TEXT["micro"],
            ),
            rx.hstack(
                rx.vstack(
                    rx.text("Source type", style=t.TEXT["label"]),
                    rx.select(
                        IngestionAiState.source_type_options,
                        value=IngestionAiState.source_type,
                        on_change=IngestionAiState.set_source_type,
                        width="240px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Period (optional)", style=t.TEXT["label"]),
                    rx.input(
                        value=IngestionAiState.period,
                        on_change=IngestionAiState.set_period,
                        placeholder="e.g. 2026-08",
                        width="180px",
                    ),
                    spacing="1",
                    align="start",
                ),
                spacing="4",
                align="end",
            ),
            rx.upload(
                rx.vstack(
                    rx.icon("upload", size=22, color=t.Color.NEUTRAL.value),
                    rx.text("Drag a spreadsheet/CSV here or click to browse", style=t.TEXT["label"]),
                    spacing="2",
                    align="center",
                    padding="24px",
                ),
                id="f3ai_upload",
                accept=_ACCEPT,
                multiple=True,
                on_drop=IngestionAiState.stage,
                border=f"1px dashed {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
                background=t.Color.SURFACE.value,
            ),
            # FilePreviewChip — staged files before upload (shared component).
            rx.cond(
                IngestionAiState.pending.length() > 0,
                rx.vstack(
                    rx.text("Ready to upload", style=t.TEXT["label"]),
                    rx.vstack(
                        rx.foreach(
                            IngestionAiState.pending,
                            lambda pf: c.file_preview_chip(
                                pf,
                                on_remove=IngestionAiState.remove_pending(pf.key),
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
                IngestionAiState.upload_progress.length() > 0,
                rx.vstack(
                    rx.foreach(
                        IngestionAiState.upload_progress,
                        lambda r: c.upload_progress_row(r),
                    ),
                    spacing="0",
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.hstack(
                rx.button(
                    "Upload & infer mapping",
                    on_click=IngestionAiState.upload_pending,
                    disabled=IngestionAiState.pending.length() == 0,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Clear",
                    on_click=IngestionAiState.clear_pending,
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                ),
                spacing="2",
            ),
            rx.cond(
                IngestionAiState.upload_error != "",
                c.inline_reason(IngestionAiState.upload_error),
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _list_filters() -> rx.Component:
    return rx.hstack(
        rx.select(
            IngestionAiState.source_filter_options,
            value=IngestionAiState.filter_source,
            on_change=IngestionAiState.set_filter_source,
            width="190px",
        ),
        rx.select(
            IngestionAiState.status_filter_options,
            value=IngestionAiState.filter_status,
            on_change=IngestionAiState.set_filter_status,
            width="190px",
        ),
        rx.select(
            IngestionAiState.sort_options,
            value=IngestionAiState.sort_key,
            on_change=IngestionAiState.set_sort_key,
            width="170px",
        ),
        rx.input(
            value=IngestionAiState.search,
            on_change=IngestionAiState.set_search,
            placeholder="Search filename / client / period",
            flex="1",
        ),
        spacing="3",
        width="100%",
        wrap="wrap",
    )


def _bulk_bar() -> rx.Component:
    return rx.cond(
        IngestionAiState.selected_count > 0,
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.text(
                        f"{IngestionAiState.selected_count} selected",
                        style=t.TEXT["body"],
                        font_weight="600",
                    ),
                    rx.text(
                        f"{IngestionAiState.bulk_eligible_count} upload(s) in this list are fully confident and eligible.",
                        style=t.TEXT["micro"],
                    ),
                    spacing="3",
                    align="center",
                ),
                rx.hstack(
                    rx.hstack(
                        rx.checkbox(
                            checked=IngestionAiState.bulk_trust,
                            on_change=IngestionAiState.set_bulk_trust,
                        ),
                        rx.text(
                            "Also trust these mappings for future reuse",
                            style=t.TEXT["label"],
                        ),
                        spacing="2",
                        align="center",
                    ),
                    rx.button(
                        "Confirm selected",
                        on_click=IngestionAiState.open_bulk_confirm,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    rx.button(
                        "Clear selection",
                        on_click=IngestionAiState.clear_selection,
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                    ),
                    spacing="3",
                    align="center",
                    wrap="wrap",
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _bulk_confirm_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Confirm selected mappings", style=t.TEXT["card_title"]),
                rx.text(
                    f"{IngestionAiState.selected_count} upload(s) will be confirmed. Only uploads whose "
                    "every field is already at or above the auto-apply threshold, and which read at least "
                    "one row, are confirmed — the rest are skipped.",
                    style=t.TEXT["body"],
                ),
                rx.cond(
                    IngestionAiState.bulk_trust,
                    c.warning_banner(
                        "Trust is on: a reusable mapping profile will be created for this client and "
                        "source type. Future uploads of this shape will reuse it automatically, without "
                        "asking you to review the mapping again."
                    ),
                    rx.fragment(),
                ),
                rx.hstack(
                    rx.dialog.close(
                        rx.button(
                            "Cancel",
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                    ),
                    rx.dialog.close(
                        rx.button(
                            "Confirm",
                            on_click=IngestionAiState.bulk_confirm,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
            ),
        ),
        open=IngestionAiState.bulk_confirm_open,
        on_open_change=IngestionAiState.close_bulk_confirm,
    )


def _upload_and_list() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text("Client:", style=t.TEXT["micro"]),
            rx.select(
                IngestionAiState.client_names,
                value=IngestionAiState.client_name,
                on_change=IngestionAiState.set_client_by_name,
                placeholder="Select a client",
                width="320px",
            ),
            spacing="2",
            align="center",
        ),
        _upload_zone(),
        _bulk_bar(),
        c.card(
            rx.vstack(
                rx.text("Uploads", style=t.TEXT["card_title"]),
                rx.text(
                    "Bulk confirm is available only for uploads whose every field is already at or "
                    "above the auto-apply threshold and which read at least one row — anything else "
                    "needs individual review.",
                    style=t.TEXT["micro"],
                ),
                _list_filters(),
                rx.cond(
                    IngestionAiState.uploads.length() > 0,
                    rx.vstack(
                        rx.hstack(
                            rx.box(width="20px"),
                            rx.text("File", style=t.TEXT["label"], flex="3"),
                            rx.text("Client", style=t.TEXT["label"], flex="2"),
                            rx.text("Period", style=t.TEXT["label"], flex="1"),
                            rx.text("Rows", style=t.TEXT["label"], flex="2"),
                            rx.text("Status", style=t.TEXT["label"], flex="3"),
                            rx.box(width="60px"),
                            width="100%",
                            padding="0 0 6px 0",
                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                        ),
                        rx.foreach(IngestionAiState.uploads, _upload_row),
                        spacing="0",
                        width="100%",
                    ),
                    c.empty_state("No uploads match this view.", icon="file-spreadsheet"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        _bulk_confirm_dialog(),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Mapping review — the real, editable review table
# ---------------------------------------------------------------------------


def _confidence_cell(row) -> rx.Component:
    return rx.match(
        row.status,
        ("auto", c.confidence_badge("ai", pct=row.confidence)),
        ("flagged", c.confidence_badge("ai", pct=row.confidence, label=f"AI — {row.confidence}% (review)")),
        ("unavailable", c.pill("Unmapped — needs input", variant="danger")),
        ("unavailable_review", c.pill("Below threshold — needs input", variant="danger")),
        ("unmapped", c.pill("Not mapped to any field", variant="placeholder")),
        c.pill("Needs input", variant="placeholder"),
    )


def _column_row(row) -> rx.Component:
    """One row of the mapping table: source column → canonical field →
    confidence, with the canonical field EDITABLE via a dropdown of valid
    canonical fields."""
    return rx.hstack(
        rx.vstack(
            rx.cond(
                row.is_unmapped_field,
                rx.text("(no source column)", style=t.TEXT["micro"], font_style="italic"),
                rx.text(row.raw_column, font_size="13px", font_weight="600"),
            ),
            rx.cond(
                row.reason != "",
                rx.text(row.reason, style=t.TEXT["micro"]),
                rx.fragment(),
            ),
            spacing="0",
            align="start",
            flex="3",
            min_width="0",
        ),
        rx.cond(
            row.is_unmapped_field,
            rx.text(row.mapped_field, style=t.TEXT["body"], font_weight="600", flex="3"),
            rx.select(
                IngestionAiState.canonical_field_select_options,
                value=rx.cond(row.mapped_field != "", row.mapped_field, _LEAVE_UNMAPPED),
                on_change=lambda v: IngestionAiState.set_column_mapping(row.raw_column, v),
                width="100%",
                flex="3",
            ),
        ),
        rx.box(_confidence_cell(row), flex="2"),
        rx.cond(
            row.required,
            rx.text("required", style=t.TEXT["micro"], color=t.Color.DANGER.value, flex="1"),
            rx.text("optional", style=t.TEXT["micro"], flex="1"),
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _raw_preview() -> rx.Component:
    """First several rows of the ACTUAL sheet, so the mapping can be
    sanity-checked against real values rather than headers alone."""
    return c.card(
        rx.vstack(
            rx.text("Raw file — first rows", style=t.TEXT["card_title"]),
            rx.text(
                f"{IngestionAiState.review_filename} · {IngestionAiState.review_source_label}",
                style=t.TEXT["micro"],
            ),
            rx.cond(
                IngestionAiState.header_row_note != "",
                rx.text(IngestionAiState.header_row_note, style=t.TEXT["micro"]),
                rx.fragment(),
            ),
            rx.text(IngestionAiState.row_count_note, style=t.TEXT["micro"]),
            rx.cond(
                IngestionAiState.preview_rows.length() > 0,
                rx.el.div(
                    rx.el.table(
                        rx.el.thead(
                            rx.el.tr(
                                rx.foreach(
                                    IngestionAiState.preview_headers,
                                    lambda h: rx.el.th(
                                        h,
                                        style={
                                            "text_align": "left",
                                            "font_size": "11px",
                                            "padding": "4px 8px",
                                            "border_bottom": f"1px solid {t.Color.BORDER.value}",
                                            "color": t.Color.TEXT_SECONDARY.value,
                                            "white_space": "nowrap",
                                        },
                                    ),
                                ),
                            ),
                        ),
                        rx.el.tbody(
                            rx.foreach(
                                IngestionAiState.preview_rows,
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
                    max_height="320px",
                ),
                rx.text(
                    rx.cond(
                        IngestionAiState.preview_note != "",
                        IngestionAiState.preview_note,
                        "No data rows to preview.",
                    ),
                    style=t.TEXT["micro"],
                ),
            ),
            rx.cond(
                IngestionAiState.review_c5_used,
                c.accent_pill("C5 instruction context applied"),
                rx.fragment(),
            ),
            rx.vstack(
                rx.foreach(IngestionAiState.notes, lambda n: rx.text(f"ℹ {n}", style=t.TEXT["micro"])),
                spacing="1",
                align="start",
            ),
            rx.vstack(
                rx.foreach(IngestionAiState.warnings, lambda w: rx.text(f"⚠ {w}", style=t.TEXT["micro"])),
                spacing="1",
                align="start",
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _mapping_table() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Column → canonical field mapping", style=t.TEXT["card_title"]),
            rx.text(
                f"{IngestionAiState.mapped_column_count} column(s) mapped · "
                f"{IngestionAiState.unmapped_column_count} canonical field(s) need your input. "
                "Every row is editable — correct any mapping before confirming.",
                style=t.TEXT["micro"],
            ),
            rx.cond(
                IngestionAiState.mapping_error != "",
                c.inline_reason(IngestionAiState.mapping_error),
                rx.fragment(),
            ),
            rx.cond(
                IngestionAiState.columns.length() > 0,
                rx.vstack(
                    rx.hstack(
                        rx.text("Source column", style=t.TEXT["label"], flex="3"),
                        rx.text("Canonical field", style=t.TEXT["label"], flex="3"),
                        rx.text("Confidence", style=t.TEXT["label"], flex="2"),
                        rx.text("", style=t.TEXT["label"], flex="1"),
                        width="100%",
                        padding="0 0 6px 0",
                        border_bottom=f"1px solid {t.Color.BORDER.value}",
                    ),
                    rx.foreach(IngestionAiState.columns, _column_row),
                    spacing="0",
                    width="100%",
                ),
                c.empty_state("No columns detected in this file.", icon="table"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _confirm_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Confirm this mapping", style=t.TEXT["card_title"]),
                rx.text(
                    "Confirming clears the hard gate and lets this file's data proceed to "
                    "reconciliation.",
                    style=t.TEXT["body"],
                ),
                rx.cond(
                    IngestionAiState.trust_reuse,
                    c.warning_banner(
                        "Trust is on: a reusable mapping profile will be created for this client and "
                        "source type. Future uploads of this shape will reuse it automatically, without "
                        "asking you to review the mapping again. You can revoke it later from "
                        "Mapping Profiles."
                    ),
                    rx.fragment(),
                ),
                rx.cond(
                    IngestionAiState.review_is_empty,
                    rx.vstack(
                        c.warning_banner(
                            "This file read 0 rows. There is no data for this mapping to apply to."
                        ),
                        rx.hstack(
                            rx.checkbox(
                                checked=IngestionAiState.allow_empty_ack,
                                on_change=IngestionAiState.set_allow_empty_ack,
                            ),
                            rx.text(
                                "This file is deliberately empty — confirm it anyway",
                                style=t.TEXT["label"],
                            ),
                            spacing="2",
                            align="center",
                        ),
                        spacing="2",
                        align="start",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                rx.hstack(
                    rx.dialog.close(
                        rx.button(
                            "Cancel",
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                    ),
                    rx.dialog.close(
                        rx.button(
                            "Confirm mapping",
                            on_click=IngestionAiState.confirm_mapping,
                            disabled=IngestionAiState.review_is_empty & ~IngestionAiState.allow_empty_ack,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
            ),
        ),
        open=IngestionAiState.confirm_open,
        on_open_change=IngestionAiState.close_confirm,
    )


def _mapping_review() -> rx.Component:
    return rx.vstack(
        _back_button("← Back to uploads", IngestionAiState.back_to_uploads),
        rx.cond(
            IngestionAiState.classification_error != "",
            c.inline_reason(IngestionAiState.classification_error),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.warning_banner != "",
            c.warning_banner(IngestionAiState.warning_banner),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.blocked_banner != "",
            c.inline_reason(IngestionAiState.blocked_banner),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.review_status == STATUS_CONFIRMED,
            c.confidence_badge("rule", label="Confirmed"),
            rx.fragment(),
        ),
        _raw_preview(),
        _mapping_table(),
        rx.cond(
            IngestionAiState.can_review,
            c.card(
                rx.vstack(
                    rx.hstack(
                        rx.checkbox(
                            checked=IngestionAiState.trust_reuse,
                            on_change=IngestionAiState.set_trust_reuse,
                        ),
                        rx.text(
                            "Trust this mapping for future reuse (this client + source type)",
                            style=t.TEXT["label"],
                        ),
                        spacing="2",
                        align="center",
                    ),
                    rx.text(
                        "Trusting creates a reusable profile that affects future uploads — "
                        "you'll be asked to confirm that before it is saved.",
                        style=t.TEXT["micro"],
                    ),
                    rx.button(
                        "Confirm mapping",
                        on_click=IngestionAiState.open_confirm,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        _confirm_dialog(),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Profiles + thresholds
# ---------------------------------------------------------------------------


def _view_mapping_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text(IngestionAiState.view_mapping_title, style=t.TEXT["card_title"]),
                rx.text(
                    "The actual column-to-field rules this mapping applies.",
                    style=t.TEXT["micro"],
                ),
                rx.cond(
                    IngestionAiState.view_mapping_rules.length() > 0,
                    rx.vstack(
                        rx.foreach(
                            IngestionAiState.view_mapping_rules,
                            lambda r: rx.text(r, style=t.TEXT["body"], font_family="monospace"),
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    c.empty_state("No rules recorded for this mapping.", icon="table"),
                ),
                rx.dialog.close(
                    rx.button(
                        "Close",
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                    ),
                ),
                spacing="3",
                align="start",
            ),
        ),
        open=IngestionAiState.view_mapping_open,
        on_open_change=IngestionAiState.close_view_mapping,
    )


def _revoke_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Revoke this mapping profile?", style=t.TEXT["card_title"]),
                rx.text(
                    "Revoking stops the mapping being reused automatically. The next file of this "
                    "shape will go back through classification and field mapping, and you will be "
                    "asked to review it again.",
                    style=t.TEXT["body"],
                ),
                rx.hstack(
                    rx.dialog.close(
                        rx.button(
                            "Cancel",
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                    ),
                    rx.dialog.close(
                        rx.button(
                            "Revoke trust",
                            on_click=IngestionAiState.confirm_revoke,
                            background=t.Color.DANGER.value,
                            color="#FFFFFF",
                        ),
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
            ),
        ),
        open=IngestionAiState.revoke_open,
        on_open_change=IngestionAiState.close_revoke,
    )


def _profile_row(p) -> rx.Component:
    return rx.hstack(
        rx.text(p.client_label, style=t.TEXT["body"], flex="2"),
        rx.text(p.source_label, style=t.TEXT["body"], flex="2"),
        rx.box(
            rx.cond(p.trusted, c.confidence_badge("rule", label="Trusted"), c.accent_pill("Not trusted")),
            flex="2",
        ),
        rx.vstack(
            rx.text(f"reused {p.usage_count} time(s)", style=t.TEXT["micro"]),
            rx.text(
                rx.cond(p.last_used_at != "", f"last used {p.last_used_at}", "never used"),
                style=t.TEXT["micro"],
            ),
            spacing="0",
            align="start",
            flex="2",
        ),
        rx.hstack(
            rx.button(
                "View mapping",
                on_click=IngestionAiState.open_view_mapping(
                    f"{p.client_label} · {p.source_label}", p.rules
                ),
                size="1",
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_PRIMARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="8px",
            ),
            rx.cond(
                p.trusted,
                rx.button(
                    "Revoke",
                    on_click=IngestionAiState.open_revoke(p.profile_id),
                    size="1",
                    variant="soft",
                    background="transparent",
                    color=t.Color.DANGER.value,
                    border=f"1px solid {t.Color.DANGER.value}",
                    border_radius="8px",
                ),
                rx.fragment(),
            ),
            spacing="2",
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _shape_row(s) -> rx.Component:
    return rx.hstack(
        rx.text(s.client_label, style=t.TEXT["body"], flex="2"),
        rx.text(s.source_label, style=t.TEXT["body"], flex="2"),
        rx.box(
            rx.cond(s.trusted, c.confidence_badge("rule", label="Trusted"), c.accent_pill("Not trusted")),
            flex="2",
        ),
        rx.vstack(
            rx.text(f"reused {s.times_used} time(s)", style=t.TEXT["micro"]),
            rx.text(
                rx.cond(s.last_used_at != "", f"last used {s.last_used_at}", "never used"),
                style=t.TEXT["micro"],
            ),
            spacing="0",
            align="start",
            flex="2",
        ),
        rx.button(
            "View mapping",
            on_click=IngestionAiState.open_view_mapping(
                f"{s.client_label} · {s.source_label} · shape {s.header_signature[:8]}", s.rules
            ),
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
    )


def _profiles_admin() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                rx.text("Mapping Profiles", style=t.TEXT["card_title"]),
                rx.text(
                    "Per client + source type: trust status, how often the mapping has actually been "
                    "reused, and revoke. Inspect the rules before deciding.",
                    style=t.TEXT["label"],
                ),
                rx.hstack(
                    rx.select(
                        IngestionAiState.source_filter_options,
                        value=IngestionAiState.profile_source_filter,
                        on_change=IngestionAiState.set_profile_source_filter,
                        width="200px",
                    ),
                    rx.input(
                        value=IngestionAiState.profile_search,
                        on_change=IngestionAiState.set_profile_search,
                        placeholder="Search client or source type",
                        flex="1",
                    ),
                    spacing="3",
                    width="100%",
                ),
                rx.cond(
                    IngestionAiState.filtered_profiles.length() > 0,
                    rx.vstack(rx.foreach(IngestionAiState.filtered_profiles, _profile_row), spacing="0", width="100%"),
                    c.empty_state("No mapping profiles match this view.", icon="table"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        c.card(
            rx.vstack(
                rx.text("Learned shapes", style=t.TEXT["card_title"]),
                rx.text(
                    "The mappings actually reused at runtime, keyed by the file's exact header "
                    "signature. A profile above can cover several shapes.",
                    style=t.TEXT["label"],
                ),
                rx.cond(
                    IngestionAiState.filtered_shapes.length() > 0,
                    rx.vstack(rx.foreach(IngestionAiState.filtered_shapes, _shape_row), spacing="0", width="100%"),
                    c.empty_state("No learned shapes match this view.", icon="table"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        _view_mapping_dialog(),
        _revoke_dialog(),
        spacing="4",
        width="100%",
        align="start",
    )


def _thresholds_admin() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Confidence Thresholds", style=t.TEXT["card_title"]),
            rx.text(
                "Admin-configurable defaults. ≥ auto-apply → applied silently; between manual and auto-apply → flagged but suggested; below manual → blank/manual.",
                style=t.TEXT["label"],
            ),
            rx.hstack(
                rx.vstack(
                    rx.text("Auto-apply threshold (%)", style=t.TEXT["label"]),
                    rx.input(
                        value=IngestionAiState.auto_apply.to_string(),
                        on_change=IngestionAiState.set_auto_apply_str,
                        width="140px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Manual threshold (%)", style=t.TEXT["label"]),
                    rx.input(
                        value=IngestionAiState.manual.to_string(),
                        on_change=IngestionAiState.set_manual_str,
                        width="140px",
                    ),
                    spacing="1",
                    align="start",
                ),
                spacing="4",
                align="end",
            ),
            rx.button(
                "Save thresholds",
                on_click=IngestionAiState.save_thresholds,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def ingestion_ai_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Smart Document Ingestion",
                "Upload a raw export — AI infers the column mapping, you confirm it.",
            ),
            rx.cond(IngestionAiState.flash != "", c.info_banner(IngestionAiState.flash), rx.fragment()),
            rx.cond(IngestionAiState.error != "", c.inline_reason(IngestionAiState.error), rx.fragment()),
            rx.cond(
                IngestionAiState.selected_upload_id != 0,
                _mapping_review(),
                rx.vstack(
                    rx.hstack(
                        _section_button("Upload & Uploads List"),
                        rx.cond(IngestionAiState.can_manage_profiles, _section_button("Mapping Profiles (Admin)"), rx.fragment()),
                        rx.cond(IngestionAiState.can_manage_thresholds, _section_button("Confidence Thresholds (Admin)"), rx.fragment()),
                        spacing="2",
                        wrap="wrap",
                        padding="4px",
                        background=t.Color.SURFACE.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="12px",
                        width="100%",
                    ),
                    rx.match(
                        IngestionAiState.section,
                        ("Mapping Profiles (Admin)", _profiles_admin()),
                        ("Confidence Thresholds (Admin)", _thresholds_admin()),
                        _upload_and_list(),
                    ),
                    spacing="4",
                    width="100%",
                    align="start",
                ),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )