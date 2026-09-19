"""C3-ext state — AI Model & Provider Configuration.

The central model registry: one row per AI touchpoint, primary + fallback
model, live OpenRouter catalogue. Every handler calls ``src.ai_models.service``.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.ai_models import catalog as catalog_mod
from src.ai_models import service as ai_models
from src.auth import service as auth
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history

_MANAGE_PERMISSION = "ingestion_ai.llm.manage"
_CATEGORY_ORDER = ["Ingestion", "Extraction", "Analysis"]


@dataclass
class TouchpointRow:
    touchpoint_key: str
    label: str
    category: str
    description: str
    primary_model: str
    fallback_model: str
    is_active: bool
    updated_at: str
    updated_by: str


@dataclass
class ModelOption:
    model_id: str
    label: str


@dataclass
class ChangeRow:
    when: str
    actor: str
    detail: str


class AiModelsState(AuthState):
    """AI Models registry state."""

    total: int = 0
    unassigned: int = 0
    catalog_live: bool = False
    catalog_count: int = 0
    catalog_fetched: str = ""
    catalog_source: str = ""

    assignments: list[TouchpointRow] = []
    options: list[ModelOption] = []
    change_log: list[ChangeRow] = []

    # F4 — per-touchpoint edit history (the reusable View History component).
    touchpoint_history: dict[str, list[HistoryEntry]] = {}

    # per-touchpoint edit buffers, keyed by touchpoint_key
    edit_primary: dict[str, str] = {}
    edit_fallback: dict[str, str] = {}
    edit_custom_primary: dict[str, str] = {}
    edit_custom_fallback: dict[str, str] = {}

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def can_manage(self) -> bool:
        return _MANAGE_PERMISSION in self._codes()

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if not self.can_manage:
            return rx.redirect(self._gate("ingestion_ai.llm.manage") or "/")
        self._load_all()

    def _load_all(self) -> None:
        summary = ai_models.assignment_summary()
        self.total = summary["total"]
        self.unassigned = summary["unassigned"]

        state = ai_models.catalog_status()
        self.catalog_live = bool(state["live"])
        self.catalog_count = int(state["count"])
        self.catalog_fetched = str(state["fetched_at"] or "").replace("T", " ").split(".")[0]
        self.catalog_source = state["source"]

        self.assignments = [
            TouchpointRow(
                touchpoint_key=a["touchpoint_key"],
                label=a["label"],
                category=a["category"],
                description=a.get("description") or "",
                primary_model=a.get("primary_model") or "",
                fallback_model=a.get("fallback_model") or "",
                is_active=bool(a["is_active"]),
                updated_at=str(a.get("updated_at") or "").replace("T", " ").split(".")[0],
                updated_by=a.get("updated_by") or "",
            )
            for a in ai_models.list_assignments()
        ]
        self.options = [
            ModelOption(model_id=o["model_id"], label=ai_models.model_label(o))
            for o in ai_models.model_options()
        ]
        # Seed edit buffers from current values (only if not already editing).
        for a in self.assignments:
            self.edit_primary.setdefault(a.touchpoint_key, a.primary_model)
            self.edit_fallback.setdefault(a.touchpoint_key, a.fallback_model)
            # F4 — per-touchpoint routing history.
            self.touchpoint_history[a.touchpoint_key] = load_history(
                "ai_model_assignment", a.touchpoint_key
            )

        self.change_log = [
            ChangeRow(
                when=str(r.get("created_at") or "").replace("T", " ").split(".")[0],
                actor=r.get("actor") or "",
                detail=r.get("detail") or r.get("action") or "",
            )
            for r in ai_models.list_change_log()
        ]

    # ------------------------------------------------------------------
    @rx.event
    def refresh_catalog(self):
        self.error = ""
        try:
            result = ai_models.refresh_catalog(actor=self.username)
            self.flash = f"Loaded {result['count']} models from OpenRouter."
        except catalog_mod.CatalogError as exc:
            self.error = str(exc)
        self._load_all()

    def set_edit_primary(self, key: str, v: str):
        self.edit_primary[key] = v

    def set_edit_fallback(self, key: str, v: str):
        self.edit_fallback[key] = v

    def set_edit_custom_primary(self, key: str, v: str):
        self.edit_custom_primary[key] = v

    def set_edit_custom_fallback(self, key: str, v: str):
        self.edit_custom_fallback[key] = v

    @rx.event
    def save_assignment(self, key: str):
        self.error = ""
        self.flash = ""
        primary = (self.edit_custom_primary.get(key) or "").strip() or self.edit_primary.get(key, "")
        fallback = (self.edit_custom_fallback.get(key) or "").strip() or self.edit_fallback.get(key, "")
        try:
            ai_models.set_assignment_models(
                key, primary_model=primary, fallback_model=fallback, actor=self.username,
            )
            self.flash = f"{key} now uses {primary}."
            self.edit_custom_primary[key] = ""
            self.edit_custom_fallback[key] = ""
        except ai_models.AIModelsError as exc:
            self.error = str(exc)
        self._load_all()