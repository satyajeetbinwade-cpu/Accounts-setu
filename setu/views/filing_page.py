"""C2 — Portal Connect & Filing.

Filing Calendar (statutory due dates from C1), Filings (prepare → Send →
outcome → acknowledgement), Connection & Sources (API/manual toggle, dormant
until C4 credentials). Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.filing_state import FilingState
from setu.views import shell

_HUB = ["Filing Calendar", "Filings", "Connection & Sources"]

_ACCEPT = {
    "application/pdf": [".pdf"],
    "image/jpeg": [".jpg", ".jpeg"],
    "image/png": [".png"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
    "text/csv": [".csv"],
    "application/zip": [".zip"],
    "application/json": [".json"],
}


def _hub_button(label: str) -> rx.Component:
    active = FilingState.section == label
    return rx.button(
        label,
        on_click=FilingState.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _status_pill(status, label) -> rx.Component:
    """Outcome pill — variant must be a Python literal, so branch with rx.match."""
    return rx.match(
        status,
        ("acknowledged", c.pill(label, variant="rule")),
        ("failed", c.pill(label, variant="danger")),
        ("ambiguous", c.pill(label, variant="ai")),
        ("sent", c.pill(label, variant="accent")),
        c.pill(label, variant="placeholder"),
    )


def _urgency_pill(variant, label) -> rx.Component:
    return rx.match(
        variant,
        ("danger", c.pill(label, variant="danger")),
        ("ai", c.pill(label, variant="ai")),
        ("rule", c.pill(label, variant="rule")),
        c.pill(label, variant="placeholder"),
    )


# ---------------------------------------------------------------------------
# Filing Calendar
# ---------------------------------------------------------------------------


def _calendar_row(r) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(f"{r.obligation} — {r.period}", style=t.TEXT["body"], font_weight="600"),
            rx.text(r.client_label, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.text(r.due_date, style=t.TEXT["label"], white_space="nowrap"),
        _urgency_pill(r.urgency_variant, r.urgency_label),
        width="100%",
        align="center",
        spacing="4",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _calendar() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                c.section_title(
                    "Filing Calendar",
                    "Statutory due dates per client per return type, sourced from C1.",
                ),
                rx.cond(
                    FilingState.calendar.length() > 0,
                    rx.vstack(rx.foreach(FilingState.calendar, _calendar_row), spacing="0", width="100%"),
                    c.empty_state(
                        "No calendar entries yet. Build the calendar for a client below to materialize due dates.",
                        icon="calendar",
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
                c.section_title("Build / refresh calendar"),
                rx.hstack(
                    rx.vstack(
                        rx.text("Client", style=t.TEXT["label"]),
                        rx.select(
                            FilingState.client_options.map(lambda o: o.legal_name),
                            value=FilingState.build_client_name,
                            on_change=FilingState.set_build_client_by_name,
                            width="240px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.vstack(
                        rx.text("Period", style=t.TEXT["label"]),
                        rx.input(
                            value=FilingState.build_period,
                            on_change=FilingState.set_build_period,
                            placeholder="YYYY-MM or YYYY",
                            width="180px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    spacing="3",
                    align="end",
                    wrap="wrap",
                ),
                rx.button(
                    "Build calendar",
                    on_click=FilingState.build_calendar,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
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
# Filings hub
# ---------------------------------------------------------------------------


def _filing_row(r) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(f"{r.obligation} — {r.period}", style=t.TEXT["body"], font_weight="600"),
            rx.text(r.client_label, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        _status_pill(r.status, r.status_label),
        rx.button(
            "Open",
            on_click=FilingState.open_filing(r.filing_id),
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


def _new_filing_form() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "+ New filing",
                "Prepare a filing package (generic manual upload this phase).",
            ),
            rx.hstack(
                rx.vstack(
                    rx.text("Client", style=t.TEXT["label"]),
                    rx.select(
                        FilingState.client_options.map(lambda o: o.legal_name),
                        value=FilingState.new_client_name,
                        on_change=FilingState.set_new_client_by_name,
                        width="240px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Return type", style=t.TEXT["label"]),
                    rx.select(
                        FilingState.obligation_options,
                        value=FilingState.new_obligation,
                        on_change=FilingState.set_new_obligation,
                        width="180px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Period", style=t.TEXT["label"]),
                    rx.input(
                        value=FilingState.new_period,
                        on_change=FilingState.set_new_period,
                        placeholder="YYYY-MM",
                        width="160px",
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
                    rx.text("Drag the filing package here or click to browse", style=t.TEXT["label"]),
                    rx.text("Any file this phase — retrofit to Module 2's GSTR-3B assembly later.", style=t.TEXT["micro"]),
                    spacing="2",
                    align="center",
                    padding="24px",
                ),
                id="c2_package",
                accept=_ACCEPT,
                multiple=False,
                border=f"1px dashed {t.Color.BORDER.value}",
                border_radius="12px",
                width="100%",
                background=t.Color.SURFACE.value,
            ),
            rx.hstack(
                rx.button(
                    "Prepare filing package",
                    on_click=FilingState.prepare_filing(rx.upload_files(upload_id="c2_package")),
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Clear",
                    on_click=rx.clear_selected_files("c2_package"),
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
    )


def _filings_hub() -> rx.Component:
    return rx.cond(
        FilingState.selected_filing_id != 0,
        _filing_detail(),
        rx.vstack(
            c.card(
                rx.vstack(
                    c.stat(FilingState.open_count.to_string(), "open filing(s)"),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            _new_filing_form(),
            c.card(
                rx.vstack(
                    c.section_title("Filings"),
                    rx.cond(
                        FilingState.filings.length() > 0,
                        rx.vstack(rx.foreach(FilingState.filings, _filing_row), spacing="0", width="100%"),
                        c.empty_state("No filings yet.", icon="file-text"),
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
    )


# ---------------------------------------------------------------------------
# Filing detail
# ---------------------------------------------------------------------------


def _send_control() -> rx.Component:
    return rx.vstack(
        c.section_title("Send"),
        rx.cond(
            FilingState.can_send,
            rx.vstack(
                rx.text(
                    "Sending a filing is irreversible within Setu — a correction is a fresh filing.",
                    style=t.TEXT["body"],
                ),
                rx.hstack(
                    rx.checkbox(
                        checked=FilingState.send_confirm,
                        on_change=FilingState.set_send_confirm,
                    ),
                    rx.text("I understand this Send is irreversible within Setu.", style=t.TEXT["label"]),
                    spacing="2",
                    align="center",
                ),
                rx.button(
                    "Send filing",
                    on_click=FilingState.send_filing,
                    disabled=~FilingState.send_confirm,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                    _disabled={"opacity": "0.5", "cursor": "not-allowed"},
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            rx.vstack(
                rx.button(
                    "Send filing",
                    disabled=True,
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                ),
                c.inline_reason(FilingState.send_deny_reason),
                spacing="1",
                align="start",
            ),
        ),
        spacing="3",
        align="start",
        width="100%",
    )


def _outcome_control() -> rx.Component:
    return rx.vstack(
        c.section_title("Record outcome", "What did the portal report back?"),
        rx.hstack(
            *[
                rx.button(
                    label,
                    on_click=FilingState.set_outcome_choice(value),
                    size="2",
                    variant="soft",
                    background=rx.cond(
                        FilingState.outcome_choice == value, t.Color.ACCENT.value, "transparent"
                    ),
                    color=rx.cond(FilingState.outcome_choice == value, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                    _hover={"background": rx.cond(FilingState.outcome_choice == value, t.Color.ACCENT.value, "#EEF2F8")},
                )
                for label, value in [
                    ("Acknowledged", "acknowledged"),
                    ("Ambiguous", "ambiguous"),
                    ("Failed", "failed"),
                ]
            ],
            spacing="2",
            wrap="wrap",
        ),
        rx.cond(
            FilingState.outcome_choice == "ambiguous",
            c.warning_banner(
                "Ambiguous is a near-emergency state: confirm manually whether the portal actually "
                "received it. This escalates to Manager + Partner."
            ),
            rx.fragment(),
        ),
        rx.input(
            value=FilingState.outcome_note,
            on_change=FilingState.set_outcome_note,
            placeholder="Outcome note (optional)",
            width="100%",
        ),
        rx.button(
            "Record outcome",
            on_click=FilingState.record_outcome,
            background=t.Color.ACCENT.value,
            color="#FFFFFF",
        ),
        spacing="3",
        align="start",
        width="100%",
    )


def _acknowledgement_control() -> rx.Component:
    return rx.vstack(
        c.section_title(
            "Acknowledgement capture",
            "Upload the portal's receipt through F3 as linked evidence, then confirm.",
        ),
        rx.upload(
            rx.vstack(
                rx.icon("upload", size=22, color=t.Color.NEUTRAL.value),
                rx.text("Acknowledgement receipt (PDF/screenshot)", style=t.TEXT["label"]),
                spacing="2",
                align="center",
                padding="20px",
            ),
            id="c2_ack",
            accept=_ACCEPT,
            multiple=False,
            border=f"1px dashed {t.Color.BORDER.value}",
            border_radius="12px",
            width="100%",
            background=t.Color.SURFACE.value,
        ),
        rx.hstack(
            rx.checkbox(
                checked=FilingState.ack_confirm,
                on_change=FilingState.set_ack_confirm,
            ),
            rx.text(
                "I confirm the portal receipt is attached and this filing is acknowledged.",
                style=t.TEXT["label"],
            ),
            spacing="2",
            align="center",
        ),
        rx.hstack(
            rx.button(
                "Confirm acknowledgement",
                on_click=FilingState.confirm_acknowledgement(rx.upload_files(upload_id="c2_ack")),
                disabled=~FilingState.ack_confirm,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
                _disabled={"opacity": "0.5", "cursor": "not-allowed"},
            ),
            rx.button(
                "Clear",
                on_click=rx.clear_selected_files("c2_ack"),
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
    )


def _audit_row(r) -> rx.Component:
    return rx.hstack(
        rx.hstack(
            rx.text(r.action, style=t.TEXT["body"], font_weight="600"),
            rx.text(f"{r.actor} · {r.detail}", style=t.TEXT["micro"]),
            spacing="2",
            align="baseline",
            wrap="wrap",
        ),
        rx.spacer(),
        rx.text(r.created_at, style=t.TEXT["micro"], white_space="nowrap"),
        width="100%",
        padding="8px 2px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
        align="center",
    )


def _filing_detail() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back to Filings",
            on_click=FilingState.back_to_filings,
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
                    rx.vstack(
                        rx.text(
                            f"{FilingState.detail_obligation} — {FilingState.detail_period}",
                            style=t.TEXT["card_title"],
                        ),
                        rx.text(FilingState.detail_client, style=t.TEXT["micro"]),
                        spacing="1",
                        align="start",
                    ),
                    rx.spacer(),
                    _status_pill(FilingState.detail_status, FilingState.detail_status_label),
                    width="100%",
                    align="center",
                ),
                rx.cond(
                    FilingState.detail_package_doc != "",
                    rx.text(f"Package: {FilingState.detail_package_doc}", style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                rx.cond(
                    FilingState.detail_outcome_note != "",
                    rx.text(f"Outcome note: {FilingState.detail_outcome_note}", style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                rx.cond(
                    FilingState.detail_sent_by != "",
                    rx.text(
                        f"Sent by {FilingState.detail_sent_by} · {FilingState.detail_sent_at}",
                        style=t.TEXT["micro"],
                    ),
                    rx.fragment(),
                ),
                rx.cond(
                    FilingState.detail_status == "failed",
                    c.warning_banner(
                        "This filing escalated to Manager + Partner (failed). A correction is a fresh "
                        "filing through the normal government process."
                    ),
                    rx.fragment(),
                ),
                rx.cond(
                    FilingState.detail_status == "ambiguous",
                    c.warning_banner(
                        "This filing escalated to Manager + Partner (ambiguous). Confirm manually whether "
                        "the portal actually received it — a correction is a fresh filing."
                    ),
                    rx.fragment(),
                ),
                c.divider(),
                rx.match(
                    FilingState.detail_status,
                    ("prepared", _send_control()),
                    ("sent", _outcome_control()),
                    ("failed", _acknowledgement_control()),
                    ("ambiguous", _acknowledgement_control()),
                    rx.cond(
                        FilingState.detail_status == "acknowledged",
                        c.info_banner("Acknowledged — this filing is complete."),
                        rx.fragment(),
                    ),
                ),
                c.divider(),
                rx.vstack(
                    c.section_title("Audit trail"),
                    rx.cond(
                        FilingState.audit.length() > 0,
                        rx.vstack(rx.foreach(FilingState.audit, _audit_row), spacing="0", width="100%"),
                        rx.text("No audit entries.", style=t.TEXT["micro"]),
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
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
# Connection & Sources
# ---------------------------------------------------------------------------


def _source_block(s) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text(s.label, style=t.TEXT["body"], font_weight="600"),
            rx.cond(
                s.api_available,
                c.pill("API available", variant="rule"),
                c.pill("API unavailable", variant="placeholder"),
            ),
            spacing="2",
            align="center",
        ),
        rx.cond(
            s.api_available,
            rx.hstack(
                *[
                    rx.button(
                        label,
                        on_click=FilingState.set_source_mode(s.source, value),
                        size="2",
                        variant="soft",
                        background=rx.cond(s.mode == value, t.Color.ACCENT.value, "transparent"),
                        color=rx.cond(s.mode == value, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                        _hover={"background": rx.cond(s.mode == value, t.Color.ACCENT.value, "#EEF2F8")},
                    )
                    for label, value in [("Manual", "manual"), ("API", "api")]
                ],
                spacing="2",
            ),
            rx.vstack(
                rx.hstack(
                    rx.button(
                        "Manual",
                        disabled=True,
                        size="2",
                        variant="soft",
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                        border_radius="9px",
                        opacity="0.5",
                    ),
                    rx.button(
                        "API",
                        disabled=True,
                        size="2",
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                        opacity="0.5",
                    ),
                    spacing="2",
                ),
                c.inline_reason(s.unavailable_reason),
                rx.text("Manual upload remains available regardless of this toggle.", style=t.TEXT["micro"]),
                spacing="1",
                align="start",
            ),
        ),
        spacing="2",
        align="start",
        width="100%",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _sources() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "Connection & Sources",
                "Per-client, per-source API vs manual mode. API mode is dormant until portal "
                "credentials are configured in C4; manual upload always remains available.",
            ),
            rx.vstack(
                rx.text("Client", style=t.TEXT["label"]),
                rx.select(
                    FilingState.client_options.map(lambda o: o.legal_name),
                    value=FilingState.source_client_name,
                    on_change=FilingState.set_source_client_by_name,
                    width="240px",
                ),
                spacing="1",
                align="start",
            ),
            rx.cond(
                FilingState.sources.length() > 0,
                rx.vstack(rx.foreach(FilingState.sources, _source_block), spacing="0", width="100%"),
                c.empty_state("Create a client first.", icon="users"),
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


def filing_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Portal Connect & Filing",
                "Filing calendar, explicit Send, and acknowledgement capture.",
            ),
            rx.cond(FilingState.flash != "", c.info_banner(FilingState.flash), rx.fragment()),
            rx.cond(FilingState.error != "", c.inline_reason(FilingState.error), rx.fragment()),
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
                FilingState.section,
                ("Filings", _filings_hub()),
                ("Connection & Sources", _sources()),
                _calendar(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )
