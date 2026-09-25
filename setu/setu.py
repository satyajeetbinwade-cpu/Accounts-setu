"""Setu — Reflex app entry point.

Boots the framework-agnostic service layer (``src/*/service.py``), then
registers every route.

Run with: reflex run
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable so the app can reuse the existing src/*
# service layer and design tokens.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reflex as rx

from src import db
from src.auth import service as auth
from setu.foundation import tokens as t
from setu.state import (
    AdminState,
    AiModelsState,
    AuthState,
    C5State,
    ClientState,
    DashboardState,
    DocumentsState,
    F5State,
    F6State,
    FilingState,
    IngestionAiState,
    InvoiceExtractState,
    Module2State,
    Module8State,
    Phase1State,
    ReconcileState,
    ReportState,
    RulesState,
    SecurityState,
    SettingsState,
    VaultState,
)
from setu.views import pages
from setu.views.login import login_page

# --- Service layer bootstrap ------------------------------------------------
# Each init_* is idempotent (creates tables / seeds first-run data only), so
# repeated starts against the same db/poc.db are safe. Modules are added here
# as they're built.
db.init_db()
auth.init_auth()

from src.f6 import service as f6  # noqa: E402
from src.invoice_extract import service as invoice_extract  # noqa: E402
from src.ingestion_ai import service as ingestion_ai  # noqa: E402

f6.init_f6()
invoice_extract.init_invoice_extract()
# F3-AI's schema init also runs the additive migrations (validation_json,
# metadata_json, rate_matrix_json, dispositions_json, ingestion_path), which
# the corrective build's gate and review screen read.
ingestion_ai.init_ingestion_ai()


def _boot() -> None:
    """Runs on the app's own startup (before any page renders)."""
    pages  # referenced so linters keep the import in a real app


app = rx.App(
    style=t.STYLES,
    head_components=[
        rx.el.link(rel="preconnect", href="https://fonts.googleapis.com"),
        rx.el.link(rel="preconnect", href="https://fonts.gstatic.com", cross_origin=""),
        rx.el.link(
            rel="stylesheet",
            href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap",
        ),
        rx.el.style(t.global_css()),
    ],
)

# --- Routes -----------------------------------------------------------------
# The sign-in gate is its own full-page route (no shell). Everything else is
# guarded: an unauthenticated visitor on any protected route is redirected to
# /login by the client-side check in each page's on_load.

app.add_page(
    login_page,
    route="/login",
    title="Sign in · Setu",
    on_load=AuthState.check_session,
)

app.add_page(
    pages.dashboard_page,
    route="/",
    title="Setu",
    on_load=[AuthState.check_session, AuthState.require_auth, DashboardState.load],
)

for _route, _renderer in pages.all_routes():
    if _route == "/":
        continue
    # Every protected route runs the gate first so a deep link from a signed
    # out browser is sent to /login (not silently dropped on the dashboard).
    _on_load = [AuthState.check_session, AuthState.require_auth]
    if _route == "/admin":
        _on_load.append(AdminState.load)
    if _route == "/clients":
        _on_load.append(ClientState.load)
    if _route == "/settings":
        _on_load.append(SettingsState.load)
    if _route == "/ai-models":
        _on_load.append(AiModelsState.load)
    if _route == "/rules":
        _on_load.append(RulesState.load)
    if _route == "/vault":
        _on_load.append(VaultState.load)
    if _route == "/ai-library":
        _on_load.append(C5State.load)
    if _route == "/documents":
        _on_load.append(DocumentsState.load)
    if _route == "/smart-ingestion":
        _on_load.append(IngestionAiState.load)
    if _route == "/format-registry":
        _on_load.append(F6State.load)
    if _route == "/data-integrity":
        _on_load.append(F5State.load)
    if _route == "/filing":
        _on_load.append(FilingState.load)
    if _route == "/reconciliation":
        _on_load.append(Module2State.load)
    if _route == "/action-center":
        _on_load.append(Module8State.load)
        _on_load.append(ReportState.load)
    if _route == "/invoice-extraction":
        _on_load.append(InvoiceExtractState.load)
    if _route in ("/run", "/compare", "/config", "/export"):
        _on_load.append(Phase1State.load)
    if _route == "/review":
        # The standalone Review screen renders the SAME Stage-4 presentation
        # as /reconcile's Review tab, driven by the same ReconcileState.
        _on_load.append(ReconcileState.load_review_page)
    if _route == "/reconcile":
        # The handler reads its own router params, so a filtered Review view
        # (/reconcile?stage=4&…) is linkable.
        _on_load.append(ReconcileState.load)
    if _route == "/security":
        _on_load.append(SecurityState.load)
    app.add_page(
        _renderer,
        route=_route,
        title=f"{_route.strip('/').replace('-', ' ').title()} · Setu",
        on_load=_on_load,
    )