"""Per-route pages.

Module pages are migrated one at a time. Screens that aren't rebuilt yet
render an honest, on-brand placeholder — never a fabricated UI — using the
Foundation's 3.5 badge so the gap is visible and named, exactly as the
Streamlit build marked unbuilt features.
"""

from __future__ import annotations

import reflex as rx

from setu.foundation import components as c
from setu.foundation import tokens as t
from setu.routes import MODULE_INVENTORY, AREAS
from setu.state import AuthState
from setu.state.dashboard_state import DashboardState
from setu.views import shell

_SETU_MODULE_COUNT = len(MODULE_INVENTORY)


def _module_status_card(unit: str, name: str) -> rx.Component:
    return rx.hstack(
        rx.box(
            rx.text(unit, font_size="11px", font_weight="700", color=t.Color.ACCENT.value),
            padding="3px 9px",
            background="#EAF0FB",
            border="1px solid #D4E1F8",
            border_radius="7px",
            flex_shrink="0",
            min_width="62px",
            text_align="center",
        ),
        rx.text(name, style=t.TEXT["body"], flex="1", min_width="0"),
        rx.spacer(),
        c.pill("Live", variant="rule", icon="check"),
        width="100%",
        align="center",
        spacing="3",
        padding="10px 2px",
        border_bottom=f"1px solid {t.Color.BORDER.value}",
    )


def dashboard_page() -> rx.Component:
    """The signed-in landing screen. In the Streamlit app this was F1's
    role-aware Home shell; here it doubles as the migration status board.
    """
    return shell.shell(
        rx.vstack(
            c.page_header(
                f"Welcome back, {AuthState.display_name}",
                "Setu runs in Reflex. Every module is live — pick a screen from the sidebar.",
            ),
            rx.grid(
                c.card(
                    c.stat(DashboardState.runs_total.to_string(), "Runs on record", accent=True),
                ),
                c.card(
                    c.stat(DashboardState.open_exceptions.to_string(), "Open exceptions"),
                ),
                c.card(
                    c.stat(DashboardState.filings_due.to_string(), "Filings due"),
                ),
                c.card(
                    c.stat(DashboardState.awaiting_review.to_string(), "Awaiting review"),
                ),
                columns="4",
                spacing="4",
                width="100%",
            ),
            c.card(
                c.section_title(
                    "Modules",
                    "Each module's Objective / Data Model / Business Rules / UX / "
                    "Acceptance Criteria carried over unchanged — only the implementation layer moved.",
                ),
                rx.box(height="10px"),
                rx.vstack(
                    *[_module_status_card(unit, name) for unit, name in MODULE_INVENTORY],
                    spacing="0",
                    width="100%",
                ),
                rx.box(height="12px"),
                rx.hstack(
                    rx.text(
                        f"{_SETU_MODULE_COUNT} modules · Foundation component library built first (Step 0)",
                        style=t.TEXT["micro"],
                    ),
                    width="100%",
                ),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )


def _placeholder_page(label: str, blurb: str, *, module: str) -> rx.Component:
    """A screen not yet rebuilt. Renders the module it's waiting on, in place
    of a fake UI."""
    return shell.shell(
        rx.vstack(
            c.page_header(label, blurb),
            c.card(
                rx.vstack(
                    rx.hstack(
                        c.placeholder_badge(module),
                        rx.spacer(),
                        width="100%",
                    ),
                    rx.text(
                        "This screen keeps its existing specification. It will be "
                        "rebuilt against the Foundation components once its module is migrated.",
                        style=t.TEXT["body"],
                    ),
                    spacing="3",
                    align="start",
                    width="100%",
                ),
            ),
            spacing="5",
            width="100%",
            align="start",
        )
    )


# Route → page. Keep the text honest: it names the module, not a fake feature.
_PLACEHOLDER_PAGES = {
    "/clients": ("Clients", "Client profiles, branches, contacts and master data.", "Module F2"),
    "/run": ("Run", "Manual reconciliation run.", "Module 2"),
    "/compare": ("Compare", "Side-by-side run comparison.", "Phase 1 tool"),
    "/export": ("Export", "Export a run's report.", "Phase 1 tool"),
    "/config": ("Config", "Matching-rule configuration.", "Phase 1 tool"),
    "/review": ("Review", "Review reconciliation results.", "Phase 1 tool"),
    "/smart-ingestion": ("Smart Ingestion", "AI-assisted document ingestion.", "Module F3-AI"),
    "/invoice-extraction": ("Invoice Extraction", "Invoice capture and Tally export.", "Module F3-B"),
    "/reconciliation": ("Reconciliation", "Exception queue and eligible-credit figure.", "Module 2"),
    "/action-center": ("Action Center", "Unified flagged-item queue.", "Module 8"),
    "/data-integrity": ("Data Integrity", "Validation gate and sync health.", "Module F5"),
    "/filing": ("Filing", "Filing calendar and portal sends.", "Module C2"),
    "/settings": ("Settings", "Firm profile, notifications, connections.", "Module C3"),
    "/rules": ("Rules", "Rules, taxonomy and regulatory configuration.", "Module C1"),
    "/ai-library": ("AI Library", "AI instruction and knowledge library.", "Module C5"),
    "/ai-models": ("AI Models", "Per-touchpoint model and provider assignment.", "Module C3-ext"),
    "/vault": ("Security & Vault", "Encrypted credential vault and DPDP workflow.", "Module C4"),
    "/documents": ("Documents", "Document and data repository.", "Module F3"),
    "/security": ("Security", "Recent security events.", "Module F1 / C4"),
    "/admin": ("Admin", "User, role and permission management.", "Module F1"),
    "/reconcile": ("Reconcile", "The guided five-stage flow from files to finished report.", "Module F1"),  # reached the guided flow
}


def placeholder_for(route: str) -> rx.Component:
    label, blurb, module = _PLACEHOLDER_PAGES[route]
    return _placeholder_page(label, blurb, module=module)


# Routes that are migrated (render a real page) — everything else falls back to
# a placeholder. This is the migration's single source of truth for progress.
BUILT_ROUTES: dict[str, callable] = {}


def _admin_route():
    from setu.views.admin_page import admin_page

    return admin_page()


def _clients_route():
    from setu.views.client_page import clients_page

    return clients_page()


def _settings_route():
    from setu.views.settings_page import settings_page

    return settings_page()


def _ai_models_route():
    from setu.views.ai_models_page import ai_models_page

    return ai_models_page()


def _rules_route():
    from setu.views.rules_page import rules_page

    return rules_page()


def _vault_route():
    from setu.views.vault_page import vault_page

    return vault_page()


def _c5_route():
    from setu.views.c5_page import c5_page

    return c5_page()


def _documents_route():
    from setu.views.documents_page import documents_page

    return documents_page()


def _ingestion_ai_route():
    from setu.views.ingestion_ai_page import ingestion_ai_page

    return ingestion_ai_page()


def _f5_route():
    from setu.views.f5_page import f5_page

    return f5_page()


def _filing_route():
    from setu.views.filing_page import filing_page

    return filing_page()


def _module2_route():
    from setu.views.module2_page import module2_page

    return module2_page()


def _action_center_route():
    from setu.views.action_center_page import action_center_page

    return action_center_page()


def _invoice_extract_route():
    from setu.views.invoice_extract_page import invoice_extract_page

    return invoice_extract_page()


def _run_route():
    from setu.views.phase1_pages import run_page

    return run_page()


def _review_route():
    from setu.views.phase1_pages import review_page

    return review_page()


def _compare_route():
    from setu.views.phase1_pages import compare_page

    return compare_page()


def _config_route():
    from setu.views.phase1_pages import config_page

    return config_page()


def _export_route():
    from setu.views.phase1_pages import export_page

    return export_page()


def _reconcile_route():
    from setu.views.reconcile_page import reconcile_page

    return reconcile_page()


def _security_route():
    from setu.views.security_page import security_page

    return security_page()


def render_route(route: str) -> rx.Component:
    if route == "/admin":
        return _admin_route()
    if route == "/clients":
        return _clients_route()
    if route == "/settings":
        return _settings_route()
    if route == "/ai-models":
        return _ai_models_route()
    if route == "/rules":
        return _rules_route()
    if route == "/vault":
        return _vault_route()
    if route == "/ai-library":
        return _c5_route()
    if route == "/documents":
        return _documents_route()
    if route == "/smart-ingestion":
        return _ingestion_ai_route()
    if route == "/data-integrity":
        return _f5_route()
    if route == "/filing":
        return _filing_route()
    if route == "/reconciliation":
        return _module2_route()
    if route == "/action-center":
        return _action_center_route()
    if route == "/invoice-extraction":
        return _invoice_extract_route()
    if route == "/run":
        return _run_route()
    if route == "/review":
        return _review_route()
    if route == "/compare":
        return _compare_route()
    if route == "/config":
        return _config_route()
    if route == "/export":
        return _export_route()
    if route == "/reconcile":
        return _reconcile_route()
    if route == "/security":
        return _security_route()
    if route in BUILT_ROUTES:
        return BUILT_ROUTES[route]()
    return placeholder_for(route)


def all_routes() -> list[tuple[str, callable]]:
    """Every declared route → its renderer, for ``app.add_page``."""
    routes: list[tuple[str, callable]] = [("/", dashboard_page)]
    seen = {"/"}
    for screens in AREAS.values():
        for screen in screens:
            if screen.route in seen:
                continue
            seen.add(screen.route)
            routes.append((screen.route, _make_page(screen.route)))
    return routes


def _make_page(route: str):
    """Bind ``route`` into a zero-arg render callable for ``add_page``."""
    if route == "/admin":
        return _admin_route
    if route == "/clients":
        return _clients_route
    if route == "/settings":
        return _settings_route
    if route == "/ai-models":
        return _ai_models_route
    if route == "/rules":
        return _rules_route
    if route == "/vault":
        return _vault_route
    if route == "/ai-library":
        return _c5_route
    if route == "/documents":
        return _documents_route
    if route == "/smart-ingestion":
        return _ingestion_ai_route
    if route == "/data-integrity":
        return _f5_route
    if route == "/filing":
        return _filing_route
    if route == "/reconciliation":
        return _module2_route
    if route == "/action-center":
        return _action_center_route
    if route == "/invoice-extraction":
        return _invoice_extract_route
    if route == "/run":
        return _run_route
    if route == "/review":
        return _review_route
    if route == "/compare":
        return _compare_route
    if route == "/config":
        return _config_route
    if route == "/export":
        return _export_route
    if route == "/reconcile":
        return _reconcile_route
    if route == "/security":
        return _security_route
    if route in BUILT_ROUTES:
        return BUILT_ROUTES[route]

    def _page() -> rx.Component:
        return placeholder_for(route)

    _page.__name__ = "page_" + (route.strip("/").replace("/", "_").replace("-", "_") or "root")
    return _page