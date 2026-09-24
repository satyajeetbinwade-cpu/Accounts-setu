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
        rx.cond(
            u.is_unfiled,
            c.pill("Unfiled", variant="danger"),
            rx.text(rx.cond(u.period != "", u.period, "—"), style=t.TEXT["body"]),
        ),
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
        rx.button(
            rx.icon("refresh-cw", size=14),
            "Re-run",
            on_click=IngestionAiState.open_rerun(u.upload_id, u.filename),
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_SECONDARY.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="8px",
        ),
        rx.cond(
            u.is_unfiled,
            rx.button(
                rx.icon("folder-input", size=14),
                "File to period",
                on_click=IngestionAiState.open_file_period(u.upload_id, u.filename),
                size="1",
                variant="soft",
                background="transparent",
                color=t.Color.DANGER.value,
                border=f"1px solid {t.Color.DANGER.value}",
                border_radius="8px",
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _rerun_dialog() -> rx.Component:
    """Per-file confirm dialog for a model re-run. A re-run is a live model
    call (~15-25s) and discards the cached mapping, so it is never one-click."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Re-run this file through the model?", style=t.TEXT["card_title"]),
                rx.text(IngestionAiState.rerun_target_name, style=t.TEXT["body"], font_weight="600"),
                rx.text(
                    "This discards the cached mapping for this file and asks the model to map "
                    "its columns again. It takes roughly 15–25 seconds and may produce a "
                    "different mapping than the one you have now.",
                    style=t.TEXT["micro"],
                ),
                c.ai_progress(
                    rx.cond(
                        IngestionAiState.busy_label != "",
                        IngestionAiState.busy_label,
                        "Asking the model to map this file…",
                    ),
                    visible=IngestionAiState.rerun_busy,
                ),
                rx.hstack(
                    rx.button(
                        "Re-run through model",
                        on_click=IngestionAiState.confirm_rerun,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                        disabled=IngestionAiState.rerun_busy,
                    ),
                    rx.button(
                        "Cancel",
                        on_click=IngestionAiState.close_rerun,
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                        disabled=IngestionAiState.rerun_busy,
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            max_width="520px",
        ),
        open=IngestionAiState.rerun_open,
        on_open_change=IngestionAiState.close_rerun,
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
                    rx.select.root(
                        rx.select.trigger(width="240px", placeholder="Choose a source type"),
                        rx.select.content(
                            rx.select.group(
                                rx.foreach(
                                    IngestionAiState.source_type_choices,
                                    lambda option: rx.select.item(option.label, value=option.key),
                                ),
                            ),
                        ),
                        value=IngestionAiState.source_type,
                        on_change=IngestionAiState.set_source_type,
                    ),
                    rx.text(IngestionAiState.source_type_hint, style=t.TEXT["micro"]),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Period", style=t.TEXT["label"]),
                    rx.input(
                        value=IngestionAiState.period,
                        on_change=IngestionAiState.set_period,
                        placeholder="YYYY-MM, e.g. 2026-08",
                        width="180px",
                    ),
                    rx.cond(
                        IngestionAiState.period_inferred_source != "",
                        rx.text(
                            "Defaulted from " + IngestionAiState.period_inferred_source
                            + " — check it before uploading.",
                            style=t.TEXT["micro"],
                        ),
                        rx.text(
                            "Required — the file is filed under this period and must match "
                            "the period you reconcile against.",
                            style=t.TEXT["micro"],
                        ),
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


def _file_period_dialog() -> rx.Component:
    """Re-file an upload that was filed without a period.

    Such a file sits in `data/<client>/-/<source_type>/`, which no period
    picker can select — so it exists but can never be reconciled. This
    dialog moves it to a real period.
    """
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("File this upload to a period", style=t.TEXT["card_title"]),
                rx.text(IngestionAiState.file_period_filename, style=t.TEXT["body"], font_weight="600"),
                c.warning_banner(
                    "This file was filed without a period, so it cannot be picked for "
                    "reconciliation. Filing it to a period moves it into the folder the "
                    "engine reads."
                ),
                rx.cond(
                    IngestionAiState.file_period_source != "",
                    rx.text(
                        "Suggested from " + IngestionAiState.file_period_source
                        + " — check it before filing.",
                        style=t.TEXT["micro"],
                    ),
                    rx.text(
                        "No period could be derived from this file — enter the period it belongs to.",
                        style=t.TEXT["micro"],
                    ),
                ),
                rx.vstack(
                    rx.text("Period", style=t.TEXT["label"]),
                    rx.input(
                        value=IngestionAiState.file_period_value,
                        on_change=IngestionAiState.set_file_period_value,
                        placeholder="YYYY-MM, e.g. 2026-08",
                        width="200px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.cond(
                    IngestionAiState.file_period_error != "",
                    c.inline_reason(IngestionAiState.file_period_error),
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
                    rx.button(
                        "File to period",
                        on_click=IngestionAiState.confirm_file_period,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
            ),
        ),
        open=IngestionAiState.file_period_open,
        on_open_change=IngestionAiState.close_file_period,
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
                            rx.box(width="70px"),
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
        _file_period_dialog(),
        _rerun_dialog(),
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
        # rule-sourced (deterministic parser / trusted profile): solid badge,
        # no percentage — structurally distinct from the AI tinted form.
        ("rule", c.confidence_badge("rule", label="Rule-based")),
        ("auto", c.confidence_badge("ai", pct=row.confidence)),
        ("flagged", c.confidence_badge("ai", pct=row.confidence, label=f"AI — {row.confidence}% (review)")),
        ("unavailable", c.pill("Needs input", variant="danger")),
        ("unavailable_review", c.pill("Below threshold", variant="danger")),
        ("unmapped", c.pill("Not mapped", variant="placeholder")),
        c.pill("Needs input", variant="placeholder"),
    )


def _column_row(row) -> rx.Component:
    """One row of the mapping table: source column → canonical field →
    confidence. The canonical field is shown as READ-ONLY text with an
    explicit Edit button — one row edits at a time (an always-open dropdown
    per row is what made the old table overflow and feel unusable)."""
    is_editing = IngestionAiState.editing_column == row.raw_column
    return rx.vstack(
        # --- read-only summary row (always) ---
        rx.hstack(
            rx.vstack(
                rx.cond(
                    row.is_unmapped_field,
                    rx.text("(no source column)", style=t.TEXT["micro"], font_style="italic"),
                    rx.text(row.raw_column, font_size="13px", font_weight="600"),
                ),
                spacing="0",
                align="start",
                flex="4",
                min_width="0",
            ),
            rx.box(
                rx.hstack(
                    rx.text(row.mapped_field, font_size="13px", font_weight="600"),
                    rx.cond(
                        row.required,
                        c.pill("required", variant="danger"),
                        c.pill("optional", variant="placeholder"),
                    ),
                    spacing="2",
                    align="center",
                ),
                flex="3",
                min_width="0",
            ),
            rx.box(_confidence_cell(row), flex="2", min_width="0"),
            rx.cond(
                row.is_unmapped_field,
                rx.box(width="70px"),
                rx.button(
                    rx.icon("pencil", size=13),
                    "Edit",
                    on_click=IngestionAiState.start_edit_column(row.raw_column),
                    size="1",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_PRIMARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                    flex_shrink="0",
                ),
            ),
            width="100%",
            align="center",
            spacing="3",
        ),
        # --- reason (below, full width, never squeezed) ---
        rx.cond(
            row.reason != "",
            rx.text(row.reason, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
            rx.fragment(),
        ),
        # --- inline editor (only for the row being edited) ---
        rx.cond(
            is_editing,
            rx.hstack(
                rx.select(
                    IngestionAiState.canonical_field_select_options,
                    value=IngestionAiState.edit_draft_field,
                    on_change=IngestionAiState.set_edit_draft_field,
                    width="280px",
                ),
                rx.button(
                    "Save",
                    on_click=IngestionAiState.save_edit_column,
                    size="1",
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Cancel",
                    on_click=IngestionAiState.cancel_edit_column,
                    size="1",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                spacing="2",
                align="center",
                width="100%",
                padding_top="6px",
            ),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        width="100%",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _raw_preview() -> rx.Component:
    """First several rows of the ACTUAL sheet, so the mapping can be
    sanity-checked against real values rather than headers alone. Sits in
    the left pane of the two-pane review layout."""
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text("Source file", style=t.TEXT["card_title"]),
                rx.spacer(),
                # §5.3 — the instruction library is shown ONLY when a model
                # actually ran. On a deterministic parse or a cache hit there
                # was no model call, so a "C5 context" badge would be a lie.
                rx.cond(
                    IngestionAiState.review_provenance_path == "ai",
                    rx.cond(
                        IngestionAiState.review_c5_used,
                        c.accent_pill("Instruction library applied"),
                        rx.fragment(),
                    ),
                    rx.fragment(),
                ),
                width="100%",
                align="center",
            ),
            rx.text(
                f"{IngestionAiState.review_filename} · {IngestionAiState.review_source_label}",
                style=t.TEXT["micro"],
            ),
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
                                            "position": "sticky",
                                            "top": "0",
                                            "background": t.Color.SURFACE.value,
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
                                                "max_width": "180px",
                                                "overflow": "hidden",
                                                "text_overflow": "ellipsis",
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
                    max_height="300px",
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
                        rx.text("Source column", style=t.TEXT["label"], flex="4"),
                        rx.text("Canonical field", style=t.TEXT["label"], flex="3"),
                        rx.text("Confidence", style=t.TEXT["label"], flex="2"),
                        rx.box(width="70px"),
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


def _checklist_row(f) -> rx.Component:
    """One row of the mapped-fields checklist: tick/cross + field name +
    required/optional tag + the source column it came from."""
    return rx.hstack(
        rx.cond(
            f.needs_attention,
            rx.icon("circle_alert", size=15, color=t.Color.DANGER.value),
            rx.icon("circle_check", size=15, color=t.Color.RULE.value),
        ),
        rx.text(f.canonical_field, font_size="13px", font_weight="600", min_width="150px"),
        rx.cond(
            f.required,
            c.pill("required", variant="danger"),
            c.pill("optional", variant="placeholder"),
        ),
        rx.text(
            rx.cond(f.raw_column != "", f.raw_column, "— not mapped —"),
            style=t.TEXT["micro"],
            color=rx.cond(
                f.raw_column != "", t.Color.TEXT_SECONDARY.value, t.Color.DANGER.value
            ),
            flex="1",
            min_width="0",
        ),
        # Where the mapping came from: rule (deterministic parser / trusted
        # profile) vs AI — never color alone, always a text label. A field
        # with no source column (residual, legitimately absent) gets none.
        rx.cond(
            (~f.needs_attention) & (f.raw_column != ""),
            rx.cond(
                f.status == "rule",
                c.pill("Rule", variant="rule"),
                c.pill("AI", variant="ai"),
            ),
            rx.fragment(),
        ),
        spacing="2",
        align="center",
        width="100%",
        padding="6px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _mapping_checklist() -> rx.Component:
    """The mapped-fields checklist: one row per canonical field, in schema
    order, with its mapping status. This is the "is everything covered?"
    view — the column table is the per-column view."""
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text("Mapping checklist", style=t.TEXT["card_title"]),
                rx.spacer(),
                rx.text(
                    f"{IngestionAiState.mapped_checklist_done} of "
                    f"{IngestionAiState.mapped_checklist.length()} mapped",
                    style=t.TEXT["label"],
                    color=t.Color.TEXT_SECONDARY.value,
                ),
                width="100%",
                align="center",
            ),
            rx.text(
                "Every canonical field this reconciliation needs, and where it comes from. "
                "Anything still unmapped is a gap the report will carry as a caveat.",
                style=t.TEXT["micro"],
            ),
            rx.vstack(
                rx.foreach(IngestionAiState.mapped_checklist, _checklist_row),
                spacing="0",
                width="100%",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _sample_value_chip(sv) -> rx.Component:
    """One mapped value: field label + the REAL first value from the file.
    Amounts get a monospace tabular treatment so figures line up."""
    return rx.vstack(
        rx.text(sv.label, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
        rx.text(
            rx.cond(sv.value != "", sv.value, "— blank —"),
            font_size="13px",
            font_weight="600",
            font_family=rx.cond(sv.is_amount, "ui-monospace, SFMono-Regular, Menlo, monospace", "inherit"),
            color=rx.cond(sv.value != "", t.Color.TEXT_PRIMARY.value, t.Color.TEXT_MUTED.value),
            no_of_lines=1,
        ),
        spacing="0",
        align="start",
        min_width="150px",
        flex="1",
    )


def _sample_values() -> rx.Component:
    """The mapped VALUES card: what the file actually contains, per field —
    a GSTIN, an invoice number, rupee amounts — so the reviewer can sanity
    check the numbers (not just the column mapping) before confirming."""
    return rx.cond(
        IngestionAiState.sample_values.length() > 0,
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.text("Mapped values — what we read from the file", style=t.TEXT["card_title"]),
                    rx.spacer(),
                    rx.text(
                        "First value found per field",
                        style=t.TEXT["micro"],
                        color=t.Color.TEXT_MUTED.value,
                    ),
                    width="100%",
                    align="center",
                ),
                rx.text(
                    "A real value from the file for each mapped field. Check the GSTIN and the "
                    "amounts read correctly — if a number looks wrong, the column is mapped wrong.",
                    style=t.TEXT["micro"],
                ),
                rx.flex(
                    rx.foreach(IngestionAiState.sample_values, _sample_value_chip),
                    wrap="wrap",
                    spacing="4",
                    width="100%",
                    row_gap="12px",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
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


def _identification_headline() -> rx.Component:
    """§7 item 1 — what this file IS: report, entity, period, counts, total,
    and its provenance (recognised / deterministic / AI proposal)."""
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.hstack(
                        rx.text(IngestionAiState.review_title, style=t.TEXT["card_title"]),
                        rx.cond(
                            IngestionAiState.review_provenance_path == "deterministic",
                            c.confidence_badge("rule", label=IngestionAiState.review_provenance_label),
                            rx.cond(
                                IngestionAiState.review_status == STATUS_CONFIRMED,
                                c.confidence_badge("rule", label="Confirmed"),
                                c.pill(IngestionAiState.review_provenance_label, variant="ai"),
                            ),
                        ),
                        spacing="3",
                        align="center",
                    ),
                    rx.cond(
                        IngestionAiState.review_entity != "",
                        rx.text(IngestionAiState.review_entity, style=t.TEXT["body"]),
                        rx.fragment(),
                    ),
                    rx.text(
                        IngestionAiState.review_period_label
                        + rx.cond(
                            IngestionAiState.review_invoice_count > 0,
                            " · " + IngestionAiState.review_invoice_count.to_string() + " invoices",
                            "",
                        )
                        + rx.cond(
                            IngestionAiState.review_supplier_count > 0,
                            " · " + IngestionAiState.review_supplier_count.to_string() + " suppliers",
                            "",
                        )
                        + rx.cond(
                            IngestionAiState.review_total_label != "",
                            " · " + IngestionAiState.review_total_label,
                            "",
                        ),
                        style=t.TEXT["body"],
                        color=t.Color.TEXT_SECONDARY.value,
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.spacer(),
                rx.vstack(
                    rx.text(
                        "Filed under " + IngestionAiState.review_client_label
                        + rx.cond(
                            IngestionAiState.review_period != "",
                            " · " + IngestionAiState.review_period,
                            "",
                        ),
                        style=t.TEXT["micro"],
                        color=t.Color.TEXT_SECONDARY.value,
                    ),
                    rx.text(IngestionAiState.review_filename, style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value),
                    spacing="0",
                    align="end",
                ),
                width="100%",
                align="start",
            ),
            rx.text(IngestionAiState.review_provenance_detail, style=t.TEXT["micro"]),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _summary_line() -> rx.Component:
    """§7 item 2 — the headline summary, composed from live state."""
    return c.card(
        rx.hstack(
            rx.hstack(
                rx.icon("circle_check", size=16, color=t.Color.RULE.value),
                rx.text(
                    IngestionAiState.summary_verified.to_string() + " verified",
                    style=t.TEXT["body"], font_weight="600",
                ),
                spacing="2", align="center",
            ),
            rx.hstack(
                rx.icon("circle_alert", size=16, color=t.Color.AI.value),
                rx.text(
                    IngestionAiState.summary_review.to_string() + " need your review",
                    style=t.TEXT["body"], font_weight="600",
                ),
                spacing="2", align="center",
            ),
            rx.hstack(
                rx.icon("circle_slash", size=16, color=t.Color.NEUTRAL.value),
                rx.text(
                    IngestionAiState.summary_absent.to_string() + " not in this file",
                    style=t.TEXT["body"], font_weight="600",
                ),
                spacing="2", align="center",
            ),
            rx.spacer(),
            rx.text(IngestionAiState.gate_summary, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
            spacing="5",
            align="center",
            width="100%",
            wrap="wrap",
        ),
        width="100%",
    )


def _check_row(r) -> rx.Component:
    return rx.hstack(
        rx.icon(
            rx.cond(r.failed, "triangle_alert", "circle_check"),
            size=15,
            color=rx.cond(r.failed, t.Color.DANGER.value, t.Color.RULE.value),
        ),
        rx.text(r.label, style=t.TEXT["body"], font_weight="600", min_width="170px"),
        rx.text(r.detail, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value, flex="1"),
        width="100%",
        align="start",
        spacing="3",
        padding="6px 0",
    )


def _what_we_checked() -> rx.Component:
    """§7 item 3 — every §8 check. Failures first; passing checks collapsed.
    Silence is never a pass signal, so the passing list is always present."""
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text("What we checked", style=t.TEXT["card_title"]),
                rx.spacer(),
                rx.cond(
                    IngestionAiState.checks_failed.length() > 0,
                    c.pill(
                        IngestionAiState.checks_failed.length().to_string() + " flagged",
                        variant="danger",
                    ),
                    c.confidence_badge("rule", label="All checks passed"),
                ),
                width="100%",
                align="center",
            ),
            rx.cond(
                IngestionAiState.checks.length() == 0,
                rx.text(
                    "No independent checks ran for this upload — nothing has been verified.",
                    style=t.TEXT["micro"],
                    color=t.Color.TEXT_SECONDARY.value,
                ),
                rx.vstack(
                    rx.foreach(IngestionAiState.checks_failed, _check_row),
                    rx.cond(
                        IngestionAiState.checks_passed.length() > 0,
                        rx.vstack(
                            rx.button(
                                rx.cond(
                                    IngestionAiState.show_passed_checks,
                                    "Hide passing checks (" + IngestionAiState.checks_passed.length().to_string() + ")",
                                    "Show passing checks (" + IngestionAiState.checks_passed.length().to_string() + ")",
                                ),
                                on_click=IngestionAiState.toggle_passed_checks,
                                size="1",
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_SECONDARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                            ),
                            rx.cond(
                                IngestionAiState.show_passed_checks,
                                rx.vstack(
                                    rx.foreach(IngestionAiState.checks_passed, _check_row),
                                    spacing="0", width="100%",
                                ),
                                rx.fragment(),
                            ),
                            spacing="2", align="start", width="100%",
                        ),
                        rx.fragment(),
                    ),
                    spacing="1",
                    width="100%",
                ),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _rate_row(r) -> rx.Component:
    """One head x rate row. Laid out as fixed-width columns so the source
    column names stay on one line — a wrapping column name is unreadable."""
    return rx.hstack(
        rx.text(r.head + " @ " + r.rate_label, style=t.TEXT["body"], font_weight="600", width="110px", flex_shrink="0"),
        rx.text("Taxable", style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value, width="60px", flex_shrink="0"),
        rx.vstack(
            rx.text(r.taxable_column, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value, no_of_lines=1),
            rx.cond(
                r.mirror_note != "",
                rx.text(r.mirror_note, style=t.TEXT["micro"], color=t.Color.AI_ON.value, no_of_lines=1),
                rx.fragment(),
            ),
            spacing="0", align="start", flex="1", min_width="0",
        ),
        rx.text("Tax", style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value, width="36px", flex_shrink="0"),
        rx.text(r.tax_column, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value, flex="1", min_width="0", no_of_lines=1),
        rx.box(
            rx.cond(
                r.setu_field != "",
                c.pill("→ " + r.setu_field.upper(), variant="accent"),
                rx.fragment(),
            ),
            width="90px",
            flex_shrink="0",
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="6px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _tax_columns_card() -> rx.Component:
    """§7 item 4 — the head x rate matrix, so the aggregation is visible and
    the mirror exclusion is explained rather than silent."""
    return rx.cond(
        IngestionAiState.rate_rows.length() > 0,
        c.card(
            rx.vstack(
                rx.text("Tax columns — how the rate buckets add up", style=t.TEXT["card_title"]),
                rx.text(IngestionAiState.rate_note, style=t.TEXT["micro"]),
                rx.foreach(IngestionAiState.rate_rows, _rate_row),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _evidence_row(f) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.hstack(
                rx.text(f.label, style=t.TEXT["body"], font_weight="600"),
                rx.cond(
                    f.tier == "required",
                    c.pill("required", variant="danger"),
                    rx.cond(
                        f.tier == "recommended",
                        c.pill("recommended", variant="ai"),
                        c.pill("optional", variant="placeholder"),
                    ),
                ),
                spacing="2",
                align="center",
            ),
            rx.cond(
                f.source_summary != "",
                rx.text(f.source_summary, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
                rx.fragment(),
            ),
            spacing="0",
            align="start",
            flex="1",
            min_width="0",
        ),
        rx.box(
            rx.match(
                f.status,
                ("verified", c.confidence_badge("rule", label="Verified")),
                ("failed", c.pill("Blocked", variant="danger")),
                ("missing", c.pill("Not in this file", variant="placeholder")),
                c.confidence_badge("ai", label="Needs review"),
            ),
            flex_shrink="0",
        ),
        width="100%",
        align="start",
        spacing="3",
        padding="6px 0",
    )


def _field_evidence_card() -> rx.Component:
    """§7 item 6 — fields grouped Verified / Needs review / Not in this file,
    with the EVIDENCE that justifies each verdict (never a bare percentage)."""
    return c.card(
        rx.vstack(
            rx.text("Setu fields", style=t.TEXT["card_title"]),
            rx.text(
                "Every field Setu will read, with the evidence that verifies it. "
                "Confidence alone never passes a field — the checks do.",
                style=t.TEXT["micro"],
            ),
            rx.cond(
                IngestionAiState.verified_fields.length() > 0,
                rx.vstack(
                    rx.text("Verified", style=t.TEXT["label"], color=t.Color.RULE_ON.value),
                    rx.foreach(IngestionAiState.verified_fields, _evidence_row),
                    spacing="0", width="100%",
                ),
                rx.fragment(),
            ),
            rx.cond(
                IngestionAiState.review_fields.length() > 0,
                rx.vstack(
                    rx.text("Needs your review", style=t.TEXT["label"], color=t.Color.AI_ON.value),
                    rx.foreach(IngestionAiState.review_fields, _evidence_row),
                    spacing="0", width="100%",
                ),
                rx.fragment(),
            ),
            rx.cond(
                IngestionAiState.absent_fields.length() > 0,
                rx.vstack(
                    rx.text("Not in this file", style=t.TEXT["label"], color=t.Color.TEXT_SECONDARY.value),
                    rx.foreach(IngestionAiState.absent_fields, _evidence_row),
                    spacing="0", width="100%",
                ),
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _normalised_preview() -> rx.Component:
    """§7 item 5 — what SETU WILL READ, with a totals row set against the
    file's own Total row. This is how a reviewer judges the mapping."""
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text("What Setu will read", style=t.TEXT["card_title"]),
                rx.spacer(),
                rx.cond(
                    IngestionAiState.normalised_rows.length() > 0,
                    rx.button(
                        rx.cond(
                            IngestionAiState.show_all_preview, "Show first 20", "Show all rows",
                        ),
                        on_click=IngestionAiState.toggle_all_preview,
                        size="1",
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="8px",
                    ),
                    rx.fragment(),
                ),
                width="100%",
                align="center",
            ),
            rx.cond(
                IngestionAiState.normalised_note != "",
                rx.text(IngestionAiState.normalised_note, style=t.TEXT["micro"]),
                rx.vstack(
                    rx.text(
                        "The normalised rows the pipeline produced. The totals row is set against "
                        "the file's own Total row — if they disagree, the mapping is wrong.",
                        style=t.TEXT["micro"],
                    ),
                    rx.box(
                        rx.el.table(
                            rx.el.thead(
                                rx.el.tr(
                                    rx.foreach(
                                        IngestionAiState.normalised_headers,
                                        lambda h: rx.el.th(
                                            h,
                                            style={
                                                "text_align": "left",
                                                "padding": "6px 10px",
                                                "font_size": "11px",
                                                "color": t.Color.TEXT_SECONDARY.value,
                                                "border_bottom": f"1px solid {t.Color.BORDER.value}",
                                                "white_space": "nowrap",
                                            },
                                        ),
                                    ),
                                ),
                            ),
                            rx.el.tbody(
                                rx.foreach(
                                    IngestionAiState.normalised_rows,
                                    lambda row: rx.el.tr(
                                        rx.foreach(
                                            row,
                                            lambda cell: rx.el.td(
                                                cell,
                                                style={
                                                    "padding": "5px 10px",
                                                    "font_size": "12px",
                                                    "border_bottom": f"1px solid {t.Color.BORDER.value}",
                                                    "white_space": "nowrap",
                                                    "font_variant_numeric": "tabular-nums",
                                                },
                                            ),
                                        ),
                                    ),
                                ),
                                rx.cond(
                                    IngestionAiState.normalised_total_row.length() > 0,
                                    rx.el.tr(
                                        rx.foreach(
                                            IngestionAiState.normalised_total_row,
                                            lambda cell: rx.el.td(
                                                cell,
                                                style={
                                                    "padding": "6px 10px",
                                                    "font_size": "12px",
                                                    "font_weight": "700",
                                                    "border_top": f"2px solid {t.Color.BORDER.value}",
                                                    "white_space": "nowrap",
                                                    "font_variant_numeric": "tabular-nums",
                                                },
                                            ),
                                        ),
                                    ),
                                    rx.fragment(),
                                ),
                            ),
                            style={"border_collapse": "collapse", "width": "100%"},
                        ),
                        overflow_x="auto",
                        width="100%",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _ignored_columns_card() -> rx.Component:
    """§5 Part 1.2 — every column's disposition, so nothing is an unexplained
    "Not mapped"."""
    return rx.cond(
        IngestionAiState.ignored_columns.length() > 0,
        c.card(
            rx.vstack(
                rx.text("Columns Setu set aside", style=t.TEXT["card_title"]),
                rx.text(
                    "These columns are not mapped to a Setu field, and here is why.",
                    style=t.TEXT["micro"],
                ),
                rx.foreach(
                    IngestionAiState.ignored_columns,
                    lambda d: rx.hstack(
                        rx.text(d.column, style=t.TEXT["body"], font_weight="600", min_width="170px"),
                        rx.box(
                            rx.match(
                                d.disposition,
                                ("validation-signal", c.pill("validation signal", variant="ai")),
                                c.pill("ignored", variant="placeholder"),
                            ),
                            flex_shrink="0",
                        ),
                        rx.text(d.detail, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value, flex="1"),
                        width="100%",
                        align="center",
                        spacing="3",
                        padding="5px 0",
                    ),
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _sticky_action_bar() -> rx.Component:
    """§7 item 8 — the action bar with a live summary and the Save-as-layout
    choice, so the reviewer always knows what confirming will do."""
    return c.card(
        rx.vstack(
            rx.cond(
                IngestionAiState.gate_blocked,
                c.inline_reason("Confirm is blocked — " + IngestionAiState.gate_reasons),
                rx.fragment(),
            ),
            rx.cond(
                IngestionAiState.mismatch_banner != "",
                rx.vstack(
                    c.warning_banner(IngestionAiState.mismatch_banner),
                    rx.hstack(
                        rx.checkbox(
                            checked=IngestionAiState.mismatch_ack,
                            on_change=IngestionAiState.set_mismatch_ack,
                        ),
                        rx.text(
                            "I have checked this file belongs to this client and period",
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
                rx.vstack(
                    rx.text("Save this layout for future files", style=t.TEXT["label"]),
                    rx.text(
                        "Future files with this exact column layout will parse without AI and are "
                        "still validated every time.",
                        style=t.TEXT["micro"],
                    ),
                    spacing="0",
                    align="start",
                ),
                rx.select(
                    ["Do not save", "This client only", "Firm-wide"],
                    value=rx.match(
                        IngestionAiState.layout_scope,
                        ("client", "This client only"),
                        ("firmwide", "Firm-wide"),
                        "Do not save",
                    ),
                    on_change=IngestionAiState.set_layout_scope_value,
                    width="220px",
                ),
                spacing="4",
                align="center",
                width="100%",
            ),
            rx.hstack(
                rx.button(
                    "Confirm mapping",
                    on_click=IngestionAiState.open_confirm,
                    disabled=IngestionAiState.gate_blocked,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.text(IngestionAiState.gate_summary, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
                rx.spacer(),
                # §5.6 — the correction/confirmation audit trail (F4).
                c.view_history(
                    "View history",
                    IngestionAiState.mapping_history,
                    count=IngestionAiState.mapping_edit_count,
                ),
                _back_button("Cancel", IngestionAiState.back_to_uploads),
                spacing="3",
                align="center",
                width="100%",
                wrap="wrap",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _discard_edits_dialog() -> rx.Component:
    """§6 (D21) — leaving the review screen with unsaved edits asks first."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Discard your unsaved edits?", style=t.TEXT["card_title"]),
                rx.text(
                    "You have changed the mapping but not confirmed it. Leaving now discards "
                    "those changes.",
                    style=t.TEXT["body"],
                ),
                rx.hstack(
                    rx.dialog.close(
                        rx.button(
                            "Keep editing",
                            on_click=IngestionAiState.cancel_discard_edits,
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                    ),
                    rx.dialog.close(
                        rx.button(
                            "Discard edits",
                            on_click=IngestionAiState.confirm_discard_edits,
                            background=t.Color.DANGER.value,
                            color="#FFFFFF",
                        ),
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
            ),
            max_width="460px",
        ),
        open=IngestionAiState.discard_prompt_open,
        on_open_change=IngestionAiState.cancel_discard_edits,
    )


def _mapping_review() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            _back_button("← Back to uploads", IngestionAiState.back_to_uploads),
            rx.spacer(),
            rx.button(
                rx.icon("refresh-cw", size=14),
                "Re-run through model",
                on_click=IngestionAiState.open_rerun(
                    IngestionAiState.selected_upload_id, IngestionAiState.review_filename
                ),
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_SECONDARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="9px",
                size="2",
            ),
            width="100%",
            justify="between",
            align="center",
        ),
        rx.cond(
            IngestionAiState.classification_error != "",
            c.inline_reason(IngestionAiState.classification_error),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.blocked_banner != "",
            c.inline_reason(IngestionAiState.blocked_banner),
            rx.fragment(),
        ),
        c.ai_progress(
            rx.cond(IngestionAiState.busy_label != "", IngestionAiState.busy_label, "Working…"),
            visible=IngestionAiState.busy_label != "",
        ),
        # 1. Identification headline
        _identification_headline(),
        # 2. Summary line
        _summary_line(),
        # 3. What we checked
        _what_we_checked(),
        # 4. Tax columns (rate-bucket matrix)
        _tax_columns_card(),
        # 5. What Setu will read (normalised preview + totals)
        _normalised_preview(),
        # 6. Setu fields, grouped with evidence
        _field_evidence_card(),
        # 7. Source file + set-aside columns
        _ignored_columns_card(),
        rx.cond(
            IngestionAiState.can_review,
            _raw_preview(),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.can_review,
            _sticky_action_bar(),
            rx.fragment(),
        ),
        _confirm_dialog(),
        _discard_edits_dialog(),
        _rerun_dialog(),
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