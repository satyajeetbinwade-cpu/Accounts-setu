"""Setu — design tokens (color, typography, layout chrome).

This module codifies section 2 of the UI Foundation spec into reusable,
module-level constants plus a CSS-variable emitter. It is presentation
infrastructure only: no widget helpers, no component renderers (those are the
separate "component specs" in section 3 of the spec).

Roles are keyed by *meaning*, not by hex — so a module's build prompt stays
correct even if a shade shifts during the later delight/polish phase. Dark
mode, animation, and brand-identity polish are explicitly out of scope (all
deferred to the later polish phase); nothing here is final visual identity.

Nothing in this module talks to any UI framework on import — it can be
imported freely by any module (UI or otherwise) without side effects.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 2.1 Color roles
# ---------------------------------------------------------------------------
#
# Named by meaning. The special "resolved"/"AI-suggested" roles carry a
# paired "on_tint" value for text rendered on a tinted fill of that hue.

COLOR = {
    "page_background": "#EEF0F2",   # neutral gray-green canvas behind all cards
    "surface": "#FFFFFF",           # card / panel / table-row background
    "text_primary": "#16181D",      # headlines, body, row text (never pure black)
    "text_secondary": "#6B7280",    # labels, table headers, dates
    "text_muted": "#9CA3AF",        # placeholder / disabled / hint text
    "border": "#E7E9EC",            # 0.5–1px hairline dividers and card outlines
    "resolved": "#6FA84B",          # rule-match: solid deterministic output
    "resolved_on": "#2E4A1E",       # text on a resolved tint
    "ai_suggested": "#E8A33D",      # AI-suggested: tinted output (never solid)
    "ai_suggested_on": "#8A5A12",   # text on an AI-suggested tint
    "danger": "#E2574C",            # blocked / escalated / overdue / delete
    "accent": "#3B6FDB",            # primary interactive action (one per view)
    "neutral": "#9CA3AF",           # non-semantic grouping, placeholders
    "neutral_bg": "#F1F2F4",        # background tint for the neutral role
}

# ---------------------------------------------------------------------------
# 2.2 Typography
# ---------------------------------------------------------------------------
#
# Inter, two weights only (400 regular, 700 bold). No in-between weight.

FONT_FAMILY = "Inter"

TYPOGRAPHY = {
    "page_headline": {"size": 36, "weight": 700},        # 34–38px, bold
    "card_title": {"size": 15, "weight": 700},           # bold
    "body": {"size": 13.5, "weight": 400},               # 13–13.5px, regular
    "label": {"size": 12, "weight": 400},                # card meta / labels
    "micro": {"size": 11, "weight": 400},                # timestamps
    "badge": {"size": 11, "weight": 600},                # badge text, semibold
}

# ---------------------------------------------------------------------------
# 2.3 Layout chrome
# ---------------------------------------------------------------------------

LAYOUT = {
    "card_radius": 16,                                   # px
    "card_padding": 21,                                  # px (20–22)
    "card_shadow": (
        "0 1px 3px rgba(20,20,20,.06), "
        "0 1px 2px rgba(20,20,20,.04)"
    ),
    "sidebar_width": 236,                                # px, fixed
    "sidebar_active_bar": 3,                             # px left accent bar
    "table_row_border": 1,                               # px hairline
    "spacing_card_gap": 16,                              # px, sibling cards in a row
    "spacing_stack_gap": 22,                             # px, stacked row groups (20–24)
    "spacing_internal": 10,                              # px, internal component spacing (8–12)
}


# ---------------------------------------------------------------------------
# CSS variable emitter
# ---------------------------------------------------------------------------

def tokens_css() -> str:
    """Return ONLY the canonical ``--setu-*`` custom properties as a bare
    ``:root { … }`` rule (no ``<style>`` wrapper).

    Consuming modules reference ``var(--setu-surface)`` etc. rather than
    hard-coding hex values, so a token shift in the polish phase propagates
    everywhere without touching per-module styles. This is the *single* source
    of truth for token values: the names are the canonical short form already
    referenced across the code base.

    GOTCHA (fixed): this used to be wrapped in ``<style>`` tags, but the one
    consumer passes it to ``rx.el.style(...)`` — which already emits the
    element. React rendered the nested ``<style>`` as escaped text, which
    invalidated the leading selector and silently stopped EVERY ``--setu-*``
    property from applying. A full document string is available via
    ``setu.foundation.tokens.global_css()``.
    """
    vars_: list[tuple[str, str]] = [
        ("--setu-font-family", FONT_FAMILY),
        # color roles
        ("--setu-page-bg", COLOR["page_background"]),
        ("--setu-surface", COLOR["surface"]),
        ("--setu-text-primary", COLOR["text_primary"]),
        ("--setu-text-secondary", COLOR["text_secondary"]),
        ("--setu-text-muted", COLOR["text_muted"]),
        ("--setu-border", COLOR["border"]),
        ("--setu-rule", COLOR["resolved"]),
        ("--setu-rule-text", COLOR["resolved_on"]),
        ("--setu-ai", COLOR["ai_suggested"]),
        ("--setu-ai-text", COLOR["ai_suggested_on"]),
        ("--setu-danger", COLOR["danger"]),
        ("--setu-accent", COLOR["accent"]),
        ("--setu-neutral", COLOR["neutral"]),
        ("--setu-neutral-bg", COLOR["neutral_bg"]),
        # layout chrome
        ("--setu-card-radius", f"{LAYOUT['card_radius']}px"),
        ("--setu-card-padding", f"{LAYOUT['card_padding']}px"),
        ("--setu-card-shadow", LAYOUT["card_shadow"]),
        ("--setu-sidebar-width", f"{LAYOUT['sidebar_width']}px"),
        ("--setu-sidebar-active-bar", f"{LAYOUT['sidebar_active_bar']}px"),
        ("--setu-table-row-border", f"{LAYOUT['table_row_border']}px"),
        ("--setu-spacing-card-gap", f"{LAYOUT['spacing_card_gap']}px"),
        ("--setu-spacing-stack-gap", f"{LAYOUT['spacing_stack_gap']}px"),
        ("--setu-spacing-internal", f"{LAYOUT['spacing_internal']}px"),
    ]
    rules = "\n".join(f"    {name}: {value};" for name, value in vars_)
    return f":root {{\n{rules}\n}}\n"