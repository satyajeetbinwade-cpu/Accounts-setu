"""F3-AI UI — Smart Document Ingestion.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt +
F3-AI's own design table):
- Upload list — blocked-file indicator: persistent "Blocked — N fields
  need mapping" label in escalated-coral (render_inline_reason), shown
  on the upload list row AND again on the Unified Review Queue entry for
  the same file (F3's Documents tab already renders that queue — this
  tab doesn't duplicate it, just links out).
- Mapping-review screen — split view inside the standard page frame: raw
  file preview card on the left, canonical field table on the right,
  each row showing a remappable source-column dropdown, a sample value,
  and a confidence badge (3.1) — tinted/percent form for AI-suggested
  fields; solid rule-match form for fields resolved via a trusted
  profile (rule-sourced, not AI, per the locked two-state spec).
- Article-Trainee — upload status view: simple, non-interactive status
  card, no dropdowns, no confidence detail exposed at this permission
  tier (role split per the build prompt).
- Trust-for-reuse: secondary checkbox next to Confirm, unchecked by
  default — never implied by Confirm alone.
- Mapping Profiles list (Admin settings): standard hairline table with a
  Revoke action, always enabled (reversible, no Delete-vs-Deactivate
  pair needed).
- No new component introduced — only the confidence badge's locked
  two-state form, reused exactly as F3 and every other AI-touched module
  already use it.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.documents import service as documents
from src.f5 import service as f5
from src.ingestion_ai import service as ingestion_ai
from src.ingestion_ai import mapper
from src.data_paths import VALID_SOURCE_TYPES
from src.ui.theme import (
    render_accent_pill,
    render_confidence_badge,
    render_inline_reason,
    render_placeholder_badge,
    render_warning_banner,
)

# Source types F5's completeness check treats as GST vs TDS recon, for the
# manual control-total record it writes (see _render_control_total_form).
_GST_RECON_SOURCES = {"gstr2b", "ims"}
_TDS_RECON_SOURCES = {"form26as", "tds"}

_SOURCE_TYPE_LABELS = {
    "tally": "Tally export (books)",
    "gstr2b": "GSTR-2B (portal)",
    "ims": "IMS export (portal)",
    "form26as": "Form 26AS (portal)",
    "tds": "TDS return/challan (portal)",
}

SK_SELECTED_UPLOAD_ID = "_f3ai_selected_upload_id"

_STATUS_LABELS = {
    "pending": "Pending",
    "blocked": "Blocked",
    "needs_confirm": "Ready for review",
    "confirmed": "Confirmed",
    "unrecognized": "Unrecognized shape",
}


def render_ingestion_ai_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "ingestion_ai.upload"):
        st.warning("You don't have access to Smart Document Ingestion.")
        return

    st.subheader("Smart Document Ingestion")
    st.caption("Upload a raw export — AI infers the column mapping, you confirm it.")

    selected_upload_id = st.session_state.get(SK_SELECTED_UPLOAD_ID)
    if selected_upload_id is not None:
        upload = ingestion_ai.get_upload(selected_upload_id)
        if upload is not None:
            _render_mapping_review(upload, current_user)
            return
        st.session_state[SK_SELECTED_UPLOAD_ID] = None

    sections = ["Upload & Uploads List"]
    if auth.has_permission(current_user, "ingestion_ai.profiles.manage"):
        sections.append("Mapping Profiles (Admin)")
    if auth.has_permission(current_user, "ingestion_ai.thresholds.manage"):
        sections.append("Confidence Thresholds (Admin)")

    section = st.radio("Section", sections, horizontal=True, label_visibility="collapsed") if len(sections) > 1 else sections[0]

    if section == "Mapping Profiles (Admin)":
        _render_profiles_admin(current_user)
    elif section == "Confidence Thresholds (Admin)":
        _render_thresholds_admin(current_user)
    else:
        _render_upload_and_list(current_user)


# ---------------------------------------------------------------------------
# Upload widget + uploads list (role-split: Article-Trainee sees a simple
# status view, not the interactive review screen)
# ---------------------------------------------------------------------------


def _render_upload_and_list(current_user: dict[str, Any]) -> None:
    all_clients = clients.list_clients(include_inactive=False)
    if not all_clients:
        st.info("No clients onboarded yet — add one from the Clients tab first.")
        return
    labels = {c["client_id"]: c["legal_name"] for c in all_clients}
    client_id = st.selectbox("Client", list(labels.keys()), format_func=lambda cid: labels[cid], key="f3ai_client_picker")

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Upload a raw source file</span>', unsafe_allow_html=True)
    source_type = st.selectbox(
        "Source type", sorted(VALID_SOURCE_TYPES), format_func=lambda s: _SOURCE_TYPE_LABELS.get(s, s),
        key="f3ai_source_type",
    )
    period = st.text_input("Period (optional)", key="f3ai_period", placeholder="e.g. 2026-08")
    uploaded = st.file_uploader(
        "File", key="f3ai_uploaded_file", type=["csv", "xlsx", "xls"],
        help="OCR of scanned/image-based documents is out of scope — spreadsheets/CSV only.",
    )
    if uploaded is not None and st.button("Upload & infer mapping", key="f3ai_upload_btn", type="primary"):
        try:
            result = ingestion_ai.upload_and_infer(
                client_id=client_id, source_type=source_type, filename=uploaded.name,
                file_bytes=uploaded.getvalue(), period=period or None, actor=current_user["username"],
                client_ref=labels.get(client_id, str(client_id)),
            )
            if result["status"] == "unrecognized":
                st.error("This file's shape doesn't match any known canonical schema — flagged as unrecognized, not force-mapped.")
            elif result["status"] == "blocked":
                n_blocked = len(result["ingestion"].unmapped_required_fields)
                st.warning(f"Blocked — {n_blocked} required field(s) need mapping.")
            else:
                st.success("Mapping inferred — ready for review.")
            st.session_state[SK_SELECTED_UPLOAD_ID] = result["upload_id"]
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    st.markdown('</div>', unsafe_allow_html=True)

    can_review = auth.has_permission(current_user, "ingestion_ai.review")
    uploads = ingestion_ai.list_uploads(client_id=client_id)
    if not uploads:
        st.info("No uploads yet for this client.")
        return

    st.markdown('<span class="setu-card-title">Uploads</span>', unsafe_allow_html=True)
    for u in uploads:
        with st.container():
            cols = st.columns([3, 2, 2, 2])
            with cols[0]:
                st.write(u["filename"])
            with cols[1]:
                st.caption(_SOURCE_TYPE_LABELS.get(u["source_type"], u["source_type"]))
            with cols[2]:
                if u["status"] == "blocked":
                    report = ingestion_ai.get_report(u["upload_id"])
                    n_blocked = 0
                    if report:
                        threshold = ingestion_ai.get_preselect_threshold()
                        n_blocked = sum(
                            1 for f in report["field_mapping"]
                            if not f.get("raw_column")
                            or f.get("confidence") is None
                            or (f.get("confidence") or 0) / 100.0 < threshold
                        )
                    render_inline_reason(f"Blocked — {n_blocked} field(s) need mapping")
                elif u["status"] == "unrecognized":
                    render_inline_reason("Unrecognized shape — needs manual review")
                elif u["status"] == "confirmed":
                    render_confidence_badge("rule", label="Confirmed")
                else:
                    st.caption(_STATUS_LABELS.get(u["status"], u["status"]))
            with cols[3]:
                if not can_review:
                    st.caption("Uploaded, pending mapping review")
                elif st.button("Open", key=f"f3ai_open_{u['upload_id']}"):
                    st.session_state[SK_SELECTED_UPLOAD_ID] = u["upload_id"]
                    st.rerun()
            st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Mapping-review screen — split view (design center of this module)
# ---------------------------------------------------------------------------


def _render_mapping_review(upload: dict[str, Any], current_user: dict[str, Any]) -> None:
    can_review = auth.has_permission(current_user, "ingestion_ai.review")

    if st.button("← Back to uploads"):
        st.session_state[SK_SELECTED_UPLOAD_ID] = None
        st.rerun()

    if not can_review:
        # Article-Trainee (upload-only): simple, non-interactive status
        # card — no dropdowns, no confidence detail at this tier.
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown(f'<span class="setu-card-title">{upload["filename"]}</span>', unsafe_allow_html=True)
        st.caption("Uploaded, pending mapping review.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    report = ingestion_ai.get_report(upload["upload_id"])
    if report is None:
        st.error("No mapping report found for this upload.")
        return

    if upload["status"] == "unrecognized":
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown(f'<span class="setu-card-title">{upload["filename"]}</span>', unsafe_allow_html=True)
        render_inline_reason("This file's shape doesn't match any known canonical schema.")
        st.caption("Resolve this from the Unified Review Queue in the Documents tab, or re-upload with the correct source type.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    # The unified layer's own result carries the classification verdict and
    # the per-field reasons; the report row carries the tiered statuses.
    stored = ingestion_ai.get_stored_result(
        _client_ref_for(upload), upload.get("period"), upload["source_type"], upload["filename"]
    )
    classification = (stored or {}).get("classification")

    if classification and not classification.get("is_financial_data", True):
        render_inline_reason(
            "This file doesn't look like reconciliation data — it doesn't contain "
            "recognizable invoice, GSTIN, or amount columns."
        )
    elif classification and (stored or {}).get("status") == "wrong_slot":
        render_warning_banner(
            (stored or {}).get("message")
            or "This file looks like it belongs in a different upload slot."
        )

    threshold = ingestion_ai.get_preselect_threshold()
    field_mapping = report["field_mapping"]
    # Only fields genuinely below the threshold (or unmapped) need a human.
    needs_attention = [
        f for f in field_mapping
        if not f.get("raw_column")
        or f.get("confidence") is None
        or (f.get("confidence") or 0) / 100.0 < threshold
    ]
    confident = [f for f in field_mapping if f not in needs_attention]

    if upload["status"] == "blocked":
        render_inline_reason(f"Blocked — {len(needs_attention)} field(s) need mapping")
    elif upload["status"] == "confirmed":
        render_confidence_badge("rule", label="Confirmed")

    left, right = st.columns([1, 1])
    with left:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Raw file</span>', unsafe_allow_html=True)
        st.caption(f"{upload['filename']} · {_SOURCE_TYPE_LABELS.get(upload['source_type'], upload['source_type'])}")
        if stored:
            if stored.get("header_row") is not None:
                st.caption(f"Header detected on row {stored['header_row'] + 1}.")
            if stored.get("sheet_name"):
                st.caption(f"Sheet used: {stored['sheet_name']}")
            if stored.get("sheet_ambiguous"):
                st.caption("More than one sheet had tabular data — the largest was used.")
            st.caption(
                f"{stored.get('row_count_in', 0)} row(s) read · "
                f"{stored.get('row_count_out', 0)} row(s) extracted."
            )
            for n in (stored.get("notes") or [])[:3]:
                st.caption(f"ℹ {n}")
            for w in (stored.get("warnings") or [])[:5]:
                st.caption(f"⚠ {w}")
        st.markdown('</div>', unsafe_allow_html=True)

    overrides: dict[str, Optional[str]] = {}
    with right:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Canonical field mapping</span>', unsafe_allow_html=True)

        if confident:
            st.caption(
                f"{len(confident)} field(s) mapped confidently by AI and pre-selected. "
                "Only the field(s) below need your attention."
            )

        # --- Fields that need a human: shown expanded, at the top ---
        for f in needs_attention:
            _render_mapping_row(upload, f, overrides, highlight=True)

        # --- Everything else: collapsed, so the user isn't scanning ten
        #     rows to find the two that need them (Prompt 2). ---
        if confident:
            with st.expander(f"Show the {len(confident)} field(s) already mapped", expanded=False):
                for f in confident:
                    _render_mapping_row(upload, f, overrides, highlight=False)
        st.markdown('</div>', unsafe_allow_html=True)

    # F5 -> F3-AI retrofit: manual control-total entry is now a required
    # field at this mapping-confirm step, not a separate screen (F5 now
    # exists). Only meaningful for portal-side source types — books
    # (tally) has no separate "portal control total" to compare against.
    _render_control_total_form(upload, current_user)

    trust = st.checkbox(
        "Trust this mapping for future reuse (this client + source type)", value=False,
        key=f"f3ai_trust_{upload['upload_id']}",
    )
    if st.button("Confirm mapping", key=f"f3ai_confirm_{upload['upload_id']}", type="primary"):
        try:
            ingestion_ai.confirm_mapping(
                upload["upload_id"], field_overrides=overrides, trust_for_reuse=trust,
                actor=current_user["username"], client_ref=_client_ref_for(upload),
            )
            st.success("Mapping confirmed — hard gate cleared, ready for reconciliation.")
            st.session_state[SK_SELECTED_UPLOAD_ID] = None
            st.rerun()
        except ingestion_ai.IngestionAIError as exc:
            st.error(str(exc))


def _client_ref_for(upload: dict[str, Any]) -> str:
    """The client folder/name the unified layer keyed this upload under.
    Falls back to the numeric client_id when the folder name isn't known."""
    return str(upload.get("client_ref") or upload.get("client_id") or "")


def _render_mapping_row(
    upload: dict[str, Any], f: dict[str, Any], overrides: dict[str, Optional[str]], *, highlight: bool
) -> None:
    """One canonical field's mapping control: a source-column dropdown, a
    confidence badge, and the AI's one-line reason shown inline (never a
    separate click-through)."""
    field = f["canonical_field"]
    candidates = [c["raw_column"] for c in f.get("candidates", []) if c.get("raw_column")]
    options = ["(leave unmapped — unavailable)"] + candidates
    current_val = f.get("raw_column")
    default_idx = options.index(current_val) if current_val in options else 0

    row_l, row_r = st.columns([2, 1])
    with row_l:
        chosen = st.selectbox(
            f"{field}{' *' if f.get('required') else ''}",
            options, index=default_idx,
            key=f"f3ai_map_{upload['upload_id']}_{field}",
        )
    with row_r:
        if f.get("from_trusted_profile"):
            render_confidence_badge("rule", label="Trusted profile")
        elif f.get("status") == "auto":
            render_confidence_badge("ai", pct=f.get("confidence"))
        elif f.get("status") == "flagged":
            render_confidence_badge("ai", pct=f.get("confidence"), label=f"AI — {f.get('confidence')}% (review)")
        elif f.get("status") in ("unavailable", "unavailable_review"):
            render_inline_reason("Unavailable — leave blank or map manually")

    # The reason is always visible inline — never map silently.
    if f.get("reason"):
        st.caption(f["reason"])
    if len(candidates) > 1 and f.get("status") in ("flagged", "auto"):
        st.caption(f"Other candidate(s): {', '.join(candidates[1:])}")

    overrides[field] = None if chosen.startswith("(leave unmapped") else chosen


# ---------------------------------------------------------------------------
# Mapping Profiles list (Admin settings)
# ---------------------------------------------------------------------------


def _render_profiles_admin(current_user: dict[str, Any]) -> None:
    st.markdown('<span class="setu-card-title">Mapping Profiles</span>', unsafe_allow_html=True)
    st.caption("Per client + source type: trust status, last used, revoke.")
    profiles = ingestion_ai.list_profiles()
    if not profiles:
        st.info("No confirmed mapping profiles yet.")
        return

    all_clients = {c["client_id"]: c["legal_name"] for c in clients.list_clients(include_inactive=True)}

    header = st.columns([2, 2, 2, 2, 1])
    for col, label in zip(header, ["Client", "Source type", "Trust status", "Last used", ""]):
        col.markdown(
            f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>',
            unsafe_allow_html=True,
        )
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    for p in profiles:
        row = st.columns([2, 2, 2, 2, 1])
        with row[0]:
            st.write(all_clients.get(p["client_id"], f"Client #{p['client_id']}"))
        with row[1]:
            st.write(_SOURCE_TYPE_LABELS.get(p["source_type"], p["source_type"]))
        with row[2]:
            if p["trusted"]:
                render_confidence_badge("rule", label="Trusted")
            else:
                render_accent_pill("Not trusted")
        with row[3]:
            st.write(p.get("last_used_at") or "—")
        with row[4]:
            if p["trusted"] and st.button("Revoke", key=f"f3ai_revoke_{p['profile_id']}"):
                ingestion_ai.revoke_profile_trust(p["profile_id"])
                st.rerun()
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


def _render_control_total_form(upload: dict[str, Any], current_user: dict[str, Any]) -> None:
    """F5 -> F3-AI retrofit: manual control-total entry, wired into F3-AI's
    already-built mapping-confirm flow (reason-capture-before-save shape,
    3.3) rather than a separate screen. Interim workflow per F5's build
    prompt — the uploader manually enters the source system's own control
    total, compared against what was actually ingested from this file.
    Only relevant for portal-side source types (gstr2b/ims/form26as/tds);
    the books side (tally) has no separate portal control total."""
    if upload["source_type"] not in (_GST_RECON_SOURCES | _TDS_RECON_SOURCES):
        return
    if not auth.has_permission(current_user, "f5.control_total.enter"):
        render_inline_reason("Requires permission to enter a completeness control total.")
        return

    recon_type = "GST" if upload["source_type"] in _GST_RECON_SOURCES else "TDS"

    try:
        version_bytes = documents.get_version_bytes(
            documents.get_document(upload["document_id"])["current_version_id"]
        )
        raw_df = mapper.read_with_detected_header(_mapper_temp_path(upload["filename"], version_bytes))
        ingested_count = len(raw_df)
    except Exception:  # noqa: BLE001 — never block confirm over a re-read failure
        ingested_count = 0

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Completeness check — manual control total (F5)</span>', unsafe_allow_html=True)
    st.caption(
        f"Ingested from this file: {ingested_count} record(s). Enter the source portal's own "
        "control total below to check completeness (interim workflow until C2's live connection exists)."
    )
    key_prefix = f"f5_control_{upload['upload_id']}"
    control_count = st.number_input(
        "Portal-reported record count", min_value=0, value=ingested_count, step=1, key=f"{key_prefix}_count",
    )
    control_amount = st.number_input(
        "Portal-reported total amount (optional, ₹)", min_value=0.0, value=0.0, step=0.01, key=f"{key_prefix}_amount",
    )
    if st.button("Record control total", key=f"{key_prefix}_save"):
        try:
            result = f5.record_control_total(
                client_id=upload["client_id"], period=None, recon_type=recon_type,
                source_type=upload["source_type"], source_file=upload["filename"],
                control_count=int(control_count), control_amount=control_amount or None,
                ingested_count=ingested_count, ingested_amount=None, entered_by=current_user["username"],
            )
            if result.get("result") == "match":
                st.success("Completeness check passed — control total matches what was ingested.")
            else:
                st.warning("Completeness mismatch — control total does not match what was ingested.")
        except f5.F5Error as exc:
            st.error(str(exc))
    st.markdown('</div>', unsafe_allow_html=True)


def _mapper_temp_path(filename: str, file_bytes: Optional[bytes]):
    import tempfile
    from pathlib import Path

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "csv"
    fd, path_str = tempfile.mkstemp(suffix=f".{ext}")
    path = Path(path_str)
    path.write_bytes(file_bytes or b"")
    import os

    os.close(fd)
    return path


def _render_thresholds_admin(current_user: dict[str, Any]) -> None:
    st.markdown('<span class="setu-card-title">Confidence thresholds</span>', unsafe_allow_html=True)
    st.caption("EDITABLE, admin-configurable — not hardcoded. Auto-apply must stay >= manual threshold.")
    thresholds = ingestion_ai.get_thresholds()
    auto_apply = st.number_input(
        "Auto-apply threshold (%)", min_value=0, max_value=100, value=thresholds["auto_apply"], key="f3ai_thresh_auto",
    )
    manual = st.number_input(
        "Manual threshold (%) — below this, a field is always left blank/flagged",
        min_value=0, max_value=100, value=thresholds["manual"], key="f3ai_thresh_manual",
    )
    if st.button("Save thresholds", type="primary", key="f3ai_thresh_save"):
        try:
            ingestion_ai.set_thresholds(auto_apply=int(auto_apply), manual=int(manual), actor=current_user["username"])
            st.success("Thresholds updated.")
        except ingestion_ai.IngestionAIError as exc:
            st.error(str(exc))
