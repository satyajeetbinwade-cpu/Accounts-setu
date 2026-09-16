"""Module 8 — Action Center (Recon Exceptions only).

The capstone working queue — built GENERICALLY against the shared Flagged
Item interface. Renders only the generic shape (never an origin-specific
field name). Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.action_center_state import Module8State
from setu.views import shell


def _priority_pill(variant, label) -> rx.Component:
    return rx.match(
        variant,
        ("danger", c.pill(label, variant="danger")),
        ("ai", c.pill(label, variant="ai")),
        ("accent", c.pill(label, variant="accent")),
        c.pill(label, variant="placeholder"),
    )


def _filter_select(label, value, options, on_change, width="180px") -> rx.Component:
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.select(options, value=value, on_change=on_change, width=width),
        spacing="1",
        align="start",
    )


def _headline() -> rx.Component:
    return c.card(
        rx.vstack(
            c.stat(Module8State.total_open.to_string(), "open item(s) in the queue", accent=True),
            rx.text(
                f"Across {Module8State.source_count} source module(s)",
                style=t.TEXT["micro"],
            ),
            spacing="1",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _grouped() -> rx.Component:
    return rx.vstack(
        rx.vstack(
            rx.text("By module of origin", style=t.TEXT["label"], font_weight="700"),
            rx.hstack(
                rx.foreach(
                    Module8State.by_source,
                    lambda s: c.accent_pill(f"{s.label} — {s.count}"),
                ),
                spacing="2",
                wrap="wrap",
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        rx.vstack(
            rx.text("By priority", style=t.TEXT["label"], font_weight="700"),
            rx.hstack(
                rx.foreach(
                    Module8State.by_priority,
                    lambda p: _priority_pill(p.variant, f"{p.label} — {p.count}"),
                ),
                spacing="2",
                wrap="wrap",
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        rx.cond(
            Module8State.stale_count > 0,
            c.warning_banner(
                f"{Module8State.stale_count} item(s) may be based on incomplete data — F5 reports "
                "degraded sync health for the underlying client."
            ),
            rx.fragment(),
        ),
        spacing="3",
        align="start",
        width="100%",
    )


def _filters() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "Filters",
                "Only the generic dimensions — client, module of origin, type, priority, age, assignee.",
            ),
            rx.hstack(
                _filter_select("Client", Module8State.filter_client, Module8State.client_options, Module8State.set_filter_client),
                _filter_select("Module of origin", Module8State.filter_source, Module8State.source_options, Module8State.set_filter_source),
                _filter_select("Type", Module8State.filter_type, Module8State.type_options, Module8State.set_filter_type, width="220px"),
                _filter_select("Priority", Module8State.filter_priority, Module8State.priority_options, Module8State.set_filter_priority, width="150px"),
                _filter_select("Assignee", Module8State.filter_assignee, Module8State.assignee_options, Module8State.set_filter_assignee),
                spacing="3",
                wrap="wrap",
                align="end",
            ),
            rx.hstack(
                rx.vstack(
                    rx.text("Min age (days)", style=t.TEXT["label"]),
                    rx.input(
                        value=Module8State.filter_min_age,
                        on_change=Module8State.set_filter_min_age,
                        width="120px",
                        type="number",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.button(
                    "Apply",
                    on_click=Module8State.apply_min_age,
                    size="2",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_PRIMARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                ),
                rx.button(
                    "Clear filters",
                    on_click=Module8State.clear_filters,
                    size="2",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_SECONDARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                ),
                spacing="3",
                align="end",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _batch_bar() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title("Batch action"),
            rx.text(
                f"{Module8State.eligible_count} item(s) eligible for batch approval · "
                f"{Module8State.excluded_count} excluded (materiality-gated).",
                style=t.TEXT["label"],
            ),
            rx.button(
                rx.text(
                    "Batch approve ",
                    Module8State.selected_refs.length().to_string(),
                    " selected",
                ),
                on_click=Module8State.batch_approve,
                disabled=Module8State.selected_refs.length() == 0,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
                _disabled={"opacity": "0.5", "cursor": "not-allowed"},
            ),
            rx.text(
                "Surfaces the originating module's own batch mechanism — the origin owns the decision.",
                style=t.TEXT["micro"],
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _assign_panel(row: FlaggedItemRow) -> rx.Component:
    return rx.vstack(
        rx.text(
            "Reassigning never changes priority — it stays visible and unchanged.",
            style=t.TEXT["micro"],
        ),
        rx.hstack(
            rx.vstack(
                rx.text("Assignee", style=t.TEXT["label"]),
                rx.input(
                    value=Module8State.assign_input,
                    on_change=Module8State.set_assign_input,
                    width="200px",
                ),
                spacing="1",
                align="start",
            ),
            rx.vstack(
                rx.text("Due date (YYYY-MM-DD)", style=t.TEXT["label"]),
                rx.input(
                    value=Module8State.due_input,
                    on_change=Module8State.set_due_input,
                    placeholder="2026-09-30",
                    width="180px",
                ),
                spacing="1",
                align="start",
            ),
            spacing="3",
            align="end",
            wrap="wrap",
        ),
        rx.hstack(
            rx.button(
                "Save assignment",
                on_click=Module8State.save_assignment,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
            ),
            rx.button(
                "Cancel",
                on_click=Module8State.close_assign,
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_SECONDARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="9px",
            ),
            spacing="2",
        ),
        rx.cond(
            Module8State.history.length() > 0,
            rx.vstack(
                rx.text("History", style=t.TEXT["label"], font_weight="700"),
                rx.foreach(
                    Module8State.history,
                    lambda h: rx.hstack(
                        rx.text(h.plain_language, style=t.TEXT["body"]),
                        rx.spacer(),
                        rx.text(h.timestamp, style=t.TEXT["micro"]),
                        width="100%",
                        padding="6px 0",
                        border_bottom=f"1px solid {t.Color.BORDER.value}",
                        align="center",
                    ),
                ),
                spacing="1",
                align="start",
                width="100%",
            ),
            rx.fragment(),
        ),
        spacing="3",
        align="start",
        width="100%",
    )


def _item_row(row: FlaggedItemRow) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                # Materiality-gated checkbox (disabled + inline reason).
                rx.cond(
                    row.materiality_gated,
                    rx.vstack(
                        rx.checkbox(
                            checked=False,
                            disabled=True,
                            opacity="0.5",
                        ),
                        spacing="1",
                        align="start",
                        min_width="34px",
                    ),
                    rx.checkbox(
                        checked=Module8State.selected_refs.contains(row.item_ref),
                        on_change=Module8State.toggle_select(row.item_ref),
                        min_width="34px",
                    ),
                ),
                rx.vstack(
                    rx.hstack(
                        rx.text(row.type, style=t.TEXT["body"], font_weight="700"),
                        rx.text(
                            f"{row.origin_label} · {row.client_name}",
                            style=t.TEXT["micro"],
                        ),
                        spacing="2",
                        align="baseline",
                        wrap="wrap",
                    ),
                    rx.hstack(
                        rx.cond(
                            row.confidence_source == "ai",
                            c.confidence_badge("ai", pct=row.confidence_pct),
                            c.confidence_badge("rule", label=row.confidence_label),
                        ),
                        _priority_pill(row.priority_variant, row.priority_label),
                        rx.cond(
                            row.age_escalated,
                            c.pill("Ageing — escalated", variant="danger"),
                            rx.fragment(),
                        ),
                        rx.cond(
                            row.stale,
                            c.pill("May be based on incomplete data", variant="ai"),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="center",
                        wrap="wrap",
                    ),
                    rx.cond(
                        row.reason != "",
                        rx.text(row.reason, style=t.TEXT["body"]),
                        rx.fragment(),
                    ),
                    rx.cond(
                        row.evidence_summary != "",
                        rx.text(row.evidence_summary, style=t.TEXT["micro"]),
                        rx.fragment(),
                    ),
                    rx.cond(
                        row.materiality_gated,
                        c.inline_reason(row.exclusion_reason),
                        rx.fragment(),
                    ),
                    spacing="2",
                    align="start",
                    flex="1",
                ),
                rx.vstack(
                    rx.text(f"Age: {row.age_days} day(s)", style=t.TEXT["micro"]),
                    rx.text(
                        f"Assignee: {rx.cond(row.assignee != '', row.assignee, 'Unassigned')}",
                        style=t.TEXT["micro"],
                    ),
                    rx.text(
                        f"Due: {rx.cond(row.due_date != '', row.due_date, '—')}",
                        style=t.TEXT["micro"],
                    ),
                    rx.hstack(
                        rx.button(
                            "Open in origin",
                            on_click=Module8State.open_in_origin(row.item_ref),
                            size="1",
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_PRIMARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="8px",
                        ),
                        rx.button(
                            "Assign",
                            on_click=Module8State.open_assign(row.item_ref, row.assignee, row.due_date),
                            size="1",
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="8px",
                        ),
                        spacing="2",
                    ),
                    spacing="2",
                    align="start",
                    min_width="180px",
                ),
                width="100%",
                align="start",
                spacing="3",
            ),
            rx.cond(
                Module8State.expanded_ref == row.item_ref,
                rx.box(
                    _assign_panel(row),
                    padding_top="8px",
                    border_top=f"1px solid {t.Color.BORDER.value}",
                    width="100%",
                ),
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _future_sources() -> rx.Component:
    return rx.vstack(
        c.divider(),
        rx.text(
            "This queue is built against the shared Flagged Item interface. "
            "Additional sources slot in without a rebuild:",
            style=t.TEXT["micro"],
        ),
        rx.hstack(
            rx.foreach(Module8State.future_sources, lambda m: c.placeholder_badge(m)),
            spacing="2",
            wrap="wrap",
        ),
        spacing="2",
        align="start",
        width="100%",
    )


def action_center_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Action Center",
                "The single working queue — every open Flagged Item, from every source module, in one place.",
            ),
            rx.cond(Module8State.flash != "", c.info_banner(Module8State.flash), rx.fragment()),
            rx.cond(Module8State.error != "", c.inline_reason(Module8State.error), rx.fragment()),
            _headline(),
            _grouped(),
            _filters(),
            _batch_bar(),
            rx.cond(
                Module8State.filtered_count > 0,
                rx.vstack(
                    rx.foreach(Module8State.items, _item_row),
                    spacing="3",
                    width="100%",
                ),
                c.empty_state("No items match these filters.", icon="inbox"),
            ),
            _future_sources(),
            spacing="4",
            width="100%",
            align="start",
        )
    )