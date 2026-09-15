"""C2 UI — Portal Connect & Filing.

Design implementation notes (Setu_Phase2_UI_Foundation_Build_Prompt +
C2's design table §7 — no new component is introduced):
- Filing calendar: full-width table/list on the page-background token,
  grouped by client. Each row's due-date urgency renders via the
  confidence badge's ADAPTED pill form (solid vs tinted, by days-until-due
  tier) — the one Foundation-sanctioned exception, per the Foundation doc.
- Prepare filing package: standard card + generic file-upload control
  (any file this phase). "+ New filing" is the view's one
  informational-accent primary action.
- Send action: visible to Manager+ only; below tier it renders disabled
  with its inline reason (3.4): "Requires Manager or above." The confirm
  dialog states the action is irreversible within Setu.
- Outcome states: Acknowledged = resolved-green; Failed = escalated-coral
  + escalation banner; Ambiguous = its own distinct tinted-amber state
  (near-emergency, never folded into Failed/Pending). Reuses semantic
  tokens directly, not a new named component.
- Acknowledgement capture: upload routes through F3; a confirmation
  checkbox is required before the Filing Record is marked complete.
- API/manual toggle: greyed-out with an inline label ("Not yet available —
  coming once portal credentials are configured"). Manual upload stays
  available regardless of toggle state.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

import streamlit as st

from src.auth import service as auth
from src.clients import service as clients
from src.documents import service as documents
from src.filing import service as filing
from src.ui.theme import (
    render_inline_reason,
    render_placeholder_badge,
    render_warning_banner,
)

_HUB_SECTIONS = ["Filing Calendar", "Filings", "Connection & Sources"]

SK_SELECTED_FILING_ID = "_filing_selected_filing_id"

# Urgency tiers (confidence badge 3.1, adapted to days-until-due per the
# Foundation's locked usage note). Overdue is the near-emergency danger
# state; due-soon is the tinted-amber urgency; comfortable is solid.
_URGENCY_OVERDUE_DAYS = 0       # due today or past -> overdue
_URGENCY_SOON_DAYS = 3          # within 3 days -> tinted amber

_OUTCOME_LABEL = {
    "acknowledged": ("rule", "Acknowledged"),
    "failed": ("danger", "Failed"),
    "ambiguous": ("ai", "Ambiguous — verify manually"),
}

_SOURCE_LABELS = {"gst_portal": "GST Portal", "traces": "TRACES"}


def render_filing_tab(current_user: dict[str, Any]) -> None:
    if not auth.has_permission(current_user, "filing.view"):
        st.warning("You don't have access to Portal Connect & Filing.")
        return

    st.subheader("Portal Connect & Filing")
    st.caption("Filing calendar, explicit Send, and acknowledgement capture.")

    selected_id = st.session_state.get(SK_SELECTED_FILING_ID)
    if selected_id is not None:
        rec = filing.get_filing(selected_id)
        if rec is not None:
            _render_filing_detail(rec, current_user)
            return
        st.session_state[SK_SELECTED_FILING_ID] = None

    section = st.radio("Filing section", _HUB_SECTIONS, horizontal=True, label_visibility="collapsed")

    if section == "Filing Calendar":
        _render_calendar(current_user)
    elif section == "Connection & Sources":
        _render_sources(current_user)
    else:
        _render_filings_hub(current_user)


# ---------------------------------------------------------------------------
# Filing calendar
# ---------------------------------------------------------------------------


def _render_calendar(current_user: dict[str, Any]) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Filing Calendar</span>', unsafe_allow_html=True)
    st.caption("Statutory due dates per client per return type, sourced from C1.")

    all_entries = filing.list_calendar()
    if not all_entries:
        st.info("No calendar entries yet. Build the calendar for a client below to materialize due dates.")
    else:
        _render_calendar_table(all_entries)

    # Materialize / refresh a client's calendar.
    st.markdown('<hr style="margin:12px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Build / refresh calendar</span>', unsafe_allow_html=True)
    client_options = {c["legal_name"]: c["client_id"] for c in clients.list_clients(include_inactive=False)}
    if not client_options:
        st.caption("Create a client (Clients tab) first.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    sel_name = st.selectbox("Client", list(client_options.keys()))
    period = st.text_input("Period", value=_default_period(), help="YYYY-MM (monthly/quarterly) or YYYY (annual).")
    if st.button("Build calendar", type="primary"):
        try:
            filing.build_calendar(client_options[sel_name], periods=[period], actor=current_user["username"])
            st.success("Calendar built.")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    st.markdown('</div>', unsafe_allow_html=True)


def _default_period() -> str:
    today = date.today()
    return f"{today.year}-{today.month:02d}"


def _render_calendar_table(entries: list[dict[str, Any]]) -> None:
    client_names = {c["client_id"]: c["legal_name"] for c in clients.list_clients(include_inactive=True)}
    # Group by client.
    by_client: dict[int, list[dict[str, Any]]] = {}
    for e in entries:
        by_client.setdefault(e["client_id"], []).append(e)

    for client_id, rows in by_client.items():
        name = client_names.get(client_id, f"Client #{client_id}")
        st.markdown(
            f'<span style="color:var(--setu-text-primary);font-weight:700;">{name}</span>',
            unsafe_allow_html=True,
        )
        for e in sorted(rows, key=lambda r: r["due_date"]):
            c1, c2, c3 = st.columns([3, 2, 2])
            with c1:
                st.write(f"**{e['obligation']}** — {e['period']}")
            with c2:
                st.write(e["due_date"])
            with c3:
                _render_urgency_pill(e.get("days_until_due"))
            st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


def _render_urgency_pill(days_until_due: Optional[int]) -> None:
    """Confidence badge (3.1) ADAPTED to days-until-due: the same
    solid-vs-tinted pill language applied to urgency rather than
    rule-vs-AI — the one Foundation-sanctioned exception."""
    if days_until_due is None:
        st.markdown('<span class="setu-token-pill placeholder">No due date</span>', unsafe_allow_html=True)
        return
    if days_until_due < _URGENCY_OVERDUE_DAYS:
        css, text = "danger", f"Overdue by {-days_until_due} day(s)"
    elif days_until_due <= _URGENCY_SOON_DAYS:
        css, text = "ai", f"Due in {days_until_due} day(s)"
    else:
        css, text = "rule", f"Due in {days_until_due} day(s)"
    st.markdown(f'<span class="setu-token-pill {css}">{text}</span>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Filings hub (headline -> grouped -> detail)
# ---------------------------------------------------------------------------


def _render_filings_hub(current_user: dict[str, Any]) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Filings</span>', unsafe_allow_html=True)

    records = filing.list_filings()
    open_count = sum(1 for r in records if r["status"] in ("prepared", "sent"))

    st.markdown(
        f'<span style="font-size:34px;font-weight:700;color:var(--setu-text-primary);">{open_count}</span> '
        f'<span style="color:var(--setu-text-secondary);font-size:13px;">open filing(s)</span>',
        unsafe_allow_html=True,
    )

    st.markdown('<hr style="margin:12px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    _render_new_filing_form(current_user)

    st.markdown('<hr style="margin:12px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    if not records:
        st.info("No filings yet.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    client_names = {c["client_id"]: c["legal_name"] for c in clients.list_clients(include_inactive=True)}
    for r in records:
        name = client_names.get(r["client_id"], f"Client #{r['client_id']}")
        c1, c2, c3 = st.columns([4, 2, 2])
        with c1:
            st.write(f"**{r['obligation']}** — {r['period']} · {name}")
        with c2:
            css, label = _OUTCOME_LABEL.get(r["status"], ("placeholder", r["status"]))
            st.markdown(f'<span class="setu-token-pill {css}">{label}</span>', unsafe_allow_html=True)
        with c3:
            if st.button("Open", key=f"filing_open_{r['filing_id']}"):
                st.session_state[SK_SELECTED_FILING_ID] = r["filing_id"]
                st.rerun()
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)


def _render_new_filing_form(current_user: dict[str, Any]) -> None:
    st.markdown('<span class="setu-card-title">+ New filing</span>', unsafe_allow_html=True)
    st.caption("Prepare a filing package (generic manual upload this phase).")

    client_options = {c["legal_name"]: c["client_id"] for c in clients.list_clients(include_inactive=False)}
    if not client_options:
        st.caption("Create a client first.")
        return

    obligations = [o["obligation"] for o in _list_obligations()]
    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        sel_name = st.selectbox("Client", list(client_options.keys()), key="filing_new_client")
    with c2:
        obligation = st.selectbox("Return type", obligations or ["GSTR-3B"], key="filing_new_obligation")
    with c3:
        period = st.text_input("Period", value=_default_period(), key="filing_new_period")

    package_file = st.file_uploader(
        "Filing package", key="filing_new_package",
        help="Any file this phase — retrofit to Module 2's GSTR-3B assembly later.",
    )

    if st.button("Prepare filing package", type="primary", key="filing_new_prepare"):
        if package_file is None:
            st.error("Upload a filing package first.")
        else:
            try:
                client_id = client_options[sel_name]
                package_doc_id = None
                # Store the package in F3's repository (manual upload path).
                try:
                    result = documents.upload_document(
                        client_id=client_id, filename=package_file.name,
                        file_bytes=package_file.getvalue(), period=period,
                        actor=current_user["username"],
                    )
                    package_doc_id = result["document_id"]
                except documents.DocumentError as exc:
                    st.error(str(exc))
                    st.markdown('</div>', unsafe_allow_html=True)
                    return
                filing_id = filing.prepare_filing(
                    client_id=client_id, obligation=obligation, period=period,
                    package_document_id=package_doc_id, actor=current_user["username"],
                )
                st.success(f"Filing package prepared (filing #{filing_id}).")
                st.session_state[SK_SELECTED_FILING_ID] = filing_id
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))


def _list_obligations() -> list[dict[str, Any]]:
    from src.rules import service as rules

    return rules.list_statutory_due_dates()


# ---------------------------------------------------------------------------
# Filing detail — send / outcome / acknowledgement
# ---------------------------------------------------------------------------


def _render_filing_detail(rec: dict[str, Any], current_user: dict[str, Any]) -> None:
    if st.button("← Back to Filings"):
        st.session_state[SK_SELECTED_FILING_ID] = None
        st.rerun()

    role = current_user.get("role_name", "")
    can_send_bool, reason = filing.can_send(role)

    client = clients.get_client(rec["client_id"])
    client_name = client["legal_name"] if client else f"Client #{rec['client_id']}"

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(
        f'<span class="setu-card-title">{rec["obligation"]} — {rec["period"]}</span> '
        f'<span style="color:var(--setu-text-secondary);font-size:12px;">{client_name}</span>',
        unsafe_allow_html=True,
    )
    css, label = _OUTCOME_LABEL.get(rec["status"], ("placeholder", rec["status"]))
    st.markdown(f'<span class="setu-token-pill {css}">{label}</span>', unsafe_allow_html=True)

    if rec.get("package_document_id"):
        st.caption(f"Package: document #{rec['package_document_id']}")
    if rec.get("outcome_note"):
        st.caption(f"Outcome note: {rec['outcome_note']}")

    # Escalation banner for failed/ambiguous.
    if rec["status"] in ("failed", "ambiguous"):
        render_warning_banner(
            "This filing escalated to **Manager + Partner** "
            f"({rec['status']}). A correction is a fresh filing through the normal government process."
        )

    st.markdown('<hr style="margin:12px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)

    if rec["status"] == "prepared":
        _render_send_control(rec, current_user, can_send_bool, reason)
    elif rec["status"] == "sent":
        _render_outcome_control(rec, current_user)
    elif rec["status"] in ("failed", "ambiguous"):
        _render_acknowledgement_control(rec, current_user, allow_resolution=True)
    elif rec["status"] == "acknowledged":
        st.success("Acknowledged — this filing is complete.")

    _render_audit_trail(rec)
    st.markdown('</div>', unsafe_allow_html=True)


def _render_send_control(rec: dict[str, Any], current_user: dict[str, Any], can_send_bool: bool, reason: Optional[str]) -> None:
    st.markdown('<span class="setu-card-title">Send</span>', unsafe_allow_html=True)

    if not can_send_bool:
        st.button("Send filing", disabled=True, key=f"filing_send_{rec['filing_id']}")
        render_inline_reason(reason or "Requires Manager or above.")
        return

    st.write("Sending a filing is **irreversible within Setu** — a correction is a fresh filing.")
    confirm = st.checkbox("I understand this Send is irreversible within Setu.", key=f"filing_send_confirm_{rec['filing_id']}")
    if st.button("Send filing", disabled=not confirm, key=f"filing_send_btn_{rec['filing_id']}"):
        try:
            filing.send_filing(rec["filing_id"], actor=current_user["username"], actor_role=current_user.get("role_name", ""))
            st.success("Filing sent.")
            st.rerun()
        except filing.FilingError as exc:
            st.error(str(exc))


def _render_outcome_control(rec: dict[str, Any], current_user: dict[str, Any]) -> None:
    st.markdown('<span class="setu-card-title">Record outcome</span>', unsafe_allow_html=True)
    st.caption("What did the portal report back?")

    outcome = st.radio(
        "Outcome", ["acknowledged", "ambiguous", "failed"],
        format_func=lambda x: {"acknowledged": "Acknowledged", "ambiguous": "Ambiguous", "failed": "Failed"}[x],
        horizontal=True, key=f"filing_outcome_{rec['filing_id']}",
    )
    note = st.text_input("Outcome note", key=f"filing_outcome_note_{rec['filing_id']}")

    if outcome == "ambiguous":
        render_warning_banner(
            "**Ambiguous** is a near-emergency state: confirm manually whether the portal actually "
            "received it. This escalates to Manager + Partner."
        )

    if st.button("Record outcome", key=f"filing_outcome_btn_{rec['filing_id']}"):
        try:
            filing.record_outcome(
                rec["filing_id"], status=outcome, outcome_note=note or None,
                actor=current_user["username"],
            )
            st.success("Outcome recorded.")
            st.rerun()
        except filing.FilingError as exc:
            st.error(str(exc))


def _render_acknowledgement_control(rec: dict[str, Any], current_user: dict[str, Any], allow_resolution: bool) -> None:
    st.markdown('<span class="setu-card-title">Acknowledgement capture</span>', unsafe_allow_html=True)
    st.caption("Upload the portal's receipt through F3 as linked evidence, then confirm.")

    ack_file = st.file_uploader(
        "Acknowledgement receipt (PDF/screenshot)", key=f"filing_ack_{rec['filing_id']}",
    )
    confirm = st.checkbox(
        "I confirm the portal receipt is attached and this filing is acknowledged.",
        key=f"filing_ack_confirm_{rec['filing_id']}",
    )
    if st.button("Confirm acknowledgement", disabled=not (ack_file is not None and confirm), key=f"filing_ack_btn_{rec['filing_id']}"):
        try:
            result = documents.upload_document(
                client_id=rec["client_id"], filename=ack_file.name,
                file_bytes=ack_file.getvalue(), period=rec["period"],
                actor=current_user["username"],
            )
            filing.confirm_acknowledgement(
                rec["filing_id"], acknowledgement_document_id=result["document_id"],
                actor=current_user["username"],
            )
            st.success("Acknowledgement captured — filing marked complete.")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))


def _render_audit_trail(rec: dict[str, Any]) -> None:
    st.markdown('<hr style="margin:12px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Audit trail</span>', unsafe_allow_html=True)
    rows = filing.list_audit_log(rec["filing_id"])
    if not rows:
        st.caption("No audit entries.")
        return
    for r in rows:
        st.markdown(
            f'<div style="border-bottom:1px solid var(--setu-border);padding:6px 2px;">'
            f'<span style="color:var(--setu-text-primary);font-size:13px;">{r["action"]}</span> '
            f'<span style="color:var(--setu-text-secondary);font-size:12px;">{r["actor"]} · {r.get("detail") or ""}</span>'
            f'<span style="float:right;color:var(--setu-text-secondary);font-size:11px;">{r["created_at"]}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Connection & sources — API/manual toggle (dormant until C4 credentials)
# ---------------------------------------------------------------------------


def _render_sources(current_user: dict[str, Any]) -> None:
    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown('<span class="setu-card-title">Connection & Sources</span>', unsafe_allow_html=True)
    st.caption(
        "Per-client, per-source API vs manual mode. API mode is dormant until "
        "portal credentials are configured in C4; manual upload always remains available."
    )

    client_options = {c["legal_name"]: c["client_id"] for c in clients.list_clients(include_inactive=False)}
    if not client_options:
        st.caption("Create a client first.")
        st.markdown('</div>', unsafe_allow_html=True)
        return

    sel_name = st.selectbox("Client", list(client_options.keys()), key="filing_src_client")
    client_id = client_options[sel_name]

    for source in filing.SOURCE_KEYS:
        label = _SOURCE_LABELS.get(source, source)
        availability = filing.source_availability(client_id, source)

        st.markdown('<hr style="margin:10px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)
        st.markdown(f"**{label}**")
        if availability["api_available"]:
            mode = st.radio(
                f"{label} mode", ["manual", "api"],
                index=0 if availability["mode"] == "manual" else 1,
                horizontal=True, key=f"filing_toggle_{client_id}_{source}",
                label_visibility="collapsed",
            )
            if st.button("Save mode", key=f"filing_toggle_save_{client_id}_{source}"):
                filing.set_toggle(client_id, source, mode, actor=current_user["username"])
                st.success("Mode saved.")
                st.rerun()
        else:
            # Greyed-out toggle + inline "coming once credentials configured".
            st.radio(
                f"{label} mode", ["manual", "api"], index=0,
                horizontal=True, disabled=True, key=f"filing_toggle_disabled_{client_id}_{source}",
                label_visibility="collapsed",
            )
            render_inline_reason(availability["unavailable_reason"])
            st.caption("Manual upload remains available regardless of this toggle.")

    st.markdown('</div>', unsafe_allow_html=True)
