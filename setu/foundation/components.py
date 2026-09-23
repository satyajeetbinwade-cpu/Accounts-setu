"""Setu — Foundation component library (Reflex form).

The shared component library built ONCE in Step 0. Every module's screens
consume these rather than styling anything ad hoc. Each component implements
the locked visual contract (same tokens, same fill/content rules).

Components (per the UI Foundation spec section 3):
  3.1  confidence_badge        — rule-match (solid, no %) vs AI (tinted, %)
  3.2  deactivate_button / delete_button — Delete vs Deactivate visual split
  3.3  reason_capture          — reason-capture-before-save field
  3.4  inline_reason           — danger-coral micro-label for a blocked action
  3.5  placeholder_badge / placeholder_pill — "Coming with [Module]"
  3.6  headline_grouped_detail — layout template (headline → group → detail)
  3.7  live_status_chip        — Connected / Needs re-auth / Expired
  3.8  activity_panel          — recent-activity list (hairline rows)

Plus the shared primitives the components and screens are built from:
``card``, ``page_header``, ``section_title``, ``pill``, ``info_banner``,
``warning_banner``, ``accent_pill``, ``freshness_chip``, ``tds_stepper``,
``stat``, ``empty_state``.

NOTHING here hard-codes a hex outside the token module.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import tokens as t

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def card(*children, **props) -> rx.Component:
    """The single quiet card surface (16px radius, token padding/shadow).

    Every panel/table/group in the app sits on one of these — there is no
    second card style.
    """
    props.setdefault("width", "100%")
    return rx.box(*children, style=t.CARD, **props)


def page_header(title: str, subtitle: str | None = None) -> rx.Component:
    """The page headline (36px/700) plus an optional one-line purpose caption."""
    return rx.vstack(
        rx.text(title, style=t.TEXT["page_headline"]),
        rx.cond(
            subtitle is not None,
            rx.text(subtitle, style=t.TEXT["label"]),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        width="100%",
    )


def section_title(title: str, subtitle: str | None = None) -> rx.Component:
    """An in-page section heading (20px/700) with an optional caption."""
    return rx.vstack(
        rx.text(title, style=t.TEXT["section_title"]),
        rx.cond(
            subtitle is not None,
            rx.text(subtitle, style=t.TEXT["label"]),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        width="100%",
    )


# Pill variants → (background, foreground, border) using tokens only.
def _pill_colors(variant: str) -> dict[str, str]:
    v = {
        "placeholder": (
            t.Color.NEUTRAL_BG.value,
            t.Color.NEUTRAL.value,
            t.Color.BORDER.value,
        ),
        "rule": (t.Color.RULE.value, "#FFFFFF", "transparent"),
        "ai": (t.Color.AI.value, t.Color.AI_ON.value, "transparent"),
        "accent": ("#EAF0FB", t.Color.ACCENT.value, "#D4E1F8"),
        "danger": ("#FDEBEA", t.Color.DANGER.value, "#F6C9C5"),
    }[variant]
    return {"background": v[0], "color": v[1], "border": f"1px solid {v[2]}"}


def pill(
    label,
    *,
    variant: str = "placeholder",
    icon: str | None = None,
    bold: bool = True,
) -> rx.Component:
    """Base rounded pill. Variants map to the semantic color roles:
    ``placeholder`` (neutral), ``rule`` (solid resolved), ``ai`` (amber
    tint), ``accent`` (blue tint), ``danger`` (coral tint).

    ``icon`` must be a Python string literal (rx.icon requires a literal
    name) — it is included as a child only when supplied, since a Python
    branch keeps the icon name a literal at compile time.
    """
    color_style = _pill_colors(variant)
    children = []
    if icon is not None:
        children.append(rx.icon(icon, size=12, stroke_width=2.5))
    children.append(
        rx.text(
            label,
            font_size="11px",
            font_weight="600" if bold else "500",
            line_height="1.5",
            white_space="nowrap",
        )
    )
    return rx.hstack(
        *children,
        spacing="1",
        align="center",
        display="inline-flex",
        padding="2px 10px",
        border_radius="999px",
        **color_style,
    )


# ---------------------------------------------------------------------------
# 3.1 Confidence badge — rule-match (solid, no %) vs AI (tinted, always %)
# ---------------------------------------------------------------------------


def confidence_badge(
    source,
    *,
    pct=None,
    label: str | None = None,
) -> rx.Component:
    """Rule-match vs AI-suggested confidence badge.

    ``source`` is ``"rule"`` (solid resolved pill, label only, NO %) or
    ``"ai"`` (tinted amber pill, ALWAYS carries a percentage). Per the
    Foundation spec the two forms must differ in BOTH fill and content.
    """
    # `source` is a compile-time literal from the caller in practice; branch in
    # Python so the rule/ai forms stay structurally distinct.
    if source == "rule":
        rule_label = "Rule match" if label is None else rx.cond(label != "", label, "Rule match")
        return pill(rule_label, variant="rule")
    ai_label = label if label is not None else (f"AI — {pct}%" if pct is not None else "AI")
    return pill(ai_label, variant="ai")


# ---------------------------------------------------------------------------
# 3.2 Delete vs Deactivate — visually distinct, never confused
# ---------------------------------------------------------------------------


def deactivate_button(
    label: str = "Deactivate", *, on_click=None, disabled: bool = False, **props
) -> rx.Component:
    """Reversible action: always enabled (unless explicitly disabled), plain
    secondary treatment — no confirm dialog. The Delete side is the one that
    takes a confirmation. Given a visible hairline border so it reads as an
    action rather than faint text.
    """
    props.setdefault("size", "2")
    return rx.button(
        label,
        on_click=on_click,
        disabled=disabled,
        variant="soft",
        background="transparent",
        color=t.Color.TEXT_SECONDARY.value,
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="9px",
        _hover={
            "background": "#F1F4FA",
            "color": t.Color.TEXT_PRIMARY.value,
            "border_color": t.Color.ACCENT.value,
        },
        **props,
    )


def delete_button(
    label: str = "Delete", *, on_click=None, disabled: bool = False, **props
) -> rx.Component:
    """Destructive action: coral/danger treatment, always behind a
    confirmation. When blocked it is disabled AND paired with an
    ``inline_reason`` (3.4) stating why — never a tooltip.
    """
    props.setdefault("size", "2")
    return rx.button(
        label,
        on_click=on_click,
        disabled=disabled,
        variant="soft",
        background="#FDEBEA",
        color=t.Color.DANGER.value,
        border=f"1px solid {t.Color.DANGER.value}",
        border_radius="9px",
        _hover={"background": "#FBDFDD"},
        _disabled={
            "background": "transparent",
            "color": t.Color.TEXT_MUTED.value,
            "border": f"1px solid {t.Color.BORDER.value}",
            "opacity": "1",
        },
        **props,
    )


# ---------------------------------------------------------------------------
# 3.3 reason-capture-before-save
# ---------------------------------------------------------------------------


def reason_capture(
    *,
    label: str = "Reason (required)",
    value=None,
    on_change=None,
    placeholder: str = "Why is this changing?",
    **props,
) -> rx.Component:
    """The reason field that must be filled before a sensitive save commits.

    Pair it with a save button whose ``disabled`` prop is driven by
    ``value == ""`` — the same gate the service layer enforces structurally.
    """
    return rx.vstack(
        rx.text(label, style=t.TEXT["label"]),
        rx.input(
            value="" if value is None else value,
            on_change=on_change,
            placeholder=placeholder,
            width="100%",
            **props,
        ),
        spacing="1",
        align="start",
        width="100%",
    )


# ---------------------------------------------------------------------------
# 3.4 disabled-with-inline-reason
# ---------------------------------------------------------------------------


def inline_reason(text) -> rx.Component:
    """Danger-coral micro-label explaining why an action is blocked.

    Always-visible text (never a tooltip, never truncated).
    """
    return rx.text(
        text,
        font_size="11px",
        color=t.Color.DANGER.value,
        line_height="1.4",
        margin_top="4px",
    )


# ---------------------------------------------------------------------------
# 3.5 Coming-with-[Module] placeholder
# ---------------------------------------------------------------------------


def placeholder_pill(label) -> rx.Component:
    """Greyed neutral-tinted pill for a not-yet-built feature. Reused with a
    caller-supplied label for placeholders whose reason isn't a module ship
    date (e.g. Backup/DR gated on a hosting decision).
    """
    return pill(label, variant="placeholder")


def placeholder_badge(module: str) -> rx.Component:
    """The canonical "Coming with [Module X]" badge, rendered in the exact
    location the real feature will occupy."""
    return placeholder_pill(f"Coming with {module}")


# ---------------------------------------------------------------------------
# 3.6 Headline → grouped → detail (layout template)
# ---------------------------------------------------------------------------


def headline_grouped_detail(
    headline: rx.Component,
    grouped: rx.Component,
    detail: rx.Component,
) -> rx.Component:
    """The three-level information architecture every queue/overview screen
    uses: a big headline figure, a row of grouping controls, then the detail.
    Deliberately a template rather than a widget — callers compose their own
    headline/group/detail and this only fixes the vertical rhythm.
    """
    return rx.vstack(
        headline,
        grouped,
        detail,
        spacing="5",
        width="100%",
        align="start",
    )


# ---------------------------------------------------------------------------
# 3.7 Live connection status chip
# ---------------------------------------------------------------------------

_LIVE_STATUS = {
    "connected": ("Connected", "rule"),
    "needs_reauth": ("Needs re-auth", "ai"),
    "expired": ("Expired", "danger"),
}


def live_status_chip(status, *, label=None) -> rx.Component:
    """Connectivity chip: Connected (solid resolved), Needs re-auth (amber
    tint), Expired (coral tint). Per the Foundation spec this is always
    paired by the calling screen with a masked reference and a Replace
    action — never a Reveal.

    ``label`` may be a Var; each branch resolves it with rx.cond so the pill
    variant stays a Python literal.
    """
    return rx.match(
        status,
        ("connected", pill(rx.cond(label is not None, label, "Connected"), variant="rule", icon="circle-check")),
        ("needs_reauth", pill(rx.cond(label is not None, label, "Needs re-auth"), variant="ai", icon="triangle-alert")),
        ("expired", pill(rx.cond(label is not None, label, "Expired"), variant="danger", icon="circle-slash")),
        pill(rx.cond(label is not None, label, "Unknown"), variant="placeholder"),
    )


# ---------------------------------------------------------------------------
# 3.8 Recent activity panel
# ---------------------------------------------------------------------------


def activity_panel(rows, *, empty_message: str = "No activity recorded yet.") -> rx.Component:
    """Card of hairline-divided rows: title (body), detail (secondary),
    right-aligned micro timestamp. Newest first is the caller's job.

    ``rows`` is a Reflex Var list of dataclass instances with ``title``,
    ``detail`` and ``timestamp`` attributes (typed, so it can be iterated).
    """
    return rx.cond(
        rows.length() > 0,
        card(
            rx.vstack(
                rx.foreach(
                    rows,
                    lambda r: rx.hstack(
                        rx.hstack(
                            rx.text(
                                r.title,
                                font_size="13px",
                                color=t.Color.TEXT_PRIMARY.value,
                                font_weight="600",
                            ),
                            rx.text(
                                r.detail,
                                font_size="12px",
                                color=t.Color.TEXT_SECONDARY.value,
                            ),
                            spacing="2",
                            align="baseline",
                            wrap="wrap",
                        ),
                        rx.spacer(),
                        rx.text(
                            r.timestamp,
                            style=t.TEXT["micro"],
                            white_space="nowrap",
                        ),
                        width="100%",
                        padding="8px 2px",
                        border_bottom=f"1px solid {t.Color.BORDER.value}",
                        align="center",
                    ),
                ),
                spacing="0",
                width="100%",
            ),
            padding="12px 18px",
        ),
        empty_state(empty_message),
    )


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------


def info_banner(text) -> rx.Component:
    """Informational-accent banner — non-warning, non-error notices."""
    return rx.box(
        rx.text(text, font_size="13px", color=t.Color.TEXT_PRIMARY.value),
        background="#EAF0FB",
        border="1px solid #D4E1F8",
        border_radius="10px",
        padding="10px 14px",
        width="100%",
    )


def warning_banner(text) -> rx.Component:
    """Warning-amber banner — high-impact / wide-blast-radius notices that
    need explicit confirmation. Uses the amber caution role (the Foundation
    has no separate warning token)."""
    return rx.box(
        rx.text(text, font_size="13px", color=t.Color.AI_ON.value),
        background="#FDF4E3",
        border="1px solid #F0D9AE",
        border_radius="10px",
        padding="10px 14px",
        width="100%",
    )


def accent_pill(text) -> rx.Component:
    """Small informational-accent tinted pill (e.g. "Overridden for N
    clients")."""
    return pill(text, variant="accent")


def freshness_chip(status, *, label=None) -> rx.Component:
    """Data-freshness chip (regulatory rules). Reuses the semantic pills —
    NOT the connectivity chip: seed→amber, reviewed→solid green,
    stale→coral."""
    return rx.match(
        status,
        ("seed", pill(rx.cond(label is not None, label, "Seed data — unverified"), variant="ai")),
        ("reviewed", pill(rx.cond(label is not None, label, "Reviewed"), variant="rule")),
        ("stale", pill(rx.cond(label is not None, label, "Stale — not reviewed recently"), variant="danger")),
        pill(rx.cond(label is not None, label, "Unknown"), variant="placeholder"),
    )


# ---------------------------------------------------------------------------
# TDS six-step chain stepper (Module 2 — the one bespoke pattern)
# ---------------------------------------------------------------------------

_STEPPER_STATE = {
    "complete": ("✓", "Complete", "rule"),
    "gap": ("✗", "Gap", "danger"),
    "timing_lag": ("~", "Timing lag", "ai"),
}


def _stepper_stage(stage) -> rx.Component:
    return rx.vstack(
        rx.box(
            rx.text(
                rx.match(
                    stage["state"],
                    ("complete", "✓"),
                    ("gap", "✗"),
                    ("timing_lag", "~"),
                    "?",
                ),
                color=rx.match(
                    stage["state"],
                    ("timing_lag", t.Color.AI_ON.value),
                    "#FFFFFF",
                ),
                font_size="11px",
                font_weight="600",
            ),
            width="22px",
            height="22px",
            border_radius="50%",
            display="flex",
            align_items="center",
            justify_content="center",
            background=rx.match(
                stage["state"],
                ("complete", t.Color.RULE.value),
                ("gap", t.Color.DANGER.value),
                ("timing_lag", t.Color.AI.value),
                t.Color.NEUTRAL.value,
            ),
        ),
        rx.text(
            stage["label"],
            font_size="12px",
            font_weight="700",
            color=t.Color.TEXT_PRIMARY.value,
            text_align="center",
        ),
        rx.text(
            rx.match(
                stage["state"],
                ("complete", "Complete"),
                ("gap", "Gap"),
                ("timing_lag", "Timing lag"),
                stage["state"],
            ),
            font_size="11px",
            color=t.Color.TEXT_SECONDARY.value,
        ),
        spacing="1",
        align="center",
        flex="1 1 0",
        min_width="90px",
    )


def tds_stepper(stages) -> rx.Component:
    """Horizontal TDS six-step chain stepper. Every stage pairs its color
    with a text state label (never color alone). ``stages`` is a list of
    dicts with ``label`` and ``state`` (complete | gap | timing_lag).
    """
    return rx.hstack(
        rx.foreach(stages, _stepper_stage),
        spacing="2",
        width="100%",
        align="start",
        wrap="wrap",
    )


# ---------------------------------------------------------------------------
# Small shared bits
# ---------------------------------------------------------------------------


def stat(value, label, *, accent: bool = False) -> rx.Component:
    """A headline figure with its label — the "headline" of 3.6."""
    return rx.vstack(
        rx.text(
            value,
            font_size="30px",
            font_weight="700",
            letter_spacing="-0.02em",
            color=t.Color.ACCENT.value if accent else t.Color.TEXT_PRIMARY.value,
            line_height="1.1",
        ),
        rx.text(label, style=t.TEXT["label"]),
        spacing="1",
        align="start",
    )


def empty_state(message: str, *, icon: str = "inbox") -> rx.Component:
    """Neutral, quiet empty state — used wherever a list/panel has no rows."""
    return rx.vstack(
        rx.icon(icon, size=22, color=t.Color.NEUTRAL.value),
        rx.text(message, style=t.TEXT["label"], text_align="center"),
        spacing="2",
        align="center",
        justify="center",
        width="100%",
        padding="28px 16px",
    )


def divider() -> rx.Component:
    """A hairline divider between major sections."""
    return rx.box(
        height="1px",
        width="100%",
        background=t.Color.BORDER.value,
    )


# ---------------------------------------------------------------------------
# Shared upload / preview / viewer (F3 + F3-B — the one implementation)
# ---------------------------------------------------------------------------


def file_preview_chip(
    pf,
    *,
    on_remove=None,
) -> rx.Component:
    """FilePreviewChip — a staged file in the drop zone before upload.

    Shows an image/PDF-first-page thumbnail (or a file-type icon) plus the
    filename, human-readable size and a remove (x). Consumed identically by
    F3's Upload document zone and F3-B's Upload invoices zone.

    ``pf`` is a ``PendingFile`` instance (filename / size / thumb_kind /
    thumb_src / ext).
    """
    thumb = rx.cond(
        pf.thumb_kind == "image",
        rx.image(src=pf.thumb_src, width="42px", height="42px", object_fit="cover", border_radius="8px"),
        rx.cond(
            pf.thumb_kind == "pdf",
            rx.image(src=pf.thumb_src, width="42px", height="42px", object_fit="cover", border_radius="8px"),
            rx.box(
                rx.icon("file", size=20, color=t.Color.TEXT_SECONDARY.value),
                width="42px",
                height="42px",
                border_radius="8px",
                background=t.Color.NEUTRAL_BG.value,
                display="flex",
                align_items="center",
                justify_content="center",
            ),
        ),
    )
    return rx.hstack(
        thumb,
        rx.vstack(
            rx.text(pf.filename, font_size="13px", font_weight="600", color=t.Color.TEXT_PRIMARY.value),
            rx.text(pf.size_label, style=t.TEXT["micro"]),
            spacing="1",
            align="start",
            flex="1",
            min_width="0",
        ),
        rx.button(
            rx.icon("x", size=14),
            on_click=on_remove,
            size="1",
            variant="soft",
            background="transparent",
            color=t.Color.TEXT_MUTED.value,
            border="none",
            _hover={"color": t.Color.DANGER.value},
            flex_shrink="0",
        ),
        width="100%",
        align="center",
        spacing="3",
        padding="8px 10px",
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="10px",
        background=t.Color.SURFACE.value,
    )


def ai_progress(label, *, visible=None) -> rx.Component:
    """A visible "AI is working" indicator for any long-running model call.

    A live model call takes ~15-25s. Without this the whole screen appears
    frozen. Renders an indeterminate striped bar (no meaningful percentage
    exists for a model call) plus a spinner and the caller's label of what
    is happening. ``label`` is a Var string; empty = nothing shown unless
    ``visible`` is supplied.
    """
    shown = visible if visible is not None else (label != "")
    return rx.cond(
        shown,
        rx.hstack(
            rx.spinner(size="2", color=t.Color.ACCENT.value),
            rx.vstack(
                rx.text(label, font_size="12px", font_weight="600", color=t.Color.TEXT_PRIMARY.value),
                rx.el.div(
                    rx.el.div(
                        style={
                            "height": "6px",
                            "border_radius": "999px",
                            "background": (
                                "repeating-linear-gradient(90deg, "
                                + t.Color.ACCENT.value + " 0 24px, "
                                + t.Color.BORDER.value + " 24px 48px)"
                            ),
                            "animation": "setu-indeterminate 1.1s linear infinite",
                            "width": "100%",
                        },
                    ),
                    style={"width": "100%", "overflow": "hidden", "border_radius": "999px"},
                ),
                spacing="2",
                align="start",
                flex="1",
                min_width="0",
            ),
            width="100%",
            align="center",
            spacing="3",
            padding="12px 14px",
            background=t.Color.SURFACE.value,
            border=f"1px solid {t.Color.BORDER.value}",
            border_radius="10px",
        ),
        rx.fragment(),
    )


def upload_progress_row(
    row,
) -> rx.Component:
    """One per-file upload stage (UploadProgressState): Queued → Uploading
    (%) → Processing (spinner) → Done / Needs review / Failed. Every stage is
    a distinct icon + label (never a colour-only swap)."""
    stage_icon = rx.match(
        row.stage,
        ("queued", rx.icon("circle-dot", size=15, color=t.Color.TEXT_MUTED.value)),
        ("uploading", rx.icon("arrow-up-from-line", size=15, color=t.Color.ACCENT.value)),
        ("processing", rx.spinner(size="1")),
        ("done", rx.icon("circle-check", size=15, color=t.Color.RULE.value)),
        ("needs_review", rx.icon("triangle-alert", size=15, color=t.Color.AI_ON.value)),
        ("failed", rx.icon("circle-slash", size=15, color=t.Color.DANGER.value)),
        rx.icon("circle-dot", size=15, color=t.Color.TEXT_MUTED.value),
    )
    label_color = rx.match(
        row.stage,
        ("failed", t.Color.DANGER.value),
        ("needs_review", t.Color.AI_ON.value),
        t.Color.TEXT_PRIMARY.value,
    )
    return rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.text(row.filename, font_size="12px", font_weight="600", color=label_color),
                rx.cond(
                    row.stage == "uploading",
                    rx.progress(value=row.pct, width="100%", size="1"),
                    rx.fragment(),
                ),
                spacing="1",
                align="start",
                flex="1",
                min_width="0",
            ),
            stage_icon,
            width="100%",
            align="center",
            spacing="2",
        ),
        rx.cond(
            row.label != "",
            rx.text(row.label, style=t.TEXT["micro"]),
            rx.fragment(),
        ),
        spacing="1",
        align="start",
        width="100%",
        padding="6px 8px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def file_viewer_panel(
    *,
    title_var,
    kind_var,
    image_src_var,
    page_var,
    page_count_var,
    table_headers_var,
    table_rows_var,
    text_var,
    download_name_var,
    download_href_var,
    on_close,
    on_page_prev,
    on_page_next,
    zoom_var=1.0,
    on_zoom_in=None,
    on_zoom_out=None,
    on_zoom_reset=None,
    highlight_active_var=False,
    highlight_x_var=0.0,
    highlight_y_var=0.0,
    highlight_w_var=0.0,
    highlight_h_var=0.0,
    highlight_page_var=1,
    highlight_row_index_var=-1,
    highlight_snippet_var="",
) -> rx.Component:
    """FileViewerPanel — the embedded in-app viewer for images and PDFs
    (page controls for multi-page), with a "download to view" fallback for
    formats that can't render inline (xlsx/docx/csv are shown as a table or
    plain text here; genuinely non-renderable bytes fall through to the
    download state).

    Highest-priority fix: no screen previously let a user see a document's
    actual content — this is the one place that does. Shared by F3 and F3-B.

    Zoom/pan: ``zoom_var`` scales the image (pan is native scroll on the
    overflow container). Zoom controls render only when the handlers are
    supplied, so callers that don't want them are unaffected.

    Click-to-highlight (F3-B only): ``highlight_*`` params draw a box over
    the source image (visual touchpoint), tint the matching table row
    (structured/Excel), or show a highlighted text excerpt (structured
    PDF/Word). All default to "off" so F3's call site is unchanged.
    """
    # Coerce the highlight/zoom defaults to Vars — callers that don't pass
    # them (F3 Documents) supply plain Python literals, which have no
    # ``.to_string()`` / arithmetic Var behaviour.
    highlight_active_var = rx.Var.create(highlight_active_var)
    highlight_x_var = rx.Var.create(highlight_x_var)
    highlight_y_var = rx.Var.create(highlight_y_var)
    highlight_w_var = rx.Var.create(highlight_w_var)
    highlight_h_var = rx.Var.create(highlight_h_var)
    highlight_page_var = rx.Var.create(highlight_page_var)
    highlight_row_index_var = rx.Var.create(highlight_row_index_var)
    highlight_snippet_var = rx.Var.create(highlight_snippet_var)
    zoom_var = rx.Var.create(zoom_var)

    header = rx.hstack(
        rx.icon("file-text", size=16, color=t.Color.ACCENT.value),
        rx.text(title_var, font_size="14px", font_weight="700", flex="1", min_width="0"),
        rx.button(
            rx.icon("x", size=16),
            on_click=on_close,
            variant="ghost",
            color_scheme="gray",
            background="transparent",
            color=t.Color.TEXT_MUTED.value,
        ),
        width="100%",
        align="center",
        spacing="2",
    )

    # Zoom controls — only rendered when the caller wires handlers.
    zoom_controls = rx.fragment()
    if on_zoom_in is not None:
        zoom_controls = rx.hstack(
            rx.button(
                rx.icon("zoom-out", size=14),
                on_click=on_zoom_out,
                size="1",
                variant="soft",
                color_scheme="gray",
                disabled=zoom_var <= 0.5,
            ),
            rx.button(
                (zoom_var * 100).to(int).to_string() + "%",
                on_click=on_zoom_reset,
                size="1",
                variant="ghost",
                color_scheme="gray",
                background="transparent",
                color=t.Color.TEXT_SECONDARY.value,
            ),
            rx.button(
                rx.icon("zoom-in", size=14),
                on_click=on_zoom_in,
                size="1",
                variant="soft",
                color_scheme="gray",
                disabled=zoom_var >= 3.0,
            ),
            spacing="1",
            align="center",
        )

    # The image, scaled by zoom, with an optional highlight box overlaid.
    # The overlay is positioned in percentages of the (unscaled) image box,
    # so it tracks the image as it scales.
    image_body = rx.box(
        rx.box(
            rx.image(src=image_src_var, width="100%", border_radius="8px", alt=title_var),
            rx.cond(
                highlight_active_var & (highlight_page_var == page_var),
                rx.box(
                    style={
                        "position": "absolute",
                        "left": (highlight_x_var * 100).to_string() + "%",
                        "top": (highlight_y_var * 100).to_string() + "%",
                        "width": (highlight_w_var * 100).to_string() + "%",
                        "height": (highlight_h_var * 100).to_string() + "%",
                        "border": f"2px solid {t.Color.ACCENT.value}",
                        "background": "rgba(59, 111, 219, 0.18)",
                        "border_radius": "4px",
                        "pointer_events": "none",
                    },
                ),
                rx.fragment(),
            ),
            position="relative",
            width="100%",
            style={
                "transform": "scale(" + zoom_var.to_string() + ")",
                "transform_origin": "top left",
                "transition": "transform 0.15s ease",
            },
        ),
        width="100%",
        overflow="auto",
        max_height="640px",
    )

    body = rx.match(
        kind_var,
        (
            "image",
            rx.vstack(
                image_body,
                rx.cond(zoom_var != 1.0, zoom_controls, rx.fragment()),
                spacing="2",
                align="start",
                width="100%",
            ),
        ),
        (
            "pdf",
            rx.vstack(
                image_body,
                rx.hstack(
                    rx.cond(
                        page_count_var.to(int) > 1,
                        rx.hstack(
                            rx.button(rx.icon("chevron-left", size=14), on_click=on_page_prev, size="1", variant="soft", color_scheme="gray", disabled=page_var.to(int) <= 1),
                            rx.text(f"Page {page_var} of {page_count_var}", style=t.TEXT["micro"]),
                            rx.button(rx.icon("chevron-right", size=14), on_click=on_page_next, size="1", variant="soft", color_scheme="gray", disabled=page_var.to(int) >= page_count_var.to(int)),
                            spacing="2",
                            align="center",
                        ),
                        rx.fragment(),
                    ),
                    rx.spacer(),
                    zoom_controls,
                    width="100%",
                    align="center",
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
        ),
        (
            "table",
            rx.el.div(
                rx.el.table(
                    rx.el.thead(
                        rx.el.tr(
                            rx.foreach(
                                table_headers_var,
                                lambda h: rx.el.th(
                                    h,
                                    style={
                                        "text_align": "left",
                                        "font_size": "11px",
                                        "padding": "4px 8px",
                                        "border_bottom": f"1px solid {t.Color.BORDER.value}",
                                        "color": t.Color.TEXT_SECONDARY.value,
                                        "white_space": "nowrap",
                                    },
                                ),
                            ),
                        ),
                    ),
                    rx.el.tbody(
                        rx.foreach(
                            table_rows_var,
                            lambda row, idx: rx.el.tr(
                                rx.foreach(
                                    row,
                                    lambda cell: rx.el.td(
                                        cell,
                                        style={
                                            "font_size": "12px",
                                            "padding": "4px 8px",
                                            "white_space": "nowrap",
                                            "color": t.Color.TEXT_PRIMARY.value,
                                        },
                                    ),
                                ),
                                id="setu-hl-row-" + idx.to_string(),
                                style=rx.cond(
                                    idx == highlight_row_index_var,
                                    {
                                        "background": "rgba(59, 111, 219, 0.16)",
                                        "outline": f"1px solid {t.Color.ACCENT.value}",
                                    },
                                    {},
                                ),
                            ),
                        ),
                    ),
                    style={"border_collapse": "collapse", "width": "100%"},
                ),
                width="100%",
                overflow="auto",
                max_height="440px",
            ),
        ),
        (
            "text",
            rx.vstack(
                rx.cond(
                    highlight_snippet_var != "",
                    rx.box(
                        rx.text("Located in the document:", style=t.TEXT["micro"]),
                        rx.text(
                            highlight_snippet_var,
                            style=t.TEXT["body"],
                            font_family="monospace",
                            font_size="12px",
                        ),
                        background="rgba(59, 111, 219, 0.12)",
                        border=f"1px solid {t.Color.ACCENT.value}",
                        border_radius="8px",
                        padding="8px 10px",
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                rx.scroll_area(
                    rx.el.pre(
                        text_var,
                        style={
                            "font_size": "12px",
                            "font_family": "monospace",
                            "white_space": "pre-wrap",
                            "color": t.Color.TEXT_PRIMARY.value,
                            "padding": "12px",
                        },
                    ),
                    height="360px",
                    width="100%",
                ),
                spacing="2",
                align="start",
                width="100%",
            ),
        ),
        (
            "download",
            rx.vstack(
                rx.icon("download", size=22, color=t.Color.NEUTRAL.value),
                rx.text("This format can't be previewed inline.", style=t.TEXT["label"]),
                rx.link(
                    rx.button("Download to view", variant="soft", background="transparent", color=t.Color.ACCENT.value, border=f"1px solid {t.Color.ACCENT.value}", border_radius="9px"),
                    href=download_href_var,
                    download=download_name_var,
                ),
                spacing="2",
                align="center",
                width="100%",
                padding="28px 16px",
            ),
        ),
        rx.text("No preview available.", style=t.TEXT["micro"]),
    )

    return rx.box(
        rx.vstack(
            header,
            rx.box(height="1px", width="100%", background=t.Color.BORDER.value),
            body,
            spacing="3",
            align="start",
            width="100%",
        ),
        width="100%",
        border=f"1px solid {t.Color.BORDER.value}",
        border_radius="12px",
        background=t.Color.SURFACE.value,
        padding="14px",
    )


# ---------------------------------------------------------------------------
# F4 — the single reusable "View History" affordance
# ---------------------------------------------------------------------------


def history_panel(rows, *, empty_message: str = "No edit history recorded yet.") -> rx.Component:
    """The per-record History panel: hairline-divided rows, newest first,
    each entry in plain language ("Changed from X to Y, by [name], on [date],
    because [reason]."). Read-only — never prompts for anything.

    ``rows`` is a Var list of typed dataclass instances with
    ``plain_language`` and ``timestamp`` attributes.
    """
    return rx.cond(
        rows.length() > 0,
        rx.vstack(
            rx.foreach(
                rows,
                lambda r: rx.hstack(
                    rx.text(r.plain_language, style=t.TEXT["body"], flex="1"),
                    rx.text(r.timestamp, style=t.TEXT["micro"], white_space="nowrap"),
                    width="100%",
                    padding="8px 2px",
                    border_bottom=f"1px solid {t.Color.BORDER.value}",
                    align="center",
                    spacing="3",
                ),
            ),
            spacing="0",
            width="100%",
        ),
        rx.text(empty_message, style=t.TEXT["micro"]),
    )


def view_history(
    label: str,
    rows,
    *,
    count=None,
) -> rx.Component:
    """The ONE reusable "View History" trigger + panel, placed inline next to
    any editable field or record header across every module.

    ``rows`` is the typed history list for the record (see
    ``setu.state.history.load_history``); ``count`` (a Var or int) labels the
    trigger. The panel is read-only. End-Client never sees the affordance —
    callers omit it for that role (fully absent, not disabled).
    """
    if count is None:
        trigger = f"🕓 {label}"
    else:
        trigger = rx.cond(
            count > 0,
            f"🕓 {label} (" + count.to_string() + ")",
            f"🕓 {label}",
        )
    return rx.popover.root(
        rx.popover.trigger(
            rx.button(
                trigger,
                size="1",
                variant="soft",
                background="transparent",
                color=t.Color.TEXT_SECONDARY.value,
                border=f"1px solid {t.Color.BORDER.value}",
                border_radius="8px",
                _hover={"background": "#F1F4FA", "color": t.Color.TEXT_PRIMARY.value},
            ),
        ),
        rx.popover.content(
            rx.box(
                history_panel(rows),
                max_height="360px",
                overflow="auto",
                width="100%",
            ),
            width="420px",
        ),
    )
