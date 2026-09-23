"""Setu — design tokens (Reflex form).

Reuses the locked values from ``src/shared/design_tokens.py`` (the single
source of truth). That module is deliberately framework-agnostic, so the
Reflex app reuses its constants *directly* rather than re-deriving them —
if a shade shifts during the polish phase it changes in exactly one place.

This module adds the Reflex-specific surface on top: a typed ``Color`` enum
for use inside component props, a ``styles`` dict for the app-wide
``rx.App(style=...)`` slot, and ``global_css()`` for the handful of rules
Reflex's style system can't express (font import, ::selection, scrollbars).
"""

from __future__ import annotations

from enum import Enum

from src.shared.design_tokens import (
    COLOR,
    FONT_FAMILY,
    LAYOUT,
    TYPOGRAPHY,
    tokens_css as _tokens_css,
)

# ---------------------------------------------------------------------------
# Color roles (meaning-named, never by hex)
# ---------------------------------------------------------------------------


class Color(str, Enum):
    """Semantic color roles. Use these in props, never a raw hex."""

    PAGE_BACKGROUND = COLOR["page_background"]
    SURFACE = COLOR["surface"]

    TEXT_PRIMARY = COLOR["text_primary"]
    TEXT_SECONDARY = COLOR["text_secondary"]
    TEXT_MUTED = COLOR["text_muted"]

    BORDER = COLOR["border"]

    # rule-match: solid deterministic output
    RULE = COLOR["resolved"]
    RULE_ON = COLOR["resolved_on"]
    # AI-suggested: tinted output (never solid)
    AI = COLOR["ai_suggested"]
    AI_ON = COLOR["ai_suggested_on"]

    DANGER = COLOR["danger"]
    ACCENT = COLOR["accent"]
    NEUTRAL = COLOR["neutral"]
    NEUTRAL_BG = COLOR["neutral_bg"]


# ---------------------------------------------------------------------------
# Typography — Inter, two weights only (400 / 700)
# ---------------------------------------------------------------------------

FONT = f"'{FONT_FAMILY}', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

TEXT = {
    "page_headline": {
        "font_size": f"{TYPOGRAPHY['page_headline']['size']}px",
        "font_weight": "700",
        "letter_spacing": "-0.02em",
        "color": Color.TEXT_PRIMARY.value,
        "line_height": "1.15",
    },
    "section_title": {
        "font_size": "20px",
        "font_weight": "700",
        "letter_spacing": "-0.01em",
        "color": Color.TEXT_PRIMARY.value,
    },
    "card_title": {
        "font_size": f"{TYPOGRAPHY['card_title']['size']}px",
        "font_weight": "700",
        "color": Color.TEXT_PRIMARY.value,
    },
    "body": {
        "font_size": f"{TYPOGRAPHY['body']['size']}px",
        "font_weight": "400",
        "color": Color.TEXT_PRIMARY.value,
        "line_height": "1.55",
    },
    "label": {
        "font_size": f"{TYPOGRAPHY['label']['size']}px",
        "font_weight": "400",
        "color": Color.TEXT_SECONDARY.value,
    },
    "micro": {
        "font_size": f"{TYPOGRAPHY['micro']['size']}px",
        "font_weight": "400",
        "color": Color.TEXT_SECONDARY.value,
    },
}


# ---------------------------------------------------------------------------
# Layout chrome
# ---------------------------------------------------------------------------

RADIUS = f"{LAYOUT['card_radius']}px"
CARD_PADDING = f"{LAYOUT['card_padding']}px"
CARD_SHADOW = LAYOUT["card_shadow"]
SIDEBAR_WIDTH = f"{LAYOUT['sidebar_width']}px"

CARD = {
    "background": Color.SURFACE.value,
    "border": f"1px solid {Color.BORDER.value}",
    "border_radius": RADIUS,
    "padding": CARD_PADDING,
    "box_shadow": CARD_SHADOW,
}

STACK_GAP = f"{LAYOUT['spacing_stack_gap']}px"
CARD_GAP = f"{LAYOUT['spacing_card_gap']}px"
INNER_GAP = f"{LAYOUT['spacing_internal']}px"


# ---------------------------------------------------------------------------
# App-wide style (rx.App(style=...))
# ---------------------------------------------------------------------------

STYLES = {
    "font_family": FONT,
    "color": Color.TEXT_PRIMARY.value,
    "background_color": Color.PAGE_BACKGROUND.value,
}


def global_css() -> str:
    """Return the app-level ``<style>`` block.

    Emitted once via ``rx.App(head_components=...)``. Prepends the canonical
    ``--setu-*`` custom properties (from the shared token module) so any
    hand-written CSS — here or in a component — references the same names.
    """
    return _tokens_css() + f"""
<style>
  :root {{ color-scheme: light; }}
  html, body {{ background-color: {Color.PAGE_BACKGROUND.value}; }}
  body {{ font-family: {FONT}; }}
  * {{ -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }}

  /* Indeterminate progress stripe for the AI-working indicator (components.ai_progress).
     A model call has no meaningful percentage, so the bar scrolls rather than fills. */
  @keyframes setu-indeterminate {{
      from {{ transform: translateX(-48px); }}
      to   {{ transform: translateX(0); }}
  }}
  ::selection {{ background: {Color.ACCENT.value}22; }}
  ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
  ::-webkit-scrollbar-thumb {{
      background: {Color.BORDER.value}; border-radius: 999px;
      border: 3px solid {Color.PAGE_BACKGROUND.value};
  }}
  ::-webkit-scrollbar-thumb:hover {{ background: {Color.NEUTRAL.value}; }}
  [data-setu-tooltip] {{ font-family: {FONT}; }}

  /* Inputs and textareas: white surface with a hairline border, matching the
     card treatment. Radix's default soft fill reads as disabled against the
     page background. */
  input:not([type="checkbox"]):not([type="radio"]),
  textarea,
  .rt-TextFieldInput,
  .rt-TextAreaInput {{
      background-color: {Color.SURFACE.value} !important;
      border: 1px solid {Color.BORDER.value} !important;
      color: {Color.TEXT_PRIMARY.value} !important;
      border-radius: 9px !important;
  }}
  input:not([type="checkbox"]):not([type="radio"]):focus,
  textarea:focus,
  .rt-TextFieldInput:focus,
  .rt-TextAreaInput:focus {{
      border-color: {Color.ACCENT.value} !important;
      outline: none !important;
      box-shadow: 0 0 0 3px {Color.ACCENT.value}22 !important;
  }}
  input::placeholder, textarea::placeholder {{ color: {Color.TEXT_MUTED.value} !important; }}

  /* Select triggers: white surface, hairline border, primary text — Radix's
     default soft fill and muted value text read as disabled. */
  .rt-SelectTrigger {{
      background-color: {Color.SURFACE.value} !important;
      border: 1px solid {Color.BORDER.value} !important;
      border-radius: 9px !important;
      color: {Color.TEXT_PRIMARY.value} !important;
  }}
  .rt-SelectTrigger:hover {{ border-color: {Color.ACCENT.value} !important; }}
  .rt-SelectTrigger .rt-SelectTriggerInner,
  .rt-SelectTrigger span {{ color: {Color.TEXT_PRIMARY.value} !important; }}
  .rt-SelectContent {{ border-radius: 10px !important; }}
</style>
"""
