"""F3-B UI — Invoice Extraction & Digitalization.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt +
F3-B's own design table). No new component is introduced — this module
draws entirely on the confidence badge (3.1) and the headline -> grouped
-> detail template (3.6) already established by F3 and F3-AI.

Screens:
  * Upload screen — drag-and-drop zone on the page-background token;
    format-icon chips for the four accepted types (image / PDF / Excel /
    Word); optional batch-name field for grouped uploads.
  * Extraction result — an auto-accepted field renders its value inline
    with NO badge (mirrors F3's "no badge once filed" convention); a
    sub-threshold field carries the confidence badge (3.1) tinted/percent
    form, e.g. "AI — 74%"; a genuinely absent field reads "not present".
  * Review Queue — headline count of uploads awaiting resolution, grouped
    by client, detail rows open the side-by-side reviewer (3.6).
  * Review screen — two-pane: source document preview (left), editable
    field table (right); flagged fields highlighted with the same amber
    tint as their badge.
  * Export history — standard hairline table: batch id, date, generated
    by, row count, download link — read-only, no edit action
    (immutability by design, not a permission gate).
  * Model assignment (admin) — reuses C3-ext's per-touchpoint
    model-assignment table shape, two rows, both rendered ACTIVE (not the
    greyed placeholder treatment used for unbuilt touchpoints).
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.invoice_extract import service as ie
from src.invoice_extract.seed import CANONICAL_FIELD_KEYS, FIELD_LABELS
from src.ui.theme import (
    render_accent_pill,
    render_confidence_badge,
    render_inline_reason,
    render_placeholder_badge,
)

SK_SELECTED_UPLOAD_ID = "_f3b_selected_upload_id"

# The four accepted source formats, with a format-icon chip label.
_FORMAT_CHIPS = [
    ("image", "\U0001F5BC Image (JPG/PNG)"),
    ("pdf", "\U0001F4C4 PDF"),
    ("excel", "\U0001F4CA Excel/CSV"),
    ("word", "\U0001F4DD Word"),
]

_STATUS_LABELS = {
    "extracted": "Extracted — ready to confirm",
    "needs_review": "Needs review",
    "confirmed": "Confirmed",
    "exported": "Exported",
}

_PATH_LABELS = {
    "visual": "Visual touchpoint (vision model)",
    "structured": "Structured touchpoint (text/table parsing)",
}


def render_invoice_extract_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "invoice_extract.upload"):
        st.warning("You don't have access to Invoice Extraction.")
        return

    st.subheader("Invoice Extraction & Digitalization")
    st.caption(
        "Upload invoices in any of four formats — AI extracts the canonical field set "
        "with a per-field confidence score; anything under the threshold is gated into review."
    )

    selected = st.session_state.get(SK_SELECTED_UPLOAD_ID)
    if selected is not None:
        upload = ie.get_upload(selected)
        if upload is not None:
            _render_review_screen(upload, current_user)
            return
        st.session_state[SK_SELECTED_UPLOAD_ID] = None

    sections = ["Upload", "Review Queue", "Export"]
    if auth.has_permission(current_user, "invoice_extract.models.manage") or auth.has_permission(
        current_user, "invoice_extract.threshold.manage"
    ):
        sections.append("Model Assignment (Admin)")

    section = (
        st.radio("Section", sections, horizontal=True, label_visibility="collapsed")
        if len(sections) > 1
        else sections[0]
    )

    if section == "Review Queue":
        _render_review_queue(current_user)
    elif section == "Export":
        _render_export(current_user)
    elif section == "Model Assignment (Admin)":
        _render_model_admin(current_user)
    else:
        _render_upload(current_user)


# ---------------------------------------------------------------------------
# Upload screen
# ---------------------------------------------------------------------------


def _render_upload(current_user: dict[str, Any]) -> None:
    all_clients = clients.list_clients(include_inactive=False)
    if not all_clients:
        st.info("No clients onboarded yet — add one from the Clients tab first.")
        return
    labels = {c["client_id"]: c["legal_name"] for c in all_clients}
    client_id = st.selectbox(
        "Client", list(labels.keys()), format_func=lambda cid: labels[cid], key="f3b_client_picker",
    )

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Upload invoices</span>', unsafe_allow_html=True)

    # Format-icon chips for the four accepted types.
    chip_html = "".join(
        f'<span class="setu-token-pill placeholder" style="margin-right:6px;">{label}</span>'
        for _, label in _FORMAT_CHIPS
    )
    st.markdown(f'<div style="margin-bottom:8px;">{chip_html}</div>', unsafe_allow_html=True)

    batch_id = st.text_input(
        "Batch name (optional)", key="f3b_batch_id",
        placeholder="e.g. Aug-2026 purchases — groups uploads into one export run",
    )
    uploaded = st.file_uploader(
        "Drag and drop invoices here", key="f3b_uploaded_files", accept_multiple_files=True,
        type=["jpg", "jpeg", "png", "pdf", "doc", "docx", "xls", "xlsx", "csv"],
        help="Images and scanned PDFs route to the visual touchpoint; native PDF/Excel/Word route to the structured touchpoint.",
    )
    if uploaded and st.button("Upload & extract", key="f3b_upload_btn", type="primary"):
        _do_upload(client_id, uploaded, batch_id or None, current_user)
    st.markdown('</div>', unsafe_allow_html=True)

    uploads = ie.list_uploads(client_id=client_id)
    if not uploads:
        st.info("No invoices uploaded yet for this client.")
        return

    st.markdown('<span class="setu-card-title">Uploads</span>', unsafe_allow_html=True)
    for u in uploads:
        _render_upload_row(u, current_user)


def _do_upload(client_id: int, uploaded: list[Any], batch_id: Optional[str], current_user: dict[str, Any]) -> None:
    results = []
    for f in uploaded:
        try:
            res = ie.upload_invoice(
                client_id=client_id, filename=f.name, file_bytes=f.getvalue(),
                batch_id=batch_id, actor=current_user["username"],
            )
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            st.error(f"{f.name}: {exc}")

    if not results:
        return
    n_review = sum(1 for r in results if r["status"] == "needs_review")
    n_dup = sum(1 for r in results if r["duplicate"])
    if n_review:
        st.warning(f"{len(results)} uploaded — {n_review} routed to the Review Queue (sub-threshold or not-present fields).")
    else:
        st.success(f"{len(results)} uploaded — every field auto-accepted at or above the threshold.")
    if n_dup:
        st.info(f"{n_dup} upload(s) matched an existing invoice number + vendor GSTIN — a duplicate warning, not a block. Confirm it's intentional or discard it.")
    st.rerun()


def _render_upload_row(u: dict[str, Any], current_user: dict[str, Any]) -> None:
    cols = st.columns([3, 2, 2, 2])
    with cols[0]:
        st.write(u["filename"])
        if u["batch_id"]:
            st.caption(f"Batch: {u['batch_id']}")
    with cols[1]:
        st.caption(_PATH_LABELS.get(u["extraction_path"], u["extraction_path"]))
    with cols[2]:
        if u["status"] == "needs_review":
            remaining = ie.get_reviewable_fields(u["upload_id"])
            render_inline_reason(f"Needs review — {len(remaining)} field(s) flagged")
        elif u["status"] == "confirmed":
            render_confidence_badge("rule", label="Confirmed")
        elif u["status"] == "exported":
            render_accent_pill("Exported")
        else:
            st.caption(_STATUS_LABELS.get(u["status"], u["status"]))
        if u["duplicate_warning"]:
            st.caption("\u26A0 Possible duplicate")
    with cols[3]:
        if st.button("Open", key=f"f3b_open_{u['upload_id']}"):
            st.session_state[SK_SELECTED_UPLOAD_ID] = u["upload_id"]
            st.rerun()
    st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Review Queue — headline -> grouped -> detail (3.6)
# ---------------------------------------------------------------------------


def _render_review_queue(current_user: dict[str, Any]) -> None:
    summary = ie.review_queue_summary()
    entries = ie.list_review_queue(status="open")

    # --- Headline -------------------------------------------------------
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(
        f'<div style="font-size:34px;font-weight:700;color:var(--setu-text-primary);">'
        f'{summary["open_count"]}</div>'
        f'<div style="font-size:12px;color:var(--setu-text-secondary);">'
        f'upload(s) awaiting resolution — this module\'s own queue, not F3\'s Unified Review Queue</div>',
        unsafe_allow_html=True,
    )
    st.markdown('</div>', unsafe_allow_html=True)

    if not entries:
        st.info("Nothing awaiting review — every upload is either auto-accepted or already confirmed.")
        return

    # --- Grouped: by client --------------------------------------------
    st.markdown("**By client**")
    client_labels = {c["client_id"]: c["legal_name"] for c in clients.list_clients(include_inactive=True)}
    group_cols = st.columns(max(1, len(summary["by_client"])))
    for col, (cid, count) in zip(group_cols, sorted(summary["by_client"].items())):
        with col:
            st.button(
                f"{client_labels.get(cid, f'Client {cid}')} — {count}",
                key=f"f3b_group_{cid}", use_container_width=True,
                on_click=_goto_queue_filter, args=(cid,),
            )

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Detail ---------------------------------------------------------
    client_filter = st.session_state.get("f3b_queue_client_filter")
    if client_filter is not None:
        entries = [e for e in entries if e["upload"] and e["upload"]["client_id"] == client_filter]
        st.caption(f"Filtered to **{client_labels.get(client_filter, client_filter)}**.")

    for e in entries:
        up = e["upload"]
        if up is None:
            continue
        with st.container():
            cols = st.columns([3, 3, 2])
            with cols[0]:
                st.write(up["filename"])
                st.caption(f"{client_labels.get(up['client_id'], up['client_id'])} · {_PATH_LABELS.get(up['extraction_path'], '')}")
            with cols[1]:
                flagged = ", ".join(FIELD_LABELS.get(f, f) for f in e["flagged_fields"])
                render_inline_reason(f"Flagged: {flagged}")
            with cols[2]:
                if st.button("Review", key=f"f3b_review_{up['upload_id']}"):
                    st.session_state[SK_SELECTED_UPLOAD_ID] = up["upload_id"]
                    st.rerun()
            st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


def _goto_queue_filter(client_id: int) -> None:
    """on_click callback — runs before the next run's widgets are created,
    so it can safely set session state the radio/selectbox reads."""
    st.session_state["f3b_queue_client_filter"] = client_id


# ---------------------------------------------------------------------------
# Review screen — two-pane (source preview | editable field table)
# ---------------------------------------------------------------------------


def _render_review_screen(upload: dict[str, Any], current_user: dict[str, Any]) -> None:
    can_review = auth.has_permission(current_user, "invoice_extract.review")

    if st.button("\u2190 Back"):
        st.session_state[SK_SELECTED_UPLOAD_ID] = None
        st.rerun()

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(f'<span class="setu-card-title">{upload["filename"]}</span>', unsafe_allow_html=True)
    st.caption(
        f"{_PATH_LABELS.get(upload['extraction_path'], upload['extraction_path'])} · "
        f"status: {_STATUS_LABELS.get(upload['status'], upload['status'])}"
    )
    if upload["duplicate_warning"]:
        st.caption("\u26A0 Possible duplicate — same invoice number + vendor GSTIN as an earlier upload.")
    st.markdown('</div>', unsafe_allow_html=True)

    if not can_review:
        st.info("You can upload invoices, but resolving flagged fields needs the review permission.")
        return

    fields = ie.get_fields(upload["upload_id"])
    threshold = ie.get_threshold()
    remaining = ie.get_reviewable_fields(upload["upload_id"])

    left, right = st.columns([1, 1])

    # --- Left: source document preview ---------------------------------
    with left:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Source document</span>', unsafe_allow_html=True)
        _render_source_preview(upload)
        st.markdown('</div>', unsafe_allow_html=True)

    # --- Right: editable field table -----------------------------------
    with right:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown('<span class="setu-card-title">Extracted fields</span>', unsafe_allow_html=True)
        st.caption(f"Confidence threshold: {threshold}% — fields below it (or absent) are flagged.")
        for f in fields:
            _render_field_row(upload, f, threshold, current_user)
        st.markdown('</div>', unsafe_allow_html=True)

    # --- Confirm / discard ---------------------------------------------
    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    if upload["status"] == "exported":
        render_accent_pill("Exported — immutable batch")
        st.caption("This upload is part of an immutable export batch. Correcting it means generating a new batch.")
        return

    if remaining:
        render_inline_reason(f"{len(remaining)} field(s) still need resolution before this upload can be confirmed.")
    else:
        if upload["status"] != "confirmed":
            if st.button("Confirm upload", key=f"f3b_confirm_{upload['upload_id']}", type="primary"):
                try:
                    ie.confirm_upload(upload["upload_id"], actor=current_user["username"])
                    st.success("Confirmed — now eligible for export.")
                    st.rerun()
                except ie.InvoiceExtractError as exc:
                    st.error(str(exc))
        else:
            render_confidence_badge("rule", label="Confirmed")

    if st.button("Discard upload", key=f"f3b_discard_{upload['upload_id']}"):
        try:
            ie.discard_upload(upload["upload_id"], actor=current_user["username"])
            st.session_state[SK_SELECTED_UPLOAD_ID] = None
            st.toast("Upload discarded.")
            st.rerun()
        except ie.InvoiceExtractError as exc:
            st.error(str(exc))


def _render_source_preview(upload: dict[str, Any]) -> None:
    """Genuine source preview: images render inline; spreadsheets render
    their parsed table; PDF/Word render their extracted text layer. Never a
    fabricated preview."""
    data = ie.get_upload_bytes(upload["upload_id"])
    fmt = upload["source_format"]
    if not data:
        st.caption("Source file not retained for preview.")
        return

    if fmt == "image":
        try:
            st.image(data, caption=upload["filename"], use_container_width=True)
            return
        except Exception:  # noqa: BLE001
            st.caption("Image preview unavailable.")
            return

    if fmt == "excel":
        try:
            import pandas as pd
            from io import BytesIO

            if data[:4] == b"PK\x03\x04" or data[:4] == b"\xd0\xcf\x11\xe0":
                df = pd.read_excel(BytesIO(data), dtype=str)
            else:
                df = pd.read_csv(BytesIO(data), dtype=str, keep_default_na=False)
            st.dataframe(df, hide_index=True, width="stretch")
            return
        except Exception:  # noqa: BLE001
            st.caption("Spreadsheet preview unavailable.")
            return

    # PDF / Word — show the extracted text layer.
    from src.invoice_extract import extractor

    if fmt == "pdf":
        text = extractor._pdf_text(data)
    elif fmt == "word":
        text = extractor._docx_text(data, upload["filename"])
    else:
        text = ""
    if text.strip():
        st.text_area("Extracted text", value=text.strip()[:4000], height=320, disabled=True,
                     key=f"f3b_preview_{upload['upload_id']}")
    else:
        st.caption("No text layer available — this file was routed to the visual touchpoint.")


def _render_field_row(
    upload: dict[str, Any], f: dict[str, Any], threshold: int, current_user: dict[str, Any],
) -> None:
    """One field row. Auto-accepted fields render their value inline with NO
    badge; sub-threshold fields carry the confidence badge (3.1) tinted/percent
    form; absent fields read "not present". Flagged rows are editable."""
    field = f["field_name"]
    conf = f["confidence"]
    is_flagged = (not f["resolved"]) and ((not f["is_present"]) or conf is None or conf < threshold)

    row_l, row_r = st.columns([3, 2])
    with row_l:
        st.markdown(f"**{f['label']}**")
        if f["source_location"]:
            st.caption(f"source: {f['source_location']}")
    with row_r:
        if f["resolved"]:
            render_confidence_badge("rule", label="Resolved")
        elif not f["is_present"] or conf is None:
            render_confidence_badge("ai", label="not present")
        elif conf >= threshold:
            # Auto-accepted: value inline, NO badge (mirrors F3's convention).
            st.caption("auto-accepted")
        else:
            render_confidence_badge("ai", pct=conf)

    if is_flagged:
        # Flagged field: editable, highlighted with the same amber tint as
        # its badge (via the warning-banner tint wrapper).
        st.markdown('<div class="setu-warning-banner" style="padding:6px 10px;">', unsafe_allow_html=True)
        new_val = st.text_input(
            f"Confirm {f['label']}", value=f["extracted_value"] or "", key=f"f3b_field_{upload['upload_id']}_{field}",
            label_visibility="collapsed", placeholder="Enter the correct value (or leave blank if genuinely absent)",
        )
        if st.button("Resolve", key=f"f3b_resolve_{upload['upload_id']}_{field}"):
            try:
                ie.resolve_field(
                    upload["upload_id"], field_name=field,
                    resolved_value=(new_val.strip() or None), actor=current_user["username"],
                )
                st.rerun()
            except ie.InvoiceExtractError as exc:
                st.error(str(exc))
        st.markdown('</div>', unsafe_allow_html=True)
    else:
        st.caption(f"value: {f['effective_value'] if f['effective_value'] is not None else '—'}")


# ---------------------------------------------------------------------------
# Export — select Confirmed uploads, generate immutable batch
# ---------------------------------------------------------------------------


def _render_export(current_user: dict[str, Any]) -> None:
    can_export = auth.has_permission(current_user, "invoice_extract.export")
    all_clients = clients.list_clients(include_inactive=False)
    if not all_clients:
        st.info("No clients onboarded yet.")
        return
    labels = {c["client_id"]: c["legal_name"] for c in all_clients}
    client_id = st.selectbox(
        "Client", list(labels.keys()), format_func=lambda cid: labels[cid], key="f3b_export_client",
    )

    confirmed = ie.list_uploads(client_id=client_id, status="confirmed")
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Generate Tally-ready export</span>', unsafe_allow_html=True)
    if not confirmed:
        st.caption("No Confirmed uploads for this client yet. Resolve every flagged field, then confirm.")
    else:
        options = {u["upload_id"]: f"{u['filename']} ({u['source_format']})" for u in confirmed}
        chosen = st.multiselect(
            "Confirmed uploads to include", list(options.keys()),
            format_func=lambda uid: options[uid], key="f3b_export_select",
        )
        fmt = st.radio("Format", ["xlsx", "csv"], horizontal=True, key="f3b_export_fmt")
        if not can_export:
            render_inline_reason("Generating an export needs the export permission.")
        elif st.button("Generate export", key="f3b_export_btn", type="primary"):
            try:
                res = ie.generate_export(upload_ids=chosen, actor=current_user["username"], fmt=fmt)
                st.session_state["f3b_last_export"] = res
                st.success(f"Batch {res['batch_id']} generated — {res['row_count']} row(s), rows {res['row_range']}.")
                st.rerun()
            except ie.InvoiceExtractError as exc:
                st.error(str(exc))
    st.markdown('</div>', unsafe_allow_html=True)

    # Download the just-generated batch (if any this session).
    last = st.session_state.get("f3b_last_export")
    if last:
        mime = (
            "text/csv" if last["filename"].endswith(".csv")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        st.download_button(
            f"Download {last['filename']}", data=last["data"], file_name=last["filename"], mime=mime, type="primary",
        )

    # --- Export history (read-only, immutable) -------------------------
    st.markdown('<span class="setu-card-title">Export history</span>', unsafe_allow_html=True)
    batches = ie.list_export_batches()
    if not batches:
        st.info("No export batches generated yet.")
        return
    st.caption("Immutable by design — re-exporting produces a new batch, never an overwrite. No edit action exists.")
    for b in batches:
        cols = st.columns([1, 3, 2, 1, 2])
        with cols[0]:
            st.write(f"#{b['batch_id']}")
        with cols[1]:
            st.write(b["filename"])
        with cols[2]:
            st.caption(b["generated_at"].replace("T", " ").split(".")[0])
        with cols[3]:
            st.caption(f"{b['row_count']} rows")
        with cols[4]:
            try:
                data = ie.regenerate_export_bytes(b["batch_id"])
                mime = "text/csv" if b["filename"].endswith(".csv") else (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                st.download_button(
                    "Download", data=data, file_name=b["filename"], mime=mime,
                    key=f"f3b_dl_{b['batch_id']}",
                )
            except Exception:  # noqa: BLE001
                st.caption("unavailable")
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Model assignment (admin) — reuses C3-ext's per-touchpoint table shape
# ---------------------------------------------------------------------------


def _render_model_admin(current_user: dict[str, Any]) -> None:
    st.caption(
        "Two new AITouchpoint rows added to C3-ext's ModelAssignment table by this build. "
        "Both are ACTIVE — this module ships functional AI extraction from day one."
    )
    touchpoints = ie.list_touchpoints()
    for t in touchpoints:
        st.markdown('<div class="setu-card">', unsafe_allow_html=True)
        st.markdown(f'<span class="setu-card-title">{t["label"]}</span>', unsafe_allow_html=True)
        st.caption(f"touchpoint key: `{t['touchpoint_key']}`")
        cols = st.columns([2, 2, 1])
        with cols[0]:
            st.markdown(f"**Primary** — {t['primary_model']}")
            st.caption(t["primary_provider"])
        with cols[1]:
            st.markdown(f"**Fallback** — {t['fallback_model']}")
            st.caption(t["fallback_provider"])
        with cols[2]:
            render_confidence_badge("rule", label="Active")
            if st.button("Test call", key=f"f3b_test_{t['touchpoint_key']}"):
                res = ie.test_touchpoint_call(t["touchpoint_key"])
                if res["ok"]:
                    render_confidence_badge("rule", label="Test call OK")
                    st.caption(res["message"])
                else:
                    render_inline_reason(res["message"])
        st.markdown('</div>', unsafe_allow_html=True)

    if auth.has_permission(current_user, "invoice_extract.models.manage"):
        st.markdown('<span class="setu-card-title">Edit a model assignment</span>', unsafe_allow_html=True)
        st.caption(
            "These are the same assignments shown under **Setup → AI Models** — "
            "changing one here changes it there too."
        )
        keys = [t["touchpoint_key"] for t in touchpoints]
        key = st.selectbox("Touchpoint", keys, key="f3b_model_key")
        current = next((t for t in touchpoints if t["touchpoint_key"] == key), None)

        # Model ids come from the central registry's OpenRouter catalogue, so
        # this screen offers the same real list rather than free text.
        try:
            from src.ai_models import service as ai_models

            catalog = ai_models.model_options()
            option_ids = [m["model_id"] for m in catalog]
            labels = {m["model_id"]: ai_models.model_label(m) for m in catalog}
        except Exception:  # noqa: BLE001
            option_ids, labels = [], {}

        def _options_with(value: str) -> list[str]:
            if value and value not in option_ids:
                return [value] + option_ids
            return option_ids or [value]

        primary_opts = _options_with(current["primary_model"] if current else "")
        fallback_opts = _options_with(current["fallback_model"] if current else "")
        c1, c2 = st.columns(2)
        with c1:
            primary = st.selectbox(
                "Primary model", primary_opts,
                index=primary_opts.index(current["primary_model"]) if current and current["primary_model"] in primary_opts else 0,
                format_func=lambda mid: labels.get(mid, mid), key="f3b_model_primary",
            )
        with c2:
            fallback = st.selectbox(
                "Fallback model", fallback_opts,
                index=fallback_opts.index(current["fallback_model"]) if current and current["fallback_model"] in fallback_opts else 0,
                format_func=lambda mid: labels.get(mid, mid), key="f3b_model_fallback",
            )
        if st.button("Save model assignment", key="f3b_model_save"):
            try:
                ie.update_touchpoint_models(
                    key, primary_model=primary, fallback_model=fallback, actor=current_user["username"],
                )
                st.success("Model assignment updated.")
                st.rerun()
            except ie.InvoiceExtractError as exc:
                st.error(str(exc))

    if auth.has_permission(current_user, "invoice_extract.threshold.manage"):
        st.markdown('<span class="setu-card-title">Confidence threshold</span>', unsafe_allow_html=True)
        current_threshold = ie.get_threshold()
        new_threshold = st.number_input(
            "Auto-accept threshold (%)", min_value=0, max_value=100, value=current_threshold, key="f3b_threshold",
        )
        if st.button("Save threshold", key="f3b_threshold_save"):
            try:
                ie.set_threshold(int(new_threshold), actor=current_user["username"])
                st.success(f"Threshold set to {int(new_threshold)}%.")
                st.rerun()
            except ie.InvoiceExtractError as exc:
                st.error(str(exc))

    # Deferred / placeholder items, honestly labelled (3.5).
    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Deferred in this build</span>', unsafe_allow_html=True)
    st.caption("Auto-splitting a multi-invoice Excel/Word file into separate records — known limitation, not built.")
    render_placeholder_badge("Module F3")
    st.caption("Live wiring to F3's Unified Review Queue and Module 2's matching workflow — structurally possible, not built now.")
