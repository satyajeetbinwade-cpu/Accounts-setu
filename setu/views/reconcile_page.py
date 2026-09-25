"""Reconcile — the guided five-stage flow (the LAST module).

Context → Upload → Reconcile → Review → Export. One guided path from source
files to a finished report. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.reconcile_state import ReconcileState
from setu.views import review_components as rc
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
# Stage 4 — Review (upgraded presentation; see setu/views/review_components.py)
# ---------------------------------------------------------------------------


def _review_stage() -> rx.Component:
    """Headline → grouped → detail, with the evidence drawer and bulk bar.

    Every figure is derived by ``src.reconciliation.review`` through
    ``ReconcileState`` — nothing is computed in a component.
    """
    return rx.vstack(
        rx.cond(
            ReconcileState.run_id == 0,
            rc.review_empty("No run yet for this context — go back to Reconcile.", icon="search-check"),
            rx.cond(
                ReconcileState.review_total == 0,
                rc.review_empty("This run produced no records — nothing to review.", icon="inbox"),
                rx.vstack(
                    # 0. context bar
                    rc.context_bar(),
                    # 1. headline
                    rc.summary_headline(),
                    # 2. KPI row
                    rc.kpi_row(),
                    # 3. accountant's read
                    rc.accountants_read(),
                    # 4. charts
                    rc.charts_row(),
                    # 5. items to review
                    rx.vstack(
                        c.section_title(
                            "Items to review",
                            "Filter, sort and group the exceptions. Open any row for the full evidence.",
                        ),
                        rc.filter_bar(),
                        rc.exception_table(),
                        rx.cond(
                            ReconcileState.review_exceptions == 0,
                            c.info_banner("Nothing needs a human decision on this run."),
                            rx.fragment(),
                        ),
                        spacing="3", width="100%", align="start",
                    ),
                    # 7. data quality & scope notes
                    rc.notes_list(),
                    # 8. integrity strip
                    rc.integrity_strip(),
                    # 9. report export (§3) — HTML / PDF / Excel
                    rc.report_export(),
                    rx.cond(
                        ReconcileState.review_standalone,
                        rx.fragment(),
                        rx.button(
                            "Continue to export →",
                            on_click=ReconcileState.continue_forward,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                    ),
                    spacing="4", width="100%", align="start",
                ),
            ),
        ),
        # 6. evidence drawer (URL-addressable, Esc closes)
        rc.evidence_drawer(),
        rc.edit_dialog(),
        # 6b. bulk action bar
        rc.bulk_bar(),
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
# Standalone Review screen (/review)
# ---------------------------------------------------------------------------


def review_page() -> rx.Component:
    """The standalone /review screen — the SAME Stage-4 Review presentation
    as /reconcile's Review tab, driven by the same ``ReconcileState`` and the
    same ``review_components``. Only the wrapper differs: no stage rail, and
    the page header reads "Review" instead of "Reconcile".
    """
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Review",
                "Reconciliation results — headline, grouped, and per-item detail. "
                "The same review surface as the Reconcile flow's Review tab.",
            ),
            _banners(),
            _review_stage(),
            spacing="5",
            width="100%",
            align="start",
        )
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
