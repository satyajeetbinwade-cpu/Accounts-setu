"""Reconcile — the guided five-stage flow (the app's default landing screen).

This module is an ORCHESTRATION / STATE layer only. Every piece of business
logic it needs already exists and is reused as-is:

  * normalization      -> src.ingestion_ai.normalize_source_file()
  * the match run      -> src.runner.execute_run()
  * the report export  -> src.export.export_run()
  * exception roll-up  -> src.module2.service / src.queries

Nothing about matching, ingestion or export is reimplemented here. Where the
existing Run tab already has a reusable helper (client-folder -> client_id
resolution, F5 sync-health recording, Module 2 exception materialization)
this module imports it rather than copying it.

The five stages:

  1. Context    — Client, Period, Recon type (auto-advances once all set)
  2. Upload     — every required slot on ONE screen, normalized inline
  3. Reconcile  — the match run (auto-runs when Stage 2 is clean)
  4. Review     — summary + exceptions inline
  5. Export     — one button, self-explanatory

State lives in st.session_state keyed by client+period+recon type, so going
back a stage to fix one upload never loses the rest of the work.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import streamlit as st

from src import queries
from src.auth import service as auth
from src.config_loader import load_config
from src.data_paths import source_data_path
from src.export import export_run
from src.ingestion_ai import service as ingestion_ai
from src.runner import RunExecutionError, execute_run
from src.ui import discovery, state
from src.ui.theme import (
    render_confidence_badge,
    render_inline_reason,
)

# Reuse the Run tab's existing orchestration helpers instead of duplicating
# them: the client-name -> F2 client_id resolution, the F5 sync-health
# recorder and Module 2's exception materialization are the SAME calls the
# Run tab makes after a successful run.
from src.ui.run_tab import (
    _client_id_for_folder,
    _generate_module2_exceptions,
    _record_sync_health,
)

# The five stages, in order. Rendered as a persistent, always-visible rail.
STAGES: list[tuple[int, str]] = [
    (1, "Context"),
    (2, "Upload"),
    (3, "Reconcile"),
    (4, "Review"),
    (5, "Export"),
]

SK_FLOW = "_reconcile_flow"

_RAIL_KEY = "rc_rail"
_EMPTY = "(none)"


# ---------------------------------------------------------------------------
# Flow state — persisted per client + period + recon type
# ---------------------------------------------------------------------------


def _flows() -> dict[str, dict[str, Any]]:
    return st.session_state.setdefault(SK_FLOW, {})


def _flow_key(client: Optional[str], period: Optional[str], recon_type: Optional[str]) -> str:
    return f"{client or '-'}|{period or '-'}|{recon_type or '-'}"


def _context() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """The flow's own context values, falling back to whatever the sidebar
    (or an earlier screen) already selected, so nothing is re-asked."""
    client = st.session_state.get("rc_client") or st.session_state.get("ctx_client")
    period = st.session_state.get("rc_period") or st.session_state.get("ctx_period")
    recon_type = st.session_state.get("rc_recon_type") or st.session_state.get("ctx_recon_type")
    return client, period, recon_type


def init_reconcile_flow(current_user: Optional[dict[str, Any]] = None) -> None:
    """Seed a default context so the advanced screens have something to work
    with if a user goes straight to them without passing through Stage 1.
    The guided flow itself always re-confirms this on Stage 1."""
    if st.session_state.get("ctx_client"):
        return
    try:
        clients_list = discovery.list_clients()
        if current_user:
            clients_list = auth.visible_clients(current_user, clients_list)
    except Exception:  # noqa: BLE001
        return
    if not clients_list:
        return
    client = clients_list[0]
    periods = discovery.list_periods(client)
    period = periods[0] if periods else None
    recon_types = discovery.list_recon_types(client, period) if period else []
    st.session_state.setdefault("ctx_client", client)
    st.session_state.setdefault("ctx_period", period)
    st.session_state.setdefault("ctx_recon_type", recon_types[0] if recon_types else None)


def _flow() -> dict[str, Any]:
    """The state record for the active context — created on first use so
    'navigate back and fix one thing' keeps everything else intact."""
    client, period, recon_type = _context()
    key = _flow_key(client, period, recon_type)
    flows = _flows()
    if key not in flows:
        flows[key] = {
            "stage": 1,
            "selections": {},        # {source_type: filename} — same shape the Run tab uses
            "run_id": None,
            "auto_ran": False,
            "pause_before_run": False,
            "export_path": None,
            "started_fresh": False,
            "rail_return": False,    # True after a deliberate backward rail click
        }
        st.session_state[SK_FLOW] = flows
    return flows[key]


def _current_flow() -> dict[str, Any]:
    """Same as _flow() but prefers an already-created record — used inside
    widget callbacks, which run before the script body of the next rerun."""
    client, period, recon_type = _context()
    key = _flow_key(client, period, recon_type)
    flows = _flows()
    if key in flows:
        return flows[key]
    return _flow()


def _goto_stage(stage: int) -> None:
    """on_click callback for the rail. Callbacks run at the START of the next
    rerun (before any widget in this run is created), which is why the rail
    can safely write the flow's stage here.

    `rail_return` marks a DELIBERATE backward navigation. Stages 1 and 2
    auto-advance the moment their conditions are met, so without this flag
    clicking back to fix one upload would bounce straight forward again.
    """
    flow = _current_flow()
    flow["rail_return"] = True
    flow["stage"] = int(stage)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _actor(current_user: Optional[dict[str, Any]]) -> str:
    return (current_user or {}).get("username") or "system"


def _chip(kind: str, text: str) -> None:
    """Compact status chip for an upload slot. Reuses the Foundation token
    pills — never a new chip shape. `kind` is one of ok/warn/bad/empty."""
    css = {"ok": "rule", "warn": "ai", "bad": "danger", "empty": "placeholder"}[kind]
    st.markdown(
        f'<span class="setu-token-pill {css}">{text}</span>', unsafe_allow_html=True
    )


def _info_banner(text: str) -> None:
    """Informational banner that still renders inline markdown (the theme's
    banner helpers pass their text through as raw HTML, so `**bold**` would
    otherwise show up literally)."""
    st.markdown(f'<div class="setu-info-banner">{text}</div>', unsafe_allow_html=True)


def _warning_banner(text: str) -> None:
    """Warning-amber banner with inline markdown support — same reason as
    _info_banner()."""
    st.markdown(f'<div class="setu-warning-banner">{text}</div>', unsafe_allow_html=True)


def _list_files(client: str, period: str, source_type: str) -> list[str]:
    directory = source_data_path(client, period, source_type)
    if not directory.exists():
        return []
    found: list[str] = []
    for pattern in ("*.csv", "*.xlsx", "*.xls"):
        found.extend(sorted(p.name for p in directory.glob(pattern)))
    return sorted(set(found))


def _file_exists(client: str, period: str, source_type: str, filename: str) -> bool:
    if not filename:
        return False
    return (source_data_path(client, period, source_type) / filename).exists()


def _display_dt(value: Any) -> str:
    text = str(value or "").replace("T", " ")
    return text.split(".")[0] if text else "—"


def _has_value(value: Any) -> bool:
    """True when a value is genuinely present. pandas turns missing values
    into NaN, which is truthy — so a plain `if value` check would render
    'nan' into user-facing labels."""
    if value is None:
        return False
    try:
        import pandas as pd

        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip() not in ("", "nan", "None", "NaT")


# ---------------------------------------------------------------------------
# Slot inspection — reads the ingestion layer's own state, never re-derives it
# ---------------------------------------------------------------------------


def _slot_info(client: str, period: str, recon_type: str, source_type: str, filename: Optional[str]) -> dict[str, Any]:
    """Summarize one upload slot from the persisted IngestionResult.

    `code` is one of: empty / not_ingested / ok / review / blocked / wrong.
    'review' still counts as resolved — those are fields the AI mapped that
    a human may want to glance at; 'blocked' is the only mapping state that
    genuinely stops the run.
    """
    if not filename or not _file_exists(client, period, source_type, filename):
        return {"code": "empty", "filename": filename, "stored": None, "rows": 0, "needs_review": 0}

    stored = ingestion_ai.get_stored_result(client, period, source_type, filename)
    if stored is None:
        return {"code": "not_ingested", "filename": filename, "stored": None, "rows": 0, "needs_review": 0}

    status = stored.get("status")
    rows = int(stored.get("row_count_out") or 0)
    mapping = stored.get("mapping") or []
    unmapped = [m for m in mapping if not m.get("source_column")]
    try:
        threshold = ingestion_ai.get_preselect_threshold()
    except Exception:  # noqa: BLE001
        threshold = 0.75
    low_conf = [
        m for m in mapping
        if m.get("source_column") and (m.get("confidence") is None or m["confidence"] < threshold)
    ]
    needs_review = len(unmapped) + len(low_conf)
    required_missing = len(stored.get("unmapped_required") or [])
    # Caveats describe what the report will NOT be able to check. Older stored
    # results predate them, so fall back to an empty list rather than guessing.
    caveats = stored.get("caveats") or []

    if status in ("wrong_slot", "unrecognized"):
        # The only genuinely fatal state: the wrong file, or not financial data.
        code = "wrong"
    elif required_missing:
        # Proceeds anyway, with a partial report. No longer a dead end.
        code = "partial"
    elif status == "partial" or needs_review:
        code = "review"
    elif status == "ok":
        code = "ok"
    else:
        code = "review"

    return {
        "code": code,
        "filename": filename,
        "stored": stored,
        "rows": rows,
        "needs_review": needs_review,
        "required_missing": required_missing,
        "caveats": caveats,
        "message": stored.get("message"),
    }


def _flash(kind: str, text: str) -> None:
    """Queue a message to render AFTER the next rerun.

    Actions like "Confirm mapping" end in st.rerun() so the slot's chip and
    readiness are recomputed — but a rerun discards anything drawn before it,
    which is why the old success message was never seen and the button looked
    like it did nothing.
    """
    st.session_state.setdefault("_rc_flash", []).append((kind, text))


def _render_flashes() -> bool:
    """Show queued messages. Returns True when anything was shown.

    The return value lets callers suppress auto-advance for one pass so the
    user can actually read the outcome of what they just did.
    """
    messages = st.session_state.pop("_rc_flash", [])
    for kind, text in messages:
        if kind == "success":
            st.success(text)
        elif kind == "warning":
            st.warning(text)
        elif kind == "error":
            st.error(text)
        else:
            st.info(text)
    return bool(messages)


def _set_slot_result(
    client: str, period: str, recon_type: str, source_type: str,
    *, kind: str, text: str,
) -> None:
    """Record the outcome of an action taken on a slot.

    Stored against the slot and rendered inside that slot's own card, so it
    survives every subsequent rerun until the next action replaces it. A
    message that vanishes because a rerun happened to follow it is
    indistinguishable, to the user, from the button doing nothing at all.
    """
    st.session_state[f"rc_result_{_slot_key(client, period, recon_type, source_type)}"] = (kind, text)


def _render_slot_result(client: str, period: str, recon_type: str, source_type: str) -> None:
    result = st.session_state.get(f"rc_result_{_slot_key(client, period, recon_type, source_type)}")
    if not result:
        return
    kind, text = result
    if kind == "success":
        st.success(text)
    elif kind == "warning":
        st.warning(text)
    else:
        st.error(text)


def _render_slot_chip(info: dict[str, Any]) -> None:
    code = info["code"]
    if code == "empty":
        _chip("empty", "— no file yet")
    elif code == "not_ingested":
        _chip("warn", "⚠ not ingested yet")
    elif code == "ok":
        _chip("ok", f"✓ {info['rows']} rows, fully mapped")
    elif code == "review":
        _chip("warn", f"⚠ {info['needs_review']} field(s) need review")
    elif code == "partial":
        # Not a failure: the report is produced, with stated gaps.
        n = info.get("required_missing") or 0
        _chip("warn", f"⚠ {n} required field(s) unmapped — partial report")
    else:
        _chip("bad", "✕ looks like the wrong file type")


def _resolved(info: dict[str, Any]) -> bool:
    """A slot is resolved when the run can proceed from it.

    `partial` (missing required columns) counts as resolved: the run now goes
    ahead and produces a partial report with the gaps stated as caveats, so it
    must not hold the flow back. Only `wrong` / `not_ingested` / `empty` stop it.
    """
    return info["code"] in ("ok", "review", "partial")


def _seed_selections_from_run(
    flow: dict[str, Any], client: str, period: str, recon_type: str, run_id: int,
) -> None:
    """Restore the upload slots from the files a run actually consumed.

    Continuing an existing run must not silently lose which files it was
    built from — otherwise stepping back to Upload would look empty and the
    user would be forced to re-pick files they already chose.
    """
    if flow.get("selections"):
        return
    run = queries.get_run(int(run_id))
    if not run:
        return
    names = set(run.get("source_file_names") or [])
    if not names:
        return
    for source_type in discovery.RECON_SOURCE_TYPES[recon_type]:
        for filename in _list_files(client, period, source_type):
            if filename in names:
                flow["selections"][source_type] = filename
                break


# ---------------------------------------------------------------------------
# Stage 2 readiness — the ONLY gate between uploading and running
# ---------------------------------------------------------------------------


def _readiness(client: str, period: str, recon_type: str, flow: dict[str, Any]) -> dict[str, Any]:
    books = discovery.BOOKS_SOURCE_TYPES
    portals = discovery.portal_source_types(recon_type)
    tracked = books + portals

    infos = {
        src: _slot_info(client, period, recon_type, src, flow["selections"].get(src))
        for src in tracked
    }
    blockers = [
        (src, infos[src]) for src in tracked
        if infos[src]["code"] in ("wrong", "not_ingested")
    ]
    books_ok = all(_resolved(infos[src]) for src in books)
    portal_ok = any(_resolved(infos[src]) for src in portals)

    return {
        "infos": infos,
        "books_ok": books_ok,
        "portal_ok": portal_ok,
        "blockers": blockers,
        "ready": books_ok and portal_ok and not blockers,
        "portals": portals,
        "books": books,
    }


def _render_blockers(readiness: dict[str, Any]) -> None:
    """Actionable pause message. Every blocker names the slot and the fix —
    and the fix itself is available in that slot's card above."""
    for src, info in readiness["blockers"]:
        label = discovery.source_type_label(src)
        st.markdown(f"**{label}** — `{info['filename']}`")
        if info["code"] == "wrong":
            render_inline_reason(
                (info.get("message") or "This doesn't look like the right file type for this slot.")
                + f" Fix it in the {label} card above: pick the correct file, or re-upload the right one."
            )
        elif info["code"] == "blocked":
            render_inline_reason(
                f"Required field(s) couldn't be mapped. Fix it in the {label} card above — "
                "open its mapping detail and confirm the columns."
            )
        else:
            render_inline_reason(
                f"This file hasn't been through ingestion yet. Re-upload it in the {label} card above."
            )


# ---------------------------------------------------------------------------
# The rail
# ---------------------------------------------------------------------------


def _max_reachable(flow: dict[str, Any], client, period, recon_type) -> int:
    if not (client and period and recon_type):
        return 1
    if not flow.get("run_id"):
        # Stages 3+ only unlock once Stage 2 is genuinely clean.
        return 3 if _readiness(client, period, recon_type, flow)["ready"] else 2
    return 5


def _render_rail(flow: dict[str, Any], client, period, recon_type) -> None:
    """The persistent stepper. Always visible; clickable BACKWARD at any time
    (to review or fix an earlier stage), and forward only up to the furthest
    stage whose conditions are actually met."""
    reached = _max_reachable(flow, client, period, recon_type)
    current = int(flow.get("stage") or 1)

    with st.container(key=_RAIL_KEY):
        cols = st.columns(len(STAGES))
        for col, (num, name) in zip(cols, STAGES):
            reached_here = num <= reached
            if num < current:
                glyph = "✓"
            elif num == current:
                glyph = "●"
            else:
                glyph = str(num)
            with col:
                st.button(
                    f"{glyph} {num}. {name}",
                    key=f"rc_rail_{num}",
                    type="primary" if num == current else "secondary",
                    disabled=not reached_here,
                    use_container_width=True,
                    on_click=_goto_stage,
                    args=(num,),
                )
    st.caption(
        f"Step {current} of {len(STAGES)} · {dict(STAGES)[current]}. "
        + (
            "Every stage is open — click one to go back and change it."
            if reached >= len(STAGES)
            else "Earlier stages stay clickable; later ones open once their checks pass."
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_reconcile_flow(current_user: dict[str, Any]) -> None:
    st.subheader("Reconcile")
    st.caption(
        "One guided path from source files to a finished report. "
        "Everything else lives under **All tools** and **Setup**."
    )

    flow = _flow()
    client, period, recon_type = _context()

    _render_rail(flow, client, period, recon_type)
    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # Messages queued by an action whose rerun would otherwise discard them.
    # A message shown this pass suppresses auto-advance so it stays readable.
    flash_shown = _render_flashes()

    stage = int(flow.get("stage") or 1)

    if stage == 1:
        _render_context_stage(current_user, flow)
    elif stage == 2:
        _render_upload_stage(current_user, flow, client, period, recon_type)
    elif stage == 3:
        _render_reconcile_stage(current_user, flow, client, period, recon_type)
    elif stage == 4:
        _render_review_stage(current_user, flow, client, period, recon_type)
    else:
        _render_export_stage(current_user, flow, client, period, recon_type)


# ---------------------------------------------------------------------------
# STAGE 1 — Context
# ---------------------------------------------------------------------------


def _on_context_change() -> None:
    """Widget callbacks run before the script body of the next rerun, so this
    is the one safe place to keep the flow's context and the sidebar's
    selectors in agreement (and to drop dependent values that are no longer
    valid for the newly chosen client/period)."""
    client = st.session_state.get("rc_client")
    if not client:
        return

    periods = discovery.list_periods(client)
    period = st.session_state.get("rc_period")
    if period not in periods:
        period = periods[0] if periods else None
        st.session_state["rc_period"] = period

    recon_types = discovery.list_recon_types(client, period) if period else []
    recon = st.session_state.get("rc_recon_type")
    if recon not in recon_types:
        recon = recon_types[0] if recon_types else None
        st.session_state["rc_recon_type"] = recon

    # Keep the legacy ctx_* keys every other screen reads up to date. The
    # sidebar no longer carries its own selectors, so there is nothing else
    # to sync — the flow is the single place this context is chosen.
    st.session_state["ctx_client"] = client
    st.session_state["ctx_period"] = period
    st.session_state["ctx_recon_type"] = recon

    # The user just changed something, so auto-advance is welcome again.
    _current_flow()["rail_return"] = False


def _render_context_stage(current_user: dict[str, Any], flow: dict[str, Any]) -> None:
    st.markdown("#### 1. Context")
    st.caption("Client, period and recon type. Pick all three and this step completes itself.")

    clients_list = discovery.list_clients()
    if current_user:
        try:
            clients_list = auth.visible_clients(current_user, clients_list)
        except Exception:  # noqa: BLE001
            pass
    if not clients_list:
        st.warning(
            "No clients available to you yet. Ask an Admin to add you to a client's "
            "team (Admin → Client Teams), or create a client under the Clients tab."
        )
        return

    if st.session_state.get("rc_client") not in clients_list:
        st.session_state["rc_client"] = clients_list[0]

    c1, c2, c3 = st.columns(3)
    with c1:
        st.selectbox("Client", clients_list, key="rc_client", on_change=_on_context_change)
    client = st.session_state.get("rc_client")

    periods = discovery.list_periods(client) if client else []
    if not periods:
        st.warning(
            f"No periods found for **{client}**. Add a period folder under "
            f"`data/{client}/` — e.g. `data/{client}/2026-08/`."
        )
        return
    if st.session_state.get("rc_period") not in periods:
        st.session_state["rc_period"] = periods[0]

    with c2:
        st.selectbox("Period", periods, key="rc_period", on_change=_on_context_change)
    period = st.session_state.get("rc_period")

    recon_types = discovery.list_recon_types(client, period)
    if not recon_types:
        st.warning("No recon types are available for this client/period yet.")
        return
    if st.session_state.get("rc_recon_type") not in recon_types:
        st.session_state["rc_recon_type"] = recon_types[0]

    with c3:
        st.selectbox(
            "Recon type", recon_types, key="rc_recon_type", on_change=_on_context_change,
            format_func=lambda rt: {"GST": "GST", "TDS": "TDS", "OTHER": "Other"}.get(rt, rt),
        )
    recon_type = st.session_state.get("rc_recon_type")

    _info_banner(
        f"Context: <b>{client}</b> · <b>{period}</b> · <b>{recon_type}</b>. "
        "You won't be asked for this again during this run."
    )

    # --- Already have a run here? Offer continue vs start fresh ----------
    try:
        runs = queries.list_runs(client=client, period=period, recon_type=recon_type)
    except Exception:  # noqa: BLE001
        runs = None

    has_run = runs is not None and not runs.empty
    existing_run_id = flow.get("run_id")

    def _start_fresh() -> None:
        flow["started_fresh"] = True
        flow["run_id"] = None
        flow["auto_ran"] = False
        flow["export_path"] = None
        flow["selections"] = {}
        state.set_selected_run_id(None)

    # A completed run already sitting in this context: never reset it blindly.
    if has_run and existing_run_id is None and not flow.get("started_fresh"):
        latest = runs.iloc[0]
        run_id = int(latest["run_id"])
        st.markdown("#### There's already a completed run in this context")
        st.write(
            f"Run **{run_id}** finished on {_display_dt(latest.get('run_timestamp'))}. "
            "You can pick it up where it left off, or start a fresh reconciliation."
        )
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Continue this run", type="primary", key="rc_continue_run"):
                flow["run_id"] = run_id
                flow["stage"] = 4
                _seed_selections_from_run(flow, client, period, recon_type, run_id)
                state.set_selected_run_id(run_id)
                st.rerun()
        with b2:
            if st.button("Start fresh", key="rc_start_fresh"):
                _start_fresh()
                st.rerun()
        return

    # This flow already owns a run — the user came back to Context on purpose,
    # so don't bounce them forward; offer the two real choices instead.
    if existing_run_id is not None:
        st.info(
            f"This flow is working from run **{existing_run_id}** in this context."
        )
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Back to results", type="primary", key="rc_back_to_results"):
                flow["stage"] = 4
                st.rerun()
        with b2:
            if st.button("Start fresh", key="rc_start_fresh_reset"):
                _start_fresh()
                st.rerun()
        return

    # --- Auto-advance ---------------------------------------------------
    # No explicit "Next" click: once client, period and recon type are all
    # set, the flow moves to Stage 2 on its own. Unless the user walked back
    # here on purpose — then they get to look and a button to move on.
    if flow.pop("rail_return", False):
        st.write("")
        if st.button("Continue to upload →", type="primary", key="rc_context_continue"):
            flow["stage"] = 2
            st.rerun()
        return

    flow["stage"] = 2
    st.rerun()


# ---------------------------------------------------------------------------
# STAGE 2 — Upload (all required slots on ONE screen)
# ---------------------------------------------------------------------------


def _render_upload_stage(
    current_user: dict[str, Any], flow: dict[str, Any],
    client: str, period: str, recon_type: str,
) -> None:
    st.markdown("#### 2. Upload")
    st.caption(
        "Every file for this recon type, side by side. Each one is normalized as soon as "
        "you drop it — there's no separate ingestion screen to visit."
    )

    source_types = discovery.RECON_SOURCE_TYPES[recon_type]
    portals = set(discovery.portal_source_types(recon_type))

    # Three slots per row keeps every slot genuinely visible on one screen
    # without a wall of narrow columns.
    chunk = 3
    for start in range(0, len(source_types), chunk):
        row = source_types[start:start + chunk]
        cols = st.columns(len(row))
        for col, source_type in zip(cols, row):
            with col:
                _render_slot(current_user, flow, client, period, recon_type, source_type, required_hint=(source_type in portals))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    readiness = _readiness(client, period, recon_type, flow)

    if readiness["blockers"]:
        _warning_banner("Something here needs your attention before this can run.")
        _render_blockers(readiness)
        return

    missing: list[str] = []
    if not readiness["books_ok"]:
        missing.append(
            f"the books side ({discovery.source_type_label(discovery.BOOKS_SOURCE_TYPES[0])})"
        )
    if not readiness["portal_ok"]:
        labels = " or ".join(discovery.source_type_label(s) for s in readiness["portals"])
        missing.append(f"at least one portal file ({labels})")

    if missing:
        st.info("Still needed: " + ", and ".join(missing) + ".")
        return

    # Every required slot is resolved — advance with no extra click. Any
    # per-slot message stays on screen inside its card, so advancing does not
    # hide it (the slot result is rendered by the slot itself, not here).
    if flow.pop("rail_return", False):
        if st.button("Continue to reconcile →", type="primary", key="rc_upload_continue"):
            flow["stage"] = 3
            st.rerun()
        return

    flow["stage"] = 3
    st.rerun()


def _slot_key(client: str, period: str, recon_type: str, source_type: str) -> str:
    """Namespace for a slot's own session-state bookkeeping."""
    return f"{client}|{period}|{recon_type}|{source_type}"


def _processed_upload(client: str, period: str, recon_type: str, source_type: str) -> Optional[str]:
    """Signature of the upload already handled in this slot, if any."""
    return st.session_state.get(f"rc_done_{_slot_key(client, period, recon_type, source_type)}")


def _mark_upload_processed(
    client: str, period: str, recon_type: str, source_type: str, signature: str
) -> None:
    st.session_state[f"rc_done_{_slot_key(client, period, recon_type, source_type)}"] = signature


def _clear_upload_marker(
    client: str, period: str, recon_type: str, source_type: str
) -> None:
    """Forget which upload was processed, so the next one is handled afresh."""
    st.session_state.pop(f"rc_done_{_slot_key(client, period, recon_type, source_type)}", None)


def _upload_signature(uploaded: Any) -> str:
    """A stable identity for an uploaded file.

    Streamlit re-serves the same UploadedFile on every rerun for as long as
    it sits in the widget, and this version's UploadedFile exposes no stable
    file id — so re-processing must be prevented by comparing content.
    """
    data = uploaded.getvalue()
    return f"{uploaded.name}:{len(data)}:{hashlib.sha256(data).hexdigest()}"


def _picker_key(source_type: str, selection: Optional[str]) -> str:
    """Widget key for a slot's file picker, derived from the current choice.

    Streamlit resolves a widget's value from its session key and forbids
    writing that key once the widget has been created — so neither `index=`
    nor a post-hoc write can move an existing picker. Deriving the key from
    the selection means a change of selection produces a FRESH widget, which
    is then initialised from `index`. That is what lets an upload take over
    the picker without an illegal session-state write.
    """
    token = hashlib.sha1((selection or "-").encode("utf-8")).hexdigest()[:10]
    return f"rc_file_{source_type}_{token}"


def _render_slot(
    current_user: dict[str, Any], flow: dict[str, Any],
    client: str, period: str, recon_type: str, source_type: str, *, required_hint: bool,
) -> None:
    label = discovery.source_type_label(source_type)
    files = _list_files(client, period, source_type)
    current = flow["selections"].get(source_type)

    with st.container(border=True):
        st.markdown(f"**{label}**")
        st.caption("One of these is required" if required_hint else "Required — your internal books")
        hint = discovery.source_type_hint(source_type)
        if hint:
            st.caption(hint)

        # --- File picker -------------------------------------------------
        # The picker's widget value and `flow["selections"]` are kept in step
        # by deriving the widget's KEY from the selection (see
        # _picker_key): a change of selection yields a fresh widget seeded
        # from `index`, so the two can never diverge. Mirroring the value into
        # a fixed key instead produced a genuine infinite loop — the widget
        # kept returning a stale "(none)", the code read that as "the user
        # cleared it" and wiped the recorded file, and the uploader re-fired.
        pick_changed = False
        selection = flow["selections"].get(source_type)
        # A remembered file that's no longer on disk can't be used — forget it
        # rather than showing a selection that isn't there.
        if selection is not None and selection not in files:
            flow["selections"][source_type] = None
            selection = None

        options = [_EMPTY] + files
        chosen = st.selectbox(
            "File to use",
            options,
            index=options.index(selection) if selection in options else 0,
            key=_picker_key(source_type, selection),
            format_func=lambda f: "no file selected" if f == _EMPTY else f,
            label_visibility="collapsed",
        )
        # Normalize the sentinel BEFORE comparing — comparing "(none)" against
        # the stored None would report a change on every render and loop.
        new_value = None if chosen == _EMPTY else chosen
        if new_value != flow["selections"].get(source_type):
            flow["selections"][source_type] = new_value
            flow["rail_return"] = False
            pick_changed = True
            # A different file means any previously processed upload in this
            # slot is no longer the current one.
            _clear_upload_marker(client, period, recon_type, source_type)
            state.set_file_selection(client, period, recon_type, source_type, new_value)
            # A file that's already on disk but has never been through the
            # ingestion layer is normalized right here, on selection — the
            # user should never have to re-upload a file that's already in
            # the slot just to get it mapped.
            if new_value and ingestion_ai.get_stored_result(
                client, period, source_type, new_value
            ) is None:
                _normalize(
                    current_user, client, period, recon_type, source_type,
                    source_data_path(client, period, source_type) / new_value,
                )
            st.rerun()

        uploaded = st.file_uploader(
            "Drop a file",
            type=["csv", "xlsx", "xls"],
            key=f"rc_upload_{source_type}",
            label_visibility="collapsed",
        )
        # Only act on an upload when this pass didn't just handle a picker
        # change — deferring by one rerun rather than dropping the upload.
        if uploaded is not None and not pick_changed:
            _ingest_upload(
                current_user, flow, client, period, recon_type, source_type, uploaded
            )

        info = _slot_info(client, period, recon_type, source_type, flow["selections"].get(source_type))
        _render_slot_chip(info)
        # Outcome of the last action on THIS slot, kept until replaced.
        _render_slot_result(client, period, recon_type, source_type)

        filename = flow["selections"].get(source_type)
        if filename and info["stored"] is not None:
            with st.expander("Mapping detail", expanded=False):
                _render_slot_detail(
                    current_user, flow, client, period, recon_type, source_type, filename, info
                )


def _ingest_upload(
    current_user: dict[str, Any], flow: dict[str, Any],
    client: str, period: str, recon_type: str, source_type: str, uploaded: Any,
) -> None:
    """Persist the dropped file, then run it through the ONE ingestion path
    immediately — right here, inline.

    Idempotent per file: Streamlit re-serves the same uploaded file on every
    subsequent rerun for as long as it sits in the widget, so without this
    guard the upload would be re-normalized (a live model call) on every
    single interaction with the page — and because each of those reruns ends
    in another rerun, the page would never settle.
    """
    signature = _upload_signature(uploaded)
    if signature == _processed_upload(client, period, recon_type, source_type):
        return

    directory = source_data_path(client, period, source_type)
    dest = directory / uploaded.name
    replaced = dest.exists()
    try:
        dest.write_bytes(uploaded.getvalue())
    except Exception as exc:  # noqa: BLE001
        # Deliberately NOT marked as processed, so the user can retry the
        # same file after fixing the problem.
        st.error(f"Couldn't save `{uploaded.name}`.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    if replaced:
        st.caption(f"Replaced the existing `{uploaded.name}` in this slot.")

    result = _normalize(current_user, client, period, recon_type, source_type, dest)
    if result is not None:
        # Shown only on the pass that actually did the work, so it doesn't
        # stack up across reruns.
        st.caption(result.summary_line())

    flow["selections"][source_type] = dest.name
    flow["rail_return"] = False
    state.set_file_selection(client, period, recon_type, source_type, dest.name)
    # No widget key is written here on purpose — Streamlit forbids it once the
    # picker exists. That key is derived from the selection (_picker_key), so
    # updating the selection alone gives the picker a fresh widget next run.
    _mark_upload_processed(client, period, recon_type, source_type, signature)
    st.rerun()


def _normalize(
    current_user: dict[str, Any], client: str, period: str,
    recon_type: str, source_type: str, path: Path, *, use_cache: bool = True,
    quiet: bool = False,
):
    """Call the existing unified ingestion layer. Never raises — a failure is
    reported and the caller keeps the raw file state.

    `quiet` suppresses the inline success/error output, for callers that
    report the outcome themselves (and whose rerun would discard it anyway).
    """
    try:
        return ingestion_ai.normalize_source_file(
            path, source_type, client, period,
            client_id=_client_id_for_folder(client),
            recon_type=recon_type,
            actor=_actor(current_user),
            use_cache=use_cache,
        )
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            st.error("AI ingestion couldn't read that file. Check the file and try again.")
            with st.expander("Details"):
                st.code(str(exc))
        return None


def _render_slot_detail(
    current_user: dict[str, Any], flow: dict[str, Any], client: str, period: str,
    recon_type: str, source_type: str, filename: str, info: dict[str, Any],
) -> None:
    """Collapsed-by-default full mapping detail. The chip above gives the
    headline; this is where you go when it says something needs attention."""
    stored = info["stored"] or {}
    mapping = stored.get("mapping") or []
    headers = stored.get("headers") or []
    options = ["(leave unmapped)"] + list(headers)

    if info["code"] == "wrong":
        render_inline_reason(
            info.get("message") or "This file doesn't look like the right type for this slot."
        )
        st.caption("Pick or upload the correct file for this slot.")
        return

    # --- What this mapping cannot support -------------------------------
    # Stated up front, in plain language: a missing column matters because of
    # the check it makes impossible, not because of the column name.
    caveats = info.get("caveats") or []
    if caveats:
        st.markdown("**This file's gaps — and what they cost the report**")
        for caveat in caveats:
            st.markdown(f"- **{caveat.get('label', caveat.get('code'))}** — {caveat.get('detail', '')}")
        st.caption(
            "The reconciliation will still run and produce a report. These checks will "
            "be listed as not performed instead of being silently skipped."
        )
        st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    if not mapping:
        st.caption("No field mapping was recorded for this file.")
        return

    st.caption(
        f"{len(mapping)} canonical field(s). Dropdowns are pre-set to what the AI mapped — "
        "change only what's wrong."
    )

    overrides: dict[str, Any] = {}
    for m in mapping:
        field = m["canonical_field"]
        col = m.get("source_column")
        conf = m.get("confidence")
        left, right = st.columns([3, 2])
        with left:
            idx = options.index(col) if col in options else 0
            picked = st.selectbox(
                f"{field}{' *' if m.get('required') else ''}",
                options,
                index=idx,
                key=f"rc_map_{source_type}_{filename}_{field}",
            )
        with right:
            if m.get("from_trusted_profile"):
                render_confidence_badge("rule", label="Trusted profile")
            elif col and conf is not None:
                render_confidence_badge("ai", pct=int(round(conf * 100)))
            else:
                render_inline_reason("Unmapped — needs your input")
            if m.get("reason"):
                st.caption(m["reason"])
        overrides[field] = None if picked.startswith("(leave unmapped") else picked

    trust = st.checkbox(
        "Trust this mapping for future files of this shape",
        value=True,
        key=f"rc_trust_{source_type}_{filename}",
    )
    if st.button("Confirm mapping", key=f"rc_confirm_{source_type}_{filename}", type="primary"):
        try:
            ingestion_ai.confirm_shape_mapping(
                client, source_type, stored.get("header_signature") or "",
                field_overrides=overrides, trust_for_reuse=trust, actor=_actor(current_user),
            )
        except Exception as exc:  # noqa: BLE001
            _set_slot_result(
                client, period, recon_type, source_type,
                kind="error", text=f"Couldn't save the mapping: {exc}",
            )
            st.rerun()
            return

        # Re-run the same ingestion path so the persisted result reflects the
        # confirmed mapping (the shape cache now carries it).
        path = source_data_path(client, period, source_type) / filename
        refreshed = _normalize(
            current_user, client, period, recon_type, source_type, path, quiet=True
        )
        flow["rail_return"] = False

        remaining = len(getattr(refreshed, "unmapped_required_fields", []) or [])
        if remaining:
            _set_slot_result(
                client, period, recon_type, source_type, kind="warning",
                text=(
                    f"Mapping saved for {filename}. {remaining} required field(s) still "
                    "couldn't be mapped — the reconciliation will still run, and the "
                    "report will list the checks it had to skip."
                ),
            )
        else:
            _set_slot_result(
                client, period, recon_type, source_type, kind="success",
                text=f"Mapping saved for {filename}. No gaps remain.",
            )
        st.rerun()


# ---------------------------------------------------------------------------
# STAGE 3 — Reconcile
# ---------------------------------------------------------------------------


def _render_reconcile_stage(
    current_user: dict[str, Any], flow: dict[str, Any],
    client: str, period: str, recon_type: str,
) -> None:
    st.markdown("#### 3. Reconcile")
    readiness = _readiness(client, period, recon_type, flow)

    # The existing-run branch comes FIRST: a completed run is a legitimate
    # reason to be on this stage even if the user has since cleared a slot
    # (they may be about to re-run). Only a genuinely unfinished Stage 2 with
    # no run behind it is a dead end.
    existing_run = flow.get("run_id")
    if existing_run:
        run = queries.get_run(int(existing_run))
        if run is not None:
            st.success(f"Run **{existing_run}** is already complete for this context.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("See the results", type="primary", key="rc_go_review"):
                    flow["stage"] = 4
                    st.rerun()
            with c2:
                if st.button("Run it again", key="rc_run_again"):
                    flow["run_id"] = None
                    flow["auto_ran"] = False
                    flow["export_path"] = None
                    # Re-running is a deliberate act, so show the checkpoint
                    # rather than silently firing another run the instant the
                    # button is pressed.
                    flow["pause_before_run"] = True
                    st.rerun()
            return

    if not readiness["ready"]:
        # Belt-and-braces: the rail never lets this happen, but a stale tab or
        # a file removed from disk between stages could.
        _warning_banner("Stage 2 isn't finished yet, so this can't run.")
        _render_blockers(readiness) if readiness["blockers"] else st.info(
            "Go back to **Upload** and finish the required slots."
        )
        if st.button("← Back to Upload", key="rc_back_from_stage3"):
            flow["stage"] = 2
            st.rerun()
        return

    selected_files = {k: v for k, v in flow["selections"].items() if v}

    # --- What's about to be matched -------------------------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Source files in this run</span>', unsafe_allow_html=True)
    for src, fname in selected_files.items():
        st.markdown(
            f'<div style="font-size:13px;color:var(--setu-text-primary);">'
            f'<span style="color:var(--setu-text-secondary);font-size:12px;">'
            f"{discovery.source_type_label(src)}:</span> {fname}</div>",
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)

    flow.pop("rail_return", False)

    # Manual checkpoint is opt-in; the default path runs without an extra click.
    pause = st.toggle(
        "Pause here so I can double-check the files first",
        value=bool(flow.get("pause_before_run")),
        key="rc_pause_toggle",
    )
    if pause != bool(flow.get("pause_before_run")):
        flow["pause_before_run"] = pause

    if pause:
        st.caption("Nothing runs until you press the button.")
        run_now = st.button("Run reconciliation", type="primary", key="rc_run_manual")
    else:
        # Auto-run: Stage 2 finished clean, so there is nothing to decide.
        run_now = not flow.get("auto_ran")

    if not run_now:
        return

    flow["auto_ran"] = True
    run_id, error = _execute_run(current_user, client, period, recon_type, selected_files)

    if error:
        _warning_banner("The reconciliation didn't complete. Nothing was changed — fix the issue and try again.")
        render_inline_reason(error)
        if st.button("← Back to Upload", key="rc_back_after_failure"):
            flow["stage"] = 2
            st.rerun()
        return

    flow["run_id"] = run_id
    flow["export_path"] = None
    state.invalidate_results_cache(run_id)
    flow["stage"] = 4
    st.rerun()


def _execute_run(
    current_user: dict[str, Any], client: str, period: str,
    recon_type: str, selected_files: dict[str, str],
) -> tuple[Optional[int], Optional[str]]:
    """Orchestrate the existing engine. Matching itself is entirely inside
    src.runner.execute_run — nothing is recomputed here."""
    actor = _actor(current_user)
    f5_client_id = _client_id_for_folder(client)

    with st.status("Running reconciliation…", expanded=True) as box:
        try:
            box.update(label="Loading configuration…")
            config = load_config()

            box.update(label="Normalizing source files and matching transactions…")
            run_id = execute_run(
                client, period, recon_type, None, config,
                selected_files=selected_files, client_id=f5_client_id, actor=actor,
            )

            box.update(label="Persisting results…")
            state.invalidate_results_cache()
            state.set_selected_run_id(run_id)

            # Same best-effort F5 + Module 2 follow-ups the Run tab performs.
            _record_sync_health(f5_client_id, selected_files, succeeded=True)
            _generate_module2_exceptions(run_id, f5_client_id, actor)

            box.update(label=f"Run {run_id} complete", state="complete")
            return run_id, None
        except RunExecutionError as exc:
            box.update(label="Run failed", state="error")
            _record_sync_health(f5_client_id, selected_files, succeeded=False, detail=str(exc))
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            box.update(label="Unexpected error", state="error")
            _record_sync_health(f5_client_id, selected_files, succeeded=False, detail=str(exc))
            return None, f"Something went wrong while running the reconciliation. {exc}"


# ---------------------------------------------------------------------------
# STAGE 4 — Review
# ---------------------------------------------------------------------------

_EXCEPTION_ORDER = ["Not in Books", "Not in Portal", "Amount Difference"]


def _render_review_stage(
    current_user: dict[str, Any], flow: dict[str, Any],
    client: str, period: str, recon_type: str,
) -> None:
    st.markdown("#### 4. Review")

    run_id = flow.get("run_id")
    if not run_id:
        st.info("No run yet for this context — go back to **Reconcile**.")
        if st.button("← Back to Reconcile", key="rc_back_from_review"):
            flow["stage"] = 3
            st.rerun()
        return

    run_id = int(run_id)
    try:
        summary = state.get_summary_cached(run_id)
    except Exception as exc:  # noqa: BLE001
        st.error("Couldn't load the results for this run.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    total = int(summary.get("total_results") or 0)
    by_cls = summary.get("by_classification") or {}
    matched = int(by_cls.get("Matched") or 0)
    exceptions = total - matched

    # --- Headline: clean vs needs-a-human, in words ----------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(
        f'<div style="font-size:20px;font-weight:700;color:var(--setu-text-primary);">'
        f"{matched} matched automatically · {exceptions} need your attention</div>"
        f'<div style="font-size:12px;color:var(--setu-text-secondary);">'
        f"Run {run_id} · {client} · {period} · {recon_type}</div>",
        unsafe_allow_html=True,
    )
    st.markdown('</div>', unsafe_allow_html=True)

    cols = st.columns(3)
    with cols[0]:
        st.metric("Total records", total)
    with cols[1]:
        st.metric("Matched", matched)
    with cols[2]:
        st.metric("Exceptions", exceptions)

    st.markdown("**Exceptions by type**")
    type_cols = st.columns(len(_EXCEPTION_ORDER))
    for col, cls in zip(type_cols, _EXCEPTION_ORDER):
        count = int(by_cls.get(cls) or 0)
        with col:
            _chip("bad" if count else "ok", f"{cls}: {count}")

    other_types = {c: n for c, n in by_cls.items() if c not in _EXCEPTION_ORDER and c != "Matched"}
    if other_types:
        st.caption(
            "Other buckets: "
            + " · ".join(f"{c} {n}" for c, n in other_types.items())
        )

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # Caveats belong next to the results: a partial report must say what it
    # could not check, on the same screen the numbers appear.
    _render_run_caveats(run_id)

    if exceptions == 0:
        st.success("Nothing needs a human decision on this run.")
    else:
        _render_inline_exceptions(run_id)

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    c1, c2 = st.columns([2, 1])
    with c1:
        if st.button("Continue to export →", type="primary", key="rc_to_export"):
            flow["stage"] = 5
            st.rerun()
    with c2:
        if st.button("Deep dive in Compare", key="rc_open_compare"):
            # Compare stays the place for a full run-to-run deep dive; the
            # headline exceptions were already visible here.
            st.session_state["_goto_tab"] = "Compare"
            st.rerun()
    if st.button("← Back to Reconcile", key="rc_back_to_reconcile"):
        flow["stage"] = 3
        st.rerun()


def _render_run_caveats(run_id: int) -> None:
    """State what this run could not check.

    A partial report that names its own gaps is trustworthy; one that
    silently omits checks is not. Rendered on Review and Export so the
    limitation travels with the numbers and into the report.
    """
    run = queries.get_run(run_id) or {}
    caveats = run.get("caveats") or []
    if not caveats:
        return

    _warning_banner(
        f"This is a <b>partial report</b>. {len(caveats)} check(s) could not be performed "
        "with the files and columns provided."
    )
    for caveat in caveats:
        label = caveat.get("label") or caveat.get("code") or "Limitation"
        source = caveat.get("filename") or caveat.get("source_type")
        suffix = f" <span style=\"color:var(--setu-text-muted);\">({source})</span>" if source else ""
        st.markdown(f"- **{label}**{suffix} — {caveat.get('detail', '')}", unsafe_allow_html=True)
    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)


def _render_inline_exceptions(run_id: int) -> None:
    """The headline exceptions, viewable without leaving this screen. Deep
    dives still live in Compare / the Reconciliation tab.

    Rendered as a scannable table plus a single inline detail panel rather
    than one expander per row — a run can carry dozens of exceptions, and a
    wall of collapsed expanders is exactly the "flat table the user has to
    scan" this screen is meant to replace.
    """
    try:
        results = queries.get_results(run_id)
    except Exception as exc:  # noqa: BLE001
        st.warning("Couldn't load the exception rows.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    if results is None or results.empty:
        return

    open_exceptions = results[results["classification"] != "Matched"].copy()
    if open_exceptions.empty:
        st.success("Nothing needs a human decision on this run.")
        return

    present_types = [c for c in _EXCEPTION_ORDER if c in set(open_exceptions["classification"])]
    pick = st.selectbox(
        "Show",
        ["All exceptions"] + present_types,
        key="rc_exception_filter",
    )
    if pick != "All exceptions":
        open_exceptions = open_exceptions[open_exceptions["classification"] == pick]

    st.caption(f"{len(open_exceptions)} exception(s) — pick one below to see the detail.")

    # --- Scannable table ------------------------------------------------
    table = open_exceptions.copy()
    table["Reviewed"] = table["reviewed"].apply(lambda v: "Yes" if v else "No")
    table["Why"] = table["match_reason"].fillna("").astype(str).str.slice(0, 120)
    table = table.rename(
        columns={
            "classification": "Classification",
            "difference_type": "Difference type",
            "confidence_band": "Confidence",
        }
    )
    st.dataframe(
        table[["Classification", "Difference type", "Confidence", "Reviewed", "Why"]],
        use_container_width=True,
        hide_index=True,
    )

    # --- Inline detail for one exception --------------------------------
    labels = {
        int(row["result_id"]): (
            f"{row['classification']}"
            + (f" · {row['difference_type']}" if _has_value(row.get("difference_type")) else "")
            + f" · #{int(row['result_id'])}"
        )
        for _, row in open_exceptions.iterrows()
    }
    if not labels:
        return

    chosen = st.selectbox(
        "Open detail for",
        list(labels.keys()),
        format_func=lambda rid: labels[rid],
        key="rc_exception_detail_pick",
    )
    row = open_exceptions[open_exceptions["result_id"] == chosen]
    if row.empty:
        return
    row = row.iloc[0]

    if row.get("reviewed"):
        _chip("ok", "Reviewed")
    else:
        _chip("warn", "Not reviewed")
    if _has_value(row.get("match_reason")):
        st.markdown(f"**Why:** {row['match_reason']}")

    b1, b2 = st.columns(2)
    with b1:
        st.markdown("**Books side**")
        _render_record(row.get("books_record"))
    with b2:
        st.markdown("**Portal side**")
        _render_record(row.get("portal_record"))

    st.caption(
        "This is the headline view. Use **Compare** for a run-to-run deep dive, or the "
        "**Reconciliation** tool for the full exception queue."
    )


def _render_record(record: Any) -> None:
    if not isinstance(record, dict) or not record:
        st.caption("(no record on this side)")
        return
    st.markdown('<div class="setu-card" style="padding:12px 16px;">', unsafe_allow_html=True)
    for k, v in record.items():
        if str(k).startswith("_") or v in (None, ""):
            continue
        st.markdown(
            f'<div style="font-size:13px;color:var(--setu-text-primary);">'
            f'<span style="color:var(--setu-text-secondary);font-size:12px;">{k}:</span> {v}</div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# STAGE 5 — Export
# ---------------------------------------------------------------------------


def _render_export_stage(
    current_user: dict[str, Any], flow: dict[str, Any],
    client: str, period: str, recon_type: str,
) -> None:
    st.markdown("#### 5. Export")

    run_id = flow.get("run_id")
    if not run_id:
        st.info("No run to export yet — go back to **Reconcile**.")
        if st.button("← Back to Reconcile", key="rc_back_from_export"):
            flow["stage"] = 3
            st.rerun()
        return
    run_id = int(run_id)

    run = queries.get_run(run_id)
    if run is None:
        st.error("That run could not be found.")
        return

    try:
        summary = state.get_summary_cached(run_id)
    except Exception:  # noqa: BLE001
        summary = {}
    total = int(summary.get("total_results") or 0)
    matched = int((summary.get("by_classification") or {}).get("Matched") or 0)

    # --- What's included, stated on this screen -------------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">What goes into this report</span>', unsafe_allow_html=True)
    st.markdown(
        f'<div style="font-size:13px;color:var(--setu-text-primary);">'
        f'<span style="color:var(--setu-text-secondary);font-size:12px;">Run:</span> {run_id} '
        f'· completed {_display_dt(run.get("run_timestamp"))}</div>'
        f'<div style="font-size:13px;color:var(--setu-text-primary);">'
        f'<span style="color:var(--setu-text-secondary);font-size:12px;">Context:</span> '
        f'{run.get("client")} · {run.get("period")} · {run.get("recon_type")}</div>'
        f'<div style="font-size:13px;color:var(--setu-text-primary);">'
        f'<span style="color:var(--setu-text-secondary);font-size:12px;">Records:</span> '
        f"{total} total · {matched} matched · {total - matched} exception(s)</div>",
        unsafe_allow_html=True,
    )
    file_names = run.get("source_file_names") or []
    if file_names:
        st.markdown(
            '<div style="font-size:12px;color:var(--setu-text-secondary);margin-top:6px;">'
            "File versions included:</div>",
            unsafe_allow_html=True,
        )
        for name in file_names:
            st.markdown(
                f'<div style="font-size:13px;color:var(--setu-text-primary);">• {name}</div>',
                unsafe_allow_html=True,
            )
    st.markdown('</div>', unsafe_allow_html=True)

    # The report must carry its own limitations, stated on the screen the user
    # is about to export from.
    _render_run_caveats(run_id)

    if st.button("Download reconciliation report", type="primary", key="rc_export_button"):
        try:
            path = export_run(run_id)
            flow["export_path"] = str(path)
        except Exception as exc:  # noqa: BLE001
            st.error("The export couldn't be written. Check that the destination folder is writable.")
            with st.expander("Details"):
                st.code(str(exc))
            return

    _render_download(flow, run_id, run)


def _render_download(flow: dict[str, Any], run_id: int, run: dict[str, Any]) -> None:
    stored_path = flow.get("export_path")
    if not stored_path:
        st.caption("One button, one file. Nothing else to configure.")
        return

    path = Path(stored_path)
    try:
        data = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        st.error("The export was written but could not be read back for download.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    ts = str(run.get("run_timestamp") or "").replace("-", "").replace(":", "").split(".")[0]
    download_name = (
        f"{run.get('client', 'client')}_{run.get('period', 'period')}_"
        f"{run.get('recon_type', 'RECON')}_run{run_id}_{ts}.xlsx"
    )

    st.success("Report ready.")
    st.download_button(
        "Download reconciliation report",
        data=data,
        file_name=download_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key="rc_export_download",
    )
    st.caption(
        f"Run {run_id} · generated {_display_dt(datetime.now().isoformat())} · "
        f"a copy is also kept on disk at `{path}`."
    )