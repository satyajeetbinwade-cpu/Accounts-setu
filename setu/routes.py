"""Route + navigation map.

The Reflex rewrite keeps the Streamlit app's three-area information
architecture (Reconcile / All tools / Setup) — it was the right call there and
nothing about moving frameworks changes it. The *routes* are per screen so
deep links and browser back/forward work naturally (something the Streamlit
tab model couldn't offer).

Each entry carries the permission a user needs to see it; the sidebar filters
on that exactly as ``app.py``'s ``_build_nav`` did, so a user only ever sees
areas and screens they can actually open.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Screen:
    label: str
    route: str
    icon: str
    permission: str | None = None  # None = always visible to a signed-in user


# Area → screens. Order is significant (it's the sidebar order).
AREAS: dict[str, list[Screen]] = {
    "Reconcile": [
        Screen("Reconcile", "/reconcile", "route"),
        Screen("Dashboard", "/", "layout-dashboard"),
    ],
    "All tools": [
        Screen("Clients", "/clients", "building-2", "clients.profile.view"),
        Screen("Review", "/review", "search-check"),
        Screen("Run", "/run", "play"),
        Screen("Compare", "/compare", "git-compare"),
        Screen("Export", "/export", "download"),
        Screen("Smart Ingestion", "/smart-ingestion", "sparkles", "ingestion_ai.upload"),
        Screen("Format Registry", "/format-registry", "database", "f6.view"),
        Screen("Invoice Extraction", "/invoice-extraction", "scan-line", "invoice_extract.upload"),
        Screen("Reconciliation", "/reconciliation", "scale", "module2.view"),
        Screen("Action Center", "/action-center", "list-checks", "action_center.view"),
        Screen("Data Integrity", "/data-integrity", "shield-check", "f5.view"),
        Screen("Filing", "/filing", "calendar-check", "filing.view"),
    ],
    "Setup": [
        Screen("Config", "/config", "sliders-horizontal"),
        Screen("Settings", "/settings", "settings", "settings.view"),
        Screen("Rules", "/rules", "book-open", "rules.view"),
        Screen("AI Library", "/ai-library", "library", "c5.view"),
        Screen("AI Models", "/ai-models", "cpu", "ingestion_ai.llm.manage"),
        Screen("Security & Vault", "/vault", "key-round", "vault.view"),
        Screen("Documents", "/documents", "folder-open", "documents.view"),
        Screen("Security", "/security", "shield-alert", None),  # Admin/Partner gated in dev
        Screen("Admin", "/admin", "users", "auth.users.manage"),
    ],
}

# Sections the dashboard's "what's next" list points at.
MODULE_INVENTORY: list[tuple[str, str]] = [
    ("F1", "Login, Access & User Management"),
    ("F2", "Client Profile & Master Data"),
    ("C3", "System Settings"),
    ("C3-ext", "AI Model & Provider Configuration"),
    ("C1", "Rules, Taxonomy & Regulatory Config"),
    ("C4", "Security & Credential Vault"),
    ("C5", "AI Instruction & Knowledge Library"),
    ("F3", "Document & Data Repository"),
    ("F3-AI", "Smart Document Ingestion"),
    ("F5", "Data Integrity & Validation Layer"),
    ("C2", "Portal Connect & Filing"),
    ("Module 2", "Reconciliation Engine"),
    ("Module 8", "Action Center"),
    ("F3-B", "Invoice Extraction & Digitalization"),
    ("F4", "Universal Edit & Version History"),
]