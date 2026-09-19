"""F6 — Source File Ingestion & Format Registry.

Upload → identification result (Path A/B/C) → mapping review (Path B) →
Format Registry screen (Setup-style, per §9's design table). Uses only
Foundation tokens/components — no new component introduced.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.f6_state import F6State
from setu.views import shell

_ACCEPT = {
    "text/csv": [".csv"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
}


def _section_button(label: str) -> rx.Component:
    active = F6State.section == label
    return rx.button(
        label,
        on_click=F6State.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


# ---------------------------------------------------------------------------
# Upload section
# ---------------------------------------------------------------------------


def _identification_result() -> rx.Component:
    """3.1's rule-vs-AI distinction, applied to Path A vs Path B (§9)."""
    return rx.cond(
        F6State.last_run_id > 0,
        c.card(
            rx.vstack(
                rx.hstack(
                    rx.cond(
                        F6State.last_path_taken == "A",
                        c.confidence_badge("rule", label="Recognised format"),
                        rx.cond(
                            F6State.last_path_taken == "B",
                            c.confidence_badge("ai", pct=0, label="Unrecognised — AI proposal"),
                            c.pill("Rejected", variant="danger"),
                        ),
                    ),
                    rx.text(F6State.last_status_label, style=t.TEXT["card_title"]),
                    spacing="3",
                    align="center",
                ),
                rx.cond(
                    F6State.last_path_taken == "C",
                    c.inline_reason(F6State.last_rejection_reason),
                    rx.fragment(),
                ),
                rx.cond(
                    F6State.last_path_taken == "A",
                    rx.text(f"{F6State.last_row_count} rows extracted.", style=t.TEXT["body"]),
                    rx.fragment(),
                ),
                rx.cond(
                    (F6State.last_path_taken == "B") & (F6State.last_ai_error != ""),
                    c.inline_reason(f"AI mapping unavailable: {F6State.last_ai_error} — map manually below."),
                    rx.fragment(),
                ),
                rx.cond(
                    F6State.last_recognised_not_parsed.length() > 0,
                    rx.vstack(
                        rx.text("Recognised but not parsed (deferred, in-scope check skipped):", style=t.TEXT["label"]),
                        rx.foreach(F6State.last_recognised_not_parsed, lambda s: rx.text(f"• {s}", style=t.TEXT["micro"])),
                        spacing="1", align="start",
                    ),
                    rx.fragment(),
                ),
                spacing="2", align="start",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _validation_panel() -> rx.Component:
    def _row(v) -> rx.Component:
        return rx.hstack(
            rx.match(
                v.result,
                ("pass", c.pill(v.result_label, variant="rule")),
                ("row_flag", c.pill(v.result_label, variant="ai")),
                ("hard_stop", c.pill(v.result_label, variant="danger")),
                c.pill(v.result_label, variant="accent"),
            ),
            rx.vstack(
                rx.text(v.check, style=t.TEXT["body"], font_weight="600"),
                rx.text(v.detail, style=t.TEXT["micro"]),
                spacing="0", align="start",
            ),
            spacing="3", align="start", width="100%", padding="8px 0",
            border_bottom=f"1px solid {t.Color.BORDER.value}",
        )

    return rx.cond(
        F6State.last_validation.length() > 0,
        c.card(
            rx.vstack(
                rx.text("Run result — every check §8 performed", style=t.TEXT["card_title"]),
                rx.foreach(F6State.last_validation, _row),
                spacing="2", align="start", width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _mapping_review() -> rx.Component:
    """Path B only. headline→grouped→detail (§9's 3.6 pattern)."""
    def _field_row(fm) -> rx.Component:
        return rx.hstack(
            rx.cond(
                fm.kind == "unavailable",
                c.pill(fm.canonical_field, variant="danger"),
                c.confidence_badge("ai", pct=fm.confidence, label=fm.canonical_field),
            ),
            rx.vstack(
                rx.text(f"from: {fm.source_columns}", style=t.TEXT["micro"]),
                rx.text(fm.rationale, style=t.TEXT["micro"]),
                spacing="0", align="start",
            ),
            spacing="3", align="start", width="100%", padding="8px 0",
            border_bottom=f"1px solid {t.Color.BORDER.value}",
        )

    return rx.cond(
        F6State.last_path_taken == "B",
        c.card(
            rx.vstack(
                rx.text("Mapping review — nothing here reaches reconciliation until confirmed", style=t.TEXT["card_title"]),
                rx.cond(
                    F6State.last_ambiguities.length() > 0,
                    c.warning_banner("The model flagged questions for you — read these before confirming."),
                    rx.fragment(),
                ),
                rx.foreach(F6State.last_ambiguities, lambda a: rx.text(f"? {a}", style=t.TEXT["micro"])),
                rx.foreach(F6State.last_field_mappings, _field_row),
                rx.text(
                    "This PoC surfaces the AI proposal for review; full inline editing + promotion is the "
                    "next incremental slice (backend confirm_mapping_proposal() is built and unit-tested).",
                    style=t.TEXT["micro"],
                ),
                spacing="2", align="start", width="100%",
            ),
            width="100%",
        ),
        rx.fragment(),
    )


def _upload_section() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.text("Client", style=t.TEXT["label"]),
                rx.select(
                    F6State.client_options.map(lambda o: o.legal_name),
                    on_change=lambda v: F6State.set_upload_client(
                        F6State.client_options.filter(lambda o: o.legal_name == v)[0].client_id.to_string()
                    ),
                    placeholder="Select a client", width="240px",
                ),
                spacing="1", align="start",
            ),
            rx.vstack(
                rx.text("Period", style=t.TEXT["label"]),
                rx.input(value=F6State.upload_period, on_change=F6State.set_upload_period,
                          placeholder="e.g. 2026-08", width="160px"),
                spacing="1", align="start",
            ),
            rx.vstack(
                rx.text("Slot", style=t.TEXT["label"]),
                rx.select(["portal", "books"], value=F6State.upload_slot, on_change=F6State.set_upload_slot, width="160px"),
                spacing="1", align="start",
            ),
            spacing="4", align="end",
        ),
        c.card(
            rx.vstack(
                rx.text("Drop a source file — the system identifies it", style=t.TEXT["card_title"]),
                rx.text(
                    "Known layouts parse deterministically and instantly. An unrecognised layout routes "
                    "to an AI mapping proposal a human confirms once, after which it's registered.",
                    style=t.TEXT["micro"],
                ),
                rx.upload(
                    rx.vstack(
                        rx.icon("upload", size=22, color=t.Color.NEUTRAL.value),
                        rx.text("Drag a spreadsheet/CSV here or click to browse", style=t.TEXT["label"]),
                        spacing="2", align="center", padding="24px",
                    ),
                    id="f6_upload", accept=_ACCEPT, multiple=False,
                    border=f"1px dashed {t.Color.BORDER.value}", border_radius="12px",
                    width="100%", background=t.Color.SURFACE.value,
                ),
                rx.hstack(
                    rx.button(
                        "Upload & identify",
                        on_click=F6State.handle_upload(rx.upload_files(upload_id="f6_upload")),
                        background=t.Color.ACCENT.value, color="#FFFFFF",
                    ),
                    rx.button(
                        "Clear", on_click=rx.clear_selected_files("f6_upload"), variant="soft",
                        background="transparent", color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}", border_radius="9px",
                    ),
                    spacing="2",
                ),
                spacing="3", align="start", width="100%",
            ),
            width="100%",
        ),
        rx.cond(F6State.flash != "", c.info_banner(F6State.flash), rx.fragment()),
        rx.cond(F6State.error != "", c.inline_reason(F6State.error), rx.fragment()),
        _identification_result(),
        _validation_panel(),
        _mapping_review(),
        spacing="4", align="start", width="100%",
    )


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


def _run_row(r) -> rx.Component:
    return rx.hstack(
        rx.match(
            r.path_taken,
            ("A", c.pill(r.path_label, variant="rule")),
            ("B", c.pill(r.path_label, variant="ai")),
            c.pill(r.path_label, variant="danger"),
        ),
        rx.text(r.filename, style=t.TEXT["body"], flex="2"),
        rx.text(r.slot, style=t.TEXT["micro"], flex="1"),
        rx.text(r.status_label, style=t.TEXT["micro"], flex="1"),
        rx.text(r.created_at, style=t.TEXT["micro"], flex="1"),
        spacing="3", align="center", width="100%", padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _runs_section() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Ingestion runs", style=t.TEXT["card_title"]),
            rx.cond(
                F6State.runs.length() > 0,
                rx.foreach(F6State.runs, _run_row),
                c.empty_state("No ingestion runs yet for this client."),
            ),
            spacing="2", align="start", width="100%",
        ),
        width="100%",
    )


# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------


def _version_row(v) -> rx.Component:
    return rx.hstack(
        rx.match(
            v.status,
            ("active", c.pill(v.status_label, variant="rule")),
            ("quarantined", c.pill(v.status_label, variant="danger")),
            c.pill(v.status_label, variant="accent"),
        ),
        rx.vstack(
            rx.text(f"{v.label} · v{v.version_number}", style=t.TEXT["body"], font_weight="600"),
            rx.text(f"{v.provenance_label} · {v.scope_label} · confirmed by {v.confirmed_by} on {v.confirmed_at}",
                     style=t.TEXT["micro"]),
            spacing="0", align="start", flex="1",
        ),
        rx.cond(
            v.status == "active",
            rx.dialog.root(
                rx.dialog.trigger(
                    rx.button("Quarantine", size="1", variant="soft", background="transparent",
                                color=t.Color.DANGER.value, border=f"1px solid {t.Color.DANGER.value}",
                                border_radius="8px", on_click=F6State.open_quarantine(v.version_id)),
                ),
                rx.dialog.content(
                    rx.vstack(
                        rx.text("Quarantine this format version", style=t.TEXT["card_title"]),
                        rx.text(
                            "Files matching this layout will be hard-stopped (Path C) rather than "
                            "routed to AI mapping — a known-bad layout should never be silently re-derived.",
                            style=t.TEXT["micro"],
                        ),
                        c.reason_capture(label="Reason (required)", value=F6State.quarantine_reason,
                                          on_change=F6State.set_quarantine_reason),
                        rx.dialog.close(
                            rx.button("Confirm quarantine", on_click=F6State.confirm_quarantine,
                                        background=t.Color.DANGER.value, color="#FFFFFF"),
                        ),
                        spacing="3", align="start",
                    ),
                ),
            ),
            rx.fragment(),
        ),
        spacing="3", align="center", width="100%", padding="10px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _registry_section() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Format Registry", style=t.TEXT["card_title"]),
            rx.text(
                "Registered layouts per format and per client — provenance, confirming user, "
                "and quarantine control. Versions deactivate/quarantine, never delete.",
                style=t.TEXT["micro"],
            ),
            rx.cond(
                F6State.versions.length() > 0,
                rx.foreach(F6State.versions, _version_row),
                c.empty_state("No registered format versions yet."),
            ),
            spacing="2", align="start", width="100%",
        ),
        width="100%",
    )


def f6_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Source File Ingestion & Format Registry",
                "Every upload is identified, parsed deterministically once known, and never reaches "
                "reconciliation on an unconfirmed AI mapping.",
            ),
            rx.hstack(
                _section_button("Upload"),
                _section_button("Runs"),
                _section_button("Format Registry"),
                spacing="2",
            ),
            rx.match(
                F6State.section,
                ("Upload", _upload_section()),
                ("Runs", _runs_section()),
                ("Format Registry", _registry_section()),
                _upload_section(),
            ),
            spacing="4", align="start", width="100%",
        )
    )
