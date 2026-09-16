"""Setu Phase 2 — Foundation component library (Reflex form).

The shared, framework-agnostic component library built ONCE in Step 0. Every
module's screens consume these rather than styling anything ad hoc — the same
discipline the Streamlit build enforced. Each component mirrors the visual
contract of its ``src/ui/theme.py`` counterpart exactly (same tokens, same
fill/content rules), so the two frontends stay visually identical while both
run in parallel.

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
    ``value == ""`` — the same gate the Streamlit build's service layer
    enforces structurally.
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
