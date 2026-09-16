"""C3-ext — AI Model & Provider Configuration.

The central model registry: headline (touchpoint count / unassigned / catalogue
source) → grouped by category → one card per touchpoint with primary + fallback
pickers. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.ai_models_state import AiModelsState
from setu.views import shell

_CATEGORY_ORDER = ["Ingestion", "Extraction", "Analysis"]


def _headline() -> rx.Component:
    return c.card(
        rx.hstack(
            c.stat(AiModelsState.total.to_string(), "AI touchpoints"),
            c.stat(AiModelsState.unassigned.to_string(), "Unassigned"),
            rx.vstack(
                rx.text("Model catalogue", style=t.TEXT["label"]),
                rx.cond(
                    AiModelsState.catalog_live,
                    c.live_status_chip("connected", label=f"{AiModelsState.catalog_count} models"),
                    c.live_status_chip("needs_reauth", label="Bundled list"),
                ),
                rx.text(
                    rx.cond(
                        AiModelsState.catalog_live,
                        f"Fetched from OpenRouter {AiModelsState.catalog_fetched}.",
                        "Showing the bundled fallback list. Refresh to load OpenRouter's full catalogue.",
                    ),
                    style=t.TEXT["micro"],
                ),
                spacing="1",
                align="start",
            ),
            spacing="8",
            width="100%",
            align="start",
        ),
        width="100%",
    )


def _model_select(key, value, on_change, options, *, disabled) -> rx.Component:
    return rx.select(
        options,
        value=value,
        on_change=on_change,
        disabled=disabled,
        width="100%",
    )


def _touchpoint_card(row) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text(row.label, style=t.TEXT["card_title"]),
                    rx.text(row.touchpoint_key, style=t.TEXT["micro"]),
                    spacing="1",
                    align="start",
                ),
                rx.spacer(),
                # F4 — the reusable View History trigger, inline next to the touchpoint.
                c.view_history(
                    "View History",
                    AiModelsState.touchpoint_history[row.touchpoint_key],
                    count=AiModelsState.touchpoint_history[row.touchpoint_key].length(),
                ),
                rx.cond(
                    row.is_active,
                    rx.cond(
                        row.primary_model != "",
                        c.accent_pill(f"Primary: {row.primary_model}"),
                        c.inline_reason("No primary model assigned."),
                    ),
                    c.accent_pill("Inactive"),
                ),
                width="100%",
                align="center",
                spacing="3",
            ),
            rx.cond(row.description != "", rx.text(row.description, style=t.TEXT["label"]), rx.fragment()),
            rx.grid(
                rx.vstack(
                    rx.text("Primary model", style=t.TEXT["label"]),
                    _model_select(
                        row.touchpoint_key,
                        AiModelsState.edit_primary[row.touchpoint_key],
                        lambda v: AiModelsState.set_edit_primary(row.touchpoint_key, v),
                        AiModelsState.options.map(lambda o: o.model_id),
                        disabled=~AiModelsState.can_manage,
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.vstack(
                    rx.text("Fallback model", style=t.TEXT["label"]),
                    _model_select(
                        row.touchpoint_key,
                        AiModelsState.edit_fallback[row.touchpoint_key],
                        lambda v: AiModelsState.set_edit_fallback(row.touchpoint_key, v),
                        AiModelsState.options.map(lambda o: o.model_id),
                        disabled=~AiModelsState.can_manage,
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                columns="2",
                spacing="4",
                width="100%",
            ),
            rx.hstack(
                rx.input(
                    value=AiModelsState.edit_custom_primary[row.touchpoint_key],
                    on_change=lambda v: AiModelsState.set_edit_custom_primary(row.touchpoint_key, v),
                    placeholder="Or enter a primary model id (vendor/model)",
                    disabled=~AiModelsState.can_manage,
                    flex="1",
                ),
                rx.input(
                    value=AiModelsState.edit_custom_fallback[row.touchpoint_key],
                    on_change=lambda v: AiModelsState.set_edit_custom_fallback(row.touchpoint_key, v),
                    placeholder="Or enter a fallback model id",
                    disabled=~AiModelsState.can_manage,
                    flex="1",
                ),
                width="100%",
                spacing="3",
            ),
            rx.hstack(
                rx.button(
                    "Save model assignment",
                    on_click=AiModelsState.save_assignment(row.touchpoint_key),
                    disabled=~AiModelsState.can_manage,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.cond(
                    row.updated_at != "",
                    rx.text(f"Last changed {row.updated_at} by {row.updated_by}.", style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                spacing="3",
                align="center",
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _category_block(category: str) -> rx.Component:
    rows = AiModelsState.assignments.filter(lambda a: a.category == category)
    return rx.cond(
        rows.length() > 0,
        rx.vstack(
            rx.text(category, style=t.TEXT["section_title"]),
            rx.vstack(rx.foreach(rows, _touchpoint_card), spacing="3", width="100%"),
            spacing="3",
            width="100%",
            align="start",
        ),
        rx.fragment(),
    )


def _change_log() -> rx.Component:
    return rx.cond(
        AiModelsState.change_log.length() > 0,
        c.card(
            rx.vstack(
                rx.text("Recent model changes", style=t.TEXT["card_title"]),
                rx.vstack(
                    rx.foreach(
                        AiModelsState.change_log,
                        lambda r: rx.hstack(
                            rx.text(f"{r.when} · {r.actor}:", style=t.TEXT["micro"]),
                            rx.text(r.detail, style=t.TEXT["body"]),
                            spacing="2",
                            align="baseline",
                            width="100%",
                            padding="6px 0",
                            border_bottom=f"1px solid {t.Color.BORDER.value}",
                        ),
                    ),
                    spacing="0",
                    width="100%",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def ai_models_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "AI Models",
                "Every AI touchpoint in the platform, and the model it calls. Pick from OpenRouter's live catalogue or enter an id directly.",
            ),
            rx.cond(AiModelsState.flash != "", c.info_banner(AiModelsState.flash), rx.fragment()),
            rx.cond(AiModelsState.error != "", c.inline_reason(AiModelsState.error), rx.fragment()),
            _headline(),
            rx.cond(
                AiModelsState.unassigned > 0,
                c.warning_banner(
                    f"{AiModelsState.unassigned} touchpoint(s) have no primary model. Calls to those touchpoints will fall back to their module's own default."
                ),
                rx.fragment(),
            ),
            rx.hstack(
                rx.button(
                    "Refresh model list",
                    on_click=AiModelsState.refresh_catalog,
                    disabled=~AiModelsState.can_manage,
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_PRIMARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="9px",
                    _hover={"background": "#EEF2F8", "border_color": t.Color.ACCENT.value},
                ),
                rx.cond(
                    AiModelsState.can_manage,
                    rx.text(
                        "The catalogue is a convenience, not a constraint: any valid vendor/model id is accepted even if it isn't listed yet.",
                        style=t.TEXT["micro"],
                    ),
                    c.inline_reason("Requires Admin or Partner — changing a model affects every run."),
                ),
                spacing="3",
                align="center",
                width="100%",
            ),
            *[_category_block(cat) for cat in _CATEGORY_ORDER],
            _change_log(),
            spacing="5",
            width="100%",
            align="start",
        )
    )