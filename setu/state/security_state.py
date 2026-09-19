"""Security events state — the standalone Recent Security Events screen.

Read-only, Partner/Admin-gated. Merges C4's credential/DPDP event sink with
F1's login/permission events (via ``vault.merged_security_events``) so both are
visible in one time-ordered list. No new business logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.vault import service as vault
from setu.state.auth_state import AuthState

# event_type → pill variant (semantic roles only; never a new color).
EVENT_VARIANT = {
    "login_failed": "danger",
    "permission_denied": "danger",
    "escalation": "danger",
    "deactivation": "danger",
    "login_success": "rule",
    "credential_retrieved": "accent",
    "credential_created": "accent",
    "credential_rotated": "accent",
    "dpdp_request_submitted": "ai",
    "dpdp_request_approved": "ai",
    "filing_sent": "accent",
}


@dataclass
class SecurityEventRow:
    event_type: str
    variant: str
    actor: str
    detail: str
    source: str
    source_label: str
    created_at: str


@dataclass
class TypeCount:
    event_type: str
    variant: str
    count: int


class SecurityState(AuthState):
    """Recent Security Events state."""

    events: list[SecurityEventRow] = []
    breakdown: list[TypeCount] = []
    total: int = 0
    filter_type: str = "All"
    type_options: list[str] = ["All"]
    allowed: bool = False

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    @rx.event
    def load(self):
        # Partner or Admin only — the standalone screen is Partner-visible,
        # Admin sees the same list inside the console. Gated server-side.
        if not self.is_authenticated:
            return rx.redirect("/login")
        if self.role_name not in ("Admin", "Partner"):
            return rx.redirect("/")
        self.allowed = True
        self._load()

    def _load(self) -> None:
        merged = vault.merged_security_events(limit=200)
        if self.filter_type != "All":
            merged = [e for e in merged if e["event_type"] == self.filter_type]

        self.events = [
            SecurityEventRow(
                event_type=e["event_type"],
                variant=EVENT_VARIANT.get(e["event_type"], "placeholder"),
                actor=e.get("actor") or "?",
                detail=e.get("detail") or "",
                source=e.get("source") or "",
                source_label="Vault (C4)" if e.get("source") == "vault" else "Identity (F1)",
                created_at=str(e.get("created_at") or "").replace("T", " ").split(".")[0],
            )
            for e in merged
        ]
        self.total = len(merged)

        # Type breakdown over the full (unfiltered) set.
        counts: dict[str, int] = {}
        for e in vault.merged_security_events(limit=200):
            counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
        self.breakdown = [
            TypeCount(
                event_type=k,
                variant=EVENT_VARIANT.get(k, "placeholder"),
                count=v,
            )
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
        self.type_options = ["All"] + sorted(counts.keys())

    @rx.event
    def set_filter_type(self, v: str):
        self.filter_type = v
        self._load()

    @rx.event
    def clear_filter(self):
        self.filter_type = "All"
        self._load()
