"""C3-ext — AI Model & Provider Configuration (multi-provider).

Provider management (one row per provider, each with its own key + status)
sits ABOVE the per-touchpoint assignment table on the same screen. Each
touchpoint row carries two INDEPENDENT (provider, model) selector pairs —
Primary and Fallback — plus a live per-leg Test call and a Recent Fallbacks
panel. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.ai_models_state import AiModelsState
from setu.views import shell

_CATEGORY_ORDER = ["Ingestion", "Extraction", "Analysis"]

_SOFT_BUTTON = {
    "variant": "soft",
    "background": "transparent",
    "color": t.Color.TEXT_PRIMARY.value,
    "border": f"1px solid {t.Color.BORDER.value}",
    "border_radius": "9px",
    "_hover": {"background": "#EEF2F8", "border_color": t.Color.ACCENT.value},
}


def _headline() -> rx.Component:
    return c.card(
        rx.hstack(
            c.stat(AiModelsState.total.to_string(), "AI touchpoints"),
            c.stat(AiModelsState.providers_configured.to_string(), "Providers with a key"),
            c.stat(AiModelsState.unassigned.to_string(), "Unassigned (active)"),
            c.stat(AiModelsState.inactive.to_string(), "Placeholders"),
            spacing="8",
            width="100%",
            align="start",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------


def _provider_row(p) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.text(p.label, style=t.TEXT["card_title"]),
                rx.text(
                    p.provider_key + " · " + p.catalog_mode + " catalogue · "
                    + p.model_count.to_string() + " model(s)",
                    style=t.TEXT["micro"],
                ),
                spacing="1",
                align="start",
            ),
            rx.spacer(),
            rx.cond(
                p.has_key,
                rx.text(p.masked_ref, style=t.TEXT["micro"]),
                rx.text("No key stored", style=t.TEXT["micro"]),
            ),
            c.live_status_chip(p.chip_status, label=p.status_label),
            spacing="3",
            align="center",
            width="100%",
        ),
        rx.cond(
            p.status_detail != "",
            rx.text(p.status_detail, style=t.TEXT["micro"]),
            rx.fragment(),
        ),
        rx.hstack(
            rx.input(
                value=AiModelsState.provider_key_input[p.provider_key],
                on_change=lambda v: AiModelsState.set_key_input(p.provider_key, v),
                placeholder="Enter API key",
                type="password",
                flex="1",
            ),
            rx.button(
                rx.cond(p.has_key, "Replace key", "Save key"),
                on_click=AiModelsState.save_provider_key(p.provider_key),
                disabled=~AiModelsState.can_manage,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
                border_radius="9px",
            ),
            rx.button(
                "Verify",
                on_click=AiModelsState.verify_provider(p.provider_key),
                disabled=~AiModelsState.can_manage,
                **_SOFT_BUTTON,
            ),
            rx.button(
                "Refresh models",
                on_click=AiModelsState.refresh_provider_catalog(p.provider_key),
                disabled=~AiModelsState.can_manage,
                **_SOFT_BUTTON,
            ),
            spacing="3",
            width="100%",
        ),
        spacing="2",
        align="start",
        width="100%",
        padding="12px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _provider_section() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Providers", style=t.TEXT["section_title"]),
            rx.text(
                "Each provider has its own API key and connection status. A key is entered "
                "once here and reused by every touchpoint's Primary and Fallback selectors below. "
                "A provider's key becoming invalid degrades only the touchpoints that use it.",
                style=t.TEXT["micro"],
            ),
            rx.vstack(rx.foreach(AiModelsState.providers, _provider_row), spacing="1", width="100%"),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Per-touchpoint legs
# ---------------------------------------------------------------------------


def _provider_select(value, on_change) -> rx.Component:
    """Provider dropdown. Providers with no valid key are shown DISABLED with
    the reason surfaced inline beneath — never silently hidden."""
    return rx.select.root(
        rx.select.trigger(placeholder="Provider", width="100%"),
        rx.select.content(
            rx.select.group(
                rx.foreach(
                    AiModelsState.provider_options,
                    lambda o: rx.select.item(o.label, value=o.key, disabled=o.disabled),
                )
            )
        ),
        value=value,
        on_change=on_change,
        width="100%",
    )


def _model_select(options, value, on_change) -> rx.Component:
    return rx.select(
        options,
        value=value,
        on_change=on_change,
        width="100%",
    )


def _leg_column(row, leg: str) -> rx.Component:
    if leg == "primary":
        provider_value = AiModelsState.edit_primary_provider[row.touchpoint_key]
        model_value = AiModelsState.edit_primary_model[row.touchpoint_key]
        on_provider = lambda v: AiModelsState.set_edit_primary_provider(row.touchpoint_key, v)
        on_model = lambda v: AiModelsState.set_edit_primary_model(row.touchpoint_key, v)
        test_ok = AiModelsState.test_primary_ok[row.touchpoint_key]
        test_text = AiModelsState.test_primary_text[row.touchpoint_key]
    else:
        provider_value = AiModelsState.edit_fallback_provider[row.touchpoint_key]
        model_value = AiModelsState.edit_fallback_model[row.touchpoint_key]
        on_provider = lambda v: AiModelsState.set_edit_fallback_provider(row.touchpoint_key, v)
        on_model = lambda v: AiModelsState.set_edit_fallback_model(row.touchpoint_key, v)
        test_ok = AiModelsState.test_fallback_ok[row.touchpoint_key]
        test_text = AiModelsState.test_fallback_text[row.touchpoint_key]

    label = "Primary" if leg == "primary" else "Fallback"
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        _provider_select(provider_value, on_provider),
        _model_select(
            AiModelsState.provider_model_options[provider_value], model_value, on_model,
        ),
        rx.cond(
            AiModelsState.provider_has_key[provider_value],
            rx.fragment(),
            c.inline_reason(
                "Add an API key for " + AiModelsState.provider_label[provider_value] + " first"
            ),
        ),
        rx.hstack(
            rx.button(
                "Test " + label.lower() + " leg",
                on_click=AiModelsState.test_leg(row.touchpoint_key, leg),
                disabled=~AiModelsState.can_manage,
                **_SOFT_BUTTON,
            ),
            spacing="2",
            align="center",
        ),
        rx.cond(
            test_text != "",
            rx.cond(
                test_ok,
                rx.text(test_text, style=t.TEXT["micro"], color=t.Color.RULE.value),
                rx.text(test_text, style=t.TEXT["micro"], color=t.Color.DANGER.value),
            ),
            rx.fragment(),
        ),
        spacing="2",
        align="start",
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
                c.view_history(
                    "View History",
                    AiModelsState.touchpoint_history[row.touchpoint_key],
                    count=AiModelsState.touchpoint_history[row.touchpoint_key].length(),
                ),
                rx.cond(
                    row.is_active,
                    c.accent_pill("Active"),
                    c.placeholder_badge(row.placeholder_module),
                ),
                width="100%",
                align="center",
                spacing="3",
            ),
            rx.cond(row.description != "", rx.text(row.description, style=t.TEXT["label"]), rx.fragment()),
            rx.cond(
                row.is_active,
                rx.vstack(
                    rx.grid(
                        _leg_column(row, "primary"),
                        _leg_column(row, "fallback"),
                        columns="2",
                        spacing="4",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.input(
                            value=AiModelsState.edit_custom_primary[row.touchpoint_key],
                            on_change=lambda v: AiModelsState.set_edit_custom_primary(row.touchpoint_key, v),
                            placeholder="Or enter a primary model id",
                            flex="1",
                        ),
                        rx.input(
                            value=AiModelsState.edit_custom_fallback[row.touchpoint_key],
                            on_change=lambda v: AiModelsState.set_edit_custom_fallback(row.touchpoint_key, v),
                            placeholder="Or enter a fallback model id",
                            flex="1",
                        ),
                        width="100%",
                        spacing="3",
                    ),
                    rx.hstack(
                        rx.button(
                            "Save assignment",
                            on_click=AiModelsState.save_assignment(row.touchpoint_key),
                            disabled=~AiModelsState.can_manage,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                            border_radius="9px",
                        ),
                        rx.cond(
                            row.updated_at != "",
                            rx.text(
                                "Last changed " + row.updated_at + " by " + row.updated_by + ".",
                                style=t.TEXT["micro"],
                            ),
                            rx.fragment(),
                        ),
                        spacing="3",
                        align="center",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                rx.text(
                    "Not yet active — this touchpoint is a placeholder and is not called by the platform.",
                    style=t.TEXT["micro"],
                ),
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


# ---------------------------------------------------------------------------
# Recent Fallbacks
# ---------------------------------------------------------------------------


def _fallback_row(r) -> rx.Component:
    return rx.hstack(
        rx.text(r.when, style=t.TEXT["micro"], min_width="140px"),
        rx.text(r.touchpoint_key, style=t.TEXT["body"], min_width="170px"),
        c.accent_pill(r.provider),
        rx.text(r.model, style=t.TEXT["micro"], flex="1"),
        rx.cond(
            r.reason != "",
            rx.text(r.reason, style=t.TEXT["micro"], flex="1"),
            rx.fragment(),
        ),
        spacing="3",
        align="center",
        width="100%",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _fallback_panel() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Recent fallbacks", style=t.TEXT["card_title"]),
            rx.text(
                "When a touchpoint's primary leg fails, the call is retried on its fallback leg. "
                "Each event records the provider that actually resolved the call.",
                style=t.TEXT["micro"],
            ),
            rx.cond(
                AiModelsState.fallback_events.length() > 0,
                rx.vstack(
                    rx.foreach(AiModelsState.fallback_events, _fallback_row),
                    spacing="0",
                    width="100%",
                ),
                c.empty_state("No fallbacks yet — every call resolved on its primary leg."),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def ai_models_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "AI Models",
                "Multi-provider AI configuration. Register a key per provider, then assign an "
                "independent Primary and Fallback (provider, model) pair to each touchpoint.",
            ),
            rx.cond(AiModelsState.flash != "", c.info_banner(AiModelsState.flash), rx.fragment()),
            rx.cond(AiModelsState.error != "", c.inline_reason(AiModelsState.error), rx.fragment()),
            _headline(),
            _provider_section(),
            rx.cond(
                AiModelsState.unassigned > 0,
                c.warning_banner(
                    AiModelsState.unassigned.to_string()
                    + " active touchpoint(s) have no model assigned. Calls to those touchpoints will "
                    "fall back to their module's own default."
                ),
                rx.fragment(),
            ),
            rx.cond(
                AiModelsState.can_manage,
                rx.text(
                    "The catalogue is a convenience, not a constraint: any valid model id is accepted "
                    "even if it isn't listed. A fallback may use a different provider from the primary.",
                    style=t.TEXT["micro"],
                ),
                c.inline_reason("Requires Admin or Partner — changing a provider affects every run."),
            ),
            *[_category_block(cat) for cat in _CATEGORY_ORDER],
            _fallback_panel(),
            spacing="5",
            width="100%",
            align="start",
        )
    )
