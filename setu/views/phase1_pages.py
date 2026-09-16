"""Phase-1 tools — Run / Review / Compare / Config / Export.

Ports the Phase-1 Streamlit tabs onto the shared engine. Every screen reads
from ``Phase1State`` (which calls ``src.*`` services). Uses only Foundation
components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.phase1_state import Phase1State
from setu.views import shell


def _context_bar() -> rx.Component:
    """The shared context strip: client / period / recon type / run."""
    return c.card(
        rx.hstack(
            rx.vstack(
                rx.text("Client", style=t.TEXT["label"]),
                rx.select(
                    Phase1State.clients,
                    value=Phase1State.ctx_client,
                    on_change=Phase1State.set_ctx_client,
                    width="200px",
                ),
                spacing="1",
                align="start",
            ),
            rx.vstack(
                rx.text("Period", style=t.TEXT["label"]),
                rx.select(
                    Phase1State.periods,
                    value=Phase1State.ctx_period,
                    on_change=Phase1State.set_ctx_period,
                    width="160px",
                ),
                spacing="1",
                align="start",
            ),
            rx.vstack(
                rx.text("Recon type", style=t.TEXT["label"]),
                rx.select(
                    Phase1State.recon_types,
                    value=Phase1State.ctx_recon_type,
                    on_change=Phase1State.set_ctx_recon_type,
                    width="150px",
                ),
                spacing="1",
                align="start",
            ),
            rx.vstack(
                rx.text("Run", style=t.TEXT["label"]),
                rx.select(
                    Phase1State.run_ids,
                    value=Phase1State.selected_run_id.to_string(),
                    on_change=Phase1State.set_selected_run,
                    width="160px",
                    placeholder="No runs yet",
                ),
                spacing="1",
                align="start",
            ),
            spacing="4",
            wrap="wrap",
            align="end",
            width="100%",
        ),
        width="100%",
    )


def _banners() -> rx.Component:
    return rx.fragment(
        rx.cond(Phase1State.flash != "", c.info_banner(Phase1State.flash), rx.fragment()),
        rx.cond(Phase1State.error != "", c.inline_reason(Phase1State.error), rx.fragment()),
    )


# ===========================================================================
# Run
# ===========================================================================


def _slot_card(s) -> rx.Component:
    files = s.files
    return c.card(
        rx.vstack(
            rx.hstack(
                rx.text(s.label, style=t.TEXT["card_title"]),
                rx.cond(s.is_books, c.pill("Books side", variant="accent"), c.pill("Portal side", variant="placeholder")),
                rx.spacer(),
                rx.cond(
                    s.selected != "",
                    rx.cond(
                        s.runnable,
                        c.pill("Ready", variant="rule"),
                        c.pill("Needs attention", variant="danger"),
                    ),
                    c.pill("Missing", variant="placeholder"),
                ),
                width="100%",
                align="center",
                spacing="3",
            ),
            rx.cond(s.hint != "", rx.text(s.hint, style=t.TEXT["micro"]), rx.fragment()),
            rx.cond(
                files.length() > 0,
                rx.select(
                    files,
                    value=s.selected,
                    on_change=lambda v: Phase1State.select_slot_file(s.source_type, v),
                    width="100%",
                ),
                c.inline_reason(f"No files found for this slot."),
            ),
            rx.cond(
                (s.selected != "") & ~s.runnable,
                c.inline_reason(s.reason),
                rx.fragment(),
            ),
            spacing="2",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _run_tool() -> rx.Component:
    return rx.vstack(
        _context_bar(),
        rx.cond(
            Phase1State.has_context,
            rx.vstack(
                rx.vstack(
                    rx.foreach(Phase1State.slots, _slot_card),
                    spacing="3",
                    width="100%",
                ),
                rx.cond(
                    Phase1State.run_blockers.length() > 0,
                    c.card(
                        rx.vstack(
                            c.section_title("Cannot run yet"),
                            rx.foreach(
                                Phase1State.run_blockers,
                                lambda b: rx.hstack(
                                    rx.icon("triangle-alert", size=14, color=t.Color.DANGER.value),
                                    rx.text(b, style=t.TEXT["body"]),
                                    spacing="2",
                                    align="center",
                                ),
                            ),
                            spacing="2",
                            align="start",
                            width="100%",
                        ),
                        width="100%",
                    ),
                    rx.button(
                        "Execute run",
                        on_click=Phase1State.execute_run,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                ),
                rx.cond(
                    Phase1State.run_result != "",
                    c.card(
                        rx.vstack(
                            c.section_title(f"Run {Phase1State.run_result} complete"),
                            rx.cond(
                                Phase1State.run_caveats.length() > 0,
                                rx.vstack(
                                    rx.text("Caveats — checks that couldn't be performed:", style=t.TEXT["label"]),
                                    rx.foreach(
                                        Phase1State.run_caveats,
                                        lambda cv: rx.hstack(
                                            rx.text("•", style=t.TEXT["body"]),
                                            rx.text(cv, style=t.TEXT["body"]),
                                            spacing="2",
                                            align="start",
                                        ),
                                    ),
                                    spacing="1",
                                    align="start",
                                ),
                                rx.fragment(),
                            ),
                            spacing="2",
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
            ),
            c.empty_state("Pick a client, period, and recon type to configure a run.", icon="play"),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ===========================================================================
# Review
# ===========================================================================


def _class_card(cn) -> rx.Component:
    return rx.vstack(
        rx.text(cn.count.to_string(), font_size="22px", font_weight="700", color=t.Color.TEXT_PRIMARY.value),
        rx.text(cn.label, style=t.TEXT["label"]),
        rx.text(cn.value, style=t.TEXT["micro"]),
        spacing="0",
        align="start",
    )


def _review_row(r) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.hstack(
                c.pill(r.classification, variant=_class_variant(r.classification)),
                rx.cond(r.difference_type != "", c.pill(r.difference_type, variant="accent"), rx.fragment()),
                c.pill(f"{r.confidence_band} ({r.confidence_score})", variant="placeholder"),
                _review_state_pill(r.review_state),
                spacing="2",
                align="center",
                wrap="wrap",
            ),
            rx.hstack(
                rx.text("Books:", style=t.TEXT["micro"]),
                rx.text(r.books_identity, style=t.TEXT["micro"]),
                rx.text("· Portal:", style=t.TEXT["micro"]),
                rx.text(r.portal_identity, style=t.TEXT["micro"]),
                spacing="1",
                wrap="wrap",
                align="baseline",
            ),
            rx.text(r.match_reason, style=t.TEXT["body"]),
            spacing="1",
            align="start",
            flex="1",
        ),
        rx.text(r.value, style=t.TEXT["label"], white_space="nowrap"),
        rx.button(
            "Open",
            on_click=Phase1State.open_result(r.result_id),
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


def _class_variant(cls) -> str:
    return {
        "Matched": "rule",
        "Amount Difference": "ai",
        "Not in Books": "danger",
        "Not in Portal": "danger",
    }.get(cls, "placeholder")


def _review_state_pill(state) -> rx.Component:
    return rx.match(
        state,
        ("reviewed", c.pill("Reviewed", variant="rule")),
        ("stale", c.pill("STALE", variant="danger")),
        c.pill("Unreviewed", variant="placeholder"),
    )


def _record_panel(title, fields) -> rx.Component:
    return rx.vstack(
        rx.text(title, style=t.TEXT["label"], font_weight="700"),
        rx.cond(
            fields.length() > 0,
            rx.vstack(
                rx.foreach(
                    fields,
                    lambda f: rx.hstack(
                        rx.hstack(
                            rx.cond(f.differs, rx.text("⚠", style=t.TEXT["micro"]), rx.fragment()),
                            rx.text(f.label, style=t.TEXT["micro"], min_width="130px"),
                            spacing="2",
                            align="baseline",
                        ),
                        rx.text(f.value, style=t.TEXT["body"]),
                        spacing="2",
                        align="baseline",
                        width="100%",
                    ),
                ),
                spacing="1",
                width="100%",
            ),
            rx.text("— no record on this side —", style=t.TEXT["micro"]),
        ),
        spacing="2",
        align="start",
        width="100%",
    )


def _review_detail() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back to list",
            on_click=Phase1State.close_result,
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
                    c.pill(Phase1State.detail_classification, variant="accent"),
                    rx.cond(Phase1State.detail_difference_type != "", c.pill(Phase1State.detail_difference_type, variant="placeholder"), rx.fragment()),
                    c.pill(f"{Phase1State.detail_band} ({Phase1State.detail_score})", variant="placeholder"),
                    _review_state_pill(Phase1State.detail_review_state),
                    spacing="2",
                    align="center",
                    wrap="wrap",
                ),
                rx.text(Phase1State.detail_reason, style=t.TEXT["body"]),
                rx.cond(
                    Phase1State.detail_review_state == "stale",
                    c.warning_banner(
                        "Review is stale — the reviewer's approval no longer applies because "
                        "the verdict changed in this run."
                    ),
                    rx.fragment(),
                ),
                c.divider(),
                rx.hstack(
                    rx.box(_record_panel("Books record", Phase1State.detail_books), flex="1"),
                    rx.box(_record_panel("Portal record", Phase1State.detail_portal), flex="1"),
                    spacing="5",
                    width="100%",
                    align="start",
                ),
                c.divider(),
                c.reason_capture(
                    label="Reviewer note",
                    value=Phase1State.review_note,
                    on_change=Phase1State.set_review_note,
                    placeholder="Optional note for this review",
                ),
                rx.hstack(
                    rx.button(
                        "Mark reviewed",
                        on_click=Phase1State.mark_reviewed,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    rx.button(
                        "Clear review",
                        on_click=Phase1State.clear_review,
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
        ),
        spacing="4",
        width="100%",
        align="start",
    )


def _review_tool() -> rx.Component:
    return rx.vstack(
        _context_bar(),
        rx.cond(
            Phase1State.selected_run_id == 0,
            c.empty_state(
                "Select a run to review — no run is selected for this context yet.",
                icon="search-check",
            ),
            rx.cond(
                Phase1State.selected_result_id != 0,
                _review_detail(),
                rx.vstack(
                    c.card(
                        rx.vstack(
                            rx.hstack(
                                rx.vstack(
                                    c.stat(Phase1State.review_total.to_string(), "results in this run"),
                                    spacing="1",
                                    align="start",
                                ),
                                rx.spacer(),
                                rx.vstack(
                                    c.stat(Phase1State.at_risk_value, "value requiring attention", accent=True),
                                    spacing="1",
                                    align="start",
                                ),
                                width="100%",
                                align="start",
                                spacing="6",
                            ),
                            c.divider(),
                            rx.hstack(
                                rx.text(f"{Phase1State.review_reviewed} reviewed", style=t.TEXT["label"]),
                                rx.text("·", style=t.TEXT["label"]),
                                rx.text(f"{Phase1State.review_unreviewed} unreviewed", style=t.TEXT["label"]),
                                rx.cond(
                                    Phase1State.review_stale > 0,
                                    rx.text(f"· {Phase1State.review_stale} stale", style=t.TEXT["label"]),
                                    rx.fragment(),
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
                    c.card(
                        rx.vstack(
                            c.section_title("By classification"),
                            rx.hstack(
                                rx.foreach(Phase1State.class_counts, _class_card),
                                spacing="6",
                                wrap="wrap",
                            ),
                            spacing="3",
                            align="start",
                            width="100%",
                        ),
                        width="100%",
                    ),
                    c.card(
                        rx.vstack(
                            rx.hstack(
                                rx.vstack(
                                    rx.text("Classification", style=t.TEXT["label"]),
                                    rx.select(
                                        ["All", "Amount Difference", "Not in Portal", "Not in Books", "Matched"],
                                        value=Phase1State.review_filter_classification,
                                        on_change=Phase1State.set_review_filter_classification,
                                        width="200px",
                                    ),
                                    spacing="1",
                                    align="start",
                                ),
                                rx.vstack(
                                    rx.text("Confidence band", style=t.TEXT["label"]),
                                    rx.select(
                                        ["All", "High", "Medium", "Low"],
                                        value=Phase1State.review_filter_band,
                                        on_change=Phase1State.set_review_filter_band,
                                        width="150px",
                                    ),
                                    spacing="1",
                                    align="start",
                                ),
                                rx.vstack(
                                    rx.text("Review state", style=t.TEXT["label"]),
                                    rx.select(
                                        ["All", "reviewed", "unreviewed", "stale"],
                                        value=Phase1State.review_filter_state,
                                        on_change=Phase1State.set_review_filter_state,
                                        width="160px",
                                    ),
                                    spacing="1",
                                    align="start",
                                ),
                                spacing="4",
                                align="end",
                                wrap="wrap",
                            ),
                            c.divider(),
                            rx.cond(
                                Phase1State.review_rows.length() > 0,
                                rx.vstack(
                                    rx.foreach(Phase1State.review_rows, _review_row),
                                    spacing="0",
                                    width="100%",
                                ),
                                c.empty_state("No results match these filters.", icon="inbox"),
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
            ),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ===========================================================================
# Compare
# ===========================================================================


def _movement_row(m) -> rx.Component:
    return rx.hstack(
        rx.text(m.from_label, style=t.TEXT["body"], flex="1"),
        rx.icon("arrow-right", size=14, color=t.Color.TEXT_MUTED.value),
        rx.text(m.to_label, style=t.TEXT["body"], flex="1"),
        rx.cond(
            m.moved,
            c.pill(m.count.to_string(), variant="ai"),
            c.pill(m.count.to_string(), variant="placeholder"),
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="6px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _change_card(ch) -> rx.Component:
    return c.card(
        rx.hstack(
            rx.vstack(
                rx.text(f"Run A: {ch.classification_a}", style=t.TEXT["body"], font_weight="600"),
                rx.cond(ch.difference_type_a != "", rx.text(ch.difference_type_a, style=t.TEXT["micro"]), rx.fragment()),
                rx.text(ch.match_reason_a, style=t.TEXT["micro"]),
                spacing="1",
                align="start",
                flex="1",
            ),
            rx.icon("arrow-right", size=16, color=t.Color.ACCENT.value),
            rx.vstack(
                rx.text(f"Run B: {ch.classification_b}", style=t.TEXT["body"], font_weight="600"),
                rx.cond(ch.difference_type_b != "", rx.text(ch.difference_type_b, style=t.TEXT["micro"]), rx.fragment()),
                rx.text(ch.match_reason_b, style=t.TEXT["micro"]),
                spacing="1",
                align="start",
                flex="1",
            ),
            width="100%",
            align="start",
            spacing="4",
        ),
        width="100%",
    )


def _compare_tool() -> rx.Component:
    return rx.vstack(
        _context_bar(),
        rx.cond(
            ~Phase1State.compare_ready,
            c.empty_state(
                "Need at least two runs for this client/period/recon type to compare. "
                "Execute another run from the Run screen first.",
                icon="git-compare",
            ),
            rx.vstack(
                c.card(
                    rx.vstack(
                        c.section_title("Movement between runs"),
                        rx.hstack(
                            rx.vstack(
                                rx.text("Run A (baseline)", style=t.TEXT["label"]),
                                rx.select(
                                    Phase1State.run_ids,
                                    value=Phase1State.compare_run_a.to_string(),
                                    on_change=Phase1State.set_compare_a,
                                    width="160px",
                                ),
                                spacing="1",
                                align="start",
                            ),
                            rx.vstack(
                                rx.text("Run B (new)", style=t.TEXT["label"]),
                                rx.select(
                                    Phase1State.run_ids,
                                    value=Phase1State.compare_run_b.to_string(),
                                    on_change=Phase1State.set_compare_b,
                                    width="160px",
                                ),
                                spacing="1",
                                align="start",
                            ),
                            spacing="4",
                            align="end",
                        ),
                        rx.cond(
                            Phase1State.compare_error != "",
                            c.inline_reason(Phase1State.compare_error),
                            rx.fragment(),
                        ),
                        c.divider(),
                        rx.hstack(
                            rx.vstack(c.stat(Phase1State.compare_changed.to_string(), "changed"), spacing="0", align="start"),
                            rx.vstack(c.stat(Phase1State.compare_unchanged.to_string(), "unchanged"), spacing="0", align="start"),
                            rx.vstack(c.stat(Phase1State.compare_change_rate, "change rate"), spacing="0", align="start"),
                            rx.vstack(c.stat(Phase1State.compare_only_a.to_string(), "only in run A"), spacing="0", align="start"),
                            rx.vstack(c.stat(Phase1State.compare_only_b.to_string(), "only in run B"), spacing="0", align="start"),
                            spacing="6",
                            wrap="wrap",
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                c.card(
                    rx.vstack(
                        c.section_title("Bucket-to-bucket movement"),
                        rx.cond(
                            Phase1State.compare_movement.length() > 0,
                            rx.vstack(
                                rx.foreach(Phase1State.compare_movement, _movement_row),
                                spacing="0",
                                width="100%",
                            ),
                            c.empty_state("No movement recorded.", icon="git-compare"),
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    width="100%",
                ),
                c.card(
                    rx.vstack(
                        c.section_title("Changed results"),
                        rx.cond(
                            Phase1State.compare_changes.length() > 0,
                            rx.vstack(
                                rx.foreach(Phase1State.compare_changes, _change_card),
                                spacing="3",
                                width="100%",
                            ),
                            c.empty_state("No classification changes between these two runs.", icon="check-circle"),
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
        ),
        spacing="4",
        width="100%",
        align="start",
    )


# ===========================================================================
# Config
# ===========================================================================


def _config_tool() -> rx.Component:
    return rx.vstack(
        _context_bar(),
        rx.cond(
            Phase1State.config_error != "",
            c.inline_reason(Phase1State.config_error),
            rx.fragment(),
        ),
        rx.cond(
            Phase1State.selected_run_id != 0,
            rx.cond(
                Phase1State.config_same,
                c.info_banner(
                    f"Run {Phase1State.selected_run_id} was produced under the config currently "
                    "on disk — no differences."
                ),
                c.warning_banner(
                    f"Run {Phase1State.selected_run_id}'s config snapshot differs from the current "
                    "matching_rules.yaml. Results from this run were not produced under today's settings."
                ),
            ),
            c.info_banner(
                "Select a run to compare its config snapshot against the current matching_rules.yaml. "
                "The current file is shown below for reference."
            ),
        ),
        rx.hstack(
            c.card(
                rx.vstack(
                    c.section_title("Current matching_rules.yaml"),
                    rx.text_area(
                        value=Phase1State.config_current,
                        read_only=True,
                        height="420px",
                        width="100%",
                        font_family="monospace",
                        font_size="12px",
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            rx.cond(
                Phase1State.selected_run_id != 0,
                c.card(
                    rx.vstack(
                        c.section_title(f"Run {Phase1State.selected_run_id} config_snapshot"),
                        rx.text_area(
                            value=Phase1State.config_snapshot,
                            read_only=True,
                            height="420px",
                            width="100%",
                            font_family="monospace",
                            font_size="12px",
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
        ),
        rx.cond(
            (Phase1State.selected_run_id != 0) & ~Phase1State.config_same,
            c.card(
                rx.vstack(
                    c.section_title("Diff (run snapshot → current)"),
                    rx.text_area(
                        value=Phase1State.config_diff,
                        read_only=True,
                        height="320px",
                        width="100%",
                        font_family="monospace",
                        font_size="12px",
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


# ===========================================================================
# Export
# ===========================================================================


def _export_tool() -> rx.Component:
    return rx.vstack(
        _context_bar(),
        rx.cond(
            Phase1State.selected_run_id == 0,
            c.empty_state("Select a run to export.", icon="download"),
            cx_card(),
        ),
        spacing="4",
        width="100%",
        align="start",
    )


def cx_card() -> rx.Component:
    return c.card(
        rx.vstack(
            c.section_title(
                f"Export run {Phase1State.selected_run_id}",
                "Generates an Excel workbook — one sheet per classification plus a summary "
                "sheet. A copy is also kept on disk for the record.",
            ),
            rx.button(
                "Generate export",
                on_click=Phase1State.generate_export,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
            ),
            rx.cond(
                Phase1State.export_b64 != "",
                rx.vstack(
                    c.info_banner(
                        f"Export ready — {Phase1State.export_sheets} sheet(s), "
                        f"{Phase1State.export_size_kb} KB."
                    ),
                    rx.link(
                        rx.button(
                            "Download Excel workbook",
                            background=t.Color.ACCENT.value,
                            color="#FFFFFF",
                        ),
                        href=(
                            "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                            ";base64," + Phase1State.export_b64
                        ),
                        download=Phase1State.export_name,
                    ),
                    spacing="2",
                    align="start",
                ),
                rx.fragment(),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


# ===========================================================================
# Page factories
# ===========================================================================


def run_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("Run", "Pick source files for the selected context and execute a reconciliation."),
            _banners(),
            _run_tool(),
            spacing="5",
            width="100%",
            align="start",
        )
    )


def review_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("Review", "Reconciliation results — headline, grouped, and per-item detail."),
            _banners(),
            _review_tool(),
            spacing="5",
            width="100%",
            align="start",
        )
    )


def compare_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("Compare", "What did a config change actually do? Two runs, side by side."),
            _banners(),
            _compare_tool(),
            spacing="5",
            width="100%",
            align="start",
        )
    )


def config_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("Config", "Compare a run's config snapshot against the current matching_rules.yaml."),
            _banners(),
            _config_tool(),
            spacing="5",
            width="100%",
            align="start",
        )
    )


def export_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header("Export", "Generate a run's Excel workbook and download it."),
            _banners(),
            _export_tool(),
            spacing="5",
            width="100%",
            align="start",
        )
    )