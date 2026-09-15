"""F3 UI — Document & Data Repository.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt +
F3's design table):
- Scoped vault (client-scoped document list): full-width table on the
  page-background token, hairline row dividers, filter bar for doc type /
  period / search. Reachable from F3's own nav and, via
  ``render_scoped_vault()``, from F2's profile Documents tab (retrofit).
- Upload → auto-classify result: confidence badge (3.1) tinted/percent
  form only, since classification here is AI-assisted, not a
  deterministic rule match. High-confidence files auto-file silently —
  no badge shown once filed.
- Unified Review Queue: headline → grouped → detail (3.6) — headline
  count of items awaiting resolution, grouped by reason, detail rows
  resolvable inline. Same generic shape F3-AI's future "couldn't map"
  tag will reuse, no rebuild needed.
- Document detail "Used in" panel: card beneath the metadata header,
  empty state reads "Not yet used in any record" rather than being
  hidden.
- Reassign misfiled document: inline reason field (3.3) beneath the
  client picker; Save stays disabled until populated — identical shape
  to F2's GSTIN/PAN edit.
- Soft-delete (Partner/Manager): Delete renders as a secondary,
  danger-coral action (3.2, Delete side only) for Partner/Manager;
  Senior/Article-Trainee see it disabled with inline reason (3.4).
- Recently deleted view: standard hairline table, restore action per
  row, reached from the vault's overflow/settings area (an expander
  here), not a primary nav item.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.documents import service as documents
from src.ui.theme import (
    render_accent_pill,
    render_confidence_badge,
    render_inline_reason,
    render_placeholder_badge,
)

_HUB_SECTIONS = ["Document Vault", "Unified Review Queue", "Recently Deleted"]

SK_SELECTED_DOCUMENT_ID = "_f3_selected_document_id"
SK_SELECTED_VAULT_CLIENT_ID = "_f3_vault_client_id"


# ---------------------------------------------------------------------------
# Standalone F3 tab entry point
# ---------------------------------------------------------------------------


def render_documents_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "documents.view"):
        st.warning("You don't have access to the Document & Data Repository.")
        return

    st.subheader("Document & Data Repository")
    st.caption("Every document, organized, searchable, and evidence-linkable.")

    section = st.radio(
        "Documents section", _HUB_SECTIONS, horizontal=True, label_visibility="collapsed",
    )

    if section == "Unified Review Queue":
        _render_review_queue(current_user)
    elif section == "Recently Deleted":
        _render_recently_deleted(current_user)
    else:
        _render_vault_hub(current_user)


def _render_vault_hub(current_user: dict[str, Any]) -> None:
    selected_document_id = st.session_state.get(SK_SELECTED_DOCUMENT_ID)
    if selected_document_id is not None:
        doc = documents.get_document(selected_document_id)
        if doc is not None:
            _render_document_detail(doc, current_user)
            return
        st.session_state[SK_SELECTED_DOCUMENT_ID] = None

    all_clients = clients.list_clients(include_inactive=False)
    if not all_clients:
        st.info("No clients onboarded yet — add one from the Clients tab first.")
        return
    labels = {c["client_id"]: c["legal_name"] for c in all_clients}
    current_client_id = st.session_state.get(SK_SELECTED_VAULT_CLIENT_ID)
    if current_client_id not in labels:
        current_client_id = all_clients[0]["client_id"]
    chosen = st.selectbox(
        "Client", list(labels.keys()), format_func=lambda cid: labels[cid],
        index=list(labels.keys()).index(current_client_id), key="f3_vault_client_picker",
    )
    st.session_state[SK_SELECTED_VAULT_CLIENT_ID] = chosen
    render_scoped_vault(chosen, current_user)


# ---------------------------------------------------------------------------
# Scoped vault — reusable by F2's Documents tab retrofit
# ---------------------------------------------------------------------------


def render_scoped_vault(client_id: int, current_user: dict[str, Any], *, embedded: bool = False) -> None:
    """Client-scoped document list + upload. ``embedded=True`` when
    called from F2's profile Documents tab (retrofit) — suppresses the
    standalone-tab chrome that would otherwise duplicate F2's own header.

    Widget keys are namespaced by context: the standalone Documents tab and
    F2's embedded profile tab can both render the same client's scoped vault
    within a single Streamlit run (all tabs execute), so a bare client_id
    key would collide (StreamlitDuplicateElementKey)."""
    can_upload = auth.has_permission(current_user, "documents.upload")
    ctx = "f2emb" if embedded else "f3tab"

    if can_upload:
        _render_upload_widget(client_id, current_user, ctx=ctx)

    filt_l, filt_m, filt_r = st.columns([2, 2, 3])
    with filt_l:
        doc_type_filter = st.selectbox(
            "Document type", ["All"] + documents.DOC_TYPES, key=f"f3_filter_type_{ctx}_{client_id}",
        )
    with filt_m:
        period_filter = st.text_input("Period", key=f"f3_filter_period_{ctx}_{client_id}", placeholder="e.g. 2026-08")
    with filt_r:
        search = st.text_input("Search filename / type", key=f"f3_filter_search_{ctx}_{client_id}")

    docs = documents.list_documents(
        client_id=client_id,
        doc_type=None if doc_type_filter == "All" else doc_type_filter,
        period=period_filter or None,
        search=search or None,
    )

    if not docs:
        st.info("No documents filed for this client yet.")
    else:
        header = st.columns([3, 2, 2, 2, 1])
        for col, label in zip(header, ["File", "Type", "Period", "Status", ""]):
            col.markdown(
                f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>',
                unsafe_allow_html=True,
            )
        st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
        for d in docs:
            row = st.columns([3, 2, 2, 2, 1])
            with row[0]:
                if st.button(d.get("latest_filename") or f"Document #{d['document_id']}", key=f"f3_open_{ctx}_{d['document_id']}", type="tertiary"):
                    st.session_state[SK_SELECTED_DOCUMENT_ID] = d["document_id"]
                    st.rerun()
            with row[1]:
                st.write(d["doc_type"])
            with row[2]:
                st.write(d.get("period") or "—")
            with row[3]:
                if d["review_status"] == "pending_review":
                    render_confidence_badge("ai", pct=d.get("classification_pct"), label="Awaiting review")
                else:
                    st.caption("Filed")
            with row[4]:
                st.write("")
            st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    if not embedded:
        with st.expander("Recently deleted"):
            _render_recently_deleted(current_user, client_id=client_id)


def _render_upload_widget(client_id: int, current_user: dict[str, Any], *, ctx: str = "f3tab") -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Upload document</span>', unsafe_allow_html=True)
    period = st.text_input("Period (optional)", key=f"f3_upload_period_{ctx}_{client_id}", placeholder="e.g. 2026-08")
    uploaded = st.file_uploader(
        "File", key=f"f3_upload_file_{ctx}_{client_id}",
        type=sorted(documents.ALLOWED_EXTENSIONS),
        help="Images (JPG/PNG), documents (DOC/DOCX/PDF), spreadsheets (XLS/XLSX/CSV). No size cap.",
    )
    if uploaded is not None and st.button("Upload", key=f"f3_upload_btn_{ctx}_{client_id}", type="primary"):
        try:
            result = documents.upload_document(
                client_id=client_id, filename=uploaded.name, file_bytes=uploaded.getvalue(),
                period=period or None, actor=current_user["username"],
            )
            if result["routed_to_queue"]:
                st.warning(
                    f"Couldn't classify with confidence — routed to the Unified Review Queue. "
                    f"Best guess: {result['doc_type']}."
                )
                render_confidence_badge("ai", pct=result["confidence"], label=f"AI — {result['confidence']}%")
            else:
                st.success(f"Filed as {result['doc_type']}.")
                render_confidence_badge("ai", pct=result["confidence"])
        except documents.DocumentError as exc:
            st.error(str(exc))
    st.markdown('</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Document detail — metadata + "Used in" panel + version history +
# reassign + soft-delete
# ---------------------------------------------------------------------------


def _render_document_detail(doc: dict[str, Any], current_user: dict[str, Any]) -> None:
    if st.button("← Back to vault"):
        st.session_state[SK_SELECTED_DOCUMENT_ID] = None
        st.rerun()

    client = clients.get_client(doc["client_id"])
    versions = documents.list_versions(doc["document_id"])
    latest = versions[0] if versions else None

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(
        f'<span class="setu-card-title" style="font-size:1.05rem;">{latest["filename"] if latest else "(no version)"}</span>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Client: {client['legal_name'] if client else '—'} · Type: {doc['doc_type']} · "
        f"Period: {doc.get('period') or '—'}"
    )
    if doc["review_status"] == "pending_review":
        render_confidence_badge("ai", pct=doc.get("classification_pct"), label="Awaiting classification review")
    if doc.get("is_deleted"):
        render_accent_pill("Deleted — see Recently deleted to restore")
    st.markdown('</div>', unsafe_allow_html=True)

    # "Used in" panel — structurally real, inspectable now even though
    # empty until Modules 2/3/5 exist.
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Used in</span>', unsafe_allow_html=True)
    links = documents.used_in(doc["document_id"])
    if not links:
        st.caption("Not yet used in any record.")
    else:
        for link in links:
            st.write(f"- {link['target_label']}")
    st.markdown('</div>', unsafe_allow_html=True)

    # Version history.
    with st.expander(f"Version history ({len(versions)})", expanded=False):
        for v in versions:
            st.write(f"v{v['version_number']} · {v['filename']} · {v['uploaded_by']} · {v['uploaded_at']}")
        if auth.has_permission(current_user, "documents.upload"):
            new_file = st.file_uploader(
                "Upload new version", key=f"f3_newver_{doc['document_id']}", type=sorted(documents.ALLOWED_EXTENSIONS),
            )
            if new_file is not None and st.button("Save new version", key=f"f3_newver_btn_{doc['document_id']}"):
                try:
                    documents.add_version(
                        doc["document_id"], filename=new_file.name, file_bytes=new_file.getvalue(),
                        actor=current_user["username"],
                    )
                    st.success("New version saved — evidence links and history preserved.")
                    st.rerun()
                except documents.DocumentError as exc:
                    st.error(str(exc))

    # Reassign misfiled document — reuses F2's reason-capture pattern.
    with st.expander("Reassign to a different client"):
        _render_reassign_form(doc, current_user)

    # Soft-delete (Partner/Manager) — Delete vs Deactivate (3.2, Delete
    # side only) + Disabled-with-inline-reason (3.4) for below-tier roles.
    _render_soft_delete_control(doc, current_user)


def _render_reassign_form(doc: dict[str, Any], current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "documents.reassign"):
        render_inline_reason("Requires Manager-level permission or above.")
        return

    all_clients = clients.list_clients(include_inactive=False)
    labels = {c["client_id"]: c["legal_name"] for c in all_clients}
    key_val = f"f3_reassign_target_{doc['document_id']}"
    key_reason = f"f3_reassign_reason_{doc['document_id']}"

    new_client_id = st.selectbox(
        "Correct client", list(labels.keys()), format_func=lambda cid: labels[cid],
        index=list(labels.keys()).index(doc["client_id"]) if doc["client_id"] in labels else 0,
        key=key_val,
    )
    is_dirty = new_client_id != doc["client_id"]
    reason = ""
    if is_dirty:
        reason = st.text_input(
            "Reason for this reassignment (required before saving)", key=key_reason,
            placeholder="e.g. uploaded under the wrong client during intake",
        )
        save_disabled = not reason.strip()
        if st.button("Save reassignment", key=f"f3_reassign_save_{doc['document_id']}", disabled=save_disabled, type="primary"):
            try:
                documents.reassign_document(
                    doc["document_id"], new_client_id, reason=reason, actor=current_user["username"],
                )
                st.success("Document reassigned — version history and evidence links preserved.")
                st.rerun()
            except documents.DocumentError as exc:
                st.error(str(exc))
        if save_disabled:
            render_inline_reason("A reason is required before this change can be saved.")


def _render_soft_delete_control(doc: dict[str, Any], current_user: dict[str, Any]) -> None:
    can_delete = auth.has_permission(current_user, "documents.delete")
    st.caption("Document actions")
    if doc.get("is_deleted"):
        if st.button("Restore", key=f"f3_restore_{doc['document_id']}", disabled=not can_delete, width="stretch"):
            documents.restore_document(doc["document_id"], actor=current_user["username"])
            st.rerun()
        if not can_delete:
            render_inline_reason("Requires Partner or Manager")
        return

    key_reason = f"f3_delete_reason_{doc['document_id']}"
    if can_delete:
        with st.popover("Delete", use_container_width=False):
            reason = st.text_input("Reason for deletion (optional)", key=key_reason)
            if st.button("Confirm delete", key=f"f3_delete_confirm_{doc['document_id']}", type="primary"):
                documents.soft_delete_document(doc["document_id"], reason=reason or None, actor=current_user["username"])
                st.success("Document soft-deleted — recoverable from Recently deleted.")
                st.rerun()
    else:
        st.button("Delete", key=f"f3_delete_disabled_{doc['document_id']}", disabled=True, width="stretch")
        render_inline_reason("Requires Partner or Manager")


# ---------------------------------------------------------------------------
# Unified Review Queue — headline → grouped → detail (3.6)
# ---------------------------------------------------------------------------


def _render_review_queue(current_user: dict[str, Any]) -> None:
    can_resolve = auth.has_permission(current_user, "documents.review_queue.resolve")
    entries = documents.list_queue("pending")

    st.markdown(
        f'<div class="setu-card"><span style="font-size:34px;font-weight:700;color:var(--setu-text-primary);">'
        f'{len(entries)}</span> '
        f'<span style="color:var(--setu-text-secondary);font-size:13px;">items awaiting resolution</span></div>',
        unsafe_allow_html=True,
    )

    if not entries:
        st.info("Nothing waiting on human review right now.")
        return

    # Grouped by reason (open-ended text, not hardcoded to a single tag —
    # F3-AI's future "couldn't map" tag lands in this same grouping).
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_reason.setdefault(e["reason"], []).append(e)

    for reason, rows in by_reason.items():
        render_accent_pill(f'"{reason}" — {len(rows)}')
        for e in rows:
            with st.expander(f"Document #{e['document_id']} · {e.get('doc_type', '—')} · {reason}"):
                client = clients.get_client(e["client_id"]) if e.get("client_id") else None
                st.caption(f"Client: {client['legal_name'] if client else '—'} · Period: {e.get('period') or '—'}")
                if not can_resolve:
                    st.caption("Requires Manager-level permission or above to resolve.")
                    continue
                corrected_type = st.selectbox(
                    "Correct document type", ["(keep as-is)"] + documents.DOC_TYPES, key=f"f3_queue_type_{e['entry_id']}",
                )
                notes = st.text_input("Resolution notes (optional)", key=f"f3_queue_notes_{e['entry_id']}")
                if st.button("Resolve", key=f"f3_queue_resolve_{e['entry_id']}", type="primary"):
                    documents.resolve_queue_entry(
                        e["entry_id"],
                        resolved_doc_type=None if corrected_type == "(keep as-is)" else corrected_type,
                        notes=notes or None, actor=current_user["username"],
                    )
                    st.success("Resolved and filed.")
                    st.rerun()


# ---------------------------------------------------------------------------
# Recently deleted — Partner/Manager recovery screen
# ---------------------------------------------------------------------------


def _render_recently_deleted(current_user: dict[str, Any], *, client_id: Optional[int] = None) -> None:
    can_delete = auth.has_permission(current_user, "documents.delete")
    rows = documents.list_recently_deleted(client_id=client_id)
    if not rows:
        st.info("No deleted documents.")
        return

    header = st.columns([3, 2, 2, 2, 1])
    for col, label in zip(header, ["File", "Type", "Deleted at", "Deleted by", ""]):
        col.markdown(
            f'<span style="color:var(--setu-text-secondary);font-size:12px;font-weight:600;">{label}</span>',
            unsafe_allow_html=True,
        )
    st.markdown('<hr style="margin:4px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    for d in rows:
        row = st.columns([3, 2, 2, 2, 1])
        with row[0]:
            st.write(d.get("latest_filename") or f"Document #{d['document_id']}")
        with row[1]:
            st.write(d["doc_type"])
        with row[2]:
            st.write(d.get("deleted_at") or "—")
        with row[3]:
            st.write(d.get("deleted_by") or "—")
        with row[4]:
            if st.button("Restore", key=f"f3_recdel_restore_{d['document_id']}", disabled=not can_delete):
                documents.restore_document(d["document_id"], actor=current_user["username"])
                st.rerun()
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
