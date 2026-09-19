"""C1 state — Rules, Taxonomy & Regulatory Configuration.

Rules Workspace (5 categories, firm-wide + per-client override, forward-only
versioning), Taxonomy Editor, Regulatory Rules Table. Every handler calls
``src.rules.service``.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.rules import service as rules
from setu.state.auth_state import AuthState
from setu.state.history import HistoryEntry, load_history

CATEGORY_LABELS = {
    "tds_rates": "TDS Section Rates",
    "gst_tolerance": "GST Tolerance",
    "suspense": "Suspense Ledger Conventions",
    "aging": "Aging Thresholds",
    "statutory_due_dates": "Statutory Due Dates",
    "materiality": "Materiality Thresholds",
}


@dataclass
class RuleRow:
    rule_id: int
    key: str
    label: str
    description: str
    value_type: str
    unit: str
    high_impact: bool
    firm_value: str
    firm_effective_from: str
    override_count: int


@dataclass
class TaxonomyRow:
    entry_id: int
    category: str
    code: str
    label: str
    description: str


@dataclass
class RegulatoryRow:
    reg_rule_id: int
    domain: str
    section: str
    name: str
    rate_or_rule: str
    effective_from: str
    seed_as_of: str
    last_reviewed_at: str
    status: str


@dataclass
class DueDateRow:
    due_date_id: int
    obligation: str
    period: str
    due_day: str
    due_month: str
    grace_days: int
    description: str


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


class RulesState(AuthState):
    """Rules / Taxonomy / Regulatory state."""

    section: str = "Rules Workspace"
    category: str = "tds_rates"

    rules: list[RuleRow] = []
    client_options: list[ClientOption] = []

    # per-rule edit buffers keyed by f"{rule_key}:{scope}:{client_id}"
    scope: dict[str, str] = {}
    client_for_rule: dict[str, int] = {}
    value: dict[str, str] = {}
    reason: dict[str, str] = {}
    confirm_high_impact: dict[str, bool] = {}

    # taxonomy
    tax_category: str = "difference_type"
    taxonomy: list[TaxonomyRow] = []
    tax_categories: list[str] = []
    tax_rename: dict[str, str] = {}
    new_tax_code: str = ""
    new_tax_label: str = ""
    new_tax_category: str = "difference_type"

    # regulatory
    regulatory: list[RegulatoryRow] = []
    reg_rate: dict[str, str] = {}
    reg_eff: dict[str, str] = {}

    # statutory due dates
    due_dates: list[DueDateRow] = []

    # F4 — per-rule edit history, keyed by f"{rule_id}:{scope}" (firm vs each
    # client's override are separate histories).
    rule_history: dict[str, list[HistoryEntry]] = {}

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def can_manage(self) -> bool:
        return "rules.manage" in self._codes()

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if (deny := self._gate("rules.view")):
            return rx.redirect(deny)
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self.tax_categories = rules.taxonomy_categories()
        if self.tax_categories and self.tax_category not in self.tax_categories:
            self.tax_category = self.tax_categories[0]
        self._load_rules()
        self._load_taxonomy()
        self._load_regulatory()
        self._load_due_dates()

    def _load_rules(self) -> None:
        self.rules = [
            RuleRow(
                rule_id=r["rule_id"],
                key=r["key"],
                label=r["label"],
                description=r.get("description") or "",
                value_type=r["value_type"],
                unit=r.get("unit") or "",
                high_impact=bool(r.get("high_impact")),
                firm_value=r.get("firm_value") or "",
                firm_effective_from=r.get("firm_effective_from") or "",
                override_count=int(r.get("override_count") or 0),
            )
            for r in rules.list_rules(category_key=self.category)
        ]
        for r in self.rules:
            k = r.key
            self.scope.setdefault(k, "firm")
            self.value.setdefault(k, r.firm_value)
            self.reason.setdefault(k, "")
        self._load_rule_history()

    def _load_rule_history(self) -> None:
        """F4 — load each rule's firm-wide history (the client-override
        history is loaded on demand when the scope switches)."""
        for r in self.rules:
            self.rule_history[f"{r.rule_id}:firm"] = load_history("rule", f"{r.rule_id}:firm")

    def _load_taxonomy(self) -> None:
        self.taxonomy = [
            TaxonomyRow(
                entry_id=e["entry_id"],
                category=e["category"],
                code=e["code"],
                label=e["label"],
                description=e.get("description") or "",
            )
            for e in rules.list_taxonomy(category=self.tax_category)
        ]
        for e in self.taxonomy:
            self.tax_rename.setdefault(str(e.entry_id), e.label)

    def _load_regulatory(self) -> None:
        self.regulatory = [
            RegulatoryRow(
                reg_rule_id=r["reg_rule_id"],
                domain=r["domain"],
                section=r.get("section") or "",
                name=r["name"],
                rate_or_rule=r["rate_or_rule"],
                effective_from=r["effective_from"],
                seed_as_of=r.get("seed_as_of") or "",
                last_reviewed_at=(r.get("last_reviewed_at") or "").split("T")[0],
                status=r["status"],
            )
            for r in rules.list_regulatory_rules()
        ]
        for r in self.regulatory:
            self.reg_rate.setdefault(str(r.reg_rule_id), r.rate_or_rule)
            self.reg_eff.setdefault(str(r.reg_rule_id), r.effective_from)

    def _load_due_dates(self) -> None:
        self.due_dates = [
            DueDateRow(
                due_date_id=d["due_date_id"],
                obligation=d["obligation"],
                period=d["period"],
                due_day=str(d["due_day"]) if d.get("due_day") else "",
                due_month=str(d["due_month"]) if d.get("due_month") else "",
                grace_days=int(d["grace_days"]),
                description=d.get("description") or "",
            )
            for d in rules.list_statutory_due_dates()
        ]

    # ------------------------------------------------------------------
    # Nav
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""

    @rx.event
    def set_category(self, category: str):
        self.category = category
        self._load_rules()

    # ------------------------------------------------------------------
    # Rule editing
    # ------------------------------------------------------------------
    def set_scope(self, key: str, scope: str):
        self.scope[key] = scope
        if scope == "firm":
            for r in self.rules:
                if r.key == key:
                    self.value[key] = r.firm_value
        else:
            self._load_client_value(key)

    def _load_client_value(self, key: str) -> None:
        cid = self.client_for_rule.get(key, 0)
        if not cid:
            self.value[key] = ""
            return
        versions = rules.rule_versions(key, scope="client", client_id=cid)
        self.value[key] = versions[0]["value"] if versions else ""

    def set_client_for_rule(self, key: str, client_id: int):
        self.client_for_rule[key] = client_id
        self._load_client_value(key)

    def set_value(self, key: str, v: str):
        self.value[key] = v

    def set_reason(self, key: str, v: str):
        self.reason[key] = v

    @rx.event
    def save_rule(self, key: str):
        self.error = ""
        self.flash = ""
        scope = self.scope.get(key, "firm")
        cid = self.client_for_rule.get(key, 0) if scope == "client" else None
        reason = self.reason.get(key, "")
        if not reason.strip():
            self.error = "A reason is required before this rule change can be saved."
            return
        try:
            rules.edit_rule_value(
                key, self.value.get(key, ""), scope=scope, client_id=cid,
                actor=self.username, reason=reason,
            )
            self.flash = f"Rule '{key}' updated — live immediately."
            self.reason[key] = ""
        except rules.RulesError as exc:
            self.error = str(exc)
        self._load_rules()

    # ------------------------------------------------------------------
    # Taxonomy
    # ------------------------------------------------------------------
    @rx.event
    def set_tax_category(self, category: str):
        self.tax_category = category
        self._load_taxonomy()

    def set_tax_rename(self, entry_id: int, v: str):
        self.tax_rename[str(entry_id)] = v

    @rx.event
    def rename_taxonomy(self, entry_id: int):
        try:
            rules.rename_taxonomy_entry(entry_id, self.tax_rename.get(str(entry_id), ""), actor=self.username)
            self.flash = "Label renamed."
        except rules.RulesError as exc:
            self.error = str(exc)
        self._load_taxonomy()

    @rx.event
    def delete_taxonomy(self, entry_id: int):
        try:
            rules.delete_taxonomy_entry(entry_id, actor=self.username)
            self.flash = "Entry deleted."
        except rules.RulesError as exc:
            self.error = str(exc)
        self._load_taxonomy()

    def set_new_tax_code(self, v: str):
        self.new_tax_code = v

    def set_new_tax_label(self, v: str):
        self.new_tax_label = v

    def set_new_tax_category(self, v: str):
        self.new_tax_category = v

    @rx.event
    def add_taxonomy(self):
        try:
            rules.add_taxonomy_entry(self.new_tax_category, self.new_tax_code, self.new_tax_label, actor=self.username)
            self.flash = "Entry added."
            self.new_tax_code = self.new_tax_label = ""
        except rules.RulesError as exc:
            self.error = str(exc)
        self._load_taxonomy()

    # ------------------------------------------------------------------
    # Regulatory
    # ------------------------------------------------------------------
    def set_reg_rate(self, reg_rule_id: int, v: str):
        self.reg_rate[str(reg_rule_id)] = v

    def set_reg_eff(self, reg_rule_id: int, v: str):
        self.reg_eff[str(reg_rule_id)] = v

    @rx.event
    def save_regulatory(self, reg_rule_id: int):
        try:
            rules.update_regulatory_rule(
                reg_rule_id,
                rate_or_rule=self.reg_rate.get(str(reg_rule_id), ""),
                effective_from=self.reg_eff.get(str(reg_rule_id), ""),
                actor=self.username,
            )
            self.flash = "Regulatory rule updated."
        except rules.RulesError as exc:
            self.error = str(exc)
        self._load_regulatory()

    @rx.event
    def mark_reviewed(self, reg_rule_id: int):
        rules.mark_regulatory_rule_reviewed(reg_rule_id, actor=self.username)
        self.flash = "Marked reviewed."
        self._load_regulatory()