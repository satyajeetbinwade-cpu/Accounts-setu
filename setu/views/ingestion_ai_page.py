"""F3-AI — Smart Document Ingestion.

Upload list, mapping-review screen (raw-file context + per-field confidence),
mapping profiles, confidence thresholds. Uses only Foundation components.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.state.ingestion_ai_state import IngestionAiState, SOURCE_TYPE_LABELS
from setu.views import shell

_ACCEPT = {
    "text/csv": [".csv"],
    "application/vnd.ms-excel": [".xls"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
}


def _section_button(label: str) -> rx.Component:
    active = IngestionAiState.section == label
    return rx.button(
        label,
        on_click=IngestionAiState.set_section(label),
        size="2",
        variant="soft",
        background=rx.cond(active, t.Color.ACCENT.value, "transparent"),
        color=rx.cond(active, "#FFFFFF", t.Color.TEXT_SECONDARY.value),
        font_weight=rx.cond(active, "600", "500"),
        border_radius="9px",
        _hover={"background": rx.cond(active, t.Color.ACCENT.value, "#EEF2F8")},
    )


# ---------------------------------------------------------------------------
# Upload + list
# ---------------------------------------------------------------------------


def _upload_row(u) -> rx.Component:
    return rx.hstack(
        rx.text(u.filename, style=t.TEXT["body"], flex="3", font_weight="600"),
        rx.text(u.source_label, style=t.TEXT["micro"], flex="2"),
        rx.box(
            rx.cond(
                u.status == "blocked",
                c.inline_reason(f"Blocked — {u.blocked_count} field(s) need mapping"),
                rx.cond(
                    u.status == "unrecognized",
                    c.inline_reason("Unrecognized shape — needs manual review"),
                    rx.cond(
                        u.status == "confirmed",
                        c.confidence_badge("rule", label="Confirmed"),
                        rx.text(u.status_label, style=t.TEXT["micro"]),
                    ),
                ),
            ),
            flex="3",
        ),
        rx.cond(
            IngestionAiState.can_review,
            rx.button(
                "Open",
                on_click=IngestionAiState.open_upload(u.upload_id),
                size="1",
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_PRIMARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="8px",
            ),
            rx.text("Uploaded, pending mapping review", style=t.TEXT["micro"]),
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _upload_and_list() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text("Client:", style=t.TEXT["micro"]),
            rx.select(
                IngestionAiState.client_options.map(lambda o: o.legal_name),
                on_change=lambda v: IngestionAiState.set_client(
                    IngestionAiState.client_options.filter(lambda o: o.legal_name == v)[0].client_id
                ),
                placeholder="Select a client",
                width="280px",
            ),
            spacing="2",
            align="center",
        ),
        c.card(
            rx.vstack(
                rx.text("Upload a raw source file", style=t.TEXT["card_title"]),
                rx.text(
                    "OCR of scanned/image-based documents is out of scope — spreadsheets/CSV only.",
                    style=t.TEXT["micro"],
                ),
                rx.hstack(
                    rx.vstack(
                        rx.text("Source type", style=t.TEXT["label"]),
                        rx.select(
                            IngestionAiState.source_type_options,
                            value=IngestionAiState.source_type,
                            on_change=IngestionAiState.set_source_type,
                            width="240px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    rx.vstack(
                        rx.text("Period (optional)", style=t.TEXT["label"]),
                        rx.input(
                            value=IngestionAiState.period,
                            on_change=IngestionAiState.set_period,
                            placeholder="e.g. 2026-08",
                            width="180px",
                        ),
                        spacing="1",
                        align="start",
                    ),
                    spacing="4",
                    align="end",
                ),
                rx.upload(
                    rx.vstack(
                        rx.icon("upload", size=22, color=t.Color.NEUTRAL.value),
                        rx.text("Drag a spreadsheet/CSV here or click to browse", style=t.TEXT["label"]),
                        spacing="2",
                        align="center",
                        padding="24px",
                    ),
                    id="f3ai_upload",
                    accept=_ACCEPT,
                    multiple=False,
                    border=f"1px dashed {t.Color.BORDER.value}",
                    border_radius="12px",
                    width="100%",
                    background=t.Color.SURFACE.value,
                ),
                rx.hstack(
                    rx.button(
                        "Upload & infer mapping",
                        on_click=IngestionAiState.handle_upload(rx.upload_files(upload_id="f3ai_upload")),
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
                    ),
                    rx.button(
                        "Clear",
                        on_click=rx.clear_selected_files("f3ai_upload"),
                        variant="soft",
                        background="transparent",
                        color=t.Color.TEXT_SECONDARY.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="9px",
                    ),
                    spacing="2",
                ),
                rx.cond(IngestionAiState.upload_error != "", c.inline_reason(IngestionAiState.upload_error), rx.fragment()),
                spacing="3",
                align="start",
                width="100%",
            ),
            width="100%",
        ),
        c.card(
            rx.vstack(
                rx.text("Uploads", style=t.TEXT["card_title"]),
                rx.cond(
                    IngestionAiState.uploads.length() > 0,
                    rx.vstack(rx.foreach(IngestionAiState.uploads, _upload_row), spacing="0", width="100%"),
                    c.empty_state("No uploads yet for this client.", icon="file-spreadsheet"),
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


# ---------------------------------------------------------------------------
# Mapping review
# ---------------------------------------------------------------------------


def _field_row(f) -> rx.Component:
    # The select's options must be a Python list of literals; the field's
    # candidate columns are a Var, so offer the current column plus a
    # leave-unmapped sentinel. (Candidates are shown as text below.)
    options = ["(leave unmapped — unavailable)"]
    return rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.hstack(
                    rx.text(f.canonical_field, font_size="13px", font_weight="600"),
                    rx.cond(f.required, rx.text("*", color=t.Color.DANGER.value, font_size="13px"), rx.fragment()),
                    spacing="1",
                    align="baseline",
                ),
                rx.cond(f.reason != "", rx.text(f.reason, style=t.TEXT["micro"]), rx.fragment()),
                rx.cond(
                    f.candidates.length() > 1,
                    rx.text(
                        f"Other candidate(s): {f.candidates[1:].join(', ')}",
                        style=t.TEXT["micro"],
                    ),
                    rx.fragment(),
                ),
                spacing="1",
                align="start",
                flex="2",
            ),
            rx.input(
                value=IngestionAiState.overrides[f.canonical_field],
                on_change=lambda v: IngestionAiState.set_override(f.canonical_field, v),
                placeholder="source column (blank = unmapped)",
                width="220px",
                flex_shrink="0",
            ),
            rx.box(
                rx.cond(
                    f.from_trusted_profile,
                    c.confidence_badge("rule", label="Trusted profile"),
                    rx.cond(
                        f.status == "auto",
                        c.confidence_badge("ai", pct=f.confidence),
                        rx.cond(
                            f.status == "flagged",
                            c.confidence_badge("ai", pct=f.confidence, label=f"AI — {f.confidence}% (review)"),
                            c.pill("Unavailable", variant="placeholder"),
                        ),
                    ),
                ),
                width="150px",
                flex_shrink="0",
            ),
            width="100%",
            align="center",
            spacing="4",
        ),
        spacing="1",
        width="100%",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _mapping_review() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← Back to uploads",
            variant="ghost",
            color_scheme="gray",
            size="1",
            on_click=IngestionAiState.back_to_uploads,
        ),
        rx.cond(
            IngestionAiState.classification_error != "",
            c.inline_reason(IngestionAiState.classification_error),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.warning_banner != "",
            c.warning_banner(IngestionAiState.warning_banner),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.blocked_banner != "",
            c.inline_reason(IngestionAiState.blocked_banner),
            rx.fragment(),
        ),
        rx.cond(
            IngestionAiState.review_status == "confirmed",
            c.confidence_badge("rule", label="Confirmed"),
            rx.fragment(),
        ),
        rx.grid(
            # Left: raw file context
            c.card(
                rx.vstack(
                    rx.text("Raw file", style=t.TEXT["card_title"]),
                    rx.text(
                        f"{IngestionAiState.review_filename} · {IngestionAiState.review_source_label}",
                        style=t.TEXT["micro"],
                    ),
                    rx.cond(IngestionAiState.header_row_note != "", rx.text(IngestionAiState.header_row_note, style=t.TEXT["micro"]), rx.fragment()),
                    rx.text(IngestionAiState.row_count_note, style=t.TEXT["micro"]),
                    rx.cond(
                        IngestionAiState.review_c5_used,
                        c.accent_pill("C5 instruction context applied"),
                        rx.fragment(),
                    ),
                    rx.vstack(
                        rx.foreach(IngestionAiState.notes, lambda n: rx.text(f"ℹ {n}", style=t.TEXT["micro"])),
                        spacing="1",
                        align="start",
                    ),
                    rx.vstack(
                        rx.foreach(IngestionAiState.warnings, lambda w: rx.text(f"⚠ {w}", style=t.TEXT["micro"])),
                        spacing="1",
                        align="start",
                    ),
                    spacing="2",
                    align="start",
                    width="100%",
                ),
                width="100%",
            ),
            # Right: canonical field mapping
            c.card(
                rx.vstack(
                    rx.text("Canonical field mapping", style=t.TEXT["card_title"]),
                    rx.cond(
                        IngestionAiState.confident_fields.length() > 0,
                        rx.text(
                            f"{IngestionAiState.confident_fields.length()} field(s) mapped confidently by AI and pre-selected. Only the field(s) below need your attention.",
                            style=t.TEXT["micro"],
                        ),
                        rx.fragment(),
                    ),
                    rx.cond(
                        IngestionAiState.needs_attention.length() > 0,
                        rx.vstack(
                            rx.foreach(IngestionAiState.needs_attention, _field_row),
                            spacing="0",
                            width="100%",
                        ),
                        rx.text("Every field mapped confidently.", style=t.TEXT["micro"]),
                    ),
                    rx.cond(
                        IngestionAiState.confident_fields.length() > 0,
                        rx.vstack(
                            rx.hstack(
                                rx.switch(
                                    checked=IngestionAiState.show_confident,
                                    on_change=IngestionAiState.set_show_confident,
                                    color_scheme="indigo",
                                ),
                                rx.text(
                                    f"Show the {IngestionAiState.confident_fields.length} field(s) already mapped",
                                    style=t.TEXT["label"],
                                ),
                                spacing="2",
                                align="center",
                            ),
                            rx.cond(
                                IngestionAiState.show_confident,
                                rx.vstack(
                                    rx.foreach(IngestionAiState.confident_fields, _field_row),
                                    spacing="0",
                                    width="100%",
                                ),
                                rx.fragment(),
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
            columns="1fr 1fr",
            spacing="4",
            width="100%",
        ),
        rx.cond(
            IngestionAiState.can_review,
            c.card(
                rx.vstack(
                    rx.hstack(
                        rx.checkbox(
                            checked=IngestionAiState.trust_reuse,
                            on_change=IngestionAiState.set_trust_reuse,
                        ),
                        rx.text(
                            "Trust this mapping for future reuse (this client + source type)",
                            style=t.TEXT["label"],
                        ),
                        spacing="2",
                        align="center",
                    ),
                    rx.button(
                        "Confirm mapping",
                        on_click=IngestionAiState.confirm_mapping,
                        background=t.Color.ACCENT.value,
                        color="#FFFFFF",
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
# Profiles + thresholds
# ---------------------------------------------------------------------------


def _profile_row(p) -> rx.Component:
    return rx.hstack(
        rx.text(p.client_label, style=t.TEXT["body"], flex="2"),
        rx.text(p.source_label, style=t.TEXT["body"], flex="2"),
        rx.box(
            rx.cond(p.trusted, c.confidence_badge("rule", label="Trusted"), c.accent_pill("Not trusted")),
            flex="2",
        ),
        rx.text(rx.cond(p.last_used_at != "", p.last_used_at, "—"), style=t.TEXT["micro"], flex="2"),
        rx.cond(
            p.trusted,
            rx.button(
                "Revoke",
                on_click=IngestionAiState.revoke_profile(p.profile_id),
                size="1",
                variant="soft",
                background="transparent",
                color=t.Color.DANGER.value,
                border=f"1px solid {t.Color.DANGER.value}",
                border_radius="8px",
            ),
            rx.fragment(),
        ),
        width="100%",
        align="center",
        spacing="4",
        padding="8px 0",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def _profiles_admin() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Mapping Profiles", style=t.TEXT["card_title"]),
            rx.text("Per client + source type: trust status, last used, revoke.", style=t.TEXT["label"]),
            rx.cond(
                IngestionAiState.profiles.length() > 0,
                rx.vstack(rx.foreach(IngestionAiState.profiles, _profile_row), spacing="0", width="100%"),
                c.empty_state("No confirmed mapping profiles yet.", icon="table"),
            ),
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
    )


def _thresholds_admin() -> rx.Component:
    return c.card(
        rx.vstack(
            rx.text("Confidence Thresholds", style=t.TEXT["card_title"]),
            rx.text(
                "Admin-configurable defaults. ≥ auto-apply → applied silently; between manual and auto-apply → flagged but suggested; below manual → blank/manual.",
                style=t.TEXT["label"],
            ),
            rx.hstack(
                rx.vstack(
                    rx.text("Auto-apply threshold (%)", style=t.TEXT["label"]),
                    rx.input(
                        value=IngestionAiState.auto_apply.to_string(),
                        on_change=IngestionAiState.set_auto_apply_str,
                        width="140px",
                    ),
                    spacing="1",
                    align="start",
                ),
                rx.vstack(
                    rx.text("Manual threshold (%)", style=t.TEXT["label"]),
                    rx.input(
                        value=IngestionAiState.manual.to_string(),
                        on_change=IngestionAiState.set_manual_str,
                        width="140px",
                    ),
                    spacing="1",
                    align="start",
                ),
                spacing="4",
                align="end",
            ),
            rx.button(
                "Save thresholds",
                on_click=IngestionAiState.save_thresholds,
                background=t.Color.ACCENT.value,
                color="#FFFFFF",
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


def ingestion_ai_page() -> rx.Component:
    return shell.shell(
        rx.vstack(
            c.page_header(
                "Smart Document Ingestion",
                "Upload a raw export — AI infers the column mapping, you confirm it.",
            ),
            rx.cond(IngestionAiState.flash != "", c.info_banner(IngestionAiState.flash), rx.fragment()),
            rx.cond(IngestionAiState.error != "", c.inline_reason(IngestionAiState.error), rx.fragment()),
            rx.cond(
                IngestionAiState.selected_upload_id != 0,
                _mapping_review(),
                rx.vstack(
                    rx.hstack(
                        _section_button("Upload & Uploads List"),
                        rx.cond(IngestionAiState.can_manage_profiles, _section_button("Mapping Profiles (Admin)"), rx.fragment()),
                        rx.cond(IngestionAiState.can_manage_thresholds, _section_button("Confidence Thresholds (Admin)"), rx.fragment()),
                        spacing="2",
                        wrap="wrap",
                        padding="4px",
                        background=t.Color.SURFACE.value,
                        border=f"1px solid {t.Color.BORDER.value}",
                        border_radius="12px",
                        width="100%",
                    ),
                    rx.match(
                        IngestionAiState.section,
                        ("Mapping Profiles (Admin)", _profiles_admin()),
                        ("Confidence Thresholds (Admin)", _thresholds_admin()),
                        _upload_and_list(),
                    ),
                    spacing="4",
                    width="100%",
                    align="start",
                ),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )