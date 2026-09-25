"""Reconcile → Review — the upgraded Stage 4 presentation components.

Presentation only. Every figure comes from ``ReconcileState``, which derives it
from ``src.reconciliation.review``. Nothing here computes a business number.

The components named in the build prompt — SummaryHeadline, KpiCard, CauseBar,
FilterBar, ExceptionTable, EvidenceDrawer, ComparisonTable, NotesList — are all
here, built exclusively from Foundation tokens/components.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx
from reflex_base.constants import EventTriggers
from reflex_base.event import passthrough_event_spec
from reflex_components_core.el.elements.typography import Div

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.reconcile_state import ReconcileState

# ---------------------------------------------------------------------------
# Click-to-filter recharts primitives (the Module 8 pattern, reused)
# ---------------------------------------------------------------------------


class _ClickablePie(rx.recharts.Pie):
    @classmethod
    def get_event_triggers(cls):
        return {**super().get_event_triggers(), EventTriggers.ON_CLICK: passthrough_event_spec(dict)}


_ClickablePie.__name__ = "Pie"


class _ClickableBar(rx.recharts.Bar):
    @classmethod
    def get_event_triggers(cls):
        return {**super().get_event_triggers(), EventTriggers.ON_CLICK: passthrough_event_spec(dict)}


_ClickableBar.__name__ = "Bar"


class _HotkeySurface(Div):
    """A focusable surface that reports J/K/R/Esc to the state.

    Reflex's stock ``Div`` does not expose ``onKeyDown``; re-declare the
    trigger with a one-argument spec so only ``event.key`` crosses the wire
    (never the whole event).
    """

    @classmethod
    def get_event_triggers(cls):
        return {**super().get_event_triggers(), EventTriggers.ON_KEY_DOWN: lambda e: [e.key]}


# The focusable surface: mounted inside the drawer, focused by the drawer's own
# on-open hook so Esc closes natively and J/K/R work without a global listener.
_HOTKEY_ID = "setu-review-hotkeys"


def _plain_button(label, *, on_click, **props) -> rx.Component:
    props.setdefault("size", "2")
    return rx.button(
        label,
        on_click=on_click,
        variant="soft",
        background="transparent",
        color=t.Color.TEXT_PRIMARY.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="9px",
        **props,
    )


def _money_text(value, **props) -> rx.Component:
    """Tabular numerals so column figures line up."""
    props.setdefault("font_variant_numeric", "tabular-nums")
    return rx.text(value, **props)


# ===========================================================================
# 0. Context bar
# ===========================================================================


def context_bar() -> rx.Component:
    """Compact: Client · Period · Recon type · Run(+when). Same selection
    behaviour as the four-dropdown card, without the banner."""
    label = {"font_size": "10px", "font_weight": "600", "letter_spacing": "0.06em",
             "text_transform": "uppercase", "color": t.Color.TEXT_MUTED.value}
    return c.card(
        rx.hstack(
            rx.vstack(
                rx.text("Client", style=label),
                rx.select(
                    ReconcileState.clients,
                    value=ReconcileState.ctx_client,
                    on_change=ReconcileState.set_ctx_client,
                    width="190px",
                    size="1",
                ),
                spacing="1", align="start",
            ),
            rx.vstack(
                rx.text("Period", style=label),
                rx.select.root(
                    rx.select.trigger(width="150px", placeholder="No periods yet", size="1"),
                    rx.select.content(
                        rx.select.group(
                            rx.foreach(
                                ReconcileState.period_options,
                                lambda o: rx.select.item(o.label, value=o.value),
                            ),
                        ),
                    ),
                    value=ReconcileState.ctx_period,
                    on_change=ReconcileState.set_ctx_period,
                ),
                spacing="1", align="start",
            ),
            rx.vstack(
                rx.text("Recon type", style=label),
                rx.select(
                    ReconcileState.recon_types,
                    value=ReconcileState.ctx_recon_type,
                    on_change=ReconcileState.set_ctx_recon_type,
                    width="130px",
                    size="1",
                ),
                spacing="1", align="start",
            ),
            rx.vstack(
                rx.text("Run", style=label),
                rx.cond(
                    ReconcileState.run_options.length() > 1,
                    rx.vstack(
                        rx.select(
                            ReconcileState.run_option_values,
                            value=ReconcileState.run_id.to_string(),
                            on_change=ReconcileState.set_review_run,
                            width="190px",
                            size="1",
                        ),
                        rx.text(ReconcileState.run_context_status, style=t.TEXT["micro"]),
                        spacing="0", align="start",
                    ),
                    rx.vstack(
                        rx.text(ReconcileState.run_context_label, style=t.TEXT["body"], font_weight="600"),
                        rx.text(ReconcileState.run_context_status, style=t.TEXT["micro"]),
                        spacing="0", align="start",
                    ),
                ),
                spacing="0", align="start",
            ),
            rx.spacer(),
            rx.vstack(
                rx.text("Period", style=label),
                rx.text(ReconcileState.period_label, style=t.TEXT["body"], font_weight="600"),
                spacing="0", align="start",
            ),
            spacing="5", align="start", wrap="wrap", width="100%",
        ),
        padding="14px 18px",
    )


# ===========================================================================
# 1. Summary headline + cause bar
# ===========================================================================


def _cause_bar_segment(seg) -> rx.Component:
    return rx.box(
        rx.cond(
            seg.tax_percent >= 8,
            rx.text(seg.tax_percent.to_string() + "%", font_size="11px", font_weight="700",
                    color="#FFFFFF", white_space="nowrap"),
            rx.fragment(),
        ),
        width=seg.tax_width,
        min_width="3px",
        height="34px",
        display="flex",
        align_items="center",
        justify_content="center",
        cursor="pointer",
        border_radius="4px",
        style=rx.match(
            seg.color_role,
            ("rule", {"background": t.Color.RULE.value}),
            ("ai", {"background": t.Color.AI.value}),
            ("danger", {"background": t.Color.DANGER.value}),
            ("accent", {"background": t.Color.ACCENT.value}),
            {"background": t.Color.NEUTRAL.value},
        ),
        opacity=rx.cond(seg.active | (ReconcileState.filter_cause == ""), "1", "0.45"),
        on_click=ReconcileState.toggle_chip("cause", seg.cause),
        title=seg.label,
        _hover={"opacity": "0.85"},
    )


def _cause_legend(seg) -> rx.Component:
    return rx.hstack(
        rx.box(
            width="10px", height="10px", border_radius="3px",
            style=rx.match(
                seg.color_role,
                ("rule", {"background": t.Color.RULE.value}),
                ("ai", {"background": t.Color.AI.value}),
                ("danger", {"background": t.Color.DANGER.value}),
                ("accent", {"background": t.Color.ACCENT.value}),
                {"background": t.Color.NEUTRAL.value},
            ),
            flex_shrink="0",
        ),
        rx.text(seg.label, font_size="12px", font_weight=rx.cond(seg.active, "700", "500"),
                color=t.Color.TEXT_PRIMARY.value),
        rx.text(seg.count_display + " · " + seg.tax_display, style=t.TEXT["micro"]),
        spacing="2",
        align="center",
        cursor="pointer",
        on_click=ReconcileState.toggle_chip("cause", seg.cause),
    )


def single_ring(label, value, color, *, height: str = "230px") -> rx.Component:
    """§2 — a donut with exactly ONE non-zero segment renders as a single
    full-colour ring with a centre label, never as a degenerate two-slice pie
    with an invisible second arc.

    Hand-drawn SVG (no chart library), so the same markup can be reused by the
    static HTML export without a JS runtime. Font sizes are set via ``style``:
    an SVG ``<text>`` needs a real CSS font-size, not a component prop.
    """
    return rx.box(
        rx.el.svg(
            rx.el.circle(cx="50", cy="50", r="38", fill="none",
                         stroke=color, stroke_width="14"),
            rx.el.text(
                label, x="50", y="47", text_anchor="middle",
                style={"font-size": "7px", "font-weight": "600",
                       "fill": t.Color.TEXT_SECONDARY.value,
                       "font-family": "Inter, system-ui, sans-serif"},
            ),
            rx.el.text(
                value, x="50", y="59", text_anchor="middle",
                style={"font-size": "9px", "font-weight": "700",
                       "fill": t.Color.TEXT_PRIMARY.value,
                       "font-family": "Inter, system-ui, sans-serif"},
            ),
            view_box="0 0 100 100", width="180", height="180",
            role="img", aria_label=label + " " + value,
        ),
        width="100%", height=height,
        display="flex", align_items="center", justify_content="center",
    )


def summary_headline() -> rx.Component:
    """The §1 headline: the TAX at stake (not gross invoice value), with its
    share of the period's ITC as a badge. Context → progress → cause split."""
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text(
                        ReconcileState.headline_value,
                        font_size="40px", font_weight="700", letter_spacing="-0.03em",
                        color=t.Color.TEXT_PRIMARY.value, line_height="1.05",
                    ),
                    rx.text(ReconcileState.headline_label, style=t.TEXT["label"]),
                    spacing="0", align="start",
                ),
                c.pill(ReconcileState.headline_pct, variant="danger"),
                rx.text(ReconcileState.headline_pct_caption, style=t.TEXT["micro"]),
                spacing="3", align="end", wrap="wrap",
            ),
            rx.text(ReconcileState.headline_sub, style=t.TEXT["micro"]),
            rx.cond(
                ReconcileState.headline_secondary != "",
                rx.text(ReconcileState.headline_secondary, style=t.TEXT["micro"]),
                rx.fragment(),
            ),
            rx.vstack(
                rx.hstack(
                    rx.text(ReconcileState.progress_label, style=t.TEXT["micro"]),
                    rx.spacer(),
                    rx.text(ReconcileState.progress_percent.to_string() + "% complete",
                            style=t.TEXT["micro"]),
                    width="100%",
                ),
                rx.progress(value=ReconcileState.progress_percent_int, width="100%", size="1"),
                spacing="1", width="100%",
            ),
            c.divider(),
            rx.vstack(
                rx.hstack(
                    rx.text("Split by cause", style=t.TEXT["label"], font_weight="600"),
                    rx.spacer(),
                    rx.text(ReconcileState.headline_scope, style=t.TEXT["micro"]),
                    width="100%",
                ),
                rx.cond(
                    ReconcileState.cause_segments.length() > 0,
                    rx.vstack(
                        rx.hstack(
                            rx.foreach(ReconcileState.cause_segments, _cause_bar_segment),
                            spacing="1", width="100%",
                        ),
                        rx.hstack(
                            rx.foreach(ReconcileState.cause_segments, _cause_legend),
                            spacing="5", wrap="wrap", padding_top="4px",
                        ),
                        spacing="2", width="100%",
                    ),
                    rx.text("No exceptions to split — every record reconciled cleanly.",
                            style=t.TEXT["label"]),
                ),
                spacing="2", width="100%",
            ),
            spacing="2", align="start", width="100%",
        ),
    )


# ===========================================================================
# 2. KPI row (clickable)
# ===========================================================================


def _kpi_card(kpi) -> rx.Component:
    """Zero-count cards stay visible but muted; the value leads, the count
    supports it."""
    return rx.button(
        rx.vstack(
            rx.text(kpi.label, style=t.TEXT["label"], text_align="left"),
            rx.text(
                kpi.value_display,
                font_size="24px", font_weight="700", letter_spacing="-0.02em",
                color=rx.cond(kpi.muted, t.Color.TEXT_MUTED.value, t.Color.TEXT_PRIMARY.value),
                line_height="1.1", text_align="left",
            ),
            rx.text(kpi.count_display, style=t.TEXT["micro"], text_align="left"),
            rx.cond(
                kpi.note != "",
                rx.text(kpi.note, style=t.TEXT["micro"], text_align="left"),
                rx.fragment(),
            ),
            spacing="1", align="start", width="100%",
        ),
        on_click=ReconcileState.set_filter_view(kpi.key),
        variant="soft",
        background=rx.cond(kpi.active, "#EAF0FB", t.Color.SURFACE.value),
        border=rx.cond(kpi.active, f"1px solid {t.Color.ACCENT.value}",
                       f"1px solid {t.Color.BORDER.value}"),
        border_radius=t.RADIUS,
        padding="14px 16px",
        width="100%",
        height="100%",
        text_align="left",
        opacity=rx.cond(kpi.muted, "0.6", "1"),
        _hover={"border_color": t.Color.ACCENT.value},
    )


def kpi_row() -> rx.Component:
    return rx.grid(
        rx.foreach(ReconcileState.kpis, _kpi_card),
        columns=rx.breakpoints(initial="2", md="5"),
        spacing="3",
        width="100%",
    )


# ===========================================================================
# 3. Accountant's Read
# ===========================================================================


def accountants_read() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text("Accountant's read", style=t.TEXT["section_title"]),
                rx.spacer(),
                rx.cond(
                    ReconcileState.read_source.startswith("AI"),
                    c.pill("AI-drafted", variant="ai"),
                    rx.cond(
                        ReconcileState.read_source == "rules",
                        c.pill("Rules-generated", variant="placeholder"),
                        rx.fragment(),
                    ),
                ),
                width="100%", align="center",
            ),
            rx.cond(
                ReconcileState.read_text != "",
                rx.text(ReconcileState.read_text, style=t.TEXT["body"]),
                rx.text("Summary unavailable for this run.", style=t.TEXT["label"]),
            ),
            rx.cond(
                ReconcileState.read_bullets.length() > 0,
                rx.vstack(
                    rx.foreach(
                        ReconcileState.read_bullets,
                        lambda b: rx.hstack(
                            rx.text("•", color=t.Color.TEXT_MUTED.value, font_size="13px"),
                            rx.text(b.text, style=t.TEXT["body"], font_size="13px"),
                            spacing="2", align="start", width="100%",
                        ),
                    ),
                    spacing="1", width="100%",
                ),
                rx.fragment(),
            ),
            rx.cond(
                ReconcileState.read_note != "",
                rx.text(ReconcileState.read_note, style=t.TEXT["micro"]),
                rx.fragment(),
            ),
            rx.cond(
                ReconcileState.read_source.startswith("AI"),
                rx.text("Drafted by the model and verified against the figures on this page.",
                        style=t.TEXT["micro"]),
                rx.hstack(
                    rx.button(
                        "Draft with AI",
                        on_click=ReconcileState.draft_read,
                        disabled=ReconcileState.read_busy,
                        size="2",
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                        border_radius="9px",
                    ),
                    rx.text("Every figure in it is checked against the aggregates above.",
                            style=t.TEXT["micro"]),
                    spacing="2", align="center", wrap="wrap",
                ),
            ),
            spacing="2", align="start", width="100%",
        ),
    )


# ===========================================================================
# 4. Charts row
# ===========================================================================


def _chart_card(title: str, subtitle: str, body) -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(title, subtitle),
            body,
            spacing="2", align="start", width="100%",
        ),
        width="100%", height="100%",
    )


def classification_chart() -> rx.Component:
    return _chart_card(
        "Classification",
        "Count or tax per exception class — one basis for every slice; click to filter.",
        rx.vstack(
            rx.hstack(
                rx.cond(
                    ReconcileState.classification_mode_value,
                    c.pill("Tax", variant="accent"),
                    c.pill("Count", variant="accent"),
                ),
                rx.button(
                    rx.cond(ReconcileState.classification_mode_value, "Show count", "Show tax"),
                    on_click=ReconcileState.toggle_classification_mode,
                    size="1", variant="soft", background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}", border_radius="8px",
                ),
                spacing="2", align="center",
            ),
            rx.cond(
                # §2 — one surviving bucket renders as a full ring, not a pie
                # with an invisible second arc.
                ReconcileState.classification_single,
                single_ring(
                    ReconcileState.classification_single_label,
                    ReconcileState.classification_single_value,
                    ReconcileState.classification_single_color,
                ),
                rx.match(
                    ReconcileState.classification_mode_value,
                    (
                        True,
                        rx.recharts.pie_chart(
                            _ClickablePie.create(
                                rx.foreach(ReconcileState.classification_colors,
                                           lambda col: rx.recharts.cell(fill=col)),
                                data=ReconcileState.classification_slices,
                                data_key="value",
                                name_key="name",
                                inner_radius="55%",
                                outer_radius="85%",
                                on_click=ReconcileState.on_chart_classification,
                            ),
                            rx.recharts.graphing_tooltip(),
                            rx.recharts.legend(),
                            height=230,
                            width="100%",
                        ),
                    ),
                    rx.recharts.pie_chart(
                        _ClickablePie.create(
                            rx.foreach(ReconcileState.classification_colors,
                                       lambda col: rx.recharts.cell(fill=col)),
                            data=ReconcileState.classification_slices,
                            data_key="count",
                            name_key="name",
                            inner_radius="55%",
                            outer_radius="85%",
                            on_click=ReconcileState.on_chart_classification,
                        ),
                        rx.recharts.graphing_tooltip(),
                        rx.recharts.legend(),
                        height=230,
                        width="100%",
                    ),
                ),
            ),
            spacing="2", width="100%",
        ),
    )


def supplier_chart() -> rx.Component:
    return _chart_card(
        "Top suppliers by unmatched value",
        "Where to look first — click a bar to filter the list.",
        rx.cond(
            ReconcileState.supplier_bars.length() > 0,
            rx.recharts.bar_chart(
                _ClickableBar.create(
                    data_key="value",
                    fill=t.Color.DANGER.value,
                    on_click=ReconcileState.on_chart_supplier,
                ),
                rx.recharts.x_axis(data_key="name", interval=0, angle=-20,
                                   text_anchor="end", height=70, font_size=10),
                rx.recharts.y_axis(font_size=10),
                rx.recharts.graphing_tooltip(),
                data=ReconcileState.supplier_bars,
                height=260,
                width="100%",
            ),
            rx.text("No unmatched value with a named supplier in this run.",
                    style=t.TEXT["label"]),
        ),
    )


def itc_chart() -> rx.Component:
    """Rendered only when the run carries an actual eligibility split.

    §2 — the slices come from the run's own §17(5) blocked-credit and
    reverse-charge markers, not from (claimed − eligible), and every
    zero-value segment is filtered out before it reaches the chart. A period
    with no ineligible ITC therefore renders one full ring, never a sliver.
    """
    return rx.cond(
        ReconcileState.itc_available,
        _chart_card(
            "Input tax credit split",
            "Eligible against ineligible ITC for this period.",
            rx.cond(
                ReconcileState.itc_single,
                single_ring(
                    ReconcileState.itc_single_label,
                    ReconcileState.itc_single_value,
                    ReconcileState.itc_single_color,
                ),
                rx.recharts.pie_chart(
                    _ClickablePie.create(
                        rx.foreach(ReconcileState.itc_colors, lambda col: rx.recharts.cell(fill=col)),
                        data=ReconcileState.itc_slices,
                        data_key="value",
                        name_key="name",
                        inner_radius="55%",
                        outer_radius="85%",
                    ),
                    rx.recharts.graphing_tooltip(),
                    rx.recharts.legend(),
                    height=230,
                    width="100%",
                ),
            ),
        ),
        rx.fragment(),
    )


def charts_row() -> rx.Component:
    return rx.grid(
        classification_chart(),
        supplier_chart(),
        itc_chart(),
        columns=rx.breakpoints(initial="1", md="2", xl="3"),
        spacing="3",
        width="100%",
    )


# ===========================================================================
# 5. Filter bar + items table
# ===========================================================================


def _chip(chip) -> rx.Component:
    return rx.button(
        rx.hstack(
            rx.text(chip.label, font_size="12px", font_weight="500", white_space="nowrap"),
            rx.text(chip.count_display, font_size="11px", font_weight="700", opacity="0.7"),
            spacing="1", align="center",
        ),
        on_click=ReconcileState.toggle_chip(chip.facet, chip.value),
        variant="soft",
        background=rx.cond(chip.active, "#EAF0FB", t.Color.NEUTRAL_BG.value),
        color=rx.cond(chip.muted, t.Color.TEXT_MUTED.value, t.Color.TEXT_PRIMARY.value),
        border=rx.cond(chip.active, f"1px solid {t.Color.ACCENT.value}",
                       f"1px solid {t.Color.BORDER.value}"),
        border_radius="999px",
        padding="3px 12px",
        opacity=rx.cond(chip.muted, "0.55", "1"),
        _hover={"border_color": t.Color.ACCENT.value},
    )


def _active_pill(f) -> rx.Component:
    return rx.hstack(
        rx.text(f.label, font_size="12px", font_weight="500"),
        rx.icon("x", size=12, cursor="pointer", on_click=ReconcileState.clear_filter(f.facet)),
        spacing="2", align="center",
        padding="2px 8px 2px 10px",
        border_radius="999px",
        background="#EAF0FB",
        color=t.Color.ACCENT.value,
        border="1px solid #D4E1F8",
    )


def filter_bar() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.input(
                    value=ReconcileState.search_query,
                    on_change=ReconcileState.set_search,
                    placeholder="Search supplier, GSTIN or invoice no.",
                    width="280px",
                    size="2",
                ),
                rx.vstack(
                    rx.text("Sort", style=t.TEXT["micro"]),
                    rx.select(
                        ["difference", "supplier", "date", "confidence", "review_state"],
                        value=ReconcileState.sort_key,
                        on_change=ReconcileState.set_sort,
                        width="160px", size="2",
                    ),
                    spacing="0",
                ),
                rx.vstack(
                    rx.text("Group by", style=t.TEXT["micro"]),
                    rx.select(
                        ["none", "supplier", "cause"],
                        value=ReconcileState.group_by,
                        on_change=ReconcileState.set_group_by,
                        width="140px", size="2",
                    ),
                    spacing="0",
                ),
                rx.hstack(
                    rx.checkbox(
                        checked=ReconcileState.active_only,
                        on_change=ReconcileState.set_active_only,
                    ),
                    rx.text("Only not reviewed", style=t.TEXT["micro"]),
                    spacing="2", align="center", padding_top="14px",
                ),
                rx.spacer(),
                rx.text(ReconcileState.page_label, style=t.TEXT["micro"]),
                spacing="3", align="end", wrap="wrap", width="100%",
            ),
            rx.hstack(
                rx.text("Classification", style=t.TEXT["micro"], min_width="92px"),
                rx.hstack(rx.foreach(ReconcileState.classification_chips, _chip),
                          spacing="1", wrap="wrap"),
                spacing="2", align="center", width="100%",
            ),
            rx.cond(
                ReconcileState.cause_chips.length() > 0,
                rx.hstack(
                    rx.text("Likely cause", style=t.TEXT["micro"], min_width="92px"),
                    rx.hstack(rx.foreach(ReconcileState.cause_chips, _chip),
                              spacing="1", wrap="wrap"),
                    spacing="2", align="center", width="100%",
                ),
                rx.fragment(),
            ),
            rx.hstack(
                rx.text("Match confidence", style=t.TEXT["micro"], min_width="92px"),
                rx.hstack(rx.foreach(ReconcileState.confidence_chips, _chip),
                          spacing="1", wrap="wrap"),
                rx.text("Review state", style=t.TEXT["micro"], margin_left="12px"),
                rx.hstack(rx.foreach(ReconcileState.review_chips, _chip),
                          spacing="1", wrap="wrap"),
                spacing="2", align="center", wrap="wrap", width="100%",
            ),
            rx.cond(
                ReconcileState.has_filters,
                rx.hstack(
                    rx.foreach(ReconcileState.active_filters, _active_pill),
                    rx.button(
                        "Clear all",
                        on_click=ReconcileState.clear_all_filters,
                        size="1", variant="ghost", color=t.Color.ACCENT.value,
                    ),
                    spacing="2", wrap="wrap", align="center", padding_top="2px",
                ),
                rx.fragment(),
            ),
            spacing="2", width="100%",
        ),
        position="sticky",
        top="0",
        z_index="5",
        background=t.Color.SURFACE.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius=t.RADIUS,
        padding="12px 16px",
        box_shadow=t.CARD_SHADOW,
        width="100%",
    )


def _status_pill(reviewed) -> rx.Component:
    return rx.cond(
        reviewed,
        c.pill("Reviewed", variant="rule"),
        c.pill("Not reviewed", variant="placeholder"),
    )


def _issue_cell(row) -> rx.Component:
    return rx.vstack(
        rx.text(row.issue, style=t.TEXT["body"], font_size="13px", font_weight="500"),
        rx.hstack(
            c.pill(row.cause_label, variant="ai"),
            spacing="1",
        ),
        spacing="1", align="start",
    )


def _match_cell(row) -> rx.Component:
    return rx.vstack(
        c.confidence_badge("rule", label=row.confidence_label),
        rx.text(row.match_key, style=t.TEXT["micro"]),
        spacing="1", align="start",
    )


def _table_row(row) -> rx.Component:
    return rx.el.tr(
        rx.el.td(
            rx.cond(
                row.bulk_blocked,
                rx.tooltip(
                    rx.checkbox(
                        checked=row.selected,
                        disabled=True,
                    ),
                    content=row.bulk_reason,
                ),
                rx.checkbox(
                    checked=row.selected,
                    on_change=ReconcileState.toggle_select_one(row.result_id),
                ),
            ),
            # The whole row opens the drawer; the checkbox must not.
            on_click=rx.stop_propagation,
            style={"padding": "8px 8px 8px 4px", "vertical-align": "top", "width": "34px"},
        ),
        rx.el.td(
            rx.vstack(
                rx.text(row.supplier, font_size="13px", font_weight="700",
                        color=t.Color.TEXT_PRIMARY.value),
                rx.text(
                    row.gstin + " · " + row.reference + " · " + row.date,
                    style=t.TEXT["micro"],
                ),
                spacing="0", align="start",
            ),
            style={"padding": "8px 10px", "vertical-align": "top"},
        ),
        rx.el.td(_money_text(row.books_display, style=t.TEXT["body"], font_size="13px"),
                 style={"padding": "8px 10px", "vertical-align": "top", "text-align": "right"}),
        rx.el.td(_money_text(row.portal_display, style=t.TEXT["body"], font_size="13px"),
                 style={"padding": "8px 10px", "vertical-align": "top", "text-align": "right"}),
        rx.el.td(
            _money_text(
                row.difference_display,
                font_size="13px", font_weight="700", font_variant_numeric="tabular-nums",
                color=rx.cond(row.difference_danger, t.Color.DANGER.value, t.Color.TEXT_PRIMARY.value),
            ),
            style={"padding": "8px 10px", "vertical-align": "top", "text-align": "right"},
        ),
        rx.el.td(_issue_cell(row), style={"padding": "8px 10px", "vertical-align": "top"}),
        rx.el.td(_match_cell(row), style={"padding": "8px 10px", "vertical-align": "top"}),
        rx.el.td(_status_pill(row.reviewed), style={"padding": "8px 10px", "vertical-align": "top"}),
        rx.el.td(
            rx.icon("chevron_right", size=16, color=t.Color.TEXT_MUTED.value),
            style={"padding": "8px 4px", "vertical-align": "middle", "text-align": "right", "width": "34px"},
        ),
        on_click=ReconcileState.open_drawer(row.result_id),
        style={"cursor": "pointer"},
        _hover={"background": "#F7F9FC"},
    )


def _group_header(h) -> rx.Component:
    return rx.el.tr(
        rx.el.td(
            rx.hstack(
                rx.text(h.label, font_size="13px", font_weight="700",
                        color=t.Color.TEXT_PRIMARY.value),
                rx.text(h.count_display, style=t.TEXT["micro"]),
                rx.spacer(),
                rx.text("Subtotal " + h.subtotal, style=t.TEXT["micro"], font_weight="600"),
                width="100%", align="center",
            ),
            col_span=9,
            style={"padding": "8px 10px", "background": t.Color.NEUTRAL_BG.value},
        ),
    )


_TH_STYLE = {
    "font-size": "11px",
    "font-weight": "600",
    "color": t.Color.TEXT_SECONDARY.value,
    "text-transform": "uppercase",
    "letter-spacing": "0.05em",
    "text-align": "left",
    "padding": "8px 10px",
    "border-bottom": f"1px solid {t.Color.BORDER.value}",
}
_TH_STYLE_RIGHT = {**_TH_STYLE, "text-align": "right"}


def _table_header() -> rx.Component:
    return rx.el.thead(
        rx.el.tr(
            rx.el.th(
            rx.checkbox(
                checked=ReconcileState.all_selectable_selected,
                on_change=ReconcileState.toggle_select_all,
            ),
            style={**_TH_STYLE, "width": "34px"},
        ),
            rx.el.th(rx.text("Supplier", style=_TH_STYLE), style=_TH_STYLE),
            rx.el.th(rx.text("Books ₹", style=_TH_STYLE_RIGHT), style=_TH_STYLE_RIGHT),
            rx.el.th(rx.text("Portal ₹", style=_TH_STYLE_RIGHT), style=_TH_STYLE_RIGHT),
            rx.el.th(rx.text("Difference ₹", style=_TH_STYLE_RIGHT), style=_TH_STYLE_RIGHT),
            rx.el.th(rx.text("Issue", style=_TH_STYLE), style=_TH_STYLE),
            rx.el.th(rx.text("Match", style=_TH_STYLE), style=_TH_STYLE),
            rx.el.th(rx.text("Status", style=_TH_STYLE), style=_TH_STYLE),
            rx.el.th(rx.fragment(), style={**_TH_STYLE, "width": "34px"}),
        ),
        style={"background": t.Color.SURFACE.value},
    )


def exception_table() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text(
                "Select all " + ReconcileState.shown_count.to_string() + " in this view",
                style=t.TEXT["micro"],
            ),
            rx.button("Select all", on_click=ReconcileState.select_all_in_view,
                      size="1", variant="ghost", color=t.Color.ACCENT.value),
            rx.cond(
                ReconcileState.selection.length() > 0,
                rx.button("Clear selection", on_click=ReconcileState.clear_selection,
                          size="1", variant="ghost", color=t.Color.TEXT_SECONDARY.value),
                rx.fragment(),
            ),
            rx.spacer(),
            rx.cond(
                ReconcileState.bulk_blocked_count > 0,
                rx.text(
                    ReconcileState.bulk_blocked_count.to_string()
                    + " item(s) in this view are materiality-gated and cannot be bulk-reviewed.",
                    style=t.TEXT["micro"],
                ),
                rx.fragment(),
            ),
            spacing="3", align="center", width="100%", padding_bottom="6px",
        ),
        rx.cond(
            ReconcileState.exception_rows.length() > 0,
            rx.box(
                rx.el.table(
                    _table_header(),
                    rx.el.tbody(
                        rx.cond(
                            ReconcileState.group_by == "none",
                            rx.foreach(ReconcileState.exception_rows, _table_row),
                            rx.foreach(
                                ReconcileState.group_headers,
                                lambda h: rx.fragment(
                                    _group_header(h),
                                    rx.foreach(
                                        ReconcileState.exception_rows,
                                        lambda row: rx.cond(
                                            row.group_key == h.group, _table_row(row), rx.fragment()
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                    style={"width": "100%", "border-collapse": "collapse"},
                ),
                width="100%",
                overflow_x="auto",
            ),
            c.empty_state("No items match the current filters.", icon="search-x"),
        ),
        rx.cond(
            ReconcileState.page_count > 1,
            rx.hstack(
                rx.button(
                    "← Previous",
                    on_click=ReconcileState.prev_page,
                    disabled=ReconcileState.page <= 1,
                    size="1", variant="soft", background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}", border_radius="8px",
                ),
                rx.text("Page " + ReconcileState.page.to_string() + " of " + ReconcileState.page_count.to_string(),
                        style=t.TEXT["micro"]),
                rx.button(
                    "Next →",
                    on_click=ReconcileState.next_page,
                    disabled=ReconcileState.page >= ReconcileState.page_count,
                    size="1", variant="soft", background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}", border_radius="8px",
                ),
                spacing="3", align="center", justify="center", width="100%", padding_top="10px",
            ),
            rx.fragment(),
        ),
        spacing="0", width="100%",
    )


def bulk_bar() -> rx.Component:
    """Sticky bar shown while rows are selected. Materiality-gated rows can
    never be selected (Module 8's gate, applied consistently)."""
    return rx.cond(
        ReconcileState.selection.length() > 0,
        rx.box(
            rx.hstack(
                rx.text(ReconcileState.selection.length().to_string() + " selected",
                        font_size="13px", font_weight="700"),
                rx.input(
                    value=ReconcileState.bulk_note,
                    on_change=ReconcileState.set_selection_note,
                    placeholder="Optional shared note for these items",
                    width="320px", size="2",
                ),
                rx.spacer(),
                rx.button(
                    "Mark " + ReconcileState.selection.length().to_string() + " reviewed",
                    on_click=ReconcileState.bulk_mark_reviewed,
                    disabled=ReconcileState.bulk_disabled,
                    size="2",
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                    border_radius="9px",
                ),
                _plain_button("Clear review", on_click=ReconcileState.bulk_clear_review),
                _plain_button("Cancel", on_click=ReconcileState.clear_selection),
                spacing="3", align="center", wrap="wrap", width="100%",
            ),
            rx.cond(
                ReconcileState.bulk_reason != "",
                c.inline_reason(ReconcileState.bulk_reason),
                rx.fragment(),
            ),
            position="sticky",
            bottom="0",
            z_index="6",
            background=t.Color.SURFACE.value,
            border=f"1px solid {t.Color.ACCENT.value}",
            border_radius=t.RADIUS,
            padding="10px 16px",
            box_shadow=t.CARD_SHADOW,
            width="100%",
        ),
        rx.fragment(),
    )


# ===========================================================================
# 6. Evidence drawer
# ===========================================================================


def _key_chip(chip) -> rx.Component:
    return rx.hstack(
        rx.text(chip.label, font_size="12px", font_weight="600",
                color=rx.cond(chip.ok, t.Color.RULE_ON.value, t.Color.DANGER.value)),
        padding="2px 10px",
        border_radius="999px",
        background=rx.cond(chip.ok, t.Color.RULE.value, "#FDEBEA"),
        border=rx.cond(chip.ok, "1px solid transparent", "1px solid #F6C9C5"),
        spacing="1", align="center",
    )


def _comparison_row(r) -> rx.Component:
    return rx.el.tr(
        rx.el.td(
            rx.hstack(
                rx.text(r.label, style=t.TEXT["body"], font_size="12px"),
                rx.cond(r.overridden, c.pill("Corrected", variant="accent"), rx.fragment()),
                spacing="2", align="center",
            ),
            style={"padding": "6px 8px"},
        ),
        rx.el.td(
            rx.hstack(
                _money_text(r.books, font_size="12px"),
                rx.cond(
                    r.books_editable,
                    rx.icon(
                        "pencil", size=12, cursor="pointer", color=t.Color.TEXT_MUTED.value,
                        on_click=ReconcileState.open_edit("books", r.key, r.books_raw),
                    ),
                    rx.fragment(),
                ),
                spacing="2", align="center", justify="end",
            ),
            style={"padding": "6px 8px", "text-align": "right"},
        ),
        rx.el.td(
            rx.hstack(
                _money_text(r.portal, font_size="12px"),
                rx.cond(
                    r.portal_editable,
                    rx.icon(
                        "pencil", size=12, cursor="pointer", color=t.Color.TEXT_MUTED.value,
                        on_click=ReconcileState.open_edit("portal", r.key, r.portal_raw),
                    ),
                    rx.fragment(),
                ),
                spacing="2", align="center", justify="end",
            ),
            style={"padding": "6px 8px", "text-align": "right"},
        ),
        rx.el.td(
            rx.hstack(
                _money_text(r.difference, font_size="12px", font_weight="600",
                            color=rx.cond(r.material, t.Color.DANGER.value,
                                          rx.cond(r.differs, t.Color.AI_ON.value,
                                                  t.Color.TEXT_MUTED.value))),
                rx.cond(r.material, c.pill("Differs · material", variant="danger"), rx.fragment()),
                rx.cond(
                    r.differs & ~r.material,
                    c.pill("Differs", variant="ai"),
                    rx.fragment(),
                ),
                spacing="2", align="center", justify="end",
            ),
            style={"padding": "6px 8px", "text-align": "right"},
        ),
        style={"background": rx.cond(r.material, "#FDEBEA", rx.cond(r.differs, "#FDF4E3", "transparent"))},
    )


def comparison_table() -> rx.Component:
    """Field | Books | Portal | Difference — human labels, ₹, grouped as
    Identity and Amounts."""
    header = {"font_size": "11px", "font_weight": "600",
              "color": t.Color.TEXT_SECONDARY.value, "text_transform": "uppercase"}
    return rx.vstack(
        rx.cond(
            ReconcileState.detail_has_material,
            c.warning_banner("Material differences are highlighted. Nothing is auto-corrected."),
            rx.fragment(),
        ),
        rx.hstack(
            rx.text("Comparison", style=t.TEXT["label"], font_weight="700"),
            rx.spacer(),
            rx.button(
                rx.cond(ReconcileState.detail_comparison_collapsed, "Show all fields", "Hide empty fields"),
                on_click=ReconcileState.toggle_comparison,
                size="1", variant="soft", background="transparent",
                color=t.Color.TEXT_SECONDARY.value,
                border=f"1px solid {t.Color.BORDER.value}", border_radius="8px",
            ),
            width="100%", align="center",
        ),
        rx.box(
            rx.el.table(
                rx.el.thead(
                    rx.el.tr(
                        rx.el.th(rx.text("Field", **header), padding="6px 8px", text_align="left"),
                        rx.el.th(rx.text("Books", **header), padding="6px 8px", text_align="right"),
                        rx.el.th(rx.text("Portal", **header), padding="6px 8px", text_align="right"),
                        rx.el.th(rx.text("Difference", **header), padding="6px 8px", text_align="right"),
                    ),
                    border_bottom=f"1px solid {t.Color.BORDER.value}",
                ),
                rx.el.tbody(
                    _comparison_section("Identity", "Identity"),
                    _comparison_section("Amounts", "Amounts"),
                    _comparison_section("Other", "Other"),
                ),
                width="100%", border_collapse="collapse",
            ),
            width="100%", overflow_x="auto",
        ),
        rx.cond(
            ReconcileState.detail_extra_count > 0,
            rx.text(
                ReconcileState.detail_extra_count.to_string()
                + " field(s) that agree on both sides are hidden.",
                style=t.TEXT["micro"],
            ),
            rx.fragment(),
        ),
        rx.cond(
            ReconcileState.detail_corrected_fields.length() > 0,
            rx.hstack(
                rx.text("Corrected:", style=t.TEXT["micro"], font_weight="600"),
                rx.foreach(
                    ReconcileState.detail_corrected_fields,
                    lambda f: rx.hstack(
                        rx.text(f.label, style=t.TEXT["micro"]),
                        rx.button("Revert", on_click=ReconcileState.revert_edit(f.side, f.key),
                                  size="1", variant="ghost", color=t.Color.ACCENT.value),
                        spacing="1", align="center",
                    ),
                ),
                spacing="2", wrap="wrap", align="center",
            ),
            rx.fragment(),
        ),
        spacing="2", width="100%", align="start",
    )


@dataclass
class CorrectedField:
    key: str
    side: str
    label: str


def edit_dialog() -> rx.Component:
    """Inline correction of one field of a matched record. A reason is required
    — a correction to a matched record is a sensitive change."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.text("Correct this value", style=t.TEXT["card_title"]),
                rx.text(
                    ReconcileState.edit_side_label + " · " + ReconcileState.edit_field_label,
                    style=t.TEXT["micro"],
                ),
                rx.vstack(
                    rx.text("New value", style=t.TEXT["label"]),
                    rx.input(
                        value=ReconcileState.edit_value,
                        on_change=ReconcileState.set_edit_value,
                        width="100%",
                    ),
                    spacing="1", align="start", width="100%",
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
                    _plain_button("Cancel", on_click=ReconcileState.close_edit),
                    spacing="2",
                ),
                spacing="3", align="start", width="100%",
            ),
            max_width="520px",
        ),
        open=ReconcileState.edit_open,
        on_open_change=ReconcileState.close_edit,
    )


def _comparison_section(title: str, key: str) -> rx.Component:
    return rx.fragment(
        rx.el.tr(
            rx.el.td(
                rx.text(title, font_size="10px", font_weight="700",
                        letter_spacing="0.06em", text_transform="uppercase",
                        color=t.Color.TEXT_MUTED.value),
                col_span=4,
                style={"padding": "8px 8px 2px 8px", "background": t.Color.NEUTRAL_BG.value},
            ),
            style={"background": t.Color.NEUTRAL_BG.value},
        ),
        rx.foreach(
            ReconcileState.detail_comparison,
            lambda r: rx.cond(r.section == key, _comparison_row(r), rx.fragment()),
        ),
    )


def _drawer_field(label: str, value, **props) -> rx.Component:
    return rx.vstack(
        rx.text(label, font_size="10px", font_weight="600", letter_spacing="0.05em",
                text_transform="uppercase", color=t.Color.TEXT_MUTED.value),
        rx.text(value, style=t.TEXT["body"], font_size="13px", **props),
        spacing="0", align="start",
    )


def evidence_drawer() -> rx.Component:
    return rx.drawer.root(
        rx.drawer.overlay(),
        rx.drawer.content(
            _HotkeySurface.create(
                rx.vstack(
                    rx.hstack(
                        rx.vstack(
                            rx.text(ReconcileState.detail_supplier, style=t.TEXT["section_title"]),
                            rx.text(
                                ReconcileState.detail_gstin + " · " + ReconcileState.detail_reference
                                + " · " + ReconcileState.detail_date,
                                style=t.TEXT["micro"],
                            ),
                            spacing="0", align="start",
                        ),
                        rx.spacer(),
                        rx.button(rx.icon("x", size=16), on_click=ReconcileState.close_drawer,
                                  variant="ghost", size="2", color=t.Color.TEXT_MUTED.value),
                        width="100%", align="start",
                    ),
                    rx.hstack(
                        c.pill(ReconcileState.detail_classification, variant="danger"),
                        c.pill(ReconcileState.detail_cause_label, variant="ai"),
                        c.confidence_badge("rule", label=ReconcileState.detail_confidence_label),
                        _status_pill(ReconcileState.detail_reviewed),
                        spacing="2", wrap="wrap", width="100%",
                    ),
                    rx.hstack(
                        _plain_button("← Previous", on_click=ReconcileState.drawer_prev,
                                      disabled=ReconcileState.detail_prev_disabled, size="1"),
                        _plain_button("Next →", on_click=ReconcileState.drawer_next,
                                      disabled=ReconcileState.detail_next_disabled, size="1"),
                        _plain_button("Next unreviewed", on_click=ReconcileState.drawer_next_unreviewed,
                                      disabled=ReconcileState.detail_next_unreviewed_disabled, size="1"),
                        rx.spacer(),
                        rx.text(ReconcileState.detail_position, style=t.TEXT["micro"]),
                        c.view_history("Edit history", ReconcileState.detail_history,
                                       count=ReconcileState.detail_history.length()),
                        spacing="2", align="center", wrap="wrap", width="100%",
                    ),
                    rx.text("J / K move between items · R marks reviewed · Esc closes",
                            style=t.TEXT["micro"]),
                    c.divider(),
                    # b. verdict
                    rx.vstack(
                        rx.text("What this means", style=t.TEXT["label"], font_weight="700"),
                        rx.text(ReconcileState.detail_verdict, style=t.TEXT["body"]),
                        spacing="1", align="start", width="100%",
                    ),
                    # c. how this was matched
                    rx.vstack(
                        rx.text("How this was matched", style=t.TEXT["label"], font_weight="700"),
                        rx.hstack(
                            rx.foreach(ReconcileState.detail_key_chips, _key_chip),
                            spacing="2", wrap="wrap",
                        ),
                        rx.text(ReconcileState.detail_reason, style=t.TEXT["body"], font_size="12px"),
                        spacing="2", align="start", width="100%",
                    ),
                    c.divider(),
                    # d. comparison
                    comparison_table(),
                    c.divider(),
                    # e. suggested next step
                    rx.vstack(
                        rx.text("Suggested next step", style=t.TEXT["label"], font_weight="700"),
                        rx.text(ReconcileState.detail_next_step, style=t.TEXT["body"]),
                        rx.hstack(
                            rx.cond(
                                ReconcileState.detail_step_link != "",
                                rx.button(
                                    ReconcileState.detail_step_link_label,
                                    on_click=rx.redirect(ReconcileState.detail_step_link),
                                    size="1", variant="soft", background="transparent",
                                    color=t.Color.ACCENT.value,
                                    border=f"1px solid {t.Color.BORDER.value}",
                                    border_radius="8px",
                                ),
                                rx.fragment(),
                            ),
                            rx.cond(
                                ReconcileState.detail_step_badge_text != "",
                                c.placeholder_badge(ReconcileState.detail_step_badge),
                                rx.fragment(),
                            ),
                            spacing="2", align="center", wrap="wrap",
                        ),
                        spacing="2", align="start", width="100%",
                    ),
                    c.divider(),
                    # f. review
                    rx.vstack(
                        rx.text("Review", style=t.TEXT["label"], font_weight="700"),
                        c.reason_capture(
                            label="Reviewer note (optional)",
                            value=ReconcileState.detail_note,
                            on_change=ReconcileState.set_detail_note,
                            placeholder="Anything the next reviewer should know",
                        ),
                        rx.hstack(
                            rx.button(
                                rx.cond(ReconcileState.detail_reviewed, "Reviewed", "Mark reviewed"),
                                on_click=ReconcileState.mark_reviewed,
                                disabled=ReconcileState.detail_reviewed,
                                size="2",
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                                border_radius="9px",
                            ),
                            _plain_button("Clear review", on_click=ReconcileState.clear_review),
                            spacing="2", align="center", wrap="wrap",
                        ),
                        spacing="2", align="start", width="100%",
                    ),
                    # g. low-weight footer
                    rx.cond(
                        ReconcileState.detail_sources.length() > 0,
                        rx.vstack(
                            rx.text("Sources", style=t.TEXT["micro"], font_weight="600"),
                            rx.foreach(
                                ReconcileState.detail_sources,
                                lambda s: rx.text(s, style=t.TEXT["micro"]),
                            ),
                            spacing="0", align="start", width="100%",
                        ),
                        rx.fragment(),
                    ),                    spacing="3",
                    align="start",
                    width="100%",
                    padding="20px 22px",
                ),
                id=_HOTKEY_ID,
                tab_index=0,
                on_key_down=ReconcileState.handle_hotkey,
                outline="none",
                width="100%",
                height="100%",
                overflow_y="auto",
                background=t.Color.SURFACE.value,
            ),
            side="right",
            width=["100%", "100%", "100%", "760px"],
            padding="0",
            background=t.Color.SURFACE.value,
            on_open_auto_focus=rx.call_script(
                f"setTimeout(() => document.getElementById('{_HOTKEY_ID}')?.focus(), 60)"
            ),
        ),
        open=ReconcileState.drawer_open,
        on_open_change=ReconcileState.on_drawer_open_change,
    )
# ===========================================================================
# 7. Data quality & scope notes
# ===========================================================================


def _quality_note(note) -> rx.Component:
    return rx.el.details(
        rx.el.summary(
            rx.hstack(
                rx.icon("triangle-alert", size=14, color=t.Color.AI_ON.value),
                rx.text(note.title, font_size="13px", font_weight="600",
                        color=t.Color.TEXT_PRIMARY.value),
                spacing="2", align="center",
            ),
            style={"cursor": "pointer", "list_style": "none"},
        ),
        rx.vstack(
            _note_line("What", note.what),
            _note_line("Why it matters", note.why),
            _note_line("How to fix", note.how),
            rx.cond(
                note.link != "",
                rx.button(
                    note.link_label,
                    on_click=rx.redirect(note.link),
                    size="1", variant="soft", background="transparent",
                    color=t.Color.ACCENT.value,
                    border=f"1px solid {t.Color.BORDER.value}", border_radius="8px",
                ),
                rx.fragment(),
            ),
            spacing="2", align="start", width="100%", padding="8px 0 4px 22px",
        ),
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _note_line(label: str, value) -> rx.Component:
    return rx.hstack(
        rx.text(label, style=t.TEXT["micro"], font_weight="600", min_width="104px"),
        rx.text(value, style=t.TEXT["body"], font_size="13px"),
        spacing="2", align="start", width="100%",
    )


def notes_list() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "Data quality & scope notes",
                "Everything this run could and could not check, with the fix for each.",
            ),
            rx.cond(
                ReconcileState.quality_notes.length() > 0,
                rx.vstack(
                    rx.foreach(ReconcileState.quality_notes, _quality_note),
                    spacing="0", width="100%",
                ),
                c.info_banner("No data-quality issues detected."),
            ),
            spacing="2", align="start", width="100%",
        ),
    )


# ===========================================================================
# 8. Integrity strip
# ===========================================================================


def _integrity_chip(row) -> rx.Component:
    return rx.hstack(
        rx.text(row.label, style=t.TEXT["micro"]),
        rx.cond(
            row.result == "PASS",
            c.pill("PASS", variant="rule"),
            c.pill("FAIL", variant="danger"),
        ),
        rx.text(row.detail, style=t.TEXT["micro"]),
        spacing="2", align="center",
    )


def integrity_strip() -> rx.Component:
    return rx.cond(
        ReconcileState.integrity_rows.length() > 0,
        rx.hstack(
            rx.text("Integrity", style=t.TEXT["micro"], font_weight="600"),
            rx.foreach(ReconcileState.integrity_rows, _integrity_chip),
            spacing="4", wrap="wrap", align="center",
            padding="8px 14px",
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius=t.RADIUS,
            background=t.Color.SURFACE.value,
            width="100%",
        ),
        rx.fragment(),
    )


# ===========================================================================
# 8. Report export — one run, three formats (§3)
# ===========================================================================


def _format_button(label: str, fmt: str, hint: str) -> rx.Component:
    return rx.vstack(
        rx.button(
            label,
            on_click=ReconcileState.prepare_report(fmt),
            disabled=ReconcileState.report_busy,
            size="2",
            variant="soft",
            background=rx.cond(ReconcileState.report_fmt == fmt, "#EAF0FB", "transparent"),
            color=t.Color.TEXT_PRIMARY.value,
            border=rx.cond(
                ReconcileState.report_fmt == fmt,
                f"1px solid {t.Color.ACCENT.value}",
                f"1px solid {t.Color.BORDER.value}",
            ),
            border_radius="9px",
        ),
        rx.text(hint, style=t.TEXT["micro"]),
        spacing="1", align="start",
    )


def report_export() -> rx.Component:
    """The export control: HTML / PDF / Excel, all built from this run's one
    result so every headline number matches the screen exactly."""
    return c.card(
        rx.vstack(
            c.section_title(
                "Export this report",
                "A finished, presentable report — not a data dump. All three formats "
                "read the same figures as this screen.",
            ),
            rx.hstack(
                _format_button("HTML", "html", "Self-contained file — opens offline"),
                _format_button("PDF", "pdf", "Print-ready, charts included"),
                _format_button("Excel", "excel", "Four sheets, live formulas"),
                spacing="4", align="start", wrap="wrap",
            ),
            rx.cond(
                ReconcileState.report_busy,
                c.info_banner("Building the report…"),
                rx.fragment(),
            ),
            rx.cond(
                ReconcileState.report_error != "",
                c.inline_reason(ReconcileState.report_error),
                rx.fragment(),
            ),
            rx.cond(
                ReconcileState.report_b64 != "",
                rx.vstack(
                    c.info_banner(
                        f"{ReconcileState.report_fmt.upper()} report ready — "
                        f"{ReconcileState.report_name}"
                    ),
                    rx.link(
                        rx.button(
                            f"Download {ReconcileState.report_fmt.upper()} report",
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        # The "data:" scheme is REQUIRED — without it the browser
                        # reads "text/html;base64,AAA…" as a relative URL and
                        # 404s instead of downloading the file.
                        href="data:" + ReconcileState.report_mime + ";base64," + ReconcileState.report_b64,
                        download=ReconcileState.report_name,
                    ),
                    spacing="2", align="start",
                ),
                rx.fragment(),
            ),
            spacing="3", align="start", width="100%",
        ),
    )


# ===========================================================================
# Empty states
# ===========================================================================


def review_empty(message: str, *, icon: str = "circle-check") -> rx.Component:
    return c.card(c.empty_state(message, icon=icon))
