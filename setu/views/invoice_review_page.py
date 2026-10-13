"""Phase 5a — the v2 review flow: the Files view and the review screen.

Presentation only, in the shape the approved mockup
(``mockups/invoice-review-v3-2.html``) lays out: a top bar that says where you
are and takes you to the next thing that needs you, a document header that names
the direction and its basis, a checks strip, and then TWO panes — the page on
the left with the document's regions drawn on it, the decisions on the right.

Every value, score, reason and state comes from ``InvoiceReviewState``, which
reads the service layer. Nothing here computes a business number, decides a
category, or writes a row.

The screen is rendered by ``invoice_extract_page`` ONLY when the flag
``invoice_extract.v2.enabled`` is on; with it off the legacy page is untouched.
"""

from __future__ import annotations

import reflex as rx
from reflex_base.constants import EventTriggers
from reflex_components_core.el.elements.typography import Div

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.invoice_review_state import InvoiceReviewState as S

#: Focus target for the Enter / E / N shortcuts, focused on mount.
HOTKEY_ID = "invoice-review-hotkeys"


class _HotkeySurface(Div):
    """A focusable surface that reports Enter / E / N to the state.

    Reflex's stock ``Div`` does not expose ``onKeyDown``; re-declare the trigger
    with a one-argument spec so only ``event.key`` crosses the wire — the same
    pattern the Reconcile review drawer already uses for J/K/R/Esc.
    """

    @classmethod
    def get_event_triggers(cls):
        return {**super().get_event_triggers(), EventTriggers.ON_KEY_DOWN: lambda e: [e.key]}


# ---------------------------------------------------------------------------
# Small local pieces
# ---------------------------------------------------------------------------


def _quiet_label(text) -> rx.Component:
    return rx.text(text, style=t.TEXT["label"], color=t.Color.TEXT_SECONDARY.value)


def _status_chip(text, *, tone: str = "neutral") -> rx.Component:
    """A state chip that always SAYS what the state is — never colour alone."""
    if tone == "ok":
        background, colour, icon = "#EAF6EE", "#1F6B3B", "check"
    elif tone == "warn":
        background, colour, icon = "#FDF4E3", "#8A5A00", "triangle-alert"
    elif tone == "fail":
        background, colour, icon = "#FBEAEA", "#8C2B2B", "x"
    else:
        background, colour, icon = t.Color.SURFACE.value, t.Color.TEXT_SECONDARY.value, "circle-dot"
    return rx.hstack(
        rx.icon(icon, size=12, color=colour),
        rx.text(text, font_size="11px", font_weight="700", color=colour),
        spacing="1",
        align="center",
        padding="2px 8px",
        background=background,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="999px",
    )


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def invoice_review_flow() -> rx.Component:
    """Everything the v2 flag replaces: Files view ⇄ review screen."""
    return _HotkeySurface.create(
        rx.vstack(
            # Poll only while something is genuinely running: the timer is not
            # rendered otherwise, so an idle screen makes no calls at all.
            rx.cond(
                S.any_job_running,
                rx.moment(interval=3000, on_change=S.poll),
                rx.fragment(),
            ),
            rx.cond(
                S.open_upload_id != 0,
                invoice_review_screen(),
                invoice_files_view(),
            ),
            width="100%",
            spacing="4",
            align="start",
        ),
        # Enter / E / N bubble here from whatever holds focus. A key pressed
        # INSIDE a typed field also bubbles, which is why the handler refuses to
        # act while a decision is mid-flight (``open_reason_for``).
        id=HOTKEY_ID,
        tab_index=0,
        on_key_down=S.handle_key,
        on_mount=rx.call_script(
            f"setTimeout(() => document.getElementById('{HOTKEY_ID}')?.focus(), 60)"
        ),
        outline="none",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Files view
# ---------------------------------------------------------------------------


def _upload_bar() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "Upload",
                "Choose a client, stage the files, then queue them for reading. "
                "The pipeline reads every page, groups the pages into documents, "
                "then extracts each one.",
            ),
            rx.hstack(
                c.client_picker(
                    S.client_options,
                    value=S.upload_client_id.to_string(),
                    on_change=S.set_upload_client,
                ),
                rx.upload(
                    rx.vstack(
                        rx.icon("upload", size=18, color=t.Color.TEXT_SECONDARY.value),
                        rx.text("Drop invoices here, or click to choose", style=t.TEXT["label"]),
                        spacing="1",
                        align="center",
                    ),
                    id="v2_upload",
                    multiple=True,
                    on_drop=S.stage,
                    padding="14px 18px",
                    border=f"1px dashed {t.Color.BORDER.value}",
                    border_radius="12px",
                    cursor="pointer",
                ),
                rx.button(
                    "Upload & extract",
                    on_click=S.upload_and_start,
                    disabled=~S.can_start_pipeline,
                    size="2",
                ),
                spacing="3",
                align="center",
                wrap="wrap",
                width="100%",
            ),
            rx.cond(
                S.pending.length() > 0,
                rx.vstack(
                    rx.foreach(
                        S.pending,
                        lambda pf: c.file_preview_chip(pf, on_remove=S.remove_pending(pf.key)),
                    ),
                    rx.hstack(
                        rx.text(f"{S.pending.length()} file(s) staged", style=t.TEXT["micro"]),
                        rx.button("Clear", on_click=S.clear_pending, size="1", variant="ghost"),
                        spacing="2",
                        align="center",
                    ),
                    width="100%",
                    spacing="2",
                    align="start",
                ),
                rx.fragment(),
            ),
            rx.cond(
                S.upload_progress.length() > 0,
                rx.vstack(
                    rx.foreach(S.upload_progress, c.upload_progress_row),
                    width="100%",
                    spacing="1",
                ),
                rx.fragment(),
            ),
            rx.cond(S.upload_error != "", c.warning_banner(S.upload_error), rx.fragment()),
            rx.cond(
                S.busy_label != "",
                c.ai_progress(S.busy_label, visible=True),
                rx.fragment(),
            ),
            spacing="3",
            width="100%",
            align="start",
        ),
    )


def _job_actions(job) -> rx.Component:
    return rx.hstack(
        rx.cond(
            job.started,
            rx.button(
                "Review",
                on_click=lambda: S.open_review(job.upload_id),
                size="1",
                variant="soft",
            ),
            rx.button(
                "Extract (new pipeline)",
                on_click=lambda: S.start_pipeline(job.upload_id),
                size="1",
                variant="solid",
            ),
        ),
        rx.cond(
            job.can_resume,
            rx.button("Resume", on_click=lambda: S.resume_job(job.upload_id), size="1", variant="soft"),
            rx.fragment(),
        ),
        rx.cond(
            job.can_retry,
            rx.button(
                "Retry failed pages",
                on_click=lambda: S.retry_job(job.upload_id),
                size="1",
                variant="ghost",
            ),
            rx.fragment(),
        ),
        spacing="2",
        wrap="wrap",
    )


def _job_row(job) -> rx.Component:
    return rx.box(
        rx.hstack(
            rx.vstack(
                rx.hstack(
                    rx.text(job.filename, style=t.TEXT["body"], font_weight="600"),
                    rx.cond(job.running, _status_chip("Working", tone="warn"),
                            rx.cond(job.can_resume | job.can_retry,
                                    _status_chip(job.status_label, tone="warn"),
                                    rx.cond(job.started, _status_chip(job.status_label, tone="ok"),
                                            _status_chip(job.status_label)))),
                    spacing="2",
                    align="center",
                    wrap="wrap",
                ),
                rx.text(job.progress_label, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
                rx.cond(
                    job.summary_label != "",
                    rx.text(job.summary_label, style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                rx.cond(
                    job.error != "",
                    rx.text(job.error, style=t.TEXT["micro"], color="#8C2B2B"),
                    rx.fragment(),
                ),
                spacing="1",
                align="start",
            ),
            rx.spacer(),
            _job_actions(job),
            width="100%",
            align="center",
            spacing="3",
        ),
        padding="12px 14px",
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="12px",
        width="100%",
    )


def invoice_files_view() -> rx.Component:
    return rx.vstack(
        c.page_header(
            "Invoice Extraction & Digitalization",
            "The v2 pipeline reads every page, groups them into documents, extracts each "
            "one, and asks you only about what it cannot settle.",
        ),
        # Amendment 5: name the models on THIS screen, and warn when the two
        # reads share a provider (agreement then proves less).
        c.card(
            rx.vstack(
                rx.text(S.models_line, style=t.TEXT["micro"], font_weight="600"),
                rx.cond(
                    S.models_warning != "",
                    rx.hstack(
                        rx.icon("triangle-alert", size=12, color="#8A5A00"),
                        rx.text(S.models_warning, style=t.TEXT["micro"], color="#8A5A00"),
                        spacing="1",
                        align="center",
                    ),
                    rx.fragment(),
                ),
                spacing="1",
                width="100%",
                align="start",
            ),
        ),
        rx.cond(S.flash != "", c.info_banner(S.flash), rx.fragment()),
        rx.cond(S.error != "", c.inline_reason(S.error), rx.fragment()),
        rx.cond(S.v2_reason != "", c.warning_banner(S.v2_reason), rx.fragment()),
        _upload_bar(),
        c.card(
            rx.vstack(
                c.section_title("Files", "Progress is saved as it goes — leaving the page is safe."),
                rx.cond(
                    S.upload_client_id == 0,
                    c.empty_state("Choose a client to see their files.", icon="users"),
                    rx.cond(
                        S.jobs.length() > 0,
                        rx.vstack(rx.foreach(S.jobs, _job_row), width="100%", spacing="2"),
                        c.empty_state("No files for this client yet.", icon="file-text"),
                    ),
                ),
                spacing="3",
                width="100%",
                align="start",
            ),
        ),
        width="100%",
        spacing="4",
        align="start",
    )


# ---------------------------------------------------------------------------
# The review screen
# ---------------------------------------------------------------------------


def _direction_card() -> rx.Component:
    """The direction card — shown whenever the rules say a person must settle it."""
    return rx.cond(
        S.doc_needs_a_human,
        c.card(
            rx.vstack(
                rx.hstack(
                    _status_chip("Needs you", tone="warn"),
                    rx.text("Which way does this invoice go?", style=t.TEXT["body"], font_weight="700"),
                    spacing="2",
                    align="center",
                ),
                rx.cond(
                    S.doc_direction_basis != "",
                    rx.text(
                        f"Rule says: {S.doc_direction_label} — {S.doc_direction_basis}",
                        style=t.TEXT["micro"],
                        color=t.Color.TEXT_SECONDARY.value,
                    ),
                    rx.text(f"Rule says: {S.doc_direction_label}", style=t.TEXT["micro"]),
                ),
                rx.hstack(
                    rx.button(
                        "This is a purchase (inward)",
                        on_click=lambda: S.settle_direction("purchase"),
                        size="2",
                    ),
                    rx.button(
                        "This is a sale (outward)",
                        on_click=lambda: S.settle_direction("sales"),
                        size="2",
                        variant="soft",
                    ),
                    spacing="2",
                    wrap="wrap",
                ),
                rx.cond(
                    S.doc_alias_text != "",
                    rx.vstack(
                        rx.checkbox(
                            "Remember this name as an alias for this client",
                            checked=S.remember_alias,
                            on_change=S.set_remember_alias,
                            size="1",
                        ),
                        rx.cond(
                            S.doc_alias_refusal != "",
                            rx.text(
                                S.doc_alias_refusal,
                                style=t.TEXT["micro"],
                                color=t.Color.TEXT_SECONDARY.value,
                            ),
                            rx.fragment(),
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.fragment(),
                ),
                spacing="2",
                width="100%",
                align="start",
            ),
        ),
        rx.fragment(),
    )


def _handwriting_note(card) -> rx.Component:
    return rx.cond(
        card.has_handwriting,
        rx.box(
            rx.text(
                "A handwritten figure overlaps this value — confirm which is the printed amount.",
                style=t.TEXT["micro"],
                font_weight="600",
                color="#8A5A00",
            ),
            rx.text(
                "Written on the document: “" + card.handwriting_text + "”  ·  that figure is "
                "never read as a value here.",
                style=t.TEXT["micro"],
            ),
            background="#FDF4E3",
            border="1px solid #F0D9AE",
            border_radius="10px",
            padding="8px 10px",
            width="100%",
        ),
        rx.fragment(),
    )


def _crop(card) -> rx.Component:
    """The field's own box, cropped out of the upright page (server-side)."""
    return rx.cond(
        card.crop_src != "",
        rx.box(
            rx.image(
                src=card.crop_src,
                max_height="86px",
                width="auto",
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="6px",
                background=t.Color.SURFACE.value,
            ),
            flex_shrink="0",
            on_click=lambda: S.focus_field(card.field_key),
            cursor="pointer",
            title="crop of this field on the page",
        ),
        rx.fragment(),
    )


def _card_row(card) -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.hstack(
                        _status_chip(card.title, tone="warn"),
                        spacing="2",
                        align="center",
                        wrap="wrap",
                    ),
                    rx.cond(
                        card.label != "",
                        rx.text(card.label, style=t.TEXT["body"], font_weight="700"),
                        rx.fragment(),
                    ),
                    rx.cond(
                        card.reason != "",
                        rx.text(card.reason, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
                        rx.fragment(),
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.spacer(),
                _crop(card),
                width="100%",
                align="start",
                spacing="3",
            ),
            _handwriting_note(card),
            rx.cond(
                card.kind == "check_failed",
                rx.hstack(
                    rx.button(
                        "Accept the figures as read",
                        on_click=lambda: S.confirm_card(card.field_key),
                        size="1",
                        variant="soft",
                    ),
                    rx.button(
                        "Mark not applicable here",
                        on_click=lambda: S.not_applicable_card(card.field_key, "not_applicable_here"),
                        size="1",
                        variant="ghost",
                    ),
                    spacing="2",
                    wrap="wrap",
                ),
                rx.vstack(
                    rx.input(
                        placeholder="The printed value",
                        value=S.field_inputs[card.field_key],
                        on_change=lambda v: S.set_field_input(v, card.field_key),
                        on_focus=lambda: S.focus_field(card.field_key),
                        size="2",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.button(
                            "Confirm value",
                            on_click=lambda: S.confirm_card(card.field_key),
                            size="1",
                        ),
                        rx.button(
                            "Not printed on the document",
                            on_click=lambda: S.reject_card(card.field_key, "not_on_document"),
                            size="1",
                            variant="soft",
                        ),
                        rx.button(
                            "Not applicable here",
                            on_click=lambda: S.not_applicable_card(card.field_key, "not_applicable_here"),
                            size="1",
                            variant="ghost",
                        ),
                        spacing="2",
                        wrap="wrap",
                    ),
                    spacing="2",
                    width="100%",
                    align="start",
                ),
            ),
            spacing="2",
            width="100%",
            align="start",
        ),
        padding="12px 14px",
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="12px",
        background="#FFFDF8",
        width="100%",
    )


def _checks_strip() -> rx.Component:
    return c.card(
        rx.vstack(
            _quiet_label("Checks on this document"),
            rx.hstack(
                rx.foreach(
                    S.checks,
                    lambda chk: rx.hstack(
                        rx.icon(
                            rx.cond(chk.ok, "check", "x"),
                            size=12,
                            color=rx.cond(chk.ok, "#1F6B3B", "#8C2B2B"),
                        ),
                        rx.text(
                            chk.label,
                            font_size="11px",
                            color=rx.cond(chk.ok, t.Color.TEXT_SECONDARY.value, "#8C2B2B"),
                            font_weight=rx.cond(chk.ok, "500", "700"),
                        ),
                        spacing="1",
                        align="center",
                        padding="2px 6px",
                        border_radius="6px",
                        background=rx.cond(chk.ok, "transparent", "#FBEAEA"),
                    ),
                ),
                spacing="3",
                wrap="wrap",
            ),
            spacing="2",
            width="100%",
            align="start",
        ),
    )


def _settled_row(row) -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.text(row.label, style=t.TEXT["micro"], font_weight="600"),
                rx.spacer(),
                rx.cond(
                    row.score_label != "",
                    rx.text(row.score_label, style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value),
                    rx.fragment(),
                ),
                rx.tooltip(
                    rx.icon("info", size=12, color=t.Color.TEXT_MUTED.value),
                    content=row.why,
                ),
                rx.cond(
                    row.tag == "derived",
                    _status_chip("Derived", tone="neutral"),
                    rx.fragment(),
                ),
                rx.cond(
                    S.open_read_only,
                    rx.fragment(),
                    rx.icon_button(
                        rx.icon("pencil", size=12),
                        on_click=lambda: S.open_reason(row.field_key),
                        size="1",
                        variant="ghost",
                    ),
                ),
                spacing="2",
                align="center",
                width="100%",
            ),
            rx.text(row.value, style=t.TEXT["body"]),
            rx.cond(
                row.edited_was != "",
                rx.text(
                    f"edited — was {row.edited_was}",
                    style=t.TEXT["micro"],
                    color=t.Color.TEXT_SECONDARY.value,
                ),
                rx.fragment(),
            ),
            rx.cond(
                row.handwriting_text != "",
                rx.text(
                    f"A handwritten “{row.handwriting_text}” overlaps this value.",
                    style=t.TEXT["micro"],
                    color="#8A5A00",
                ),
                rx.fragment(),
            ),
            rx.cond(
                S.open_reason_for == row.field_key,
                rx.vstack(
                    rx.hstack(
                        rx.button(
                            "Correct as read",
                            on_click=lambda: S.confirm_settled(row.field_key, "correct"),
                            size="1",
                            variant="soft",
                        ),
                        rx.button(
                            "OCR slipped",
                            on_click=lambda: S.confirm_settled(row.field_key, "ocr_error"),
                            size="1",
                            variant="ghost",
                        ),
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.hstack(
                        rx.input(
                            value=S.edit_value,
                            on_change=S.set_edit_value,
                            size="1",
                            placeholder="the right value",
                        ),
                        rx.button(
                            "Log the correction",
                            on_click=lambda: S.edit_settled(row.field_key, "correct"),
                            size="1",
                        ),
                        rx.button("Cancel", on_click=S.cancel_reason, size="1", variant="ghost"),
                        spacing="2",
                        wrap="wrap",
                    ),
                    spacing="2",
                    width="100%",
                    align="start",
                ),
                rx.fragment(),
            ),
            spacing="1",
            width="100%",
            align="start",
        ),
        padding="8px 10px",
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="10px",
        width="100%",
    )


def _right_pane() -> rx.Component:
    return rx.vstack(
        rx.cond(
            S.cards.length() > 0,
            rx.vstack(
                _quiet_label("Needs you"),
                rx.foreach(S.cards, _card_row),
                width="100%",
                spacing="2",
                align="start",
            ),
            rx.fragment(),
        ),
        _direction_card(),
        c.card(
            rx.vstack(
                rx.hstack(
                    _quiet_label("Settled"),
                    rx.spacer(),
                    rx.hstack(
                        rx.text("Sort", style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value),
                        rx.switch(
                            checked=S.settled_document_order,
                            on_change=S.set_settled_order,
                            size="1",
                        ),
                        rx.text("invoice order", style=t.TEXT["micro"]),
                        spacing="2",
                        align="center",
                    ),
                    rx.button(
                        rx.cond(S.settled_collapsed, "Show", "Hide"),
                        on_click=S.toggle_settled,
                        size="1",
                        variant="ghost",
                    ),
                    width="100%",
                    align="center",
                ),
                rx.cond(
                    S.settled_collapsed,
                    rx.text(
                        f"{S.settled.length()} value(s) settled",
                        style=t.TEXT["micro"],
                        color=t.Color.TEXT_SECONDARY.value,
                    ),
                    rx.grid(
                        rx.foreach(S.settled, _settled_row),
                        columns="2",
                        spacing="2",
                        width="100%",
                    ),
                ),
                spacing="2",
                width="100%",
                align="start",
            ),
        ),
        rx.cond(
            S.not_applicable.length() > 0,
            c.card(
                rx.vstack(
                    _quiet_label("Not applicable — put back only if it IS printed here"),
                    rx.foreach(
                        S.not_applicable,
                        lambda row: rx.hstack(
                            rx.text(row.label, style=t.TEXT["micro"], font_weight="600"),
                            rx.text(f"· {row.reason}", style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value),
                            rx.spacer(),
                            rx.cond(
                                S.open_read_only,
                                rx.fragment(),
                                rx.button(
                                    "Override",
                                    on_click=lambda: S.override_to_required(row.field_key),
                                    size="1",
                                    variant="ghost",
                                ),
                            ),
                            width="100%",
                            align="center",
                            spacing="2",
                        ),
                    ),
                    spacing="2",
                    width="100%",
                    align="start",
                ),
            ),
            rx.fragment(),
        ),
        rx.cond(
            S.derived.length() > 0,
            c.card(
                rx.vstack(
                    _quiet_label("Derived (calculated, never read — no score)"),
                    rx.foreach(
                        S.derived,
                        lambda row: rx.hstack(
                            rx.text(row.label, style=t.TEXT["micro"], font_weight="600"),
                            rx.text(row.value, style=t.TEXT["micro"]),
                            rx.spacer(),
                            _status_chip("Derived"),
                            width="100%",
                            align="center",
                            spacing="2",
                        ),
                    ),
                    spacing="2",
                    width="100%",
                    align="start",
                ),
            ),
            rx.fragment(),
        ),
        width="100%",
        spacing="3",
        align="start",
    )


def _viewer_pane() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.hstack(
                rx.foreach(
                    S.view_page_chips,
                    lambda chip: _status_chip(chip.label),
                ),
                spacing="2",
                wrap="wrap",
            ),
            rx.spacer(),
            rx.button(
                rx.icon("crosshair", size=13),
                "Jump to the field's page",
                on_click=S.jump_to_field_page,
                size="1",
                variant="soft",
            ),
            width="100%",
            align="center",
            spacing="2",
        ),
        c.file_viewer_panel(
            title_var=S.open_filename,
            kind_var="image",
            image_src_var=S.viewer_image_src,
            page_var=S.view_page,
            page_count_var=S.view_page_count,
            table_headers_var=S.empty_headers,
            table_rows_var=S.empty_rows,
            text_var="",
            download_name_var=S.open_filename,
            download_href_var="",
            on_close=S.back_to_files,
            on_page_prev=S.view_page_prev,
            on_page_next=S.view_page_next,
            zoom_var=S.viewer_zoom,
            on_zoom_in=S.viewer_zoom_in,
            on_zoom_out=S.viewer_zoom_out,
            on_zoom_reset=S.viewer_zoom_reset,
            regions_var=S.view_regions,
        ),
        width="100%",
        spacing="3",
        align="start",
    )


def _document_header() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text(S.open_filename, style=t.TEXT["body"], font_weight="700"),
                rx.text(f"· {S.open_client_label}", style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
                rx.spacer(),
                rx.cond(
                    S.open_read_only,
                    _status_chip("Read-only — exported batch", tone="warn"),
                    rx.fragment(),
                ),
                width="100%",
                align="center",
                spacing="2",
            ),
            rx.hstack(
                _status_chip(S.doc_label),
                rx.cond(
                    S.doc_needs_a_human,
                    _status_chip(S.doc_direction_label, tone="warn"),
                    _status_chip(S.doc_direction_label, tone="ok"),
                ),
                _status_chip(S.doc_category_label),
                spacing="2",
                wrap="wrap",
            ),
            rx.cond(
                S.doc_direction_basis != "",
                rx.text(S.doc_direction_basis, style=t.TEXT["micro"], color=t.Color.TEXT_SECONDARY.value),
                rx.fragment(),
            ),
            rx.text(S.page_line, style=t.TEXT["micro"]),
            rx.cond(S.page_tag != "", _status_chip(S.page_tag), rx.fragment()),
            rx.cond(
                S.open_read_only,
                rx.text(S.open_read_only_note, style=t.TEXT["micro"], color="#8A5A00"),
                rx.fragment(),
            ),
            spacing="2",
            width="100%",
            align="start",
        ),
    )


def _document_controls() -> rx.Component:
    return c.card(
        rx.hstack(
            rx.cond(
                S.open_read_only,
                rx.fragment(),
                rx.hstack(
                    rx.text("Move to", style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value),
                    rx.select(
                        ["tax_invoice", "no_gst_expense", "supporting", "not_a_voucher"],
                        value=S.doc_category,
                        on_change=S.recategorise,
                        size="1",
                    ),
                    rx.cond(
                        S.doc_label != "",
                        rx.hstack(
                            rx.button(
                                "Exclude this document",
                                on_click=lambda: S.exclude_document("review screen: excluded by a person"),
                                size="1",
                                variant="ghost",
                            ),
                            rx.button(
                                "Put it back in",
                                on_click=S.include_document,
                                size="1",
                                variant="ghost",
                            ),
                            spacing="2",
                        ),
                        rx.fragment(),
                    ),
                    spacing="2",
                    align="center",
                    wrap="wrap",
                ),
            ),
            rx.spacer(),
            rx.text(S.review_counter, style=t.TEXT["micro"]),
            rx.cond(
                S.open_read_only,
                rx.fragment(),
                rx.button(
                    "Mark reviewed",
                    on_click=S.mark_reviewed,
                    disabled=~S.can_mark_reviewed,
                    size="1",
                ),
            ),
            spacing="3",
            align="center",
            width="100%",
            wrap="wrap",
        ),
    )


def invoice_review_screen() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.button(
                rx.icon("arrow-left", size=13),
                "Files",
                on_click=S.back_to_files,
                size="1",
                variant="ghost",
            ),
            _status_chip(f"Document #{S.open_ordinal} of {S.document_count}"),
            rx.button(
                rx.icon("chevron-left", size=13),
                on_click=S.prev_document,
                disabled=~S.can_go_prev,
                size="1",
                variant="soft",
            ),
            rx.button(
                rx.icon("chevron-right", size=13),
                on_click=S.next_document,
                disabled=~S.can_go_next,
                size="1",
                variant="soft",
            ),
            rx.button(
                "Next issue",
                on_click=S.next_issue,
                disabled=~S.has_next_issue,
                size="1",
            ),
            rx.button(
                "Documents",
                on_click=S.toggle_drawer,
                size="1",
                variant="soft",
            ),
            rx.spacer(),
            rx.cond(S.flash != "", rx.text(S.flash, style=t.TEXT["micro"]), rx.fragment()),
            width="100%",
            align="center",
            spacing="2",
            wrap="wrap",
        ),
        rx.cond(S.error != "", c.inline_reason(S.error), rx.fragment()),
        rx.cond(S.refusal != "", c.warning_banner(S.refusal), rx.fragment()),
        rx.cond(
            S.drawer_open,
            c.card(
                rx.vstack(
                    _quiet_label("Documents in this file"),
                    rx.foreach(
                        S.documents,
                        lambda doc: rx.hstack(
                            rx.button(
                                doc.label,
                                on_click=lambda: S.open_document(doc.document_id),
                                size="1",
                                variant=rx.cond(doc.document_id == S.current_document_id, "solid", "ghost"),
                            ),
                            _status_chip(doc.category_label),
                            rx.cond(doc.needs_you > 0, _status_chip(f"{doc.needs_you} need you", tone="warn"),
                                    rx.fragment()),
                            rx.cond(doc.excluded, _status_chip("excluded"), rx.fragment()),
                            rx.cond(doc.reviewed, _status_chip("reviewed", tone="ok"), rx.fragment()),
                            rx.spacer(),
                            rx.text(
                                rx.cond(doc.pages.length() > 0, "has pages", "no page regions"),
                                style=t.TEXT["micro"],
                                color=t.Color.TEXT_MUTED.value,
                            ),
                            width="100%",
                            align="center",
                            spacing="2",
                        ),
                    ),
                    spacing="2",
                    width="100%",
                    align="start",
                ),
            ),
            rx.fragment(),
        ),
        _document_header(),
        _checks_strip(),
        rx.hstack(
            rx.box(_viewer_pane(), width="52%"),
            rx.box(_right_pane(), width="48%"),
            spacing="4",
            width="100%",
            align="start",
        ),
        _document_controls(),
        width="100%",
        spacing="4",
        align="start",
    )
