"""C3-ext state — AI Model & Provider Configuration (multi-provider).

Two sections on one screen, per the build spec:

  * Provider management — one row per platform provider
    (OpenRouter / Anthropic / Deepseek / Sarvam): its own key entry, masked
    reference once saved, a replace action, and its own live status chip.
  * Per-touchpoint assignment — Primary and Fallback legs, each an
    INDEPENDENT (provider, model) pair; the fallback provider may differ.

Every handler calls ``src.ai_models.service`` / ``src.ai_models.gateway``.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.ai_models import service as ai_models
from src.auth import service as auth
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history

_MANAGE_PERMISSION = "ingestion_ai.llm.manage"
_CATEGORY_ORDER = ["Ingestion", "Extraction", "Analysis"]

# C4/vault status -> the Foundation 3.7 live_status_chip's 3-state vocabulary.
_CHIP_STATUS = {
    "live": "connected",
    "invalid": "expired",
    "unreachable": "expired",
    "unconfigured": "needs_reauth",
}

_STATUS_LABEL = {
    "live": "Live",
    "invalid": "Invalid key",
    "unreachable": "Unreachable",
    "unconfigured": "No key",
}


@dataclass
class ProviderRow:
    provider_key: str
    label: str
    status: str
    status_label: str
    chip_status: str
    status_detail: str
    masked_ref: str
    has_key: bool
    catalog_mode: str
    model_count: int


@dataclass
class ProviderOption:
    key: str
    label: str
    disabled: bool


@dataclass
class TouchpointRow:
    touchpoint_key: str
    label: str
    category: str
    description: str
    placeholder_module: str
    primary_provider: str
    primary_model: str
    fallback_provider: str
    fallback_model: str
    is_active: bool
    updated_at: str
    updated_by: str


@dataclass
class FallbackRow:
    when: str
    touchpoint_key: str
    provider: str
    model: str
    reason: str


def _short(value) -> str:
    return str(value or "").replace("T", " ").split(".")[0]


def _placeholder_module(description: str) -> str:
    """Extract the 'Module N' from a placeholder description, for the
    Coming-with-[Module] badge. Empty when the description names none."""
    text = description or ""
    marker = "Module "
    idx = text.find(marker)
    if idx == -1:
        return ""
    tail = text[idx + len(marker):]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    return f"Module {digits}" if digits else ""


class AiModelsState(AuthState):
    """Multi-provider AI configuration state."""

    total: int = 0
    unassigned: int = 0
    inactive: int = 0
    providers_configured: int = 0

    providers: list[ProviderRow] = []
    provider_options: list[ProviderOption] = []
    # Lookups keyed by provider_key — indexed in the view by a Var key.
    provider_has_key: dict[str, bool] = {}
    provider_label: dict[str, str] = {}
    # model_id list per provider, for the per-leg model dropdown.
    provider_model_options: dict[str, list[str]] = {}

    assignments: list[TouchpointRow] = []
    touchpoint_history: dict[str, list[HistoryEntry]] = {}
    fallback_events: list[FallbackRow] = []

    # key entry buffer per provider (never persisted beyond the input)
    provider_key_input: dict[str, str] = {}
    # leg edit buffers, keyed by touchpoint_key
    edit_primary_provider: dict[str, str] = {}
    edit_primary_model: dict[str, str] = {}
    edit_fallback_provider: dict[str, str] = {}
    edit_fallback_model: dict[str, str] = {}
    edit_custom_primary: dict[str, str] = {}
    edit_custom_fallback: dict[str, str] = {}
    # per-leg test-call outcomes, keyed by touchpoint_key (always pre-seeded so
    # the view never indexes a missing key)
    test_primary_ok: dict[str, bool] = {}
    test_primary_text: dict[str, str] = {}
    test_fallback_ok: dict[str, bool] = {}
    test_fallback_text: dict[str, str] = {}

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
        self.inactive = summary.get("inactive", 0)

        providers = ai_models.list_providers()
        self.providers_configured = sum(1 for p in providers if p["has_key"])
        self.providers = [
            ProviderRow(
                provider_key=p["provider_key"],
                label=p["label"],
                status=p["status"],
                status_label=_STATUS_LABEL.get(p["status"], p["status"]),
                chip_status=_CHIP_STATUS.get(p["status"], "needs_reauth"),
                status_detail=p.get("status_detail") or "",
                masked_ref=p.get("masked_ref") or "",
                has_key=bool(p["has_key"]),
                catalog_mode=p["catalog_mode"],
                model_count=len(ai_models.models_for_provider(p["provider_key"])),
            )
            for p in providers
        ]
        self.provider_options = [
            ProviderOption(key=p.provider_key, label=p.label, disabled=not p.has_key)
            for p in self.providers
        ]
        self.provider_has_key = {p.provider_key: p.has_key for p in self.providers}
        self.provider_label = {p.provider_key: p.label for p in self.providers}
        self.provider_model_options = {
            p.provider_key: [m["model_id"] for m in ai_models.models_for_provider(p.provider_key)]
            for p in self.providers
        }

        self.assignments = [
            TouchpointRow(
                touchpoint_key=a["touchpoint_key"],
                label=a["label"],
                category=a["category"],
                description=a.get("description") or "",
                placeholder_module=_placeholder_module(a.get("description") or ""),
                primary_provider=a.get("primary_provider") or "openrouter",
                primary_model=a.get("primary_model") or "",
                fallback_provider=a.get("fallback_provider") or "openrouter",
                fallback_model=a.get("fallback_model") or "",
                is_active=bool(a["is_active"]),
                updated_at=_short(a.get("updated_at")),
                updated_by=a.get("updated_by") or "",
            )
            for a in ai_models.list_assignments()
        ]
        for a in self.assignments:
            self.edit_primary_provider.setdefault(a.touchpoint_key, a.primary_provider)
            self.edit_primary_model.setdefault(a.touchpoint_key, a.primary_model)
            self.edit_fallback_provider.setdefault(a.touchpoint_key, a.fallback_provider)
            self.edit_fallback_model.setdefault(a.touchpoint_key, a.fallback_model)
            self.test_primary_ok.setdefault(a.touchpoint_key, False)
            self.test_primary_text.setdefault(a.touchpoint_key, "")
            self.test_fallback_ok.setdefault(a.touchpoint_key, False)
            self.test_fallback_text.setdefault(a.touchpoint_key, "")
            self.touchpoint_history[a.touchpoint_key] = load_history(
                "ai_model_assignment", a.touchpoint_key
            )

        # Ensure a currently-assigned model is ALWAYS a selectable option, even
        # when the provider's catalogue has not been fetched yet — otherwise the
        # saved assignment would render as an empty dropdown.
        options_by_provider = {k: list(v) for k, v in self.provider_model_options.items()}
        for a in self.assignments:
            for prov_key, model in (
                (a.primary_provider, a.primary_model),
                (a.fallback_provider, a.fallback_model),
            ):
                if model and prov_key in options_by_provider and model not in options_by_provider[prov_key]:
                    options_by_provider[prov_key].append(model)
        self.provider_model_options = options_by_provider

        self.fallback_events = [
            FallbackRow(
                when=_short(e.get("created_at")),
                touchpoint_key=e.get("touchpoint_key") or "",
                provider=e.get("resolved_provider") or "",
                model=e.get("resolved_model") or "",
                reason=e.get("reason") or "",
            )
            for e in ai_models.list_fallback_events()
        ]

    # ------------------------------------------------------------------
    # Provider management
    # ------------------------------------------------------------------
    def set_key_input(self, provider_key: str, v: str):
        self.provider_key_input[provider_key] = v

    @rx.event
    def save_provider_key(self, provider_key: str):
        self.flash = ""
        self.error = ""
        secret = (self.provider_key_input.get(provider_key) or "").strip()
        if not secret:
            self.error = "Enter an API key first."
            return
        try:
            row = ai_models.get_provider_row(provider_key) or {}
            if row.get("has_key"):
                ai_models.replace_provider_key(provider_key, secret, actor=self.username)
                self.flash = f"API key replaced for {row.get('label') or provider_key}."
            else:
                ai_models.set_provider_key(provider_key, secret, actor=self.username)
                self.flash = f"API key stored for {row.get('label') or provider_key}."
            self.provider_key_input[provider_key] = ""
        except ai_models.AIModelsError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def verify_provider(self, provider_key: str):
        self.flash = ""
        self.error = ""
        try:
            result = ai_models.verify_provider(provider_key, actor=self.username)
            label = (ai_models.get_provider_row(provider_key) or {}).get("label") or provider_key
            if result["status"] == "live":
                self.flash = f"{label} is reachable."
            else:
                self.error = f"{label}: {result['detail']}"
        except ai_models.AIModelsError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def refresh_provider_catalog(self, provider_key: str):
        self.flash = ""
        self.error = ""
        try:
            result = ai_models.refresh_provider_catalog(provider_key, actor=self.username)
            label = (ai_models.get_provider_row(provider_key) or {}).get("label") or provider_key
            self.flash = f"Loaded {result['count']} model(s) for {label}."
        except ai_models.AIModelsError as exc:
            self.error = str(exc)
        self._load_all()

    # ------------------------------------------------------------------
    # Per-touchpoint leg editing
    # ------------------------------------------------------------------
    def set_edit_primary_provider(self, key: str, v: str):
        self.edit_primary_provider[key] = v
        # keep the model within the new provider's catalogue
        options = self.provider_model_options.get(v) or []
        if self.edit_primary_model.get(key) not in options:
            self.edit_primary_model[key] = options[0] if options else ""

    def set_edit_primary_model(self, key: str, v: str):
        self.edit_primary_model[key] = v

    def set_edit_fallback_provider(self, key: str, v: str):
        self.edit_fallback_provider[key] = v
        options = self.provider_model_options.get(v) or []
        if self.edit_fallback_model.get(key) not in options:
            self.edit_fallback_model[key] = options[0] if options else ""

    def set_edit_fallback_model(self, key: str, v: str):
        self.edit_fallback_model[key] = v

    def set_edit_custom_primary(self, key: str, v: str):
        self.edit_custom_primary[key] = v

    def set_edit_custom_fallback(self, key: str, v: str):
        self.edit_custom_fallback[key] = v

    @rx.event
    def save_assignment(self, key: str):
        self.error = ""
        self.flash = ""
        primary_provider = self.edit_primary_provider.get(key, "")
        fallback_provider = self.edit_fallback_provider.get(key, "")
        primary_model = (self.edit_custom_primary.get(key) or "").strip() or self.edit_primary_model.get(key, "")
        fallback_model = (self.edit_custom_fallback.get(key) or "").strip() or self.edit_fallback_model.get(key, "")
        try:
            ai_models.set_assignment_legs(
                key,
                primary_provider=primary_provider, primary_model=primary_model,
                fallback_provider=fallback_provider, fallback_model=fallback_model,
                actor=self.username,
            )
            self.flash = f"{key} now routes via {primary_provider} → {fallback_provider}."
            self.edit_custom_primary[key] = ""
            self.edit_custom_fallback[key] = ""
        except ai_models.AIModelsError as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def test_leg(self, key: str, leg: str):
        self.flash = ""
        self.error = ""
        out = ai_models.test_leg(key, leg, actor=self.username)
        ok = bool(out.get("ok"))
        if ok:
            text = (
                f"{out.get('provider')} · {out.get('model')} · "
                f"{int(out.get('latency_ms') or 0)} ms · “{out.get('sample') or ''}”"
            )
        else:
            text = f"{out.get('provider')} · {out.get('model')} — {out.get('error') or 'failed'}"
        if leg == "primary":
            self.test_primary_ok[key] = ok
            self.test_primary_text[key] = text
        else:
            self.test_fallback_ok[key] = ok
            self.test_fallback_text[key] = text
