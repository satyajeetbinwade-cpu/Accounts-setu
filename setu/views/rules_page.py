"""C1 — Rules, Taxonomy & Regulatory Configuration.

Hub: Rules Workspace (5 categories, firm-wide + per-client override),
Taxonomy Editor, Regulatory Rules Table. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.rules_state import CATEGORY_LABELS, RulesState
from setu.views import shell

_HUB = ["Rules Workspace", "Taxonomy Editor", "Regulatory Rules Table"]


def _hub_button(label: str) -> rx.Component:
    active = RulesState.section == label
    return rx.button(
        label,
        on_click=RulesState.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _cat_button(key: str) -> rx.Component:
    active = RulesState.category == key
    return rx.button(
        CATEGORY_LABELS[key],
        on_click=RulesState.set_category(key),
        variant="ghost",
        size="2",
        width="100%",
        justify="start",
        background=rx.cond(active, "#EAF0FB", "transparent"),
        color=rx.cond(active, t.Color.ACCENT.value, t.Color.TEXT_PRIMARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_left=rx.cond(active, f"3px solid {t.Color.ACCENT.value}", "3px solid transparent"),
        border_radius="8px",
        _hover={"background": "#F1F4FA"},
    )


# ---------------------------------------------------------------------------
# Rules Workspace
# ---------------------------------------------------------------------------


def _rule_row(rule) -> rx.Component:
    key = rule.key
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text(rule.label, style=t.TEXT["card_title"]),
                    rx.cond(rule.description != "", rx.text(rule.description, style=t.TEXT["micro"]), rx.fragment()),
                    spacing="1",
                    align="start",
                ),
                rx.spacer(),
                # F4 — the reusable View History trigger, inline next to the rule.
                c.view_history(
                    "View History",
                    RulesState.rule_history[f"{rule.rule_id}:firm"],
                    count=RulesState.rule_history[f"{rule.rule_id}:firm"].length(),
                ),
                rx.cond(
                    rule.override_count > 0,
                    c.accent_pill(f"Overridden for {rule.override_count} client(s)"),
                    rx.fragment(),
                ),
                width="100%",
                align="center",
                spacing="3",
            ),
            # Scope switcher
            rx.hstack(
                rx.text("Scope:", style=t.TEXT["micro"]),
                rx.button(
                    "Firm-wide",
                    on_click=RulesState.set_scope(key, "firm"),
                    size="1",
                    variant="soft",
                    background=rx.cond(RulesState.scope[key] == "firm", t.Color.ACCENT.value, "transparent"),
                    color=rx.cond(RulesState.scope[key] == "firm", "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                rx.button(
                    "Client override",
                    on_click=RulesState.set_scope(key, "client"),
                    size="1",
                    variant="soft",
                    background=rx.cond(RulesState.scope[key] == "client", t.Color.ACCENT.value, "transparent"),
                    color=rx.cond(RulesState.scope[key] == "client", "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                spacing="2",
                align="center",
            ),
            # Client picker (override scope only)
            rx.cond(
                RulesState.scope[key] == "client",
                rx.select(
                    RulesState.client_options.map(lambda o: o.legal_name),
                    on_change=lambda v: RulesState.set_client_for_rule(
                        key,
                        RulesState.client_options.filter(lambda o: o.legal_name == v)[0].client_id,
                    ),
                    placeholder="Select a client",
                    width="280px",
                ),
                rx.fragment(),
            ),
            # Value input
            rx.hstack(
                rx.vstack(
                    rx.text(
                        rx.cond(rule.unit != "", f"Value ({rule.unit})", "Value"),
                        style=t.TEXT["label"],
                    ),
                    rx.input(
                        value=RulesState.value[key],
                        on_change=lambda v: RulesState.set_value(key, v),
                        disabled=~RulesState.can_manage,
                        width="240px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.cond(
                    rule.firm_effective_from != "",
                    rx.text(f"Eff. {rule.firm_effective_from}", style=t.TEXT["micro"]),
                    rx.fragment(),
                ),
                spacing="3",
                align="end",
            ),
            # Reason-capture-before-save (3.3)
            rx.cond(
                RulesState.can_manage,
                rx.vstack(
                    rx.text("Reason for this rule change (required before saving)", style=t.TEXT["label"]),
                    rx.input(
                        value=RulesState.reason[key],
                        on_change=lambda v: RulesState.set_reason(key, v),
                        width="100%",
                    ),
                    rx.cond(
                        RulesState.reason[key] == "",
                        c.inline_reason("A reason is required before this rule change can be saved."),
                        rx.fragment(),
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.fragment(),
            ),
            # High-impact confirmation (firm-wide only)
            rx.cond(
                rule.high_impact & (RulesState.scope[key] == "firm"),
                c.warning_banner(
                    "Firm-wide impact — this change affects every client that has not overridden this rule. Review before saving."
                ),
                rx.fragment(),
            ),
            rx.cond(
                RulesState.can_manage,
                rx.button(
                    rx.cond(
                        rule.high_impact & (RulesState.scope[key] == "firm"),
                        "Confirm and save",
                        "Save",
                    ),
                    on_click=RulesState.save_rule(key),
                    disabled=RulesState.reason[key] == "",
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _statutory_due_dates() -> rx.Component:
    return rx.vstack(
        rx.text("Statutory Due Dates", style=t.TEXT["card_title"]),
        rx.text("Filing due dates — read by C2's filing calendar.", style=t.TEXT["label"]),
        rx.cond(
            RulesState.due_dates.length() > 0,
            rx.vstack(
                rx.hstack(
                    rx.text("Obligation", style=t.TEXT["label"], flex="3"),
                    rx.text("Period", style=t.TEXT["label"], flex="2"),
                    rx.text("Due", style=t.TEXT["label"], flex="2"),
                    rx.text("Grace (days)", style=t.TEXT["label"], flex="2"),
                    width="100%",
                    padding="0 4px 6px 4px",
                    border_bottom=f"1px solid {t.Color.BORDER.value}",
                ),
                rx.foreach(
                    RulesState.due_dates,
                    lambda d: rx.hstack(
                        rx.text(d.obligation, style=t.TEXT["body"], flex="3"),
                        rx.text(d.period, style=t.TEXT["body"], flex="2"),
                        rx.text(
                            rx.cond(d.due_day != "", f"Day {d.due_day}", rx.cond(d.due_month != "", f"Month {d.due_month}", "—")),
                            style=t.TEXT["body"],
                            flex="2",
                        ),
                        rx.text(d.grace_days.to_string(), style=t.TEXT["body"], flex="2"),
                        width="100%",
                        padding="8px 4px",
                        border_bottom=f"1px solid {t.Color.BORDER.value}",
                    ),
                ),
                spacing="0",
                width="100%",
            ),
            c.empty_state("No statutory due dates defined yet.", icon="calendar"),
        ),
        spacing="3",
        align="start",
        width="100%",
    )


def _rules_workspace() -> rx.Component:
    return rx.grid(
        c.card(
            rx.vstack(
                *[_cat_button(k) for k in CATEGORY_LABELS],
                spacing="1",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(
            RulesState.category == "statutory_due_dates",
            _statutory_due_dates(),
            rx.vstack(
                rx.text(
                    rx.match(
                        RulesState.category,
                        ("tds_rates", "TDS Section Rates"),
                        ("gst_tolerance", "GST Tolerance"),
                        ("suspense", "Suspense Ledger Conventions"),
                        ("aging", "Aging Thresholds"),
                        ("materiality", "Materiality Thresholds"),
                        "Rules",
                    ),
                    style=t.TEXT["section_title"],
                ),
                rx.text(
                    "Each rule is editable firm-wide, or overridden per client. Edits are forward-only.",
                    style=t.TEXT["label"],
                ),
                rx.cond(
                    RulesState.rules.length() > 0,
                    rx.vstack(rx.foreach(RulesState.rules, _rule_row), spacing="3", width="100%"),
                    c.empty_state("No rules defined for this category yet.", icon="book-open"),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
        ),
        columns="1fr 3fr",
        spacing="4",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Taxonomy Editor
# ---------------------------------------------------------------------------


def _taxonomy_row(e) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(e.label, font_size="13px", font_weight="600"),
            rx.text(e.code, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="3",
        ),
        rx.input(
            value=RulesState.tax_rename[e.entry_id.to_string()],
            on_change=lambda v: RulesState.set_tax_rename(e.entry_id, v),
            disabled=~RulesState.can_manage,
            flex="4",
        ),
        rx.cond(
            RulesState.can_manage,
            rx.button(
                "Rename",
                on_click=RulesState.rename_taxonomy(e.entry_id),
                size="1",
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_PRIMARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="8px",
            ),
            rx.fragment(),
        ),
        rx.cond(
            RulesState.can_manage,
            c.delete_button("Delete", on_click=RulesState.delete_taxonomy(e.entry_id), size="1"),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _taxonomy_editor() -> rx.Component:
    return rx.vstack(
        rx.text("Taxonomy Editor", style=t.TEXT["section_title"]),
        rx.text(
            "Pre-seeded with the Flagged Item vocabulary from the Information Architecture (Step 3). Entries rename or delete only — no deactivate.",
            style=t.TEXT["label"],
        ),
        rx.hstack(
            rx.foreach(
                RulesState.tax_categories,
                lambda cat: rx.button(
                    cat,
                    on_click=RulesState.set_tax_category(cat),
                    size="1",
                    variant="soft",
                    background=rx.cond(RulesState.tax_category == cat, t.Color.ACCENT.value, "transparent"),
                    color=rx.cond(RulesState.tax_category == cat, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
            ),
            spacing="2",
            wrap="wrap",
        ),
        rx.cond(
            RulesState.taxonomy.length() > 0,
            rx.vstack(rx.foreach(RulesState.taxonomy, _taxonomy_row), spacing="0", width="100%"),
            c.empty_state("No taxonomy entries.", icon="tags"),
        ),
        rx.cond(
            RulesState.can_manage,
            c.card(
                rx.vstack(
                    rx.text("+ Add taxonomy entry", style=t.TEXT["card_title"]),
                    rx.hstack(
                        rx.input(
                            value=RulesState.new_tax_code,
                            on_change=RulesState.set_new_tax_code,
                            placeholder="Code *",
                            width="200px",
                        ),
                        rx.input(
                            value=RulesState.new_tax_label,
                            on_change=RulesState.set_new_tax_label,
                            placeholder="Label *",
                            width="240px",
                        ),
                        rx.select(
                            RulesState.tax_categories,
                            value=RulesState.new_tax_category,
                            on_change=RulesState.set_new_tax_category,
                            width="200px",
                        ),
                        rx.button(
                            "Add entry",
                            on_click=RulesState.add_taxonomy,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        spacing="3",
                        align="center",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.fragment(),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Regulatory Rules Table
# ---------------------------------------------------------------------------


def _reg_status(reg) -> rx.Component:
    return rx.match(
        reg.status,
        ("seed", c.freshness_chip("seed", label="Seed data — unverified")),
        ("reviewed", c.freshness_chip("reviewed", label=f"Reviewed {reg.last_reviewed_at}")),
        ("stale", c.freshness_chip("stale", label="Stale — not reviewed in 6 months")),
        c.freshness_chip("seed"),
    )


def _regulatory_row(reg) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text(reg.domain, style=t.TEXT["body"], flex="2"),
            rx.text(rx.cond(reg.section != "", reg.section, "—"), style=t.TEXT["body"], flex="1"),
            rx.text(reg.name, style=t.TEXT["body"], flex="2"),
            rx.text(reg.rate_or_rule, style=t.TEXT["body"], flex="2"),
            rx.box(_reg_status(reg), flex="2"),
            width="100%",
            align="center",
            padding="8px 0",
            border_bottom=f"1px solid {t.Color.BORDER.value}",
        ),
        rx.cond(
            RulesState.can_manage,
            rx.hstack(
                rx.input(
                    value=RulesState.reg_rate[reg.reg_rule_id.to_string()],
                    on_change=lambda v: RulesState.set_reg_rate(reg.reg_rule_id, v),
                    placeholder="Rate / rule",
                    width="200px",
                ),
                rx.input(
                    value=RulesState.reg_eff[reg.reg_rule_id.to_string()],
                    on_change=lambda v: RulesState.set_reg_eff(reg.reg_rule_id, v),
                    placeholder="Effective from (YYYY-MM-DD)",
                    width="220px",
                ),
                rx.button(
                    "Save changes",
                    on_click=RulesState.save_regulatory(reg.reg_rule_id),
                    size="1",
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Mark reviewed",
                    on_click=RulesState.mark_reviewed(reg.reg_rule_id),
                    size="1",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_PRIMARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                rx.text(f"Seed data as of {reg.seed_as_of}", style=t.TEXT["micro"]),
                spacing="3",
                align="center",
                padding="0 0 8px 0",
            ),
            rx.fragment(),
        ),
        spacing="0",
        width="100%",
    )


def _regulatory_table() -> rx.Component:
    return rx.vstack(
        rx.text("Regulatory Rules Table", style=t.TEXT["section_title"]),
        rx.text(
            "Pre-seeded with known current rates as of a stated date. Verify before relying on for filings.",
            style=t.TEXT["label"],
        ),
        rx.cond(
            RulesState.regulatory.length() > 0,
            rx.vstack(
                rx.hstack(
                    rx.text("Domain", style=t.TEXT["label"], flex="2"),
                    rx.text("Section", style=t.TEXT["label"], flex="1"),
                    rx.text("Name", style=t.TEXT["label"], flex="2"),
                    rx.text("Rate / rule", style=t.TEXT["label"], flex="2"),
                    rx.text("Status", style=t.TEXT["label"], flex="2"),
                    width="100%",
                    padding="0 4px 6px 4px",
                    border_bottom=f"1px solid {t.Color.BORDER.value}",
                ),
                rx.foreach(RulesState.regulatory, _regulatory_row),
                spacing="0",
                width="100%",
            ),
            c.empty_state("No regulatory rules.", icon="scale"),
        ),
        spacing="3",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def rules_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("Rules, Taxonomy & Regulatory Configuration", "The platform's editable rulebook."),
            rx.cond(RulesState.flash != "", c.info_banner(RulesState.flash), rx.fragment()),
            rx.cond(RulesState.error != "", c.inline_reason(RulesState.error), rx.fragment()),
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
                RulesState.section,
                ("Rules Workspace", _rules_workspace()),
                ("Taxonomy Editor", _taxonomy_editor()),
                ("Regulatory Rules Table", _regulatory_table()),
                _rules_workspace(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )