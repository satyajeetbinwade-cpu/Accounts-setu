"""F1 retrofit item 4 — role-aware post-login landing shell.

Every role lands on a legible "coming soon" shell listing the eventual
module areas, each greyed out with the platform's existing "coming with
[Module X]" convention. A one-line, role-aware description sits under
each badge.

Admin is the exception — they land directly in the Identity & Access
console (which lives in the Admin tab; the Home tab still lists the
future module areas for reference).

End-Client (shared login) sees the same pattern, scoped to their eventual
Information Request / document vault view — EXCEPT for contact-edit
(F2 retrofit: real now, "swap in the real contact-edit form — End-Client's
one working F2 capability — instead of a fully greyed placeholder") and
basic document upload (F3 retrofit, second incremental upgrade: a real
upload into the End-Client's scoped vault, even without Module 1's
request-tracking context yet).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.ui.theme import render_confidence_badge, render_placeholder_badge

# The eventual module areas, in the canonical build-order grouping. Each
# entry: (display name, "coming with" label).
# F3 (Document & Data Repository), F3-AI (Smart Document Ingestion), F5
# (Data Integrity & Validation), Module 2 (Reconciliation Engine) and
# Module 8 (Action Center) have all been removed from this list — all five
# are now live (see the "Documents" / "Smart Ingestion" / "Data Integrity" /
# "Reconciliation" / "Action Center" tabs). "System Settings" stays listed
# under its stale "Module C3" label pending a separate cleanup pass — out of
# scope for this change.
_MODULE_AREAS = [
    ("System Settings", "Module C3"),
]

# Role-aware one-liners describing what will appear in each area once live.
_ROLE_LINES = {
    "Senior Accountant": "Your assigned-client review queue will appear here once the Action Center is live.",
    "Article-Trainee": "Your assigned-client work queue will appear here once the Action Center is live.",
    "Manager": "Your team's exception queue and review dashboards will appear here once modules go live.",
    "Partner": "Firm-wide oversight dashboards will appear here once modules go live.",
    "End-Client": "Your Information Requests and document vault will appear here once built.",
    "Admin": "You'll manage configuration and access for these areas from the Identity & Access console.",
}


def _role_line(role_name: str) -> str:
    return _ROLE_LINES.get(role_name, "This module area will become visible here once it's live.")


def render_home_tab(user: dict[str, Any]) -> None:
    role = user.get("role_name", "")
    line = _role_line(role)

    st.subheader(f"Welcome, {user['display_name']}")

    if role == "Admin":
        st.info(
            "You're an administrator \u2014 head to the **Admin** tab for the "
            "Identity & Access console. The eventual module areas are listed below "
            "for reference as they come online."
        )
    else:
        st.write("Here's what's coming to your workspace as the platform is built out.")

    if role != "End-Client":
        st.success(
            "\U0001F4C1 **Client Profile & Master Data (F2)** is now live \u2014 see the **Clients** tab."
        )
        st.success(
            "\U0001F5C4\uFE0F **Document & Data Repository (F3)** is now live \u2014 see the **Documents** tab."
        )
        st.success(
            "\U0001F504 **Reconciliation Engine (Module 2, 2A/2B/2C)** is now live \u2014 "
            "see the **Reconciliation** tab."
        )
        st.success(
            "\U0001F9FE **Invoice Extraction & Digitalization (F3-B)** is now live \u2014 "
            "see the **Invoice Extraction** tab."
        )
    else:
        _render_end_client_contact_form(user)
        _render_end_client_document_upload(user)

    for name, coming_with in _MODULE_AREAS:
        with st.container():
            st.markdown(
                f'<div class="setu-card">'
                f'<span class="setu-card-title">{name}</span> '
                f'<span class="setu-token-pill placeholder">Coming with {coming_with}</span>'
                f'<div style="margin-top:6px;color:var(--setu-text-secondary);font-size:12px;">{line}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


def _render_end_client_contact_form(user: dict[str, Any]) -> None:
    """F2 retrofit of F1's End-Client landing shell: this is End-Client's
    one working F2 capability today \u2014 editing their own internal
    contact-directory entry \u2014 wired for real instead of a fully greyed
    placeholder. Everything else on this tab remains the "coming soon"
    shell.
    """
    from src.clients import service as clients

    end_client_ref = user.get("end_client_ref")
    if not end_client_ref:
        st.info(
            "Your account isn't linked to a client profile yet \u2014 ask an "
            "Admin to complete End-Client login provisioning from Client Profiles."
        )
        return

    try:
        client_id = int(end_client_ref)
    except (TypeError, ValueError):
        st.info("Your account's client link looks invalid \u2014 contact an Admin.")
        return

    client = clients.get_client(client_id)
    if client is None:
        st.info("Your linked client profile could not be found \u2014 contact an Admin.")
        return

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(f'<span class="setu-card-title">Your contact details \u2014 {client["legal_name"]}</span>', unsafe_allow_html=True)
    contacts = clients.list_contacts(client_id)
    if not contacts:
        st.caption("No contact record on file yet \u2014 ask your engagement team to add one.")
    for contact in contacts:
        with st.container(border=True):
            name = st.text_input("Name", value=contact["name"], key=f"ec_name_{contact['contact_id']}")
            email = st.text_input("Email", value=contact.get("email") or "", key=f"ec_email_{contact['contact_id']}")
            phone = st.text_input("Phone", value=contact.get("phone") or "", key=f"ec_phone_{contact['contact_id']}")
            if st.button("Save", key=f"ec_save_{contact['contact_id']}"):
                clients.update_contact_field(contact["contact_id"], "name", name, actor=user["username"])
                clients.update_contact_field(contact["contact_id"], "email", email, actor=user["username"])
                clients.update_contact_field(contact["contact_id"], "phone", phone, actor=user["username"])
                st.success("Contact details updated.")
                st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def _render_end_client_document_upload(user: dict[str, Any]) -> None:
    """F3 retrofit of F1's End-Client landing shell (second incremental
    upgrade, after F2's contact-edit form): basic document upload into
    the End-Client's own scoped vault, even without Module 1's
    request-tracking context yet. Uses F3's real upload path — not a
    placeholder \u2014 but keeps the surface minimal (upload + own recent
    files only, no review-queue/reassign/delete UI, which stay
    Manager+ concerns).
    """
    from src.clients import service as clients
    from src.documents import service as documents

    end_client_ref = user.get("end_client_ref")
    if not end_client_ref:
        return
    try:
        client_id = int(end_client_ref)
    except (TypeError, ValueError):
        return
    client = clients.get_client(client_id)
    if client is None:
        return

    st.markdown('<div class="setu-card">', unsafe_allow_html=True)
    st.markdown(f'<span class="setu-card-title">Upload a document \u2014 {client["legal_name"]}</span>', unsafe_allow_html=True)
    uploaded = st.file_uploader(
        "File", key="ec_doc_upload", type=sorted(documents.ALLOWED_EXTENSIONS),
        help="Images (JPG/PNG), documents (DOC/DOCX/PDF), spreadsheets (XLS/XLSX/CSV). No size cap.",
    )
    if uploaded is not None and st.button("Upload", key="ec_doc_upload_btn", type="primary"):
        try:
            result = documents.upload_document(
                client_id=client_id, filename=uploaded.name, file_bytes=uploaded.getvalue(),
                period=None, actor=user["username"],
            )
            if result["routed_to_queue"]:
                st.warning("Uploaded \u2014 routed to your engagement team's review queue for filing.")
            else:
                st.success(f"Uploaded and filed as {result['doc_type']}.")
        except documents.DocumentError as exc:
            st.error(str(exc))

    recent = documents.list_documents(client_id=client_id)[:5]
    if recent:
        st.caption("Recently uploaded:")
        for d in recent:
            st.write(f"- {d.get('latest_filename') or d['doc_type']}")
    st.markdown('</div>', unsafe_allow_html=True)