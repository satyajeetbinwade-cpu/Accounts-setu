"""Module 2 — Reconciliation Engine (2A GST / 2B TDS / 2C Other).

AI-FIRST: the exception detail leads with the AI analysis (issue summary,
probable causes, suggested fix, reasoning, confidence) before the raw
books-vs-portal comparison. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.module2_state import Module2State
from setu.views import shell

_HUB = ["Reconciliation overview", "Exception queue", "Matching configuration", "TDS classifier"]
_CLASSIFICATIONS = ["Amount Difference", "Not in Portal", "Not in Books"]


def _hub_button(label: str) -> rx.Component:
    active = Module2State.section == label
    return rx.button(
        label,
        on_click=Module2State.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _client_select() -> rx.Component:
    return rx.hstack(
        rx.text("Client", style=t.TEXT["label"]),
        rx.select(
            Module2State.client_options.map(lambda o: o.legal_name),
            on_change=Module2State.set_client_by_name,
            width="260px",
        ),
        spacing="2",
        align="center",
    )


def _priority_pill(priority, label) -> rx.Component:
    return rx.match(
        priority,
        ("escalated", c.pill(label, variant="danger")),
        ("high", c.pill(label, variant="ai")),
        c.pill(label, variant="placeholder"),
    )


def _status_pill(status, label) -> rx.Component:
    return rx.match(
        status,
        ("open", c.pill(label, variant="accent")),
        ("resolved", c.pill(label, variant="rule")),
        ("escalated", c.pill(label, variant="danger")),
        c.pill(label, variant="placeholder"),
    )


# ---------------------------------------------------------------------------
# Overview — headline (eligible credit) → grouped → detail
# ---------------------------------------------------------------------------


def _overview() -> rx.Component:
    return rx.vstack(
        c.card(
            rx.vstack(
                rx.cond(
                    Module2State.has_credit,
                    rx.vstack(
                        c.stat(Module2State.eligible_credit, "Eligible credit as things stand (2A GST)", accent=True),
                        rx.text(
                            f"Recalculated on each run · period {Module2State.eligible_period}",
                            style=t.TEXT["micro"],
                        ),
                        rx.hstack(
                            _metric("Total ITC claimed", Module2State.total_itc),
                            _metric("Matched ITC", Module2State.matched_itc),
                            _metric("At-risk ITC", Module2State.at_risk_itc),
                            _metric("Blocked (17(5))", Module2State.blocked_itc),
                            spacing="6",
                            wrap="wrap",
                            padding_top="8px",
                        ),
                        rx.cond(
                            Module2State.reverse_charge_itc != "₹0.00",
                            rx.text(
                                f"Reverse-charge ITC tracked separately: {Module2State.reverse_charge_itc} "
                                "(claimable only once the tax is paid by the recipient).",
                                style=t.TEXT["micro"],
                            ),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="start",
                        width="100%",
                    ),
                    rx.vstack(
                        c.stat("—", "Eligible credit — no matching run yet for this client"),
                        spacing="1",
                        align="start",
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
                c.section_title(
                    "Exceptions by classification",
                    "Click a group to open the pre-filtered queue.",
                ),
                rx.hstack(
                    *[
                        rx.button(
                            rx.hstack(
                                rx.text(cls, font_size="13px", font_weight="600"),
                                c.pill(count, variant="placeholder"),
                                spacing="2",
                                align="center",
                            ),
                            on_click=Module2State.goto_classification(cls),
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_PRIMARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="10px",
                            padding="10px 16px",
                            _hover={"background": "#EEF2F8", "border_color": t.Color.ACCENT.value},
                        )
                        for cls, count in [
                            ("Amount Difference", Module2State.count_amount_difference.to_string()),
                            ("Not in Portal", Module2State.count_not_in_portal.to_string()),
                            ("Not in Books", Module2State.count_not_in_books.to_string()),
                        ]
                    ],
                    spacing="3",
                    wrap="wrap",
                ),
                rx.hstack(
                    rx.text(f"{Module2State.open_exceptions} open", style=t.TEXT["label"]),
                    rx.text("·", style=t.TEXT["label"]),
                    rx.text(f"{Module2State.total_exceptions} total", style=t.TEXT["label"]),
                    spacing="2",
                    align="center",
                ),
                rx.cond(
                    Module2State.escalated_exceptions > 0,
                    c.warning_banner(
                        f"{Module2State.escalated_exceptions} exception(s) are above the materiality "
                        "threshold and have been escalated."
                    ),
                    rx.fragment(),
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


def _metric(label, value) -> rx.Component:
    return rx.vstack(
        rx.text(value, font_size="18px", font_weight="700", color=t.Color.TEXT_PRIMARY.value),
        rx.text(label, style=t.TEXT["micro"]),
        spacing="0",
        align="start",
    )


# ---------------------------------------------------------------------------
# Exception queue
# ---------------------------------------------------------------------------


def _exception_row(e) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.hstack(
                rx.text(
                    f"#{e.exception_id} · {e.classification}",
                    style=t.TEXT["body"],
                    font_weight="600",
                ),
                rx.cond(
                    e.difference_type != "",
                    rx.text(f"· {e.difference_type}", style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                spacing="2",
                align="baseline",
                wrap="wrap",
            ),
            rx.hstack(
                c.pill(e.sub_type_label, variant="placeholder"),
                rx.cond(
                    e.confidence_source == "ai",
                    c.confidence_badge("ai", pct=e.confidence_score),
                    c.confidence_badge("rule", label=f"Rule match — {e.confidence_band}"),
                ),
                rx.cond(
                    e.above_materiality,
                    c.pill("Above materiality", variant="danger"),
                    rx.fragment(),
                ),
                rx.cond(
                    e.recommendation != "",
                    c.pill(f"IMS: {e.recommendation}", variant="accent"),
                    rx.fragment(),
                ),
                spacing="2",
                align="center",
                wrap="wrap",
            ),
            rx.cond(
                e.match_reason != "",
                rx.text(e.match_reason, style=t.TEXT["micro"]),
                rx.fragment(),
            ),
            spacing="2",
            align="start",
            flex="1",
        ),
        _priority_pill(e.priority, e.priority),
        _status_pill(e.status, e.status),
        rx.button(
            "Open",
            on_click=Module2State.open_exception(e.exception_id),
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
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _queue() -> rx.Component:
    return rx.cond(
        Module2State.selected_exception_id != 0,
        _detail(),
        c.card(
            rx.vstack(
                c.section_title(
                    "Exception queue",
                    "Module 2's own scoped view. Module 8 (Action Center) owns the cross-module queue.",
                ),
                rx.hstack(
                    rx.vstack(
                        rx.text("Sub-scope", style=t.TEXT["label"]),
                        rx.select(
                            ["All", "2A", "2B", "2C"],
                            value=Module2State.filter_sub_type,
                            on_change=Module2State.set_filter_sub_type,
                            width="140px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.vstack(
                        rx.text("Classification", style=t.TEXT["label"]),
                        rx.select(
                            ["All"] + _CLASSIFICATIONS,
                            value=Module2State.filter_classification,
                            on_change=Module2State.set_filter_classification,
                            width="200px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    spacing="3",
                    align="end",
                    wrap="wrap",
                ),
                rx.cond(
                    Module2State.exceptions.length() > 0,
                    rx.vstack(rx.foreach(Module2State.exceptions, _exception_row), spacing="0", width="100%"),
                    c.empty_state("No exceptions match this filter.", icon="circle_check"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
    )


# ---------------------------------------------------------------------------
# Exception detail — AI-FIRST
# ---------------------------------------------------------------------------


def _record_panel(title: str, fields) -> rx.Component:
    return rx.vstack(
        rx.text(title, style=t.TEXT["label"], font_weight="700"),
        rx.cond(
            fields.length() > 0,
            rx.vstack(
                rx.foreach(
                    fields,
                    lambda f: rx.hstack(
                        rx.text(f.label, style=t.TEXT["micro"], min_width="120px"),
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
    )


def _ai_panel() -> rx.Component:
    """The AI-first lead panel: analysis before the raw comparison."""
    return rx.cond(
        Module2State.ai_eligible,
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.icon("sparkles", size=16, color=t.Color.AI_ON.value),
                    rx.text("AI analysis", style=t.TEXT["card_title"]),
                    rx.spacer(),
                    rx.cond(
                        Module2State.ai_status == "success",
                        rx.hstack(
                            c.confidence_badge("ai", pct=Module2State.ai_confidence),
                            rx.cond(
                                Module2State.ai_cached,
                                c.pill("cached", variant="placeholder"),
                                rx.fragment(),
                            ),
                            spacing="2",
                            align="center",
                        ),
                        rx.fragment(),
                    ),
                    width="100%",
                    align="center",
                ),
                rx.cond(
                    Module2State.ai_status == "success",
                    rx.vstack(
                        rx.cond(
                            ~Module2State.ai_data_sufficient,
                            c.warning_banner(
                                "AI — insufficient data: this item needs information the system does not have."
                            ),
                            rx.fragment(),
                        ),
                        rx.text(Module2State.ai_issue_summary, style=t.TEXT["body"], font_weight="600"),
                        rx.cond(
                            Module2State.ai_causes.length() > 0,
                            rx.vstack(
                                rx.foreach(
                                    Module2State.ai_causes,
                                    lambda cause: rx.hstack(
                                        rx.text("•", style=t.TEXT["body"]),
                                        rx.text(cause, style=t.TEXT["body"]),
                                        spacing="2",
                                        align="start",
                                    ),
                                ),
                                spacing="1",
                                align="start",
                                width="100%",
                            ),
                            rx.fragment(),
                        ),
                        rx.cond(
                            Module2State.ai_suggested_fix != "",
                            rx.hstack(
                                rx.text("Suggested fix:", style=t.TEXT["label"], font_weight="700"),
                                rx.text(Module2State.ai_suggested_fix, style=t.TEXT["body"]),
                                spacing="2",
                                align="baseline",
                                wrap="wrap",
                            ),
                            rx.fragment(),
                        ),
                        rx.cond(
                            Module2State.ai_reasoning != "",
                            rx.accordion.root(
                                rx.accordion.item(
                                    header="Why the model concluded this",
                                    content=rx.text(Module2State.ai_reasoning, style=t.TEXT["body"]),
                                    value="reasoning",
                                ),
                                collapsible=True,
                                width="100%",
                            ),
                            rx.fragment(),
                        ),
                        rx.hstack(
                            rx.text(f"model: {Module2State.ai_model}", style=t.TEXT["micro"]),
                            rx.spacer(),
                            rx.button(
                                "Re-analyze",
                                on_click=Module2State.analyze(True),
                                size="1",
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_SECONDARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                            ),
                            width="100%",
                            align="center",
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    rx.cond(
                        Module2State.ai_status == "failed",
                        rx.vstack(
                            c.inline_reason(
                                f"AI analysis failed ({Module2State.ai_error_type})"
                            ),
                            rx.cond(
                                Module2State.ai_error_detail != "",
                                rx.text(Module2State.ai_error_detail, style=t.TEXT["micro"]),
                                rx.fragment(),
                            ),
                            rx.button(
                                "Retry",
                                on_click=Module2State.analyze(True),
                                size="1",
                                variant="soft",
                                background="transparent",
                                color=t.Color.TEXT_SECONDARY.value,
                                border=f"1px solid {t.Color.BORDER.value}",
                                border_radius="8px",
                            ),
                            spacing="2",
                            align="start",
                        ),
                        rx.vstack(
                            rx.text(
                                f"Would route to model: {Module2State.ai_route_hint}",
                                style=t.TEXT["micro"],
                            ),
                            rx.button(
                                "Analyze this item",
                                on_click=Module2State.analyze(False),
                                background=t.Color.ACCENT.value,
                                color="#FFFFFF",
                            ),
                            spacing="2",
                            align="start",
                        ),
                    ),
                ),
                rx.text(
                    "On-demand review aid. Nothing is posted or corrected automatically — an accountant reviews and acts.",
                    style=t.TEXT["micro"],
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _detail() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back to Exception queue",
            on_click=Module2State.back_to_queue,
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
                            f"Exception #{Module2State.selected_exception_id} — {Module2State.detail_classification}",
                            style=t.TEXT["card_title"],
                        ),
                        rx.hstack(
                            c.pill(Module2State.detail_sub_type_label, variant="placeholder"),
                            rx.cond(
                                Module2State.detail_difference_type != "",
                                c.pill(Module2State.detail_difference_type, variant="accent"),
                                rx.fragment(),
                            ),
                            spacing="2",
                            align="center",
                        ),
                        spacing="2",
                        align="start",
                    ),
                    rx.spacer(),
                    _priority_pill(Module2State.detail_priority, Module2State.detail_priority),
                    _status_pill(Module2State.detail_status, Module2State.detail_status),
                    width="100%",
                    align="center",
                ),
                rx.cond(
                    Module2State.detail_above_materiality,
                    c.warning_banner(
                        f"Above the materiality threshold ({Module2State.detail_materiality}) — "
                        "escalating routes it into Module 8's escalation/priority mechanism."
                    ),
                    rx.fragment(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        # AI-FIRST: analysis leads.
        _ai_panel(),
        c.card(
            rx.vstack(
                c.section_title("Books vs Portal"),
                rx.hstack(
                    rx.box(_record_panel("Books side", Module2State.books_fields), flex="1"),
                    rx.box(_record_panel("Portal side", Module2State.portal_fields), flex="1"),
                    spacing="5",
                    width="100%",
                    align="start",
                ),
                rx.cond(
                    Module2State.detail_match_reason != "",
                    rx.vstack(
                        rx.text("Match reasoning", style=t.TEXT["label"], font_weight="700"),
                        rx.text(Module2State.detail_match_reason, style=t.TEXT["body"]),
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
            width="100%",
        ),
        # TDS six-step chain (2B only)
        rx.cond(
            Module2State.detail_sub_type == "2B",
            c.card(
                rx.vstack(
                    c.section_title(
                        "TDS six-step chain",
                        "Applicability → Rate → Deduction → Deposit → Return → 26AS reflection.",
                    ),
                    c.tds_stepper(Module2State.chain_stages),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        # IMS recommendation (2A only)
        rx.cond(
            Module2State.detail_sub_type == "2A",
            c.card(
                rx.vstack(
                    c.section_title("IMS recommendation"),
                    rx.cond(
                        Module2State.detail_recommendation != "",
                        rx.vstack(
                            c.accent_pill(f"Recommend: {Module2State.detail_recommendation}"),
                            rx.text(Module2State.detail_recommendation_reason, style=t.TEXT["body"]),
                            spacing="2",
                            align="start",
                        ),
                        rx.text("No IMS recommendation recorded.", style=t.TEXT["micro"]),
                    ),
                    rx.text(
                        "Module 2 recommends only — it never submits. C2's explicit Send gate executes once this is approved.",
                        style=t.TEXT["micro"],
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        _resolve_panel(),
        spacing="4",
        width="100%",
        align="start",
    )


def _resolve_panel() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title("Resolve"),
            rx.cond(
                Module2State.can_resolve,
                rx.vstack(
                    c.reason_capture(
                        label="Resolution note (optional)",
                        value=Module2State.resolve_note,
                        on_change=Module2State.set_resolve_note,
                        placeholder="Why are you resolving this?",
                    ),
                    rx.hstack(
                        rx.button(
                            "Accept",
                            on_click=Module2State.resolve("accepted"),
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.button(
                            "Reject",
                            on_click=Module2State.resolve("rejected"),
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_SECONDARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="9px",
                        ),
                        rx.button(
                            "Escalate",
                            on_click=Module2State.resolve("escalated"),
                            variant="soft",
                            background="#FDEBEA",
                            color=t.Color.DANGER.value,
                            border=f"1px solid {t.Color.DANGER.value}",
                            border_radius="9px",
                        ),
                        spacing="2",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                rx.vstack(
                    rx.button(
                        "Accept",
                        disabled=True,
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                    ),
                    c.inline_reason("Requires Senior Accountant or above."),
                    spacing="1",
                    align="start",
                ),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Matching configuration
# ---------------------------------------------------------------------------


def _config_row(cfg) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(cfg.label, style=t.TEXT["body"], font_weight="600"),
            rx.text(cfg.match_keys, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.text(cfg.tolerance, style=t.TEXT["label"], white_space="nowrap"),
        rx.text(cfg.date_tolerance, style=t.TEXT["label"], white_space="nowrap"),
        rx.text(f"fuzzy {cfg.fuzzy_threshold}", style=t.TEXT["label"], white_space="nowrap"),
        rx.text(cfg.materiality, style=t.TEXT["label"], white_space="nowrap"),
        width="100%",
        align="center",
        spacing="4",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _configs() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "Matching configuration",
                "Per sub-type matching keys and tolerances, read from C1. The six 2C sources "
                "layer onto the SAME shared engine validated in 2A/2B.",
            ),
            rx.hstack(
                rx.text("Sub-type", style=t.TEXT["micro"], flex="1"),
                rx.text("Amount tol.", style=t.TEXT["micro"]),
                rx.text("Date tol.", style=t.TEXT["micro"]),
                rx.text("Fuzzy", style=t.TEXT["micro"]),
                rx.text("Materiality", style=t.TEXT["micro"]),
                width="100%",
                spacing="4",
                padding="0 0 4px 0",
            ),
            rx.cond(
                Module2State.configs.length() > 0,
                rx.vstack(rx.foreach(Module2State.configs, _config_row), spacing="0", width="100%"),
                c.empty_state("No matching-key configurations seeded.", icon="settings"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# TDS classifier (AI touchpoint)
# ---------------------------------------------------------------------------


def _tds_classifier() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                "TDS section classification from narration",
                "The one judgment-adjacent AI step. The model suggests a section from narration "
                "text; the rate is ALWAYS looked up deterministically from C1 — the model never "
                "invents a rate.",
            ),
            rx.input(
                value=Module2State.narration,
                on_change=Module2State.set_narration,
                placeholder="e.g. Professional fees for legal advisory services",
                width="100%",
            ),
            rx.button(
                "Classify",
                on_click=Module2State.classify_narration,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
            ),
            rx.cond(
                Module2State.tds_section != "",
                rx.vstack(
                    rx.hstack(
                        c.pill(f"Section {Module2State.tds_section}", variant="accent"),
                        c.pill(f"Rate {Module2State.tds_rate} (from C1)", variant="rule"),
                        rx.cond(
                            Module2State.tds_c5_used,
                            c.pill("C5 context used", variant="ai"),
                            rx.fragment(),
                        ),
                        spacing="2",
                        align="center",
                        wrap="wrap",
                    ),
                    rx.text(Module2State.tds_reasoning, style=t.TEXT["body"]),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                rx.cond(
                    Module2State.narration != "",
                    c.inline_reason("AI unavailable — proceed manually."),
                    rx.fragment(),
                ),
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


def module2_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Reconciliation Engine",
                "Module 2 — 2A GST · 2B TDS · 2C Other. Extends Phase 1's matching engine; "
                "every non-clean match becomes an exception.",
            ),
            _client_select(),
            rx.cond(Module2State.flash != "", c.info_banner(Module2State.flash), rx.fragment()),
            rx.cond(Module2State.error != "", c.inline_reason(Module2State.error), rx.fragment()),
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
                Module2State.section,
                ("Exception queue", _queue()),
                ("Matching configuration", _configs()),
                ("TDS classifier", _tds_classifier()),
                _overview(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )
