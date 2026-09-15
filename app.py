"""Setu PoC \u2014 reconciliation review app.

Disposable PoC UI: Streamlit only, no auth, no config editing, no file
upload. Calls into src/runner.py, src/queries.py and src/export.py for all
logic; this file and src/ui/* are presentation only.

Run with: streamlit run app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

from src import db
from src.auth import service as auth
from src.clients import service as clients
from src.settings import service as settings
from src.rules import service as rules
from src.c5 import service as c5
from src.vault import service as vault
from src.documents import service as documents
from src.ingestion_ai import service as ingestion_ai
from src.invoice_extract import service as invoice_extract
from src.f5 import service as f5
from src.filing import service as filing
from src.module2 import service as module2
from src.action_center import service as action_center
from src.f4 import service as f4
from src.ai_models import service as ai_models
from src.ui import state
from src.ui.auth_ui import (
    render_admin_tab,
    render_login_gate,
    render_logout_control,
    render_security_events_tab,
    render_session_timeout_warning,
)
from src.ui.client_tab import render_clients_tab
from src.ui.home_tab import render_home_tab
from src.ui.sidebar import render_sidebar
from src.ui.settings_tab import render_settings_tab
from src.ui.rules_tab import render_rules_tab
from src.ui.c5_tab import render_c5_tab
from src.ui.vault_ui import render_vault_tab
from src.ui.documents_tab import render_documents_tab
from src.ui.ingestion_ai_tab import render_ingestion_ai_tab
from src.ui.invoice_extract_tab import render_invoice_extract_tab
from src.ui.f5_tab import render_f5_tab
from src.ui.filing_tab import render_filing_tab
from src.ui.module2_tab import render_module2_tab
from src.ui.action_center_tab import render_action_center_tab
from src.ui.ai_models_tab import render_ai_models_tab
from src.ui.run_tab import render_run_tab
from src.ui.review_tab import render_review_tab
from src.ui.compare_tab import render_compare_tab
from src.ui.config_tab import render_config_tab
from src.ui.export_tab import render_export_tab
from src.ui.reconcile_flow import init_reconcile_flow, render_reconcile_flow
from src.ui.theme import inject_css

st.set_page_config(page_title="Setu Recon Review", layout="wide")

inject_css()

db.init_db()
auth.init_auth()
clients.init_clients()
settings.init_settings()
rules.init_rules()
c5.init_c5()
vault.init_vault()
documents.init_documents()
ingestion_ai.init_ingestion_ai()
invoice_extract.init_invoice_extract()
f5.init_f5()
filing.init_filing()
module2.init_module2()
f4.init_f4()
ai_models.init_ai_models()
state.init_state()

# F1 login gate — renders the sign-in form and halts the script (st.stop())
# until a session is established. Everything below only runs once signed in.
current_user = render_login_gate()
render_logout_control(current_user)

# F1 retrofit item 5: warn before the inactivity timeout fires.
render_session_timeout_warning()

# Seed the default client/period/recon context for the advanced screens. The
# guided Reconcile flow (Stage 1) is still the place it's actually chosen and
# confirmed; this only prevents the advanced screens (and the sidebar's
# context readout) from starting blank.
init_reconcile_flow(current_user)

render_sidebar(current_user)

def _build_nav(current_user: dict) -> dict[str, list[str]]:
    """The three top-level areas of the app.

    `reconcile` is the default landing experience — the guided five-stage
    flow that takes a user from source files to a finished report. Everything
    else is deliberately secondary:

      * `tools`  — the individual screens the flow orchestrates. They stay
        fully functional for deep dives and manual control, but they are no
        longer the first thing anyone sees or needs to touch.
      * `setup`  — firm-wide configuration. Not part of the day-to-day
        reconciliation job, so it doesn't compete for attention with it.

    Permission filtering is applied per area, exactly as the old flat tab list
    did, so a user only ever sees areas they can actually open.
    """
    tools = ["Home"]
    if auth.has_permission(current_user, "clients.profile.view"):
        tools.append("Clients")
    tools += ["Review", "Run", "Compare", "Export"]
    if auth.has_permission(current_user, "ingestion_ai.upload"):
        tools.append("Smart Ingestion")
    if auth.has_permission(current_user, "invoice_extract.upload"):
        tools.append("Invoice Extraction")
    if auth.has_permission(current_user, "module2.view"):
        tools.append("Reconciliation")
    if auth.has_permission(current_user, "action_center.view"):
        tools.append("Action Center")
    if auth.has_permission(current_user, "f5.view"):
        tools.append("Data Integrity")
    if auth.has_permission(current_user, "filing.view"):
        tools.append("Filing")

    setup = ["Config"]
    if auth.has_permission(current_user, "settings.view"):
        setup.append("Settings")
    if auth.has_permission(current_user, "rules.view"):
        setup.append("Rules")
    if auth.has_permission(current_user, "c5.view"):
        setup.append("AI Library")
    if auth.has_permission(current_user, "ingestion_ai.llm.manage"):
        setup.append("AI Models")
    if auth.has_permission(current_user, "vault.view"):
        setup.append("Security & Vault")
    if auth.has_permission(current_user, "documents.view"):
        setup.append("Documents")
    if auth.is_admin(current_user) or current_user.get("role_name") == "Partner":
        setup.append("Security")
    if auth.is_admin(current_user):
        setup.append("Admin")

    return {"Reconcile": ["Reconcile"], "All tools": tools, "Setup": setup}


def _render_area(area: str, name: str, current_user: dict) -> None:
    """Render one screen by name. Each branch calls the exact same render
    function the old flat tab list called — no screen's behaviour changes;
    only how it is reached does."""
    if name == "Reconcile":
        render_reconcile_flow(current_user)
    elif name == "Home":
        render_home_tab(current_user)
    elif name == "Clients":
        render_clients_tab(current_user)
    elif name == "Review":
        render_review_tab()
    elif name == "Run":
        render_run_tab()
    elif name == "Compare":
        render_compare_tab()
    elif name == "Export":
        render_export_tab()
    elif name == "Config":
        render_config_tab()
    elif name == "Settings":
        render_settings_tab(current_user)
    elif name == "Rules":
        render_rules_tab(current_user)
    elif name == "AI Library":
        render_c5_tab(current_user)
    elif name == "AI Models":
        render_ai_models_tab(current_user)
    elif name == "Security & Vault":
        render_vault_tab(current_user)
    elif name == "Documents":
        render_documents_tab(current_user)
    elif name == "Smart Ingestion":
        render_ingestion_ai_tab(current_user)
    elif name == "Invoice Extraction":
        render_invoice_extract_tab(current_user)
    elif name == "Data Integrity":
        render_f5_tab(current_user)
    elif name == "Filing":
        render_filing_tab(current_user)
    elif name == "Reconciliation":
        render_module2_tab(current_user)
    elif name == "Action Center":
        render_action_center_tab(current_user)
    elif name == "Security":
        render_security_events_tab(current_user)
    elif name == "Admin":
        render_admin_tab(current_user)


NAV = _build_nav(current_user)

# Cross-area handoff: a screen can request that a specific tab be opened
# (e.g. Module 8's Action Center routing into Module 2's exception detail).
# "_goto_tab" holds a screen name; we resolve which area owns it, select that
# area AND that tab, then clear the request. Both keys are written BEFORE the
# widgets that own them are created, which is what makes this legal.
_goto_tab = st.session_state.pop("_goto_tab", None)
_goto_area: str | None = None
if _goto_tab:
    for _area_name, _tabs in NAV.items():
        if _goto_tab in _tabs:
            _goto_area = _area_name
            st.session_state["_nav_area"] = _area_name
            st.session_state["_area_tabs"] = f"{_area_name}::{_goto_tab}"
            break

_area = st.radio(
    "Area",
    list(NAV.keys()),
    horizontal=True,
    key="_nav_area",
    label_visibility="collapsed",
)
if _area not in NAV:
    _area = "Reconcile"

if _area == "Reconcile":
    render_reconcile_flow(current_user)
else:
    area_tabs = NAV[_area]
    # The tabs widget's key carries the area, so switching areas starts on that
    # area's first screen instead of on a tab label that doesn't exist there.
    _area_tab_key = f"_area_tabs"
    _requested = st.session_state.get(_area_tab_key)
    if isinstance(_requested, str) and _requested.startswith(f"{_area}::"):
        st.session_state[_area_tab_key] = _requested.split("::", 1)[1]
    elif _requested not in area_tabs:
        st.session_state.pop(_area_tab_key, None)

    tabs = st.tabs(area_tabs, key=_area_tab_key, on_change="rerun")
    for tab, name in zip(tabs, area_tabs):
        with tab:
            _render_area(_area, name, current_user)
