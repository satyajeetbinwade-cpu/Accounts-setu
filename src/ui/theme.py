"""Light theming and shared UI constants for the Setu review app.

This module is presentation-only. It exposes:

- ``inject_css()`` — a single ``st.markdown(..., unsafe_allow_html=True)``
  call that adds minimal CSS for spacing, section headers, and status
  pills. Called once from ``app.py`` after ``st.set_page_config``.
- ``CLASSIFICATION_HELP`` / ``DIFFERENCE_TYPE_HELP`` — plain-language
  explanations for the four classifications and the difference_type
  values the matchers emit. These are the strings shown via the ``help=``
  parameter on filter widgets; the underlying classification /
  difference_type values themselves are unchanged.
- ``render_status_pill()`` — a small rounded label used in places where
  ``st.badge`` isn't appropriate (e.g. inside a metric card's caption).
"""

from __future__ import annotations

import streamlit as st

from src.ui.design_tokens import FONT_FAMILY, tokens_css

# ---------------------------------------------------------------------------
# Plain-language help text for filter widgets
# ---------------------------------------------------------------------------

CLASSIFICATION_HELP: dict[str, str] = {
    "Matched": "Books and portal agree on this transaction \u2014 no action needed.",
    "Not in Books": "The portal shows this transaction but it is missing from the books export.",
    "Not in Portal": "The books show this transaction but it is missing from the portal return.",
    "Amount Difference": "Both sides have the transaction but the value differs \u2014 review the amounts.",
}

# These are the difference_type values the matchers actually emit. The
# strings must stay exactly as built \u2014 downstream code and the
# verification harness depend on them. The help text is plain-language
# only.
DIFFERENCE_TYPE_HELP: dict[str, str] = {
    "Tax Split Mismatch": "Books and portal agree on the total tax but split it differently between CGST/SGST/IGST.",
    "Taxable Value Difference": "The taxable value (before tax) differs between books and portal.",
    "Tax Amount Difference": "The total tax amount differs between books and portal.",
    "Rate Mismatch": "The tax rate applied in books does not match the rate on the portal return.",
    "Document Type Mismatch": "Books and portal classify the document differently (e.g. invoice vs credit note).",
    "Timing Difference": "Same transaction, but recorded in different periods on the two sides.",
    "Rounding": "A small difference caused by rounding \u2014 usually safe to ignore.",
    "Duplicate in Books": "The same invoice appears more than once in the books export.",
    "Unexplained": "Books and portal disagree but the engine couldn't pinpoint the specific reason.",
    "Short Deduction": "Tax was deducted but the amount is less than what the section requires.",
}

BAND_HELP: dict[str, str] = {
    "High": "The match is very likely correct \u2014 little reason to second-guess.",
    "Medium": "Probably correct, but worth a quick look.",
    "Low": "Uncertain \u2014 review carefully before approving.",
}

REVIEW_STATE_HELP: dict[str, str] = {
    "Unreviewed": "No reviewer has signed off on this row yet.",
    "Reviewed": "A reviewer has approved this row in a previous run.",
    "Stale": "Previously reviewed, but the verdict has changed since \u2014 review again.",
}


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
<style>
/* Apply Inter globally. Foundation 2.2: two weights only (400 regular, 700
   bold). The token layer (single source of truth) is prepended separately. */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap');

html, body, [class*="css"] {
    font-family: __FONT__, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
}

/* Foundation 2.1/2.3: page canvas is the neutral gray-green token \u2014 cards
   read as surfaces above it; content never touches the page edge. */
html, body, [data-testid="stAppViewContainer"] {
    background-color: var(--setu-page-bg);
}
.main .block-container {
    padding-top: 1.5rem;
    padding-bottom: 2rem;
    max-width: 1440px;
}

/* Foundation 2.3 sidebar: fixed 236px; active item = 3px accent bar on an
   accent tint \u2014 never bold text alone. */
[data-testid="stSidebar"] {
    width: var(--setu-sidebar-width) !important;
    min-width: var(--setu-sidebar-width) !important;
    background: var(--setu-surface);
    border-right: 1px solid var(--setu-border);
}
[data-testid="stSidebar"] [data-testid="stSidebarNav"] li,
[data-testid="stSidebar"] [role="radiogroup"] label {
    border-left: var(--setu-sidebar-active-bar) solid transparent;
}

/* Section spacing rhythm \u2014 consistent gap between blocks across tabs. */
.block-container { padding-top: 2rem; padding-bottom: 2rem; }
div[data-testid="stVerticalBlock"] > div { gap: 0.6rem; }
h2, h3 { margin-top: 0.4rem; }

/* Foundation 2.2: headers use the locked weight pair only (400/700). */
h2 { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.01em; }
h3 { font-size: 1.10rem; font-weight: 700; }

/* Status pills \u2014 small rounded labels for classification / band /
   review state. Used in places where st.badge isn't appropriate. */
.setu-pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    line-height: 1.4;
    margin-right: 4px;
    border: 1px solid transparent;
}
.setu-pill.green   { background: #e6f4ea; color: #1e6b3a; border-color: #c6e6cf; }
.setu-pill.orange  { background: #fff1e0; color: #9a5a00; border-color: #f4d6a8; }
.setu-pill.red     { background: #fde7e7; color: #a4262c; border-color: #f5b5b5; }
.setu-pill.violet  { background: #ede7f6; color: #5e35b1; border-color: #d1c4e9; }
.setu-pill.gray    { background: #eef0f3; color: #4a5560; border-color: #d6dae0; }
.setu-pill.navy    { background: #e3eaf3; color: #1f3a5f; border-color: #c5d2e3; }

/* Welcome / landing card \u2014 used on the Review tab when no run is
   selected. */
.setu-welcome {
    border: 1px solid #d6dae0;
    border-radius: 10px;
    padding: 1.25rem 1.5rem;
    background: #ffffff;
    margin-bottom: 1rem;
}
.setu-welcome h3 { margin-top: 0; }
.setu-checklist { list-style: none; padding-left: 0; margin: 0.5rem 0; }
.setu-checklist li { padding: 4px 0; }
.setu-checklist .ok   { color: #1e6b3a; font-weight: 600; }
.setu-checklist .miss { color: #a4262c; font-weight: 600; }

/* Subtle separator between major sections within a tab. */
.setu-section-rule {
    border: 0;
    border-top: 1px solid #e3e6eb;
    margin: 1rem 0;
}

/* Card title — 15px, 700 bold (Foundation 2.2). */
.setu-card-title {
    font-size: 0.9375rem;
    font-weight: 700;
    color: var(--setu-text-primary);
}

/* Single quiet card shadow — never heavier or colored (Foundation 2.3).
   Radius/padding/shadow drawn from the canonical token layer. */
.setu-card {
    background: var(--setu-surface);
    border: 1px solid var(--setu-border);
    border-radius: var(--setu-card-radius);
    padding: var(--setu-card-padding);
    box-shadow: var(--setu-card-shadow);
    margin-bottom: var(--setu-spacing-card-gap);
}

/* Foundation component pills (semantic fill forms). */
.setu-token-pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.6875rem;   /* 11px micro/badge */
    font-weight: 600;
    line-height: 1.5;
    margin-right: 4px;
    border: 1px solid transparent;
}
/* 3.5 Coming-with-[Module] placeholder badge — neutral tinted pill. */
.setu-token-pill.placeholder {
    background: var(--setu-neutral-bg);
    color: var(--setu-neutral);
    border-color: var(--setu-border);
}
/* 3.1 Confidence badge: rule-match = solid (no %); AI = tinted amber
   (always carries a %). The two forms must differ in fill AND content. */
.setu-token-pill.rule   { background: var(--setu-rule);   color: #FFFFFF; }
.setu-token-pill.ai     { background: var(--setu-ai);     color: var(--setu-ai-text); }
/* Informational-accent tint (chip / banner that is NOT a warning). */
.setu-token-pill.accent { background: #EAF0FB;            color: var(--setu-accent); border-color: #D4E1F8; }
/* Danger-coral (escalated / danger text) */
.setu-token-pill.danger { background: #FDEBEA;            color: var(--setu-danger); border-color: #F6C9C5; }

/* 3.4 Disabled-with-inline-reason — danger-coral micro label, never in a
   tooltip, never truncated. */
.setu-inline-reason {
    display: block;
    font-size: 0.6875rem;   /* micro / label size */
    color: var(--setu-danger);
    margin-top: 4px;
    line-height: 1.4;
}

/* 3.7 Live connection status chip — three states. Reuses the semantic
   color roles directly (NOT the confidence badge): Connected = solid
   resolved-green; Needs re-auth = tinted ai-amber; Expired = tinted
   escalated-coral. The text label is always present (never color alone),
   and it's always paired with a masked reference + a Replace action. */
.setu-live-chip {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.6875rem;   /* 11px micro/badge */
    font-weight: 600;
    line-height: 1.5;
    border: 1px solid transparent;
}
.setu-live-chip.connected    { background: var(--setu-rule); color: #FFFFFF; }
.setu-live-chip.needs_reauth { background: var(--setu-ai);   color: var(--setu-ai-text); }
.setu-live-chip.expired      { background: #FDEBEA;          color: var(--setu-danger); border-color: #F6C9C5; }

/* Informational banner — accent tint for informational (not warning) notices,
   e.g. the "affected-user count" on the Permission Management screen. */
.setu-info-banner {
    background: #EAF0FB;
    border: 1px solid #D4E1F8;
    border-radius: 10px;
    padding: 10px 14px;
    color: var(--setu-text-primary);
    font-size: 0.8125rem;   /* 13px body */
    margin: 8px 0;
}

/* Warning-amber banner — for high-impact / wide-blast-radius notices that
   require an explicit confirmation before committing (C1 rule edits).
   Reuses the AI-suggested amber role as the caution hue (the Foundation
   has no separate "warning" token). Light-tint background, matching the
   .setu-info-banner pattern. */
.setu-warning-banner {
    background: #FDF4E3;          /* light tint of the amber role */
    border: 1px solid #F0D9AE;    /* amber border tint */
    border-radius: 10px;
    padding: 10px 14px;
    color: var(--setu-ai-text);   /* amber on-tint text */
    font-size: 0.8125rem;         /* 13px body */
    margin: 8px 0;
}

/* Module 2 — TDS six-step chain stepper (2B). The ONE bespoke pattern this
   build order introduces beyond the Foundation doc, deliberately minimal:
   it reuses the existing semantic color tokens (resolved-green / escalated-
   coral / AI-suggested-amber) rather than inventing new ones, and every
   stage carries a text label alongside its color (never color alone). */
.setu-stepper {
    display: flex;
    align-items: flex-start;
    gap: 0;
    margin: 10px 0 4px 0;
    flex-wrap: wrap;
}
.setu-stepper-stage {
    flex: 1 1 0;
    min-width: 110px;
    text-align: center;
    position: relative;
    padding: 0 4px;
}
.setu-stepper-dot {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    margin: 0 auto 6px auto;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    color: #FFFFFF;
}
.setu-stepper-dot.complete   { background: var(--setu-rule); }
.setu-stepper-dot.gap        { background: var(--setu-danger); }
.setu-stepper-dot.timing_lag { background: var(--setu-ai); color: var(--setu-ai-text); }
.setu-stepper-label {
    font-size: 12px;
    color: var(--setu-text-primary);
    font-weight: 700;
}
.setu-stepper-state {
    font-size: 11px;
    color: var(--setu-text-secondary);
}
.setu-stepper-connector {
    flex: 0 0 18px;
    height: 2px;
    background: var(--setu-border);
    margin-top: 10px;
}

/* Reconcile guided flow — the persistent stage rail.

   The rail is a row of stage buttons. It is deliberately visually quiet when
   a stage is not the active one: the active stage is the only accent-filled
   button, every other reachable stage is a plain secondary button, and an
   unreachable (not-yet-earned) stage is neutral/disabled with its inline
   reason shown as text beside the rail — never a tooltip. Streamlit stamps
   the container's `key` onto the DOM as `st-key-<key>`, which is what these
   selectors hang off. */
.st-key-rc_rail [data-testid="stHorizontalBlock"] {
    gap: 0.35rem;
    align-items: stretch;
}
.st-key-rc_rail [data-testid="stBaseButton-secondary"] button,
.st-key-rc_rail [data-testid="stBaseButton-primary"] button {
    white-space: nowrap;
    font-size: 0.8125rem;
    font-weight: 700;
    padding: 6px 8px;
    min-height: 38px;
}
.st-key-rc_rail [data-testid="stBaseButton-secondary"] button {
    background: var(--setu-surface);
    border: 1px solid var(--setu-border);
    color: var(--setu-text-secondary);
}
/* A stage the user has already passed: reachable, but not the current one. */
.st-key-rc_rail [data-testid="stBaseButton-secondary"] button:hover {
    border-color: var(--setu-accent);
    color: var(--setu-accent);
}
/* A stage that hasn't been earned yet — visibly unavailable, and the caption
   under the rail always says why. */
.st-key-rc_rail [data-testid="stBaseButton-secondary"] button:disabled,
.st-key-rc_rail button:disabled {
    background: var(--setu-neutral-bg) !important;
    color: var(--setu-text-muted) !important;
    border-color: var(--setu-border) !important;
}
</style>
"""


def inject_css() -> None:
    """Inject the app's CSS once per page load.

    The token layer is emitted by ``design_tokens.tokens_css()`` (the single
    source of truth) and prepended to the base stylesheet; the font family is
    substituted into the template before both are written to the page.
    """
    css = _CSS.replace("__FONT__", FONT_FAMILY)
    st.markdown(tokens_css() + css, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Status pill renderer
# ---------------------------------------------------------------------------

_PILL_COLOR: dict[str, str] = {
    "Matched": "green",
    "Amount Difference": "orange",
    "Not in Books": "red",
    "Not in Portal": "red",
    "High": "green",
    "Medium": "orange",
    "Low": "red",
    "Reviewed": "green",
    "Unreviewed": "gray",
    "Stale": "violet",
}


def render_status_pill(label: str, *, color: str | None = None) -> None:
    """Render a small rounded status pill as inline HTML.

    ``color`` defaults to the canonical mapping in ``_PILL_COLOR``; pass
    an explicit value to override (e.g. for ad-hoc labels).
    """
    css_class = color or _PILL_COLOR.get(label, "gray")
    st.markdown(
        f'<span class="setu-pill {css_class}">{label}</span>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Phase 2 UI Foundation component render helpers
# ---------------------------------------------------------------------------
# These are thin, token-driven wrappers so every module renders the locked
# components identically (see Setu_Phase2_UI_Foundation_Build_Prompt.docx
# section 3), rather than re-deriving markup per screen.


def render_confidence_badge(source: str, *, pct: int | None = None, label: str | None = None) -> None:
    """3.1 — Confidence badge: rule-match vs AI-suggested.

    ``source`` is ``"rule"`` (solid resolved pill, label only, no %) or
    ``"ai"`` (tinted amber pill, always carries a percentage). The two
    forms must differ in BOTH fill (solid vs tinted) and content (no % vs
    %), per the Foundation spec.
    """
    if source == "rule":
        css = "rule"
        text = label or "Rule match"
    else:
        css = "ai"
        if label is not None:
            text = label
        elif pct is not None:
            text = f"AI \u2014 {pct}%"
        else:
            text = "AI"
    st.markdown(f'<span class="setu-token-pill {css}">{text}</span>', unsafe_allow_html=True)


def render_placeholder_badge(module: str) -> None:
    """3.5 — "Coming with [Module X]" placeholder badge.

    Renders in the exact location the real feature will eventually occupy.
    """
    render_placeholder_pill(f"Coming with {module}")


def render_placeholder_pill(text: str) -> None:
    """Reuses the 3.5 placeholder badge's exact visual shape (greyed,
    neutral-tinted pill) but with caller-supplied label text — for
    placeholders whose reason isn't "coming with a future module" (e.g.
    C4's Backup/DR section, gated on a hosting decision rather than a
    module ship date). Per the C4 build prompt: same visual treatment,
    deliberately different label pattern.
    """
    st.markdown(
        f'<span class="setu-token-pill placeholder">{text}</span>',
        unsafe_allow_html=True,
    )


def render_inline_reason(text: str) -> None:
    """3.4 — danger-coral micro-label for a blocked action's reason.

    Always-visible text (never a tooltip, never truncated)."""
    st.markdown(
        f'<span class="setu-inline-reason">{text}</span>',
        unsafe_allow_html=True,
    )


def render_info_banner(text: str) -> None:
    """Informational-accent banner — for non-warning, non-error notices
    (e.g. the "affected-user count" that accompanies a role-wide change)."""
    st.markdown(
        f'<div class="setu-info-banner">{text}</div>',
        unsafe_allow_html=True,
    )


def render_warning_banner(text: str) -> None:
    """Warning-amber banner — for high-impact / wide-blast-radius notices
    that require an explicit confirmation before committing. Not an error
    (danger) banner; uses the amber caution role."""
    st.markdown(
        f'<div class="setu-warning-banner">{text}</div>',
        unsafe_allow_html=True,
    )


def render_accent_pill(text: str) -> None:
    """Informational-accent tinted pill — for small non-warning badges such
    as "Overridden for N clients". Reuses the existing .setu-token-pill.accent
    (accent tint) shape; no new component."""
    st.markdown(
        f'<span class="setu-token-pill accent">{text}</span>',
        unsafe_allow_html=True,
    )


# 3.7 live connection status states → CSS class + display label.
_LIVE_STATUS = {
    "connected": ("connected", "Connected"),
    "expired": ("expired", "Expired"),
    "needs_reauth": ("needs_reauth", "Needs re-auth"),
}


def render_live_status_chip(status: str, *, label: str | None = None) -> None:
    """3.7 — Live connection status chip.

    ``status`` is one of ``connected`` (solid resolved pill),
    ``needs_reauth`` (tinted ai-amber pill) or ``expired`` (tinted coral
    pill). Per Foundation 3.7 this is always paired with a masked
    reference and a "Replace" action by the calling screen, never a
    "Reveal". ``label`` overrides the default display text.
    """
    css_class, default_label = _LIVE_STATUS.get(status, ("", "Unknown"))
    text = label if label is not None else default_label
    st.markdown(
        f'<span class="setu-live-chip {css_class}">{text}</span>',
        unsafe_allow_html=True,
    )


def render_activity_panel(
    rows: list[dict[str, str]], *, empty_message: str = "No activity recorded yet.",
) -> None:
    """3.8 — Recent activity panel.

    Card token wrapper; each row shows the event/type in body text, a
    secondary detail in the secondary-text token, and a right-aligned
    timestamp at the micro/meta size. Hairline dividers between rows,
    newest first (callers are responsible for the ordering), no icons
    required. ``rows`` is a list of dicts with keys ``title`` (body text,
    e.g. the event type or "who did what"), ``detail`` (secondary text)
    and ``timestamp`` (micro/meta text).

    This is the single shared renderer for the panel first stubbed
    locally in F1 (Recent Security Events) and C3-ext (Recent fallbacks),
    and now permanently homed by C4's Security Overview screen — the
    panel's shape doesn't change when its data source changes underneath
    it, per the Foundation spec.
    """
    if not rows:
        st.info(empty_message)
        return
    st.markdown('<div class="setu-card" style="padding:12px 18px;">', unsafe_allow_html=True)
    for r in rows:
        st.markdown(
            f'<div style="border-bottom:1px solid var(--setu-border);padding:8px 2px;">'
            f'<span style="color:var(--setu-text-primary);font-size:13px;">{r.get("title", "")}</span> '
            f'<span style="color:var(--setu-text-secondary);font-size:12px;">{r.get("detail", "")}</span>'
            f'<span style="float:right;color:var(--setu-text-secondary);font-size:11px;">'
            f'{r.get("timestamp", "")}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)


_FRESHNESS_LABELS = {
    "seed": "Seed data \u2014 unverified",
    "reviewed": None,  # filled with "Reviewed [date]" by the caller
    "stale": None,     # filled with "Stale \u2014 not reviewed in N months"
}


def render_freshness_chip(status: str, *, label: str | None = None) -> None:
    """C1 data-freshness status chip (Regulatory Rules Table).

    Reuses the existing semantic color-token pills directly (NOT the Live
    connection status chip 3.7, which is scoped to credential/portal
    connectivity — per the Foundation doc, this is a data-freshness signal,
    not connectivity): ``seed`` -> tinted ai-amber, ``reviewed`` -> solid
    resolved-green, ``stale`` -> tinted danger-coral. ``label`` overrides
    the display text.
    """
    _mapped = {"seed": "ai", "reviewed": "rule", "stale": "danger"}
    css_class = _mapped.get(status, "placeholder")
    text = label if label is not None else _FRESHNESS_LABELS.get(status, "Unknown")
    st.markdown(
        f'<span class="setu-token-pill {css_class}">{text}</span>',
        unsafe_allow_html=True,
    )


# Module 2 — TDS six-step chain stepper (2B). The one bespoke pattern this
# build order introduces beyond the Foundation doc. Reuses existing semantic
# color tokens only; every stage pairs its color with a state label so the
# distinction holds for colorblind users (never color alone).
_STEPPER_STATE_LABELS = {
    "complete": "Complete",
    "gap": "Gap",
    "timing_lag": "Timing lag",
}
_STEPPER_STATE_GLYPH = {"complete": "\u2713", "gap": "\u2717", "timing_lag": "~"}


def render_tds_chain_stepper(stages: list[dict[str, str]]) -> None:
    """Render the TDS six-step chain as a horizontal stepper.

    ``stages`` is a list of dicts with keys ``label`` (stage name) and
    ``state`` (``complete`` | ``gap`` | ``timing_lag``), in chain order:
    applicability -> rate -> deduction -> deposit -> return -> 26AS
    reflection. Each stage shows a colored dot PLUS a text state label —
    never color alone. ``detail`` (optional) is rendered beneath the
    stepper as always-visible text, never a tooltip.
    """
    if not stages:
        st.info("No TDS chain trace recorded for this exception.")
        return

    parts: list[str] = ['<div class="setu-stepper">']
    for i, stage in enumerate(stages):
        state = stage.get("state", "gap")
        glyph = _STEPPER_STATE_GLYPH.get(state, "?")
        state_label = _STEPPER_STATE_LABELS.get(state, state)
        parts.append(
            '<div class="setu-stepper-stage">'
            f'<div class="setu-stepper-dot {state}">{glyph}</div>'
            f'<div class="setu-stepper-label">{stage.get("label", "")}</div>'
            f'<div class="setu-stepper-state">{state_label}</div>'
            '</div>'
        )
        if i < len(stages) - 1:
            parts.append('<div class="setu-stepper-connector"></div>')
    parts.append('</div>')
    st.markdown("".join(parts), unsafe_allow_html=True)

    # Always-visible detail text (never a tooltip), one line per stage.
    for stage in stages:
        detail = stage.get("detail")
        if detail:
            st.caption(f"{stage.get('label', '')}: {detail}")


# ---------------------------------------------------------------------------
# F4 — reusable "View History" trigger + per-record History panel
# ---------------------------------------------------------------------------
# The ONE new affordance F4 introduces beyond the Foundation doc: a small
# "View History" trigger placed inline next to any editable field or record
# header. The panel it opens reuses the Recent activity panel (3.8) shape —
# a per-record purpose rather than a system-wide feed. There is deliberately
# NO bespoke history screen built per module.

_HISTORY_PANEL_CSS = """
<style>
.setu-history-trigger button {
    background: transparent !important;
    border: none !important;
    color: var(--setu-text-secondary) !important;
    font-size: 0.6875rem !important;   /* micro size */
    padding: 0 2px !important;
    min-height: 0 !important;
    box-shadow: none !important;
    justify-content: flex-start !important;
}
.setu-history-trigger button:hover { color: var(--setu-accent) !important; }
.setu-history-panel { padding: 4px 2px; }
</style>
"""


def render_view_history(
    record_type: str,
    record_id: object,
    *,
    current_user: dict | None = None,
    client_id: int | None = None,
    label: str = "View History",
    key: str | None = None,
) -> None:
    """F4 \u2014 the single reusable "View History" link/icon + History panel.

    Place one call inline next to any editable field or record header across
    every module that retrofits into F4. Opening it renders a lightweight
    per-record History panel (card token, hairline dividers, newest first),
    each entry in plain language: "Changed from [old] to [new], by [name],
    on [date], because [reason]." — no reason clause where none was given.

    The panel is READ-ONLY and never prompts for anything. End-Client never
    sees the affordance at all — fully absent, not disabled-with-reason,
    consistent with how F1 treats role-gated UI elsewhere.
    """
    if current_user is not None and current_user.get("role_name") == "End-Client":
        return

    # Local import keeps theme.py presentation-only (no top-level service dep).
    from src.f4 import service as f4

    rows = f4.history_for_record(record_type, record_id, client_id=client_id)
    st.markdown(_HISTORY_PANEL_CSS, unsafe_allow_html=True)

    with st.popover(f"\U0001F553 {label}{f' ({len(rows)})' if rows else ''}", use_container_width=False):
        st.markdown('<div class="setu-history-panel">', unsafe_allow_html=True)
        _render_history_entries(rows)
        st.markdown('</div>', unsafe_allow_html=True)


def _render_history_entries(rows: list[dict]) -> None:
    """Render edit-history entries as the 3.8 activity-panel shape: title in
    body text, the "by / on / because" clause in secondary text, right-aligned
    micro timestamp, hairline dividers, newest first."""
    if not rows:
        st.info("No edit history recorded yet.")
        return
    st.markdown('<div class="setu-card" style="padding:12px 18px;">', unsafe_allow_html=True)
    for r in rows:
        title = f"Changed from {r.get('old_value') or '(none)'} to {r.get('new_value') or '(none)'}"
        clause = f"by {r.get('changed_by') or '?'}"
        if r.get("reason"):
            clause += f", because {r['reason']}"
        st.markdown(
            f'<div style="border-bottom:1px solid var(--setu-border);padding:8px 2px;">'
            f'<span style="color:var(--setu-text-primary);font-size:13px;">{title}</span> '
            f'<span style="color:var(--setu-text-secondary);font-size:12px;">{clause}</span>'
            f'<span style="float:right;color:var(--setu-text-secondary);font-size:11px;">'
            f'{r.get("created_at", "")}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)
