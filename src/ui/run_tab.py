"""Run tab: pick source files for the selected context, execute a run via
src.runner.execute_run, and switch the app to the new run.

Per source type the tab shows what's on disk and lets staff choose which
file feeds the run (or upload a replacement). The chosen filenames are
passed explicitly into execute_run(selected_files=...), so the runner uses
what the user picked rather than rediscovering files independently.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from src import queries
from src.clients import service as clients
from src.config_loader import load_config
from src.data_paths import source_data_path
from src.f5 import service as f5
from src.ingestion_ai import service as ingestion_ai
from src.runner import execute_run, RunExecutionError
from src.ui import discovery, state
from src.ui.formatting import money, run_label
from src.ui.theme import render_inline_reason

# Map a run's source_type to F5's sync-health source key. "tally" (books,
# manual upload) maps to the 'tally' health source; portal sources all
# collapse onto 'gst_portal'/'traces' per F5's SYNC_SOURCES — this PoC
# only distinguishes tally vs portal since there's no live API connection.
_SYNC_SOURCE_FOR = {
    "tally": "tally", "gstr2b": "gst_portal", "ims": "gst_portal",
    "form26as": "traces", "tds": "traces",
    # 2C "Other" sources — bank maps to F5's bank sync source; the rest are
    # books-side/manual sources that collapse onto tally.
    "bank": "bank", "vendor_ledger": "tally", "opening_balances": "tally",
    "loan_sheet": "tally", "salary": "tally",
}


def _normalize_client_name(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _client_id_for_folder(client_folder: str) -> int | None:
    """Best-effort match of the sidebar's data/ folder name to an F2
    end_client row (exact, then normalized prefix — the folder is often an
    abbreviated form of the legal name). Returns None if no match — F5's
    sync-health recording and Module 2's exception generation are skipped
    gracefully rather than fabricating a client_id."""
    try:
        candidates = clients.list_clients(include_inactive=True)
    except Exception:  # noqa: BLE001
        return None
    target = _normalize_client_name(client_folder)
    for c in candidates:
        if _normalize_client_name(c["legal_name"]) == target:
            return c["client_id"]
    for c in candidates:
        norm = _normalize_client_name(c["legal_name"])
        if target and (norm.startswith(target) or target.startswith(norm)):
            return c["client_id"]
    return None

_FILE_EXTENSIONS = ("*.csv", "*.xlsx", "*.xls")


def _list_files_in_source(client: str, period: str, source_type: str) -> list[str]:
    """Sorted list of CSV/Excel files currently present in a source dir."""
    directory = source_data_path(client, period, source_type)
    if not directory.exists():
        return []
    files: list[str] = []
    for pattern in _FILE_EXTENSIONS:
        files.extend(sorted(m.name for m in directory.glob(pattern)))
    return sorted(set(files))


def _upload_key(client: str, period: str, recon_type: str, source_type: str) -> str:
    return f"upload_{client}_{period}_{recon_type}_{source_type}"


def _conflict_key(source_type: str) -> str:
    return f"conflict_{source_type}"


def render_run_tab() -> None:
    client = st.session_state.get("ctx_client")
    period = st.session_state.get("ctx_period")
    recon_type = st.session_state.get("ctx_recon_type")

    if not (client and period and recon_type):
        st.info(
            "Pick a **client**, **period**, and **recon type** in the sidebar first. "
            "Then come back here to execute a reconciliation."
        )
        return

    st.subheader(f"Source files \u2014 {client} / {period} / {recon_type}")

    selected_files = state.get_file_selections(client, period, recon_type)
    source_types = discovery.RECON_SOURCE_TYPES[recon_type]

    # --- One row per required source type ------------------------------
    for source_type in source_types:
        _render_source_row(client, period, recon_type, source_type, selected_files)

    # --- Readiness -----------------------------------------------------
    # A run needs the books side (tally) plus at least one portal-side
    # source, AND every selected file must have been normalized into a
    # usable canonical frame. The gate is driven by the ingestion layer's
    # real confidence data (Prompt 4) — not by a hardcoded column-name
    # check — so a schema mismatch is explained here, before Execute run
    # is even clickable.
    books_ready = all(
        selected_files.get(src) and _file_exists(client, period, src, selected_files[src])
        for src in discovery.BOOKS_SOURCE_TYPES
    )
    portal_types = discovery.portal_source_types(recon_type)
    portal_ready = any(
        selected_files.get(src) and _file_exists(client, period, src, selected_files[src])
        for src in portal_types
    )

    # Which selected files are actually runnable, per the ingestion layer.
    blocking: list[tuple[str, str, str]] = []  # (source_type, filename, reason)
    for src in source_types:
        fn = selected_files.get(src)
        if not fn or not _file_exists(client, period, src, fn):
            continue
        ok, reason = _file_is_runnable(client, period, recon_type, src, fn)
        if not ok:
            blocking.append((src, fn, reason or "This file needs attention before the run."))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    if not books_ready:
        st.warning(
            "Cannot run yet \u2014 select a file for the books side ("
            + discovery.source_type_label(discovery.BOOKS_SOURCE_TYPES[0]) + ")."
        )
        run_clicked = st.button("Execute run", type="primary", disabled=True)
    elif not portal_ready:
        portal_labels = " or ".join(discovery.source_type_label(s) for s in portal_types)
        st.warning(
            f"Cannot run yet \u2014 select a file for at least one portal source ({portal_labels})."
        )
        run_clicked = st.button("Execute run", type="primary", disabled=True)
    elif blocking:
        st.warning("Cannot run yet \u2014 the file(s) below need attention first.")
        for st_type, fn, reason in blocking:
            st.markdown(
                f"**{discovery.source_type_label(st_type)}** \u2014 `{fn}`",
                unsafe_allow_html=False,
            )
            render_inline_reason(reason)
        run_clicked = st.button("Execute run", type="primary", disabled=True)
    else:
        run_clicked = st.button("Execute run", type="primary", disabled=False)

    if run_clicked:
        _execute_with_status(client, period, recon_type, selected_files)


def _file_is_runnable(
    client: str, period: str, recon_type: str, source_type: str, filename: str
) -> tuple[bool, str | None]:
    """Ask the ingestion layer whether a selected file is ready to feed the
    engine. Returns (ok, reason). A file that has never been normalized is
    NOT runnable — the user must upload it through this screen (which
    normalizes it) or open it in Smart Ingestion.

    Never raises: an ingestion-layer failure is reported as a reason, so
    the Run screen degrades to "needs attention" rather than crashing.
    """
    try:
        stored = ingestion_ai.get_stored_result(client, period, source_type, filename)
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't read the ingestion state for this file: {exc}"

    if stored is None:
        return False, (
            "This file hasn't been through AI ingestion yet. Re-upload it here "
            "(or open it in Smart Ingestion) so its columns can be mapped."
        )

    status = stored.get("status")
    if status in ("wrong_slot", "unrecognized"):
        return False, stored.get("message") or (
            "This file doesn't look like reconciliation data for this slot."
        )
    # Missing required fields no longer block: the run proceeds and produces a
    # partial report whose caveats name the checks that couldn't be made.
    # Only genuinely wrong data (above) stops the run.
    if status not in ("ok", "partial", "blocked"):
        return False, f"This file is in state {status!r} and can't be used yet."
    return True, None


def _file_exists(client: str, period: str, source_type: str, filename: str) -> bool:
    if not filename:
        return False
    return (source_data_path(client, period, source_type) / filename).exists()


def _render_source_row(
    client: str, period: str, recon_type: str, source_type: str, selected_files: dict[str, str]
) -> None:
    label = discovery.source_type_label(source_type)
    files = _list_files_in_source(client, period, source_type)
    current = selected_files.get(source_type)

    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 3, 1])

        with c1:
            st.markdown(f"**{label}**")
            st.caption(source_data_path(client, period, source_type))

        with c2:
            if files:
                options = [None] + files
                idx = options.index(current) if current in options else 0
                chosen = st.selectbox(
                    "File to use",
                    options,
                    index=idx,
                    format_func=lambda f: "(none selected)" if f is None else f,
                    key=f"file_sel_{client}_{period}_{recon_type}_{source_type}",
                )
                if chosen != current:
                    state.set_file_selection(client, period, recon_type, source_type, chosen)
                    # The rest of this render pass (this row's own status
                    # indicator, and the tab-level readiness check) reads a
                    # snapshot of selections taken before this change, so
                    # without a rerun both would show stale ("not chosen")
                    # state even though the selection just succeeded.
                    st.rerun()
            else:
                st.info("No files present yet \u2014 upload one below.")

        with c3:
            if current and current in files:
                _render_ingestion_status(client, period, recon_type, source_type, current)
            elif files:
                st.warning(f"{len(files)} file(s), none chosen" if len(files) > 1 else "1 file, not chosen")
            else:
                st.error("No file")

        # --- Inline mapping / confirmation UI (Prompt 4) ----------------
        # A partial or blocked file is fixed HERE, on the Run screen —
        # the user is never sent to a different tab to resolve it.
        if current and current in files:
            _render_inline_ingestion(client, period, recon_type, source_type, current)

        # --- Upload replacement / new file ------------------------------
        _render_uploader(client, period, recon_type, source_type)


def _render_ingestion_status(
    client: str, period: str, recon_type: str, source_type: str, filename: str
) -> None:
    """One-line confirmation of the file's ingestion state (Prompt 4)."""
    try:
        stored = ingestion_ai.get_stored_result(client, period, source_type, filename)
    except Exception:  # noqa: BLE001
        stored = None

    if stored is None:
        st.warning("Not ingested yet")
        return

    status = stored.get("status")
    if status == "ok":
        st.success(f"{stored.get('row_count_out', 0)} rows \u00b7 all fields mapped")
    elif status == "partial":
        n = len(stored.get("mapping") or []) - len(
            [m for m in (stored.get("mapping") or []) if m.get("source_column")]
        )
        st.warning(f"{stored.get('row_count_out', 0)} rows \u00b7 {n} field(s) unmapped")
    elif status == "blocked":
        st.error("Blocked \u2014 needs mapping")
    elif status == "wrong_slot":
        st.error("Wrong slot")
    elif status == "unrecognized":
        st.error("Not reconciliation data")
    else:
        st.caption(status)


def _render_inline_ingestion(
    client: str, period: str, recon_type: str, source_type: str, filename: str
) -> None:
    """The same inline mapping/confirmation UI Smart Ingestion uses, shown
    right here on the Run screen for a file that isn't fully mapped."""
    try:
        stored = ingestion_ai.get_stored_result(client, period, source_type, filename)
    except Exception:  # noqa: BLE001
        return
    if stored is None:
        return

    status = stored.get("status")
    if status in ("wrong_slot", "unrecognized"):
        render_inline_reason(stored.get("message") or "This file doesn't belong in this slot.")
        return
    if status == "ok":
        return

    # Partial or blocked — surface the mapping controls inline.
    with st.expander(f"Map columns for `{filename}`", expanded=(status == "blocked")):
        _render_mapping_controls(
            client, period, recon_type, source_type, filename, stored,
            key_prefix=f"run_{source_type}",
        )


def _render_mapping_controls(
    client: str, period: str, recon_type: str, source_type: str, filename: str,
    stored: dict[str, Any], *, key_prefix: str,
) -> None:
    """Shared mapping UI: pre-selects every field the AI mapped above the
    confidence threshold, and highlights ONLY the fields that genuinely
    need a human (Prompt 2)."""
    from src.ui.theme import render_confidence_badge

    threshold = ingestion_ai.get_preselect_threshold()
    mapping = stored.get("mapping") or []
    headers = stored.get("headers") or []
    options = ["(leave unmapped)"] + list(headers)

    needs_attention = [
        m for m in mapping
        if not m.get("source_column") or (m.get("confidence") or 0) < threshold
    ]
    confident = [m for m in mapping if m not in needs_attention]

    if confident:
        st.caption(
            f"{len(confident)} field(s) mapped confidently by AI and pre-selected below."
        )

    overrides: dict[str, Any] = {}
    for m in mapping:
        field = m["canonical_field"]
        col = m.get("source_column")
        conf = m.get("confidence")
        flagged = m in needs_attention

        row_l, row_r = st.columns([2, 2])
        with row_l:
            default_idx = options.index(col) if col in options else 0
            chosen = st.selectbox(
                f"{field}{' *' if m.get('required') else ''}",
                options, index=default_idx,
                key=f"{key_prefix}_map_{client}_{period}_{filename}_{field}",
            )
        with row_r:
            if m.get("from_trusted_profile"):
                render_confidence_badge("rule", label="Trusted profile")
            elif conf is not None and not flagged:
                render_confidence_badge("ai", pct=int(round(conf * 100)))
            elif conf is not None:
                render_confidence_badge("ai", pct=int(round(conf * 100)), label=f"AI \u2014 {int(round(conf*100))}% (review)")
            else:
                render_inline_reason("Unmapped \u2014 needs your input")
            if m.get("reason"):
                st.caption(m["reason"])

        overrides[field] = None if chosen.startswith("(leave unmapped") else chosen

    trust = st.checkbox(
        "Trust this mapping for future reuse (this client + source type)",
        value=False, key=f"{key_prefix}_trust_{client}_{period}_{filename}",
    )
    if st.button("Confirm mapping", key=f"{key_prefix}_confirm_{client}_{period}_{filename}", type="primary"):
        try:
            ingestion_ai.confirm_shape_mapping(
                client, source_type, stored.get("header_signature") or "",
                field_overrides=overrides, trust_for_reuse=trust, actor=current_actor(),
            )
            st.success("Mapping confirmed \u2014 this file is ready for the run.")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't save the mapping: {exc}")


def _render_uploader(client: str, period: str, recon_type: str, source_type: str) -> None:
    label = discovery.source_type_label(source_type)
    uploaded = st.file_uploader(
        f"Add a new {label} file",
        type=["csv", "xlsx", "xls"],
        key=_upload_key(client, period, recon_type, source_type),
    )

    if uploaded is None:
        return

    filename = uploaded.name
    directory = source_data_path(client, period, source_type)
    dest = directory / filename
    conflict_key = _conflict_key(source_type)

    if dest.exists():
        # Don't silently overwrite — ask the staff what they want.
        if conflict_key not in st.session_state:
            st.session_state[conflict_key] = None
        if st.session_state[conflict_key] is None:
            st.warning(f"`{filename}` already exists in this folder.")
            c_over, c_ts = st.columns(2)
            with c_over:
                if st.button("Replace existing file", key=f"replace_{source_type}"):
                    st.session_state[conflict_key] = "replace"
                    st.rerun()
            with c_ts:
                if st.button("Save with timestamp", key=f"ts_{source_type}"):
                    st.session_state[conflict_key] = "timestamp"
                    st.rerun()
            return

        mode = st.session_state[conflict_key]
        if mode == "timestamp":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem, ext = dest.stem, dest.suffix
            dest = directory / f"{stem}_{ts}{ext}"
        # mode == "replace" keeps dest unchanged.
        st.session_state[conflict_key] = None  # reset for next upload

    try:
        dest.write_bytes(uploaded.getvalue())
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't upload `{filename}`.")
        with st.expander("Details"):
            st.code(str(exc))
        return

    # Prompt 4: normalize IMMEDIATELY on upload, through the same function
    # Smart Ingestion uses — no separate/duplicate logic. The result is
    # what drives the confirmation line, the inline mapping UI, and the
    # run button's enabled state.
    _ingest_uploaded_file(client, period, recon_type, source_type, dest)

    # Pre-select the newly uploaded file.
    state.set_file_selection(client, period, recon_type, source_type, dest.name)
    st.rerun()


def _ingest_uploaded_file(
    client: str, period: str, recon_type: str, source_type: str, dest: Path
) -> None:
    """Run the unified ingestion layer over a just-uploaded file and report
    the outcome in one line. Never raises — a failure is shown as an
    actionable message, not a traceback."""
    try:
        result = ingestion_ai.normalize_source_file(
            dest, source_type, client, period,
            client_id=_client_id_for_folder(client), recon_type=recon_type,
            actor=current_actor(),
        )
    except Exception as exc:  # noqa: BLE001
        st.error(
            "The file was saved, but AI ingestion couldn't read it. "
            "Check the file and try again."
        )
        with st.expander("Details"):
            st.code(str(exc))
        return

    if result.status in ("wrong_slot", "unrecognized"):
        st.error(result.message or "This file doesn't look like reconciliation data.")
    elif result.status == "blocked":
        st.warning(
            f"Uploaded `{dest.name}` \u2014 {len(result.unmapped_required_fields)} required "
            "field(s) need mapping. Map them below."
        )
    elif result.status == "partial":
        st.info(f"Uploaded `{dest.name}` \u2014 {result.summary_line()}.")
    else:
        st.success(f"Uploaded `{dest.name}` \u2014 {result.summary_line()}.")


def _execute_with_status(
    client: str, period: str, recon_type: str, selected_files: dict[str, str]
) -> None:
    """Run the reconciliation with a real stage-by-stage status indicator."""
    f5_client_id = _client_id_for_folder(client)
    with st.status("Starting reconciliation\u2026", expanded=True) as status_box:
        try:
            status_box.update(label="Loading configuration\u2026")
            config = load_config()

            status_box.update(label="Loading and normalizing source files\u2026")
            # The runner does ingestion + matching in one call; we surface
            # the stages the user actually sees.
            status_box.update(label="Matching transactions\u2026")
            run_id = execute_run(
                client, period, recon_type, None, config, selected_files=selected_files,
                client_id=f5_client_id, actor=current_actor(),
            )

            status_box.update(label="Persisting results\u2026")
            state.invalidate_results_cache()  # a new run can affect stale flags on prior runs too
            state.set_selected_run_id(run_id)

            summary = state.get_summary_cached(run_id)
            run_row = queries.get_run(run_id)
            status_box.update(
                label=f"Run {run_id} complete \u2014 {run_label(run_row, summary)}",
                state="complete",
            )
            _record_sync_health(f5_client_id, selected_files, succeeded=True)

            # Module 2 retrofit: materialize the exception queue, IMS
            # recommendations, TDS chain traces, eligible-credit figure and
            # F5's recon-of-recon check on top of this run. Best-effort —
            # never breaks the run flow if Module 2 or the client match is
            # unavailable.
            _generate_module2_exceptions(run_id, f5_client_id, current_actor())
        except RunExecutionError as exc:
            status_box.update(label="Run failed", state="error")
            # The ingestion layer's blocks carry a specific, plain-language
            # reason ("missing required field(s): ...", "this file looks
            # like a GSTR-2B but you uploaded it as ..."). Show that
            # verbatim rather than the old generic "malformed source file"
            # text, which told the user nothing actionable.
            st.error(str(exc))
            with st.expander("Details"):
                st.code(str(exc))
            _record_sync_health(f5_client_id, selected_files, succeeded=False, detail=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface any failure to the accountant
            status_box.update(label="Unexpected error", state="error")
            st.error(
                "Something went wrong while running the reconciliation. "
                "The details below should help diagnose it."
            )
            with st.expander("Details"):
                st.code(str(exc))
            _record_sync_health(f5_client_id, selected_files, succeeded=False, detail=str(exc))
            return

    # Success state \u2014 headline summary, not a printed dict.
    _render_run_success(run_id, summary)


def current_actor() -> str:
    """The signed-in user's username, for audit logging. Falls back to
    'system' when no session is present (e.g. a scripted run)."""
    try:
        user = st.session_state.get("current_user") or {}
        return user.get("username") or "system"
    except Exception:  # noqa: BLE001
        return "system"


def _generate_module2_exceptions(run_id: int, client_id: "int | None", actor: str) -> None:
    """Module 2 retrofit: after a successful run, materialize the exception
    queue and the production layer on top of the run's match_results.
    Best-effort — never breaks the run flow."""
    if client_id is None:
        return
    try:
        from src.module2 import service as module2

        module2.generate_exceptions_for_run(run_id, client_id=client_id, actor=actor)
    except Exception:  # noqa: BLE001
        pass


def _record_sync_health(
    client_id: "int | None", selected_files: dict[str, str], *, succeeded: bool, detail: "str | None" = None,
) -> None:
    """F5 retrofit: for the manual-upload path (the only path that exists
    this phase), a run attempt is a REAL sync-health signal for each
    source that had a file selected. Best-effort — never breaks the run
    flow if F5 or the client match is unavailable."""
    if client_id is None:
        return
    status = "succeeded" if succeeded else "attempted_failed"
    try:
        seen_sources: set[str] = set()
        for source_type in selected_files:
            health_source = _SYNC_SOURCE_FOR.get(source_type)
            if health_source and health_source not in seen_sources:
                seen_sources.add(health_source)
                f5.record_sync_health(client_id=client_id, source=health_source, status=status, detail=detail)
    except Exception:  # noqa: BLE001
        pass


def _render_run_success(run_id: int, summary: dict[str, Any]) -> None:
    """Show a clear success state with the headline summary."""
    total = summary.get("total_results", 0)
    by_cls = summary.get("by_classification", {})
    value_by_cls = summary.get("value_by_classification", {})
    exceptions = total - by_cls.get("Matched", 0)

    st.success(
        f"**Run {run_id} complete.** "
        f"{total} result(s) \u00b7 {exceptions} exception(s) to review."
    )

    cols = st.columns(3)
    with cols[0]:
        st.metric("Total results", total)
    with cols[1]:
        st.metric("Exceptions", exceptions)
    with cols[2]:
        st.metric("Matched", by_cls.get("Matched", 0))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    st.markdown("**Value by classification**")
    cls_cols = st.columns(len(by_cls) if by_cls else 1)
    if by_cls:
        for col, (cls, count) in zip(cls_cols, by_cls.items()):
            with col:
                st.metric(cls, count, delta=money(value_by_cls.get(cls, 0.0)), delta_color="off")
    else:
        with cls_cols[0]:
            st.caption("No classifications to show.")

    st.info(
        "Switched to this run. Open the **Review** tab to start working through the exceptions."
    )
