"""Reconcile — the guided five-stage flow (the LAST module).

Context → Upload → Reconcile → Review → Export. One guided path from source
files to a finished report. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.reconcile_state import ReconcileState
from setu.views import shell

_ACCEPT = {
    "text/csv": [".csv"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
}


# ---------------------------------------------------------------------------
# The rail
# ---------------------------------------------------------------------------


def _rail_chip(chip) -> rx.Component:
    return rx.button(
        rx.hstack(
            rx.text(
                chip.number.to_string(),
                font_size="11px",
                font_weight="700",
                color=rx.cond(chip.active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
            ),
            rx.text(
                chip.label,
                font_size="12px",
                font_weight=rx.cond(chip.active, "700", "500"),
                color=rx.cond(chip.active, "#FFFFFF", t.Color.TEXT_PRIMARY.value),
            ),
            spacing="2",
            align="center",
        ),
        on_click=ReconcileState.goto_stage(chip.number),
        disabled=~chip.reachable,
        variant="soft",
        background=rx.cond(chip.active, t.Color.ACCENT.value, "transparent"),
        border=rx.cond(chip.active, f"1px solid {t.Color.ACCENT.value}", f"1px solid {t.Color.BORDER.value}"),
        border_radius="10px",
        padding="8px 14px",
        _disabled={"opacity": "0.45", "cursor": "not-allowed"},
        _hover={"background": rx.cond(chip.active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _rail() -> rx.Component:
    return rx.hstack(
        rx.foreach(ReconcileState.stage_chips, _rail_chip),
        spacing="2",
        wrap="wrap",
        padding="4px",
        background=t.Color.SURFACE.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="12px",
        width="100%",
    )


def _banners() -> rx.Component:
    return rx.fragment(
        rx.cond(ReconcileState.flash != "", c.info_banner(ReconcileState.flash), rx.fragment()),
        rx.cond(ReconcileState.error != "", c.inline_reason(ReconcileState.error), rx.fragment()),
    )


# ---------------------------------------------------------------------------
# Stage 1 — Context
# ---------------------------------------------------------------------------


def _context_stage() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                c.section_title(
                    "1. Context",
                    "Client, period and recon type. Pick all three and this step completes itself.",
                ),
                rx.hstack(
                    rx.vstack(
                        rx.text("Client", style=t.TEXT["label"]),
                        rx.select(
                            ReconcileState.clients,
                            value=ReconcileState.ctx_client,
                            on_change=ReconcileState.set_ctx_client,
                            width="220px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.vstack(
                        rx.text("Period", style=t.TEXT["label"]),
                        rx.select.root(
                            rx.select.trigger(width="160px", placeholder="No periods yet"),
                            rx.select.content(
                                rx.select.group(
                                    rx.foreach(
                                        ReconcileState.period_options,
                                        lambda option: rx.select.item(option.label, value=option.value),
                                    ),
                                ),
                            ),
                            value=ReconcileState.ctx_period,
                            on_change=ReconcileState.set_ctx_period,
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.vstack(
                        rx.text("Recon type", style=t.TEXT["label"]),
                        rx.select(
                            ReconcileState.recon_types,
                            value=ReconcileState.ctx_recon_type,
                            on_change=ReconcileState.set_ctx_recon_type,
                            width="150px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    spacing="4",
                    align="end",
                    wrap="wrap",
                ),
                rx.cond(
                    ReconcileState.ctx_client != "",
                    c.info_banner(
                        f"Context: {ReconcileState.ctx_client} · {ReconcileState.ctx_period} · "
                        f"{ReconcileState.ctx_recon_type}. You won't be asked for this again during this run."
                    ),
                    rx.fragment(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            ReconcileState.has_existing_run & (ReconcileState.run_id == 0) & ~ReconcileState.started_fresh,
            c.card(
                rx.vstack(
                    c.section_title("There's already a completed run in this context"),
                    rx.text(
                        f"Run {ReconcileState.existing_run_id} finished on "
                        f"{ReconcileState.existing_run_when}. You can pick it up where it left off, "
                        "or start a fresh reconciliation.",
                        style=t.TEXT["body"],
                    ),
                    rx.hstack(
                        rx.button(
                            "Continue this run",
                            on_click=ReconcileState.continue_existing_run,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.button(
                            "Start fresh",
                            on_click=ReconcileState.start_fresh,
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                        spacing="2",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.cond(
                ReconcileState.run_id != 0,
                c.card(
                    rx.vstack(
                        c.info_banner(f"This flow is working from run {ReconcileState.run_id} in this context."),
                        rx.hstack(
                            rx.button(
                                "Back to results",
                                on_click=ReconcileState.goto_stage(4),
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                            ),
                            rx.button(
                                "Start fresh",
                                on_click=ReconcileState.start_fresh,
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_SECONDARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="9px",
                            ),
                            spacing="2",
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                rx.cond(
                    ReconcileState.ready,
                    rx.button(
                        "Continue to upload →",
                        on_click=ReconcileState.continue_forward,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    rx.cond(
                        ReconcileState.ctx_client != "",
                        rx.button(
                            "Continue to upload →",
                            on_click=ReconcileState.continue_forward,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.fragment(),
                    ),
                ),
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Stage 2 — Upload
# ---------------------------------------------------------------------------


def _slot_card(s) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text(s.label, style=t.TEXT["card_title"]),
                rx.cond(s.is_books, c.pill("Books side", variant="accent"), c.pill("Portal side", variant="placeholder")),
                rx.spacer(),
                rx.match(
                    s.chip_variant,
                    ("rule", c.pill(s.chip_label, variant="rule")),
                    ("ai", c.pill(s.chip_label, variant="ai")),
                    ("danger", c.pill(s.chip_label, variant="danger")),
                    c.pill(s.chip_label, variant="placeholder"),
                ),
                width="100%",
                align="center",
                spacing="3",
            ),
            rx.cond(s.hint != "", rx.text(s.hint, style=t.TEXT["micro"]), rx.fragment()),
            rx.cond(
                s.files.length() > 0,
                rx.select(
                    s.files,
                    value=s.selected,
                    on_change=lambda v: ReconcileState.select_slot_file(s.source_type, v),
                    width="100%",
                    placeholder="no file selected",
                ),
                rx.text("No files on disk for this slot yet — upload one above.", style=t.TEXT["micro"]),
            ),
            rx.cond(
                s.caveats.length() > 0,
                rx.vstack(
                    rx.text("This file's gaps — and what they cost the report:", style=t.TEXT["micro"]),
                    rx.foreach(
                        s.caveats,
                        lambda cv: rx.text(f"• {cv}", style=t.TEXT["micro"]),
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.fragment(),
            ),
            rx.cond(
                s.selected != "",
                rx.hstack(
                    rx.button(
                        rx.icon("refresh-cw", size=14),
                        "Re-run through model",
                        on_click=ReconcileState.open_rerun(s.source_type, s.selected),
                        size="1",
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="8px",
                    ),
                    spacing="2",
                ),
                rx.fragment(),
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _rerun_dialog() -> rx.Component:
    """Per-file confirm dialog for a model re-run. A re-run is a live model
    call (~15-25s) and discards the cached mapping, so it is never one-click."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Re-run this file through the model?", style=t.TEXT["card_title"]),
                rx.text(
                    f"{ReconcileState.rerun_filename}",
                    style=t.TEXT["body"],
                    font_weight="600",
                ),
                rx.text(
                    "This discards the cached mapping for this file and asks the model to map "
                    "its columns again. It takes roughly 15–25 seconds and may produce a "
                    "different mapping than the one you have now.",
                    style=t.TEXT["micro"],
                ),
                rx.hstack(
                    rx.button(
                        "Re-run through model",
                        on_click=ReconcileState.confirm_rerun,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                        disabled=ReconcileState.rerun_busy,
                    ),
                    rx.button(
                        "Cancel",
                        on_click=ReconcileState.close_rerun,
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            max_width="520px",
        ),
        open=ReconcileState.rerun_open,
        on_open_change=ReconcileState.close_rerun,
    )


def _upload_card() -> rx.Component:
    """One upload widget for the whole stage — the source-type is chosen from
    a dropdown (a literal option list), so the widget id stays a Python
    literal (rx.upload ids cannot contain a Var)."""
    return c.card(
        rx.vstack(
            c.section_title(
                "Upload a file",
                "Pick the slot it belongs to, then drop the file. It's normalized immediately.",
            ),
            rx.hstack(
                rx.vstack(
                    rx.text("Slot", style=t.TEXT["label"]),
                    rx.select(
                        ReconcileState.slot_labels,
                        value=ReconcileState.upload_slot_label,
                        on_change=ReconcileState.set_upload_slot_label,
                        width="260px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("File", style=t.TEXT["label"]),
                    rx.upload(
                        rx.vstack(
                            rx.icon("upload", size=16, color=t.Color.NEUTRAL.value),
                            rx.text("Drop a file here", style=t.TEXT["micro"]),
                            spacing="1",
                            align="center",
                            padding="12px 20px",
                        ),
                        id="rc_upload",
                        accept=_ACCEPT,
                        multiple=False,
                        border=f"1px dashed {t.Color.BORDER.value}",
                        border_radius="10px",
                        background=t.Color.SURFACE.value,
                    ),
                    spacing="1",
                    align="start",
                ),
                spacing="4",
                align="end",
                wrap="wrap",
            ),
            rx.hstack(
                rx.button(
                    "Upload & normalize",
                    on_click=ReconcileState.handle_upload(rx.upload_files(upload_id="rc_upload")),
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Clear",
                    on_click=rx.clear_selected_files("rc_upload"),
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                ),
                spacing="2",
            ),
            rx.cond(
                ReconcileState.upload_error != "",
                c.inline_reason(ReconcileState.upload_error),
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _upload_stage() -> rx.Component:
    return rx.vstack(
        c.section_title(
            "2. Upload",
            "Every file for this recon type, side by side. Each one is normalized as soon as "
            "you drop it — there's no separate ingestion screen to visit.",
        ),
        _upload_card(),
        _rerun_dialog(),
        rx.grid(
            rx.foreach(ReconcileState.slots, _slot_card),
            columns="3",
            spacing="3",
            width="100%",
        ),
        rx.cond(
            ReconcileState.blockers.length() > 0,
            c.card(
                rx.vstack(
                    c.warning_banner("Something here needs your attention before this can run."),
                    rx.foreach(
                        ReconcileState.blockers,
                        lambda b: c.inline_reason(b),
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.cond(
                ReconcileState.ready,
                rx.button(
                    "Continue to reconcile →",
                    on_click=ReconcileState.continue_forward,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                c.info_banner(
                    "Still needed: the books side and at least one portal file."
                ),
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Stage 3 — Reconcile
# ---------------------------------------------------------------------------


def _reconcile_stage() -> rx.Component:
    return rx.vstack(
        c.section_title("3. Reconcile"),
        rx.cond(
            ReconcileState.run_id != 0,
            c.card(
                rx.vstack(
                    c.info_banner(f"Run {ReconcileState.run_id} is already complete for this context."),
                    rx.hstack(
                        rx.button(
                            "See the results",
                            on_click=ReconcileState.goto_stage(4),
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.button(
                            "Run it again",
                            on_click=ReconcileState.run_again,
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                        spacing="2",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.cond(
                ~ReconcileState.ready,
                c.card(
                    rx.vstack(
                        c.warning_banner("Stage 2 isn't finished yet, so this can't run."),
                        rx.button(
                            "← Back to Upload",
                            on_click=ReconcileState.goto_stage(2),
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                c.card(
                    rx.vstack(
                        c.section_title("Source files in this run"),
                        rx.foreach(
                            ReconcileState.slots,
                            lambda s: rx.cond(
                                s.selected != "",
                                rx.hstack(
                                    rx.text(f"{s.label}:", style=t.TEXT["micro"]),
                                    rx.text(s.selected, style=t.TEXT["body"]),
                                    spacing="2",
                                    align="baseline",
                                ),
                                rx.fragment(),
                            ),
                        ),
                        c.divider(),
                        rx.hstack(
                            rx.checkbox(
                                checked=ReconcileState.pause_before_run,
                                on_change=ReconcileState.set_pause_before_run,
                            ),
                            rx.text(
                                "Pause here so I can double-check the files first",
                                style=t.TEXT["label"],
                            ),
                            spacing="2",
                            align="center",
                        ),
                        rx.cond(
                            ReconcileState.pause_before_run,
                            rx.vstack(
                                rx.text("Nothing runs until you press the button.", style=t.TEXT["micro"]),
                                rx.button(
                                    "Run reconciliation",
                                    on_click=ReconcileState.run_reconciliation,
                                    background=t.Color.ACCENT.value,
                                    color="#FFFFFF",
                                ),
                                spacing="2",
                                align="start",
                            ),
                            rx.button(
                                "Run reconciliation",
                                on_click=ReconcileState.run_reconciliation,
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                            ),
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Stage 4 — Review
# ---------------------------------------------------------------------------


def _exception_row(e) -> rx.Component:
    return rx.hstack(
        rx.box(c.pill(e.classification, variant="danger"), flex="3"),
        rx.box(
            rx.cond(e.difference_type != "", c.pill(e.difference_type, variant="accent"), rx.text("—", style=t.TEXT["micro"])),
            flex="2",
        ),
        rx.box(c.confidence_badge("rule", label=e.confidence_band), flex="2"),
        rx.box(
            rx.cond(e.reviewed, c.pill("Reviewed", variant="rule"), c.pill("Not reviewed", variant="ai")),
            flex="2",
        ),
        rx.box(rx.text(e.match_reason, style=t.TEXT["micro"]), flex="6"),
        rx.button(
            "Open",
            on_click=ReconcileState.open_exception(e.result_id),
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


def _record_panel(title, fields) -> rx.Component:
    return rx.vstack(
        rx.text(title, style=t.TEXT["label"], font_weight="700"),
        rx.cond(
            fields.length() > 0,
            rx.vstack(
                rx.foreach(
                    fields,
                    lambda f: rx.hstack(
                        rx.text(f.label, style=t.TEXT["micro"], min_width="120px"),
                        rx.text(f.value, style=t.TEXT["body"], flex="1"),
                        rx.cond(
                            f.overridden,
                            c.pill("Corrected", variant="accent"),
                            rx.fragment(),
                        ),
                        rx.button(
                            rx.icon("pencil", size=13),
                            on_click=ReconcileState.open_edit(f.side, f.key, f.value),
                            size="1",
                            variant="ghost",
                            color=t.Color.TEXT_MUTED.value,
                            title="Correct this value",
                        ),
                        rx.cond(
                            f.overridden,
                            rx.button(
                                rx.icon("undo-2", size=13),
                                on_click=ReconcileState.revert_edit(f.side, f.key),
                                size="1",
                                variant="ghost",
                                color=t.Color.TEXT_MUTED.value,
                                title="Revert to the engine's value",
                            ),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="center",
                        width="100%",
                    ),
                ),
                spacing="1",
                width="100%",
            ),
            rx.text("(no record on this side)", style=t.TEXT["micro"]),
        ),
        spacing="2",
        align="start",
        width="100%",
    )


def _edit_dialog() -> rx.Component:
    """Inline correction of one field of a matched record. A reason is
    required — a correction to a matched record is a sensitive change."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Correct this value", style=t.TEXT["card_title"]),
                rx.text(
                    f"{ReconcileState.edit_side} · {ReconcileState.edit_field}",
                    style=t.TEXT["micro"],
                ),
                rx.vstack(
                    rx.text("New value", style=t.TEXT["label"]),
                    rx.input(
                        value=ReconcileState.edit_value,
                        on_change=ReconcileState.set_edit_value,
                        width="100%",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                c.reason_capture(
                    label="Reason (required)",
                    value=ReconcileState.edit_reason,
                    on_change=ReconcileState.set_edit_reason,
                    placeholder="Why is this value being corrected?",
                ),
                rx.cond(
                    ReconcileState.edit_error != "",
                    c.inline_reason(ReconcileState.edit_error),
                    rx.fragment(),
                ),
                rx.text(
                    "The correction is logged, carried forward across re-runs, and applied to "
                    "the reconciliation exceptions and Action Center.",
                    style=t.TEXT["micro"],
                ),
                rx.hstack(
                    rx.button(
                        "Save correction",
                        on_click=ReconcileState.save_edit,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                        disabled=ReconcileState.edit_reason == "",
                    ),
                    rx.button(
                        "Cancel",
                        on_click=ReconcileState.close_edit,
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                    ),
                    spacing="2",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            max_width="520px",
        ),
        open=ReconcileState.edit_open,
        on_open_change=ReconcileState.close_edit,
    )


def _review_stage() -> rx.Component:
    return rx.vstack(
        c.section_title("4. Review"),
        rx.cond(
            ReconcileState.run_id == 0,
            c.empty_state("No run yet for this context — go back to Reconcile.", icon="search-check"),
            rx.cond(
                ReconcileState.selected_result_id != 0,
                rx.vstack(
                    rx.button(
                        "← Back to exceptions",
                        on_click=ReconcileState.close_exception,
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                        size="2",
                    ),
                    c.card(
                        rx.vstack(
                            rx.hstack(
                                c.pill(ReconcileState.detail_classification, variant="danger"),
                                rx.cond(
                                    ReconcileState.detail_difference_type != "",
                                    c.pill(ReconcileState.detail_difference_type, variant="accent"),
                                    rx.fragment(),
                                ),
                                rx.cond(
                                    ReconcileState.detail_reviewed,
                                    c.pill("Reviewed", variant="rule"),
                                    c.pill("Not reviewed", variant="ai"),
                                ),
                                rx.spacer(),
                                c.view_history(
                                    "Edit history",
                                    ReconcileState.detail_history,
                                    count=ReconcileState.detail_history.length(),
                                ),
                                spacing="2",
                                align="center",
                                wrap="wrap",
                                width="100%",
                            ),
                            rx.text(ReconcileState.detail_reason, style=t.TEXT["body"]),
                            c.divider(),
                            rx.hstack(
                                rx.box(_record_panel("Books side", ReconcileState.detail_books), flex="1"),
                                rx.box(_record_panel("Portal side", ReconcileState.detail_portal), flex="1"),
                                spacing="5",
                                width="100%",
                                align="start",
                            ),
                            spacing="3",
                            align="start",
                            width="100%",
                        ),
                        width="100%",
                    ),
                    _edit_dialog(),
                    c.card(
                        rx.vstack(
                            rx.text("Wrong column mapping?", style=t.TEXT["label"], font_weight="700"),
                            rx.text(
                                "If a field was mapped from the wrong source column, correct the "
                                "mapping in Smart Ingestion — the fix is remembered for this file "
                                "shape and applied on the next run.",
                                style=t.TEXT["micro"],
                            ),
                            rx.button(
                                "Correct mapping in Smart Ingestion →",
                                on_click=rx.redirect("/smart-ingestion"),
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_PRIMARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="9px",
                                size="2",
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
                ),
                rx.vstack(
                    c.card(
                        rx.vstack(
                            rx.text(
                                f"{ReconcileState.review_matched} matched automatically · "
                                f"{ReconcileState.review_exceptions} need your attention",
                                font_size="20px",
                                font_weight="700",
                                color=t.Color.TEXT_PRIMARY.value,
                            ),
                            rx.text(
                                f"Run {ReconcileState.run_id} · {ReconcileState.ctx_client} · "
                                f"{ReconcileState.ctx_period} · {ReconcileState.ctx_recon_type}",
                                style=t.TEXT["micro"],
                            ),
                            c.divider(),
                            rx.hstack(
                                rx.vstack(c.stat(ReconcileState.review_total.to_string(), "total records"), spacing="0", align="start"),
                                rx.vstack(c.stat(ReconcileState.review_matched.to_string(), "matched"), spacing="0", align="start"),
                                rx.vstack(c.stat(ReconcileState.review_exceptions.to_string(), "exceptions"), spacing="0", align="start"),
                                spacing="6",
                                wrap="wrap",
                            ),
                            spacing="3",
                            align="start",
                            width="100%",
                        ),
                        width="100%",
                    ),
                    rx.cond(
                        ReconcileState.caveats.length() > 0,
                        c.card(
                            rx.vstack(
                                c.warning_banner(
                                    f"This is a partial report. {ReconcileState.caveats.length()} "
                                    "check(s) could not be performed with the files and columns provided."
                                ),
                                rx.foreach(
                                    ReconcileState.caveats,
                                    lambda cv: rx.vstack(
                                        rx.text(cv.label, style=t.TEXT["body"], font_weight="600"),
                                        rx.text(cv.detail, style=t.TEXT["micro"]),
                                        spacing="0",
                                        align="start",
                                    ),
                                ),
                                spacing="2",
                                align="start",
                                width="100%",
                            ),
                            width="100%",
                        ),
                        rx.fragment(),
                    ),
                    rx.cond(
                        ReconcileState.review_exceptions == 0,
                        c.info_banner("Nothing needs a human decision on this run."),
                        c.card(
                            rx.vstack(
                                rx.hstack(
                                    rx.text("Show", style=t.TEXT["label"]),
                                    rx.select(
                                        ["All exceptions", "Not in Books", "Not in Portal", "Amount Difference"],
                                        value=ReconcileState.exception_filter,
                                        on_change=ReconcileState.set_exception_filter,
                                        width="200px",
                                    ),
                                    rx.spacer(),
                                    rx.text(
                                        f"{ReconcileState.exception_rows.length()} shown",
                                        style=t.TEXT["micro"],
                                    ),
                                    spacing="2",
                                    align="center",
                                    width="100%",
                                ),
                                rx.cond(
                                    ReconcileState.exception_rows.length() > 0,
                                    rx.vstack(
                                        rx.hstack(
                                            rx.text("Classification", style=t.TEXT["label"], flex="3"),
                                            rx.text("Difference", style=t.TEXT["label"], flex="2"),
                                            rx.text("Confidence", style=t.TEXT["label"], flex="2"),
                                            rx.text("Status", style=t.TEXT["label"], flex="2"),
                                            rx.text("Why", style=t.TEXT["label"], flex="6"),
                                            rx.box(width="60px"),
                                            width="100%",
                                            padding="0 0 6px 0",
                                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                                        ),
                                        rx.foreach(ReconcileState.exception_rows, _exception_row),
                                        spacing="0",
                                        width="100%",
                                    ),
                                    c.empty_state("No exceptions match this filter.", icon="circle_check"),
                                ),
                                spacing="3",
                                align="start",
                                width="100%",
                            ),
                            width="100%",
                        ),
                    ),
                    rx.button(
                        "Continue to export →",
                        on_click=ReconcileState.continue_forward,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    spacing="4",
                    width="100%",
                    align="start",
                ),
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Stage 5 — Export
# ---------------------------------------------------------------------------


def _export_stage() -> rx.Component:
    return rx.vstack(
        c.section_title("5. Export"),
        rx.cond(
            ReconcileState.run_id == 0,
            c.empty_state("No run to export yet — go back to Reconcile.", icon="download"),
            c.card(
                rx.vstack(
                    c.section_title("What goes into this report"),
                    rx.text(
                        f"Run: {ReconcileState.run_id} · Context: {ReconcileState.ctx_client} · "
                        f"{ReconcileState.ctx_period} · {ReconcileState.ctx_recon_type}",
                        style=t.TEXT["body"],
                    ),
                    rx.text(
                        f"Records: {ReconcileState.review_total} total · "
                        f"{ReconcileState.review_matched} matched · "
                        f"{ReconcileState.review_exceptions} exception(s)",
                        style=t.TEXT["body"],
                    ),
                    rx.cond(
                        ReconcileState.caveats.length() > 0,
                        c.warning_banner(
                            f"This report carries {ReconcileState.caveats.length()} stated limitation(s) — "
                            "the checks it could not perform."
                        ),
                        rx.fragment(),
                    ),
                    rx.button(
                        "Download reconciliation report",
                        on_click=ReconcileState.generate_export,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    rx.cond(
                        ReconcileState.export_ready,
                        rx.vstack(
                            c.info_banner("Report ready."),
                            rx.link(
                                rx.button(
                                    "Download reconciliation report",
                                    background=t.Color.ACCENT.value,
                                    color="#FFFFFF",
                                ),
                                href=(
                                    "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                    ";base64," + ReconcileState.export_b64
                                ),
                                download=ReconcileState.export_name,
                            ),
                            spacing="2",
                            align="start",
                        ),
                        rx.text("One button, one file. Nothing else to configure.", style=t.TEXT["micro"]),
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def reconcile_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Reconcile",
                "One guided path from source files to a finished report. "
                "Everything else lives under All tools and Setup.",
            ),
            _banners(),
            _rail(),
            rx.match(
                ReconcileState.stage,
                (1, _context_stage()),
                (2, _upload_stage()),
                (3, _reconcile_stage()),
                (4, _review_stage()),
                _export_stage(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )
