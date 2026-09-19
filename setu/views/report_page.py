"""Module 8 — Visual Reconciliation Report (presentation layer).

The report screen: KPI row → Accountant's Read → charts → detail table →
Data Quality & Scope Notes → compact integrity strip. Every chart is a real
``rx.recharts`` component coloured from Foundation tokens, and every chart
segment is click-to-filter into the detail table below it.

This is additive: it sits above the existing Action Center queue and reuses
Module 2's output unchanged.
"""

from __future__ import annotations

import reflex as rx
from reflex_base.constants import EventTriggers
from reflex_base.event import passthrough_event_spec

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.report_state import ReportState

# ---------------------------------------------------------------------------
# Clickable recharts primitives
# ---------------------------------------------------------------------------
# Reflex's stock Pie/Bar bind on_click with `no_args_event_spec`, so the
# clicked sector/bar's own data object is discarded. Recharts DOES pass that
# payload to the handler; we re-declare the trigger with a passthrough spec so
# the datum (which carries the bucket `key` and `side`) reaches the State.
# The class name is kept as "Pie"/"Bar" so the parent chart's child-validation
# still recognises them.


class _ClickablePie(rx.recharts.Pie):
    @classmethod
    def get_event_triggers(cls):
        return {
            **super().get_event_triggers(),
            EventTriggers.ON_CLICK: passthrough_event_spec(dict),
        }


_ClickablePie.__name__ = "Pie"


class _ClickableBar(rx.recharts.Bar):
    @classmethod
    def get_event_triggers(cls):
        return {
            **super().get_event_triggers(),
            EventTriggers.ON_CLICK: passthrough_event_spec(dict),
        }


_ClickableBar.__name__ = "Bar"


# ---------------------------------------------------------------------------
# Small building blocks
# ---------------------------------------------------------------------------


def _chart_card(title: str, subtitle: str, chart: rx.Component, *, empty: rx.Component | None = None) -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(title, subtitle),
            chart if empty is None else rx.cond(ReportState.loaded, chart, empty),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _kpi_card(value, label, *, on_click, active, note: str = "") -> rx.Component:
    """A clickable KPI card. Clicking filters everything below it."""
    return rx.button(
        rx.vstack(
            rx.text(
                value,
                font_size="26px",
                font_weight="700",
                letter_spacing="-0.02em",
                color=t.Color.TEXT_PRIMARY.value,
                line_height="1.1",
            ),
            rx.text(label, style=t.TEXT["label"], text_align="left"),
            rx.cond(
                note != "",
                rx.text(note, style=t.TEXT["micro"], text_align="left"),
                rx.fragment(),
            ),
            spacing="1",
            align="start",
            width="100%",
        ),
        on_click=on_click,
        variant="soft",
        background=rx.cond(active, "#EAF0FB", t.Color.SURFACE.value),
        border=rx.cond(active, f"1px solid {t.Color.ACCENT.value}", f"1px solid {t.Color.BORDER.value}"),
        border_radius=t.RADIUS,
        padding=t.CARD_PADDING,
        box_shadow=t.CARD_SHADOW,
        width="100%",
        height="100%",
        text_align="left",
        _hover={"border_color": t.Color.ACCENT.value},
    )


def _kpi_row() -> rx.Component:
    return rx.grid(
        _kpi_card(
            ReportState.match_rate,
            "Match rate",
            on_click=ReportState.filter_kpi("match"),
            active=(ReportState.active_bucket == "") & (ReportState.active_side == "") & (ReportState.active_party == ""),
            note=f"{ReportState.matched_count} of {ReportState.total_count} matched",
        ),
        _kpi_card(
            ReportState.itc_eligible,
            "ITC eligible (GST)",
            on_click=ReportState.filter_kpi("itc"),
            active=ReportState.active_side == "GST",
            note=rx.cond(ReportState.itc_eligible_available, "Eligible input tax credit", "No 2A credit figure"),
        ),
        _kpi_card(
            ReportState.net_payable,
            "Net payable",
            on_click=ReportState.filter_kpi("net"),
            active=False,
            note="No output-tax source in this run",
        ),
        _kpi_card(
            ReportState.exception_count.to_string(),
            "Exceptions",
            on_click=ReportState.filter_kpi("exceptions"),
            active=ReportState.active_bucket == "__exceptions__",
            note=f"worth {ReportState.exceptions_value}",
        ),
        columns=rx.breakpoints(initial="1", sm="2", lg="4"),
        spacing="4",
        width="100%",
    )


def _narrative() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                c.section_title(
                    "Accountant's Read",
                    "Plain-language summary. Figures are deterministic Tier-A output; only this "
                    "paragraph may be AI-drafted, and it is guard-verified.",
                ),
                rx.spacer(),
                rx.cond(
                    ReportState.narrative_source.startswith("ai"),
                    c.pill("AI-drafted · verified", variant="ai"),
                    c.pill("Deterministic", variant="rule"),
                ),
                width="100%",
                align="start",
            ),
            rx.text(ReportState.narrative_text, style=t.TEXT["body"]),
            rx.cond(
                ReportState.narrative_guard_note != "",
                c.inline_reason(ReportState.narrative_guard_note),
                rx.fragment(),
            ),
            rx.hstack(
                rx.button(
                    "Draft with AI",
                    on_click=ReportState.draft_narrative,
                    size="1",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                rx.text(
                    "The draft is rejected if it cites any figure or classification this run "
                    "did not produce.",
                    style=t.TEXT["micro"],
                ),
                spacing="2",
                align="center",
                wrap="wrap",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _classification_donut(slices, colors, side_label: str, title: str, subtitle: str) -> rx.Component:
    return _chart_card(
        title,
        subtitle,
        rx.recharts.pie_chart(
            _ClickablePie.create(
                rx.foreach(colors, lambda col: rx.recharts.cell(fill=col)),
                data=slices,
                data_key="count",
                name_key="name",
                inner_radius="55%",
                outer_radius="85%",
                on_click=ReportState.filter_bucket,
            ),
            rx.recharts.graphing_tooltip(),
            rx.recharts.legend(),
            height=260,
            width="100%",
        ),
    )


def _itc_donut() -> rx.Component:
    return _chart_card(
        "Input tax credit split (GST)",
        "Eligible vs ineligible ITC, by value.",
        rx.vstack(
            rx.recharts.pie_chart(
                _ClickablePie.create(
                    rx.foreach(ReportState.itc_slice_colors, lambda col: rx.recharts.cell(fill=col)),
                    data=ReportState.itc_slices,
                    data_key="value",
                    name_key="name",
                    inner_radius="55%",
                    outer_radius="85%",
                    on_click=ReportState.filter_bucket,
                ),
                rx.recharts.graphing_tooltip(),
                rx.recharts.legend(),
                height=260,
                width="100%",
            ),
            rx.cond(
                ReportState.itc_note != "",
                rx.text(ReportState.itc_note, style=t.TEXT["micro"]),
                rx.fragment(),
            ),
            spacing="2",
            width="100%",
        ),
    )


def _tds_deposit_bar() -> rx.Component:
    return _chart_card(
        "TDS deducted vs deposited",
        "What was deducted against what reached the government.",
        rx.recharts.bar_chart(
            _ClickableBar.create(
                rx.foreach(ReportState.tds_deposit_colors, lambda col: rx.recharts.cell(fill=col)),
                data_key="value",
                on_click=ReportState.filter_bucket,
            ),
            rx.recharts.x_axis(data_key="name"),
            rx.recharts.y_axis(),
            rx.recharts.graphing_tooltip(),
            data=ReportState.tds_deposit_bars,
            height=260,
            width="100%",
        ),
    )


def _supplier_bar() -> rx.Component:
    return _chart_card(
        "Exceptions by supplier",
        "Top parties by unmatched value — where to look first.",
        rx.recharts.bar_chart(
            _ClickableBar.create(
                data_key="value",
                fill=t.Color.DANGER.value,
                on_click=ReportState.filter_bucket,
            ),
            rx.recharts.x_axis(data_key="party", interval=0, angle=-20, text_anchor="end", height=70),
            rx.recharts.y_axis(),
            rx.recharts.graphing_tooltip(),
            data=ReportState.supplier_bars,
            height=300,
            width="100%",
        ),
    )


def _trend_bar() -> rx.Component:
    return _chart_card(
        "Period-over-period match rate",
        "This period against the prior period, where data exists.",
        rx.cond(
            ReportState.trend_available,
            rx.recharts.bar_chart(
                rx.recharts.bar(data_key="rate", fill=t.Color.ACCENT.value),
                rx.recharts.x_axis(data_key="name"),
                rx.recharts.y_axis(),
                rx.recharts.graphing_tooltip(),
                data=ReportState.trend_bars,
                height=260,
                width="100%",
            ),
            rx.vstack(
                rx.icon("trending-up", size=22, color=t.Color.NEUTRAL.value),
                rx.text(ReportState.trend_message, style=t.TEXT["label"]),
                rx.text(
                    "A trend appears once a second period for this client and reconciliation "
                    "type has been run.",
                    style=t.TEXT["micro"],
                    text_align="center",
                ),
                spacing="2",
                align="center",
                justify="center",
                width="100%",
                padding="40px 16px",
            ),
        ),
    )


def _charts_row() -> rx.Component:
    return rx.vstack(
        rx.grid(
            rx.cond(
                ReportState.has_gst,
                _classification_donut(
                    ReportState.gst_slices, ReportState.gst_slice_colors, "GST",
                    "GST classification",
                    "Matched / Amount Diff / Not in Books / Not in Portal — click a segment to filter.",
                ),
                _no_side_card("GST", "gstr2b", "GSTR-2B", "tally", "Books / Purchase Register"),
            ),
            rx.cond(
                ReportState.has_gst,
                _itc_donut(),
                _no_side_card("GST", "gstr2b", "GSTR-2B", "tally", "Books / Purchase Register"),
            ),
            columns=rx.breakpoints(initial="1", lg="2"),
            spacing="4",
            width="100%",
        ),
        rx.grid(
            rx.cond(
                ReportState.has_tds,
                _classification_donut(
                    ReportState.tds_slices, ReportState.tds_slice_colors, "TDS",
                    "TDS classification",
                    "Matched / Amount Diff / Missing / Late Deposit — click a segment to filter.",
                ),
                _no_side_card("TDS", "form26as", "Form 26AS", "tds", "TDS working / challan register"),
            ),
            rx.cond(
                ReportState.has_tds,
                _tds_deposit_bar(),
                _no_side_card("TDS", "form26as", "Form 26AS", "tds", "TDS working / challan register"),
            ),
            columns=rx.breakpoints(initial="1", lg="2"),
            spacing="4",
            width="100%",
        ),
        rx.grid(
            _supplier_bar(),
            _trend_bar(),
            columns=rx.breakpoints(initial="1", lg="2"),
            spacing="4",
            width="100%",
        ),
        spacing="4",
        width="100%",
    )


def _no_side_card(side: str, slot_a: str, label_a: str, slot_b: str, label_b: str) -> rx.Component:
    """The honest empty state for a reconciliation side with no data — it says
    what's missing and how to add it, never a blank card."""
    return c.card(
        rx.vstack(
            c.section_title(f"{side} reconciliation"),
            rx.hstack(
                rx.icon("info", size=18, color=t.Color.AI.value),
                rx.text(f"No {side} data uploaded yet for this period.", style=t.TEXT["body"], font_weight="700"),
                spacing="2",
                align="center",
            ),
            rx.text(
                f"To include {side} in this report, run a {side} reconciliation for this period "
                f"with both sides present — {label_a} (slot \u201c{slot_a}\u201d) and "
                f"{label_b} (slot \u201c{slot_b}\u201d).",
                style=t.TEXT["label"],
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Detail table + evidence modal
# ---------------------------------------------------------------------------


def _detail_row(row) -> rx.Component:
    return rx.hstack(
        rx.text(row.side, style=t.TEXT["micro"], min_width="42px"),
        rx.text(row.classification, style=t.TEXT["body"], font_weight="700", min_width="150px"),
        rx.text(row.party, style=t.TEXT["body"], flex="1", min_width="0"),
        rx.text(row.reference, style=t.TEXT["micro"], min_width="110px"),
        rx.text(row.date, style=t.TEXT["micro"], min_width="90px"),
        rx.text(row.value, style=t.TEXT["body"], min_width="110px", text_align="right"),
        rx.button(
            "Evidence",
            on_click=ReportState.open_evidence(row.result_id, row.side),
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
        padding="8px 2px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _detail_table() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                c.section_title(
                    "Detail records",
                    "Filtered by the chart segment or KPI card you clicked. Each row opens its evidence.",
                ),
                rx.spacer(),
                rx.cond(
                    ReportState.filter_label != "All records",
                    rx.hstack(
                        c.accent_pill(ReportState.filter_label),
                        rx.button(
                            "Clear",
                            on_click=ReportState.clear_filter,
                            size="1",
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="8px",
                        ),
                        spacing="2",
                        align="center",
                    ),
                    rx.fragment(),
                ),
                width="100%",
                align="center",
            ),
            rx.hstack(
                rx.text("Side", style=t.TEXT["micro"], min_width="42px"),
                rx.text("Classification", style=t.TEXT["micro"], min_width="150px"),
                rx.text("Party", style=t.TEXT["micro"], flex="1"),
                rx.text("Reference", style=t.TEXT["micro"], min_width="110px"),
                rx.text("Date", style=t.TEXT["micro"], min_width="90px"),
                rx.text("Value", style=t.TEXT["micro"], min_width="110px", text_align="right"),
                rx.text("", style=t.TEXT["micro"], min_width="70px"),
                width="100%",
                align="center",
                spacing="3",
                padding="4px 2px",
            ),
            rx.cond(
                ReportState.detail_count > 0,
                rx.vstack(
                    rx.foreach(ReportState.detail_rows, _detail_row),
                    spacing="0",
                    width="100%",
                ),
                c.empty_state("No records match this filter.", icon="circle_check"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _field_panel(title: str, fields) -> rx.Component:
    return rx.vstack(
        rx.text(title, style=t.TEXT["label"], font_weight="700"),
        rx.cond(
            fields.length() > 0,
            rx.vstack(
                rx.foreach(
                    fields,
                    lambda f: rx.hstack(
                        rx.text(f.label, style=t.TEXT["micro"], min_width="130px"),
                        rx.text(f.value, style=t.TEXT["body"]),
                        spacing="2",
                        align="baseline",
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
        flex="1",
    )


def _evidence_modal() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.hstack(
                    rx.vstack(
                        rx.text(ReportState.modal_title, style=t.TEXT["card_title"]),
                        rx.text(ReportState.modal_classification, style=t.TEXT["label"]),
                        spacing="1",
                        align="start",
                    ),
                    rx.spacer(),
                    rx.dialog.close(
                        rx.button(
                            rx.icon("x", size=16),
                            on_click=ReportState.close_evidence,
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                        ),
                    ),
                    width="100%",
                    align="start",
                ),
                c.divider(),
                rx.hstack(
                    _field_panel("Books side", ReportState.modal_books),
                    rx.box(width="1px", background=t.Color.BORDER.value, align_self="stretch"),
                    _field_panel("Portal side", ReportState.modal_portal),
                    spacing="4",
                    align="start",
                    width="100%",
                ),
                rx.cond(
                    ReportState.modal_deltas.length() > 0,
                    rx.vstack(
                        rx.text("Match deltas", style=t.TEXT["label"], font_weight="700"),
                        rx.foreach(
                            ReportState.modal_deltas,
                            lambda d: rx.hstack(
                                rx.text(d.label, style=t.TEXT["micro"], min_width="130px"),
                                rx.text(d.value, style=t.TEXT["body"]),
                                spacing="2",
                                align="baseline",
                                width="100%",
                            ),
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                rx.cond(
                    ReportState.modal_match_reason != "",
                    rx.vstack(
                        rx.text("Match reason", style=t.TEXT["label"], font_weight="700"),
                        rx.text(ReportState.modal_match_reason, style=t.TEXT["body"]),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            max_width="820px",
        ),
        open=ReportState.modal_open,
        on_open_change=ReportState.set_modal_open,
    )


# ---------------------------------------------------------------------------
# Data Quality & Scope Notes + integrity strip
# ---------------------------------------------------------------------------


def _quality_note(note) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.cond(
                note.kind == "caveat",
                c.pill("Couldn't verify", variant="ai"),
                rx.cond(
                    note.kind == "scope",
                    c.pill("Scope", variant="placeholder"),
                    c.pill("Note", variant="accent"),
                ),
            ),
            rx.text(note.title, style=t.TEXT["body"], font_weight="700"),
            spacing="2",
            align="center",
            wrap="wrap",
        ),
        rx.text(note.detail, style=t.TEXT["label"]),
        spacing="1",
        align="start",
        width="100%",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _data_quality() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "Data Quality & Scope Notes",
                "What this run could and couldn't verify, given the files provided.",
            ),
            rx.cond(
                ReportState.quality_notes.length() > 0,
                rx.vstack(
                    rx.foreach(ReportState.quality_notes, _quality_note),
                    spacing="0",
                    width="100%",
                ),
                c.empty_state("No data-quality notes for this run.", icon="circle_check"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _check_row(row) -> rx.Component:
    return rx.hstack(
        rx.text(row.label, style=t.TEXT["micro"], flex="1"),
        rx.text(row.detail, style=t.TEXT["micro"], color=t.Color.TEXT_MUTED.value),
        rx.match(
            row.result,
            ("PASS", c.pill("PASS", variant="rule")),
            ("FAIL", c.pill("FAIL", variant="danger")),
            c.pill("—", variant="placeholder"),
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="4px 0",
    )


def _integrity_strip() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text("Integrity checks", style=t.TEXT["label"], font_weight="700"),
                rx.spacer(),
                rx.match(
                    ReportState.integrity_overall,
                    ("PASS", c.pill("Overall PASS", variant="rule")),
                    ("FAIL", c.pill("Overall FAIL", variant="danger")),
                    c.pill("—", variant="placeholder"),
                ),
                width="100%",
                align="center",
            ),
            rx.hstack(
                rx.vstack(
                    rx.text("F5 data-integrity gate", style=t.TEXT["micro"], font_weight="700"),
                    rx.foreach(ReportState.f5_checks, _check_row),
                    spacing="1",
                    align="start",
                    width="100%",
                    flex="1",
                ),
                rx.vstack(
                    rx.text("Independent verification", style=t.TEXT["micro"], font_weight="700"),
                    rx.foreach(ReportState.verification_checks, _check_row),
                    spacing="1",
                    align="start",
                    width="100%",
                    flex="1",
                ),
                spacing="6",
                align="start",
                width="100%",
                wrap="wrap",
            ),
            rx.text(
                "A separate code path recomputes counts and totals from the raw records and "
                "compares them against the aggregated buckets.",
                style=t.TEXT["micro"],
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def _period_picker() -> rx.Component:
    return c.card(
        rx.hstack(
            rx.vstack(
                rx.text("Report period", style=t.TEXT["label"]),
                rx.select(
                    ReportState.period_options.map(lambda o: o.label),
                    value=ReportState.selected_label,
                    on_change=ReportState.select_period,
                    width="320px",
                ),
                spacing="1",
                align="start",
            ),
            rx.spacer(),
            rx.vstack(
                rx.text("Client", style=t.TEXT["label"]),
                rx.text(ReportState.client_name, style=t.TEXT["body"], font_weight="700"),
                spacing="1",
                align="start",
            ),
            rx.vstack(
                rx.text("Generated", style=t.TEXT["label"]),
                rx.text(ReportState.generated_at, style=t.TEXT["body"]),
                spacing="1",
                align="start",
            ),
            width="100%",
            align="end",
            spacing="6",
            wrap="wrap",
        ),
        width="100%",
    )


def report_section() -> rx.Component:
    """The report body — rendered inside the Action Center page."""
    return rx.vstack(
        rx.cond(
            ReportState.error != "",
            c.inline_reason(ReportState.error),
            rx.fragment(),
        ),
        _period_picker(),
        rx.cond(
            ReportState.loaded,
            rx.vstack(
                _kpi_row(),
                _narrative(),
                _charts_row(),
                _detail_table(),
                _data_quality(),
                _integrity_strip(),
                spacing="5",
                width="100%",
                align="start",
            ),
            c.empty_state("Select a period to build the report.", icon="file-text"),
        ),
        _evidence_modal(),
        spacing="4",
        width="100%",
        align="start",
    )
