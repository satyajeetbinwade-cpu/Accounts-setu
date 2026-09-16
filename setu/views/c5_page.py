"""C5 — AI Instruction & Knowledge Library.

Instruction Library (search/filter, entry form with optional AI draft, detail
with version history), Approval Queue. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.c5_state import C5State
from setu.views import shell

_HUB = ["Instruction Library", "Approval Queue"]


def _hub_button(label: str) -> rx.Component:
    active = C5State.section == label
    return rx.button(
        label,
        on_click=C5State.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


def _field(label: str, value, on_change, *, placeholder: str = "", disabled=None) -> rx.Component:
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.input(value=value, on_change=on_change, placeholder=placeholder, disabled=disabled, width="100%"),
        spacing="1",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# Instruction Library
# ---------------------------------------------------------------------------


def _conflict_banner(group) -> rx.Component:
    return c.warning_banner(
        rx.text(
            "Conflicting instructions — ",
            rx.text.span(group.touchpoint, font_weight="700"),
            f" ({group.scope}): ",
            group.names.join(" · "),
            ". Both are active; resolve manually.",
            font_size="13px",
            color=t.Color.AI_ON.value,
        )
    )


def _instruction_row(inst) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.text(inst.title, style=t.TEXT["card_title"]),
                rx.text(inst.explanation, style=t.TEXT["micro"]),
                spacing="1",
                align="start",
                flex="3",
            ),
            rx.hstack(
                rx.foreach(inst.tag_labels, lambda lbl: c.accent_pill(lbl)),
                spacing="1",
                wrap="wrap",
                flex="3",
            ),
            rx.text(
                rx.cond(inst.scope == "firm", "Firm-wide", f"Client #{inst.client_id}"),
                style=t.TEXT["body"],
                flex="2",
            ),
            rx.box(
                rx.cond(inst.is_active, c.pill("Active", variant="rule"), c.pill("Inactive", variant="placeholder")),
                flex="1",
            ),
            rx.button(
                "Open",
                on_click=C5State.open_instruction(inst.instruction_id),
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
        ),
        rx.cond(
            C5State.selected_id == inst.instruction_id,
            _instruction_detail(inst),
            rx.fragment(),
        ),
        spacing="3",
        width="100%",
        padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _version_row(v) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(v.change_summary, font_size="13px", color=t.Color.TEXT_PRIMARY.value),
            rx.text(v.title, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
            opacity=rx.cond(v.is_current, "1", "0.55"),
        ),
        rx.text(f"{v.created_at} · {v.changed_by}", style=t.TEXT["micro"], white_space="nowrap"),
        width="100%",
        align="center",
        padding="8px 2px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _instruction_detail(inst) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text(f"Instruction: {inst.title}", style=t.TEXT["card_title"]),
                rx.spacer(),
                rx.cond(
                    C5State.can_manage,
                    rx.hstack(
                        rx.switch(
                            checked=inst.is_active,
                            on_change=lambda v: C5State.toggle_active(inst.instruction_id, v),
                            color_scheme="indigo",
                        ),
                        rx.text("Active", style=t.TEXT["label"]),
                        spacing="2",
                        align="center",
                    ),
                    rx.fragment(),
                ),
                width="100%",
                align="center",
            ),
            rx.cond(
                C5State.can_manage,
                rx.button(
                    "Edit instruction",
                    on_click=C5State.edit_instruction(inst.instruction_id),
                    size="1",
                    variant="soft",
                    background="transparent",
                    color=t.Color.TEXT_PRIMARY.value,
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                rx.fragment(),
            ),
            rx.text("Version history", style=t.TEXT["card_title"]),
            rx.cond(
                C5State.versions.length() > 0,
                rx.vstack(rx.foreach(C5State.versions, _version_row), spacing="0", width="100%"),
                rx.text("No versions recorded.", style=t.TEXT["micro"]),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _instruction_form() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text(
                rx.cond(C5State.editing_id != 0, "Edit instruction", "New instruction"),
                style=t.TEXT["card_title"],
            ),
            rx.cond(C5State.error != "", c.inline_reason(C5State.error), rx.fragment()),
            rx.grid(
                _field("Title *", C5State.form_title, C5State.set_form_title),
                rx.vstack(
                    rx.text("Touchpoint tags", style=t.TEXT["label"]),
                    rx.vstack(
                        rx.foreach(
                            C5State.touchpoints,
                            lambda tp: rx.hstack(
                                rx.checkbox(
                                    checked=C5State.form_tags.contains(tp.key),
                                    on_change=lambda _: C5State.toggle_form_tag(tp.key),
                                ),
                                rx.text(tp.label, style=t.TEXT["body"]),
                                spacing="2",
                                align="center",
                            ),
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                columns="2",
                spacing="4",
                width="100%",
            ),
            rx.vstack(
                rx.text("Explanation *", style=t.TEXT["label"]),
                rx.text_area(
                    value=C5State.form_explanation,
                    on_change=C5State.set_form_explanation,
                    rows="5",
                    width="100%",
                ),
                spacing="1",
                align="start",
                width="100%",
            ),
            # Scope selector
            rx.hstack(
                rx.text("Scope:", style=t.TEXT["micro"]),
                rx.button(
                    "Firm-wide",
                    on_click=C5State.set_form_scope("firm"),
                    size="1",
                    variant="soft",
                    background=rx.cond(C5State.form_scope == "firm", t.Color.ACCENT.value, "transparent"),
                    color=rx.cond(C5State.form_scope == "firm", "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                rx.button(
                    "Specific client",
                    on_click=C5State.set_form_scope("client"),
                    size="1",
                    variant="soft",
                    background=rx.cond(C5State.form_scope == "client", t.Color.ACCENT.value, "transparent"),
                    color=rx.cond(C5State.form_scope == "client", "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                    border=f"1px solid {t.Color.BORDER.value}",
                    border_radius="8px",
                ),
                spacing="2",
                align="center",
            ),
            rx.cond(
                C5State.form_scope == "client",
                rx.select(
                    C5State.client_options.map(lambda o: o.legal_name),
                    on_change=lambda v: C5State.set_form_client_id(
                        C5State.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                    ),
                    placeholder="Select a client",
                    width="280px",
                ),
                rx.fragment(),
            ),
            # Optional AI draft
            c.card(
                rx.vstack(
                    rx.text("Suggest wording (optional AI draft)", style=t.TEXT["card_title"]),
                    rx.hstack(
                        rx.input(
                            value=C5State.suggest_pattern,
                            on_change=C5State.set_suggest_pattern,
                            placeholder="Describe the pattern",
                            flex="1",
                        ),
                        rx.button(
                            "Suggest wording",
                            on_click=C5State.suggest_wording,
                            variant="soft",
                            background="transparent",
                            color=t.Color.TEXT_PRIMARY.value,
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="8px",
                        ),
                        spacing="2",
                        width="100%",
                    ),
                    rx.cond(
                        C5State.draft != "",
                        rx.vstack(
                            rx.text(
                                "AI-drafted — review before saving",
                                font_size="11px",
                                font_weight="600",
                                color=t.Color.AI_ON.value,
                            ),
                            rx.text_area(value=C5State.draft, rows="4", width="100%", read_only=True),
                            rx.button(
                                "Use this draft",
                                on_click=C5State.use_draft,
                                variant="soft",
                                background="transparent",
                                color=t.Color.ACCENT.value,
                                border=f"1px solid {t.Color.ACCENT.value}",
                                border_radius="8px",
                            ),
                            spacing="2",
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
            rx.cond(
                C5State.editing_id != 0,
                _field("Change summary *", C5State.form_summary, C5State.set_form_summary),
                rx.fragment(),
            ),
            rx.hstack(
                rx.button(
                    rx.cond(C5State.editing_id != 0, "Save changes", "Create instruction"),
                    on_click=C5State.save_instruction,
                    background=t.Color.ACCENT.value,
                    color="#FFFFFF",
                ),
                rx.button(
                    "Cancel",
                    on_click=C5State.toggle_form,
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


def _instruction_library() -> rx.Component:
    return rx.vstack(
        rx.cond(
            C5State.has_conflicts,
            rx.vstack(rx.foreach(C5State.conflicts, _conflict_banner), spacing="2", width="100%"),
            rx.fragment(),
        ),
        c.card(
            rx.vstack(
                rx.text("Instruction library", style=t.TEXT["card_title"]),
                rx.text("Search and filter before adding, to catch a near-duplicate first.", style=t.TEXT["label"]),
                rx.hstack(
                    rx.input(
                        value=C5State.search,
                        on_change=C5State.set_search,
                        placeholder="Search by title or explanation…",
                        flex="1",
                    ),
                    rx.cond(
                        C5State.can_manage,
                        rx.button(
                            "+ New instruction",
                            on_click=C5State.toggle_form,
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.fragment(),
                    ),
                    spacing="3",
                    width="100%",
                ),
                rx.hstack(
                    rx.text("Scope:", style=t.TEXT["micro"]),
                    rx.foreach(
                        ["All", "Firm-wide", "Client-specific"],
                        lambda opt: rx.button(
                            opt,
                            on_click=C5State.set_scope_filter(opt),
                            size="1",
                            variant="soft",
                            background=rx.cond(C5State.scope_filter == opt, t.Color.ACCENT.value, "transparent"),
                            color=rx.cond(C5State.scope_filter == opt, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
                            border=f"1px solid {t.Color.BORDER.value}",
                            border_radius="8px",
                        ),
                    ),
                    spacing="2",
                    align="center",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        rx.cond(C5State.show_form & C5State.can_manage, _instruction_form(), rx.fragment()),
        rx.cond(
            C5State.instructions.length() > 0,
            rx.vstack(rx.foreach(C5State.instructions, _instruction_row), spacing="0", width="100%"),
            c.empty_state("No instructions match. Use + New instruction to add one.", icon="library"),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# Approval Queue
# ---------------------------------------------------------------------------


def _proposal_row(p) -> rx.Component:
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text(p.title, style=t.TEXT["card_title"]),
                    rx.text(p.explanation, style=t.TEXT["body"]),
                    spacing="1",
                    align="start",
                    flex="1",
                ),
                rx.text(
                    f"submitted by {p.submitted_by} · {p.submitted_at} · {p.source}",
                    style=t.TEXT["micro"],
                    white_space="nowrap",
                ),
                width="100%",
                align="start",
            ),
            rx.cond(
                C5State.can_manage,
                rx.vstack(
                    _field(
                        "Title",
                        C5State.prop_title[p.proposal_id.to_string()],
                        lambda v: C5State.set_prop_title(p.proposal_id, v),
                    ),
                    rx.vstack(
                        rx.text("Explanation", style=t.TEXT["label"]),
                        rx.text_area(
                            value=C5State.prop_explanation[p.proposal_id.to_string()],
                            on_change=lambda v: C5State.set_prop_explanation(p.proposal_id, v),
                            rows="3",
                            width="100%",
                        ),
                        spacing="1",
                        align="start",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.button(
                            "Confirm (activate)",
                            on_click=C5State.approve_proposal(p.proposal_id),
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        rx.button(
                            "Reject",
                            on_click=C5State.reject_proposal(p.proposal_id),
                            variant="soft",
                            background="transparent",
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
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _approval_queue() -> rx.Component:
    return c.card(
        rx.vstack(
            c.stat(C5State.proposals.length().to_string(), "Proposals awaiting Admin confirmation"),
            rx.cond(
                C5State.proposals.length() > 0,
                rx.vstack(rx.foreach(C5State.proposals, _proposal_row), spacing="3", width="100%"),
                c.empty_state("No proposals awaiting confirmation.", icon="inbox"),
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


def c5_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "AI Instruction & Knowledge Library",
                "Judgment-shaped instructions that AI touchpoints read as runtime context.",
            ),
            rx.cond(C5State.flash != "", c.info_banner(C5State.flash), rx.fragment()),
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
                C5State.section,
                ("Instruction Library", _instruction_library()),
                ("Approval Queue", _approval_queue()),
                _instruction_library(),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )