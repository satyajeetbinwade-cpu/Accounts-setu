"""Reconcile — the guided five-stage flow (the LAST module).

Context → Upload → Reconcile → Review → Export. One guided path from source
files to a finished report. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from src.shared import discovery
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
                # The client picker is driven by the folders under
                # data/<client>/<period>/<source_type>/ (src/shared/discovery.py),
                # NOT by the client DB — so a server with no data folders yet
                # renders empty dropdowns that look broken. Say so plainly.
                rx.cond(
                    ReconcileState.clients.length() > 0,
                    rx.hstack(
                        c.client_picker(
                            ReconcileState.clients,
                            value=ReconcileState.ctx_client_id.to_string(),
                            on_change=ReconcileState.set_ctx_client,
                            placeholder="Select a client",
                            width="220px",
                            add_new_label="Add New Client",
                            on_add_new=ReconcileState.add_new_client,
                            show_add_new=ReconcileState.can_manage_clients,
                        ),
                        rx.vstack(
                            c.period_picker(
                                year=ReconcileState.ctx_year,
                                month=ReconcileState.ctx_month,
                                years=ReconcileState.year_options,
                                months=ReconcileState.month_options,
                                on_year_change=ReconcileState.set_ctx_year,
                                on_month_change=ReconcileState.set_ctx_month,
                            ),
                            # A client created a moment ago has no periods yet, and
                            # the engine reads its sources from data/<client>/<period>/
                            # — so offer to open the first period here instead of
                            # leaving the flow with nowhere to put an upload.
                            rx.cond(
                                ReconcileState.ctx_period == "",
                                rx.form(
                                    rx.hstack(
                                        rx.input(
                                            name="new_period",
                                            placeholder="YYYY-MM",
                                            width="110px",
                                            size="2",
                                        ),
                                        rx.button(
                                            "Open period",
                                            type="submit",
                                            size="2",
                                            variant="soft",
                                            disabled=ReconcileState.ctx_client_id == 0,
                                        ),
                                        spacing="2",
                                        align="center",
                                    ),
                                    on_submit=ReconcileState.add_period,
                                    reset_on_submit=True,
                                ),
                                rx.fragment(),
                            ),
                            rx.cond(
                                ReconcileState.period_error != "",
                                rx.text(
                                    ReconcileState.period_error,
                                    font_size="11px",
                                    color=t.Color.DANGER.value,
                                ),
                                rx.fragment(),
                            ),
                            spacing="1",
                            align="start",
                        ),
                        c.recon_type_picker(
                            ReconcileState.recon_types,
                            value=ReconcileState.ctx_recon_type,
                            on_change=ReconcileState.set_ctx_recon_type,
                        ),
                        spacing="4",
                        align="end",
                        wrap="wrap",
                    ),
                    rx.vstack(
                        rx.cond(
                            ReconcileState.can_manage_clients,
                            rx.vstack(
                                c.empty_state(
                                    "You don't have any clients yet. Create your first "
                                    "one to start reconciling.",
                                    icon="building-2",
                                ),
                                rx.button(
                                    "+ Create a client",
                                    on_click=ReconcileState.add_new_client,
                                    size="3",
                                    background=t.Color.ACCENT.value,
                                    color="#FFFFFF",
                                    border_radius="10px",
                                ),
                                spacing="3",
                                align="center",
                                width="100%",
                            ),
                            c.empty_state(
                                "No clients are assigned to you yet. Ask an Admin to "
                                "set one up before you reconcile.",
                                icon="building-2",
                            ),
                        ),
                        rx.text(
                            "Once a client exists, pick it here, open a period, and "
                            "upload its source files in Stage 2.",
                            style=t.TEXT["micro"],
                        ),
                        spacing="3",
                        width="100%",
                        align="center",
                    ),
                ),
                rx.cond(
                    ReconcileState.context_complete,
                    c.info_banner(
                        f"Context: {ReconcileState.ctx_client_name} · {ReconcileState.ctx_period} · "
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
                    ReconcileState.context_complete,
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
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Stage 2 — Upload
# ---------------------------------------------------------------------------


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


# The rows Stage 2 shows, per recon type — literal tuples rather than the
# slots list. `rx.upload` is a memoized component and Reflex hoists its props
# out of the render scope, so a row's file input cannot take a Var belonging to
# a `rx.foreach` item. Static rows keep every prop of that widget a literal.
_ROWS_BY_RECON_TYPE: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (recon_type, tuple(discovery.slot_types_for(recon_type)))
    for recon_type in discovery.RECON_TYPES
)


def _row_border(s) -> rx.Var:
    """A row's outline: dashed and inert while it waits, solid and tinted in
    the colour of its own verdict once it has one."""
    return rx.match(
        s.state,
        ("uploading", f"1px solid {t.Color.ACCENT.value}"),
        ("ok", f"1px solid {t.Color.RULE.value}"),
        ("reused", f"1px solid {t.Color.RULE.value}"),
        ("review", f"1px solid {t.Color.AI.value}"),
        ("partial", f"1px solid {t.Color.AI.value}"),
        ("wrong", f"1px solid {t.Color.AI.value}"),
        ("not_ingested", f"1px solid {t.Color.AI.value}"),
        ("failed", f"1px solid {t.Color.DANGER.value}"),
        f"1px dashed {t.Color.BORDER.value}",
    )


def _row_tint(s) -> rx.Var:
    """A wash of the row's OWN token colour, so the fill can never drift from
    the border it sits behind."""
    return rx.match(
        s.state,
        ("uploading", f"{t.Color.ACCENT.value}0A"),
        ("ok", f"{t.Color.RULE.value}0D"),
        ("reused", f"{t.Color.RULE.value}0D"),
        ("review", f"{t.Color.AI.value}0D"),
        ("partial", f"{t.Color.AI.value}0D"),
        ("wrong", f"{t.Color.AI.value}0D"),
        ("not_ingested", f"{t.Color.AI.value}0D"),
        ("failed", f"{t.Color.DANGER.value}0A"),
        t.Color.SURFACE.value,
    )


def _tick(color: str) -> rx.Component:
    """A solid disc with a check in it — the "this is done" lead."""
    return rx.center(
        rx.icon("check", size=11, color="#FFFFFF"),
        width="17px",
        height="17px",
        border_radius="999px",
        background=color,
        flex_shrink="0",
    )


def _mark(glyph: str, color: str) -> rx.Component:
    """A hollow disc with a mark in it — "needs a look" and "couldn't read it".
    The glyph carries the meaning, so the state never reads by colour alone."""
    return rx.center(
        rx.text(glyph, font_size="10px", font_weight="700", color=color),
        width="17px",
        height="17px",
        border_radius="999px",
        border=f"1.5px solid {color}",
        background=t.Color.SURFACE.value,
        flex_shrink="0",
    )


def _row_lead(s) -> rx.Component:
    """The status icon at the head of the row."""
    return rx.box(
        rx.match(
            s.state,
            ("uploading", rx.spinner(size="1")),
            ("ok", _tick(t.Color.RULE.value)),
            # The mockup's card 6: a green row whose tick is the ACCENT one, so
            # "we reused a mapping" never reads as "we read it just now".
            ("reused", _tick(t.Color.ACCENT.value)),
            ("review", _mark("!", t.Color.AI.value)),
            ("partial", _mark("!", t.Color.AI.value)),
            ("wrong", _mark("!", t.Color.AI.value)),
            ("not_ingested", _mark("!", t.Color.AI.value)),
            ("failed", _mark("\u2715", t.Color.DANGER.value)),
            rx.fragment(),
        ),
        width="19px",
        flex_shrink="0",
        display="flex",
        justify_content="center",
    )


def _row_button(label, on_click, *, primary: bool = False, busy=None, **props) -> rx.Component:
    """A row action. Primary = the one thing this state wants; secondary =
    bordered text, which is what the destructive and re-run actions stay."""
    if primary:
        props.setdefault("background", t.Color.ACCENT.value)
        props.setdefault("color", "#FFFFFF")
    else:
        props.setdefault("variant", "soft")
        props.setdefault("background", "transparent")
        props.setdefault("color", t.Color.TEXT_SECONDARY.value)
        props.setdefault("border", f"1px solid {t.Color.BORDER.value}")
        props.setdefault("border_radius", "9px")
    if busy is not None:
        props["disabled"] = busy
    return rx.button(label, on_click=on_click, size="1", **props)


def _primary_action(source_type: str, s) -> rx.Component:
    """The state's own leading action, or nothing when the row's only move is
    to choose a different file.

    A model re-run is a live call that discards the cached mapping, so it is
    NOT this — it stays behind its own confirm dialog (see `_rerun_dialog`).
    """
    return rx.match(
        s.state,
        (
            "not_ingested",
            _row_button(
                "Normalize it now",
                ReconcileState.normalize_slot(source_type),
                primary=True,
                busy=s.busy,
            ),
        ),
        # "Try again" and "Normalize it now" are the same work on the same
        # file — only the reason differs — so they share one handler.
        (
            "failed",
            _row_button(
                "Try again",
                ReconcileState.normalize_slot(source_type),
                primary=True,
                busy=s.busy,
            ),
        ),
        (
            "wrong",
            rx.cond(
                s.suggested_source_type != "",
                _row_button(
                    f"Move it to the {s.suggested_label} row",
                    ReconcileState.move_slot(source_type, s.suggested_source_type),
                    primary=True,
                    busy=s.busy,
                ),
                # No suggestion (the file was not identifiable at all): there is
                # no row to name, and "keep it here anyway" is deliberately not
                # offered — it would let a mismatched file count as supplied.
                rx.fragment(),
            ),
        ),
        (
            "review",
            _row_button(
                s.primary_label,
                ReconcileState.toggle_row_detail(source_type),
                primary=True,
                busy=s.busy,
            ),
        ),
        (
            "partial",
            _row_button(
                s.primary_label,
                ReconcileState.toggle_row_detail(source_type),
                primary=True,
                busy=s.busy,
            ),
        ),
        rx.fragment(),
    )


def _upload_row(source_type: str) -> rx.Component:
    """One document this run needs, as its own row.

    Choosing a file here uploads AND normalizes it in one action — the whole
    job the old "Upload & normalize" button did, with nothing to pick first,
    because the row already names the document. The row then reports what
    normalize found.
    """
    s = ReconcileState.slot_rows[source_type]
    return rx.hstack(
        _row_lead(s),
        rx.vstack(
            # The document title and its side pill lead EVERY row, so the title
            # never has to wrap around a file name that appears later.
            rx.hstack(
                rx.text(s.label, style=t.TEXT["card_title"]),
                rx.cond(
                    s.is_optional,
                    c.pill("Books side · optional", variant="accent"),
                    rx.cond(
                        s.is_books,
                        c.pill("Books side", variant="accent"),
                        c.pill("Portal side", variant="placeholder"),
                    ),
                ),
                spacing="2",
                align="center",
                wrap="wrap",
            ),
            # The file on its own line, so a long export name cannot push the
            # title around.
            rx.cond(
                s.selected != "",
                rx.text(
                    s.selected,
                    font_size="13px",
                    font_weight="600",
                    color=t.Color.TEXT_PRIMARY.value,
                    word_break="break-all",
                ),
                rx.fragment(),
            ),
            rx.text(s.status_text, style=t.TEXT["micro"]),
            # No percentage: a file being read and a model being asked report
            # no progress, so the bar scrolls rather than filling.
            rx.cond(s.busy, c.indeterminate_bar(width="230px"), rx.fragment()),
            # More than one file on disk for this document: the row collapses
            # to a picker rather than guessing at which one was meant. The
            # newest is already selected, so it never opens empty.
            rx.cond(
                s.files.length() > 1,
                rx.vstack(
                    rx.text(
                        "Two files on disk for this document — the newest is selected.",
                        style=t.TEXT["micro"],
                    ),
                    rx.select(
                        s.files,
                        value=s.selected,
                        on_change=lambda v: ReconcileState.select_slot_file(source_type, v),
                        width="100%",
                        placeholder="which file?",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.fragment(),
            ),
            # The unfoldable detail panel: what this file's mapping could not
            # support, or which required columns went missing.
            rx.cond(
                ReconcileState.expanded_rows.contains(source_type),
                rx.vstack(
                    rx.text(
                        "This file's gaps — and what they cost the report:",
                        style=t.TEXT["micro"],
                    ),
                    rx.foreach(
                        s.detail_lines,
                        lambda line: rx.text(f"• {line}", style=t.TEXT["micro"]),
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.fragment(),
            ),
            # Rows with no detail button of their own still state their gaps
            # plainly, rather than hiding them behind an action they don't have.
            rx.cond(
                (s.caveats.length() > 0)
                & (s.state != "review")
                & (s.state != "partial"),
                rx.vstack(
                    rx.text(
                        "This file's gaps — and what they cost the report:",
                        style=t.TEXT["micro"],
                    ),
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
            # The engine's own message, shown only where the row has no prose of
            # its own to give (an unreadable file names the actual reason). On a
            # wrong-file row the engine's sentence says the same thing as the
            # row's status line, and printing both reads as two problems.
            rx.cond(
                (s.state == "failed") & (s.message != ""),
                c.inline_reason(s.message),
                rx.fragment(),
            ),
            rx.cond(s.error != "", c.inline_reason(s.error), rx.fragment()),
            spacing="1",
            align="start",
            flex="1",
            min_width="0",
        ),
        rx.hstack(
            _primary_action(source_type, s),
            # Choosing a file is the ONLY way to upload: the row already names
            # the document, so there is no slot to pick first. While the row is
            # busy its controls are inert, so the same row cannot be started
            # twice; a not-ingested row's move is "Normalize it now" instead.
            rx.cond(
                (s.state != "uploading") & (s.state != "not_ingested"),
                rx.upload(
                    rx.hstack(
                        rx.icon("upload", size=14, color=t.Color.NEUTRAL.value),
                        rx.text(s.action_label, style=t.TEXT["micro"]),
                        spacing="1",
                        align="center",
                        padding="7px 12px",
                    ),
                    id=f"rc_up_{source_type}",
                    accept=_ACCEPT,
                    multiple=False,
                    disabled=s.busy,
                    # `rx.upload` defaults to a 5em-padded, centre-aligned DROP
                    # ZONE. Here it has to read as one more action button in the
                    # row's cluster, so the padding and centring are explicitly
                    # overridden (and the border made solid to match its
                    # siblings — the row itself already carries the dashed
                    # "waiting" outline).
                    padding="0",
                    text_align="left",
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                    background="transparent",
                    cursor="pointer",
                    on_drop=ReconcileState.upload_slot(
                        source_type, rx.upload_files(upload_id=f"rc_up_{source_type}")
                    ),
                ),
                rx.fragment(),
            ),
            # A model re-run is a live call that discards the cached mapping, so
            # it stays behind its own confirm dialog. `is_note` (not
            # `is_optional`) gates it: a note register is parsed
            # deterministically, so there is no model to re-run.
            rx.cond(
                (s.selected != "")
                & (~s.is_note)
                & (
                    (s.state == "ok")
                    | (s.state == "reused")
                    | (s.state == "review")
                    | (s.state == "partial")
                ),
                _row_button(
                    rx.hstack(
                        rx.icon("refresh-cw", size=14),
                        rx.text("Re-run through model"),
                        spacing="1",
                        align="center",
                    ),
                    ReconcileState.open_rerun(source_type, s.selected),
                    busy=s.busy,
                ),
                rx.fragment(),
            ),
            rx.cond(
                (s.state != "waiting")
                & (s.state != "optional")
                & (s.state != "uploading")
                & (s.state != "wrong"),
                _row_button(
                    "Remove",
                    ReconcileState.remove_slot(source_type),
                    variant="ghost",
                    border="none",
                    color=t.Color.TEXT_SECONDARY.value,
                    busy=s.busy,
                ),
                rx.fragment(),
            ),
            spacing="2",
            align="center",
            wrap="wrap",
            justify="end",
            flex_shrink="0",
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="10px 12px",
        border=_row_border(s),
        background=_row_tint(s),
        border_radius="10px",
    )


def _upload_rows(source_types: tuple[str, ...]) -> rx.Component:
    """One column, one row per document — never a grid.

    Row order is `slot_types_for`'s: everything required first (books, then the
    portals), the optional note register last.
    """
    return rx.vstack(
        *(_upload_row(source_type) for source_type in source_types),
        spacing="2",
        width="100%",
    )


def _upload_stage() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "2. Upload",
                "Every file this run needs gets its own row. Choose the file and it's "
                "uploaded and normalized straight away — the row then tells you what it found.",
            ),
            _rerun_dialog(),
            rx.cond(
                ReconcileState.ctx_recon_type != "",
                rx.match(
                    ReconcileState.ctx_recon_type,
                    *((recon_type, _upload_rows(types)) for recon_type, types in _ROWS_BY_RECON_TYPE),
                    _upload_rows(_ROWS_BY_RECON_TYPE[0][1]),
                ),
                c.info_banner(
                    "Pick a client, a period and a recon type in Stage 1 to see the files this run needs."
                ),
            ),
            # Blockers first — they name the specific problem ("holds a file
            # that belongs in another row"), where "still needed" only names
            # the document.
            rx.cond(
                ReconcileState.blockers.length() > 0,
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
                rx.fragment(),
            ),
            rx.cond(
                ReconcileState.still_needed != "",
                rx.text(
                    ReconcileState.still_needed,
                    font_size="12px",
                    font_weight="600",
                    color=t.Color.AI_ON.value,
                ),
                rx.fragment(),
            ),
            # Always visible, so the way forward is never hidden — it is simply
            # disabled until the rows say the run can go.
            rx.button(
                "Continue to reconcile →",
                on_click=ReconcileState.continue_forward,
                disabled=~ReconcileState.ready,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
                _disabled={
                    "background": t.Color.NEUTRAL_BG.value,
                    "color": t.Color.TEXT_MUTED.value,
                    "cursor": "not-allowed",
                },
            ),
            spacing="4",
            align="start",
            width="100%",
        ),
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
            rx.vstack(
                c.card(
                    rx.vstack(
                        c.section_title("What goes into this report"),
                        rx.text(
                            f"Run: {ReconcileState.run_id} · Context: {ReconcileState.ctx_client_name} · "
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
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                # HTML / PDF / Excel — the same figures the Review screen shows.
                rc.report_export(),
                spacing="4",
                width="100%",
                align="start",
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
            c.ai_progress(
                ReconcileState.busy_label,
                visible=ReconcileState.busy_label != "",
            ),
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
            # One global loader: any background step (a live mapping call, the
            # matching engine, the AI read, a report render) shows here so the
            # screen never looks frozen.
            c.ai_progress(
                ReconcileState.busy_label,
                visible=ReconcileState.busy_label != "",
            ),
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
