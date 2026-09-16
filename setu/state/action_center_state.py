"""Module 8 state — Action Center (Recon Exceptions only).

The capstone working queue — built GENERICALLY against the shared Flagged
Item interface from Step 3's Information Architecture, so Modules 1/4/5/6
slot in later without a rebuild. Populated this phase solely by Module 2's
Reconciliation Exceptions.

The State renders ONLY the generic Flagged Item shape — it never touches an
origin-specific field name. Every handler calls ``src.action_center.service``.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.action_center import service as ac
from src.auth import service as auth
from setu.state.auth_state import AuthState


@dataclass
class FlaggedItemRow:
    item_ref: str
    origin_id: int
    source_module: str
    origin_label: str
    type: str
    classification: str
    client_name: str
    priority: str
    priority_label: str
    priority_variant: str
    confidence_source: str
    confidence_pct: int
    confidence_label: str
    reason: str
    evidence_summary: str
    materiality_gated: bool
    exclusion_reason: str
    age_days: int
    age_escalated: bool
    assignee: str
    assignee_source: str
    due_date: str
    stale: bool
    stale_message: str
    item_created_at: str


@dataclass
class SourceCount:
    label: str
    count: int


@dataclass
class PriorityCount:
    label: str
    count: int
    variant: str


@dataclass
class HistoryRow:
    plain_language: str
    timestamp: str


_PRIORITY_LABEL = {
    "escalated": "Escalated",
    "high": "High",
    "normal": "Normal",
    "low": "Low",
}
_PRIORITY_VARIANT = {
    "escalated": "danger",
    "high": "ai",
    "normal": "accent",
    "low": "placeholder",
}


class Module8State(AuthState):
    """Action Center state."""

    # headline
    total_open: int = 0
    source_count: int = 0
    material_gated_count: int = 0
    escalated_count: int = 0
    stale_count: int = 0
    by_source: list[SourceCount] = []
    by_priority: list[PriorityCount] = []

    # filters
    filter_client: str = "All"
    filter_source: str = "All"
    filter_type: str = "All"
    filter_priority: str = "All"
    filter_assignee: str = "All"
    filter_min_age: str = "0"

    # option lists
    client_options: list[str] = ["All"]
    source_options: list[str] = ["All"]
    type_options: list[str] = ["All"]
    priority_options: list[str] = ["All"]
    assignee_options: list[str] = ["All"]

    # queue
    items: list[FlaggedItemRow] = []
    filtered_count: int = 0
    eligible_count: int = 0
    excluded_count: int = 0

    # selection + batch
    selected_refs: list[str] = []

    # assignment panel
    expanded_ref: str = ""
    assign_input: str = ""
    due_input: str = ""
    history: list[HistoryRow] = []

    age_threshold: int = 30

    # future sources
    future_sources: list[str] = ["Module 1", "Module 4", "Module 5", "Module 6"]

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "action_center.view" not in self._codes():
            return rx.redirect("/")
        # Ageing/escalation runs idempotently on load so a past-threshold item
        # visibly escalates without a separate cron.
        try:
            ac.run_ageing_escalation(actor=self.username)
        except Exception:  # noqa: BLE001
            pass
        self.age_threshold = ac.age_escalation_days()
        self._load_all()

    def _load_all(self) -> None:
        all_items = ac.list_flagged_items(status="open")
        counts = ac.item_counts(all_items)

        self.total_open = counts["total"]
        self.source_count = len(counts["by_source"])
        self.material_gated_count = counts["material_gated"]
        self.escalated_count = counts["escalated"]
        self.stale_count = counts["stale"]
        self.by_source = [
            SourceCount(label=k, count=v) for k, v in sorted(counts["by_source"].items())
        ]
        self.by_priority = [
            PriorityCount(
                label=_PRIORITY_LABEL.get(k, k),
                count=v,
                variant=_PRIORITY_VARIANT.get(k, "placeholder"),
            )
            for k, v in sorted(
                counts["by_priority"].items(), key=lambda kv: _priority_rank(kv[0])
            )
        ]

        # Filter option lists (generic dimensions only).
        self.client_options = ["All"] + sorted({i["client_name"] for i in all_items})
        self.source_options = ["All"] + sorted({i["source_module"] for i in all_items})
        self.type_options = ["All"] + sorted({i["type"] for i in all_items})
        self.priority_options = ["All"] + sorted({i["priority"] for i in all_items})
        self.assignee_options = ["All"] + sorted(
            {i["assignee"] for i in all_items if i.get("assignee")}
        )

        filtered = self._apply_filters(all_items)
        self.filtered_count = len(filtered)
        eligible, excluded = ac.eligible_for_batch(filtered)
        self.eligible_count = len(eligible)
        self.excluded_count = len(excluded)
        self.items = [self._to_row(i) for i in filtered]

        # Drop selections that no longer match the filter.
        visible = {i.item_ref for i in self.items}
        self.selected_refs = [r for r in self.selected_refs if r in visible]
        if self.expanded_ref and self.expanded_ref not in visible:
            self.expanded_ref = ""

    def _apply_filters(self, items: list[dict]) -> list[dict]:
        min_age = _to_int(self.filter_min_age)

        def _keep(i: dict) -> bool:
            if self.filter_client != "All" and i["client_name"] != self.filter_client:
                return False
            if self.filter_source != "All" and i["source_module"] != self.filter_source:
                return False
            if self.filter_type != "All" and i["type"] != self.filter_type:
                return False
            if self.filter_priority != "All" and i["priority"] != self.filter_priority:
                return False
            if self.filter_assignee != "All" and (i.get("assignee") or "") != self.filter_assignee:
                return False
            if min_age and (i.get("age_days") or 0) < min_age:
                return False
            return True

        return [i for i in items if _keep(i)]

    def _to_row(self, i: dict) -> FlaggedItemRow:
        exclusion = ac.batch_exclusion_reason(i) or ""
        return FlaggedItemRow(
            item_ref=i["item_ref"],
            origin_id=int(i.get("origin_id") or 0),
            source_module=i["source_module"],
            origin_label=i.get("origin_label") or i["source_module"],
            type=i["type"],
            classification=i.get("classification") or "",
            client_name=i.get("client_name") or "—",
            priority=i["priority"],
            priority_label=_PRIORITY_LABEL.get(i["priority"], i["priority"]),
            priority_variant=_PRIORITY_VARIANT.get(i["priority"], "placeholder"),
            confidence_source=i.get("confidence_source") or "rule",
            confidence_pct=int(i.get("confidence_pct") or 0),
            confidence_label=i.get("confidence_label") or "Rule match",
            reason=i.get("reason") or "",
            evidence_summary=i.get("evidence_summary") or "",
            materiality_gated=bool(i.get("materiality_gated")),
            exclusion_reason=exclusion,
            age_days=int(i.get("age_days") or 0),
            age_escalated=bool(i.get("age_escalated")),
            assignee=i.get("assignee") or "",
            assignee_source=i.get("assignee_source") or "unassigned",
            due_date=i.get("due_date") or "",
            stale=bool(i.get("staleness")),
            stale_message=(i.get("staleness") or {}).get("message", "") if i.get("staleness") else "",
            item_created_at=str(i.get("item_created_at") or "").replace("T", " ").split(".")[0],
        )

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    @rx.event
    def set_filter_client(self, v: str):
        self.filter_client = v
        self._load_all()

    @rx.event
    def set_filter_source(self, v: str):
        self.filter_source = v
        self._load_all()

    @rx.event
    def set_filter_type(self, v: str):
        self.filter_type = v
        self._load_all()

    @rx.event
    def set_filter_priority(self, v: str):
        self.filter_priority = v
        self._load_all()

    @rx.event
    def set_filter_assignee(self, v: str):
        self.filter_assignee = v
        self._load_all()

    def set_filter_min_age(self, v: str):
        self.filter_min_age = v

    @rx.event
    def apply_min_age(self):
        self._load_all()

    @rx.event
    def clear_filters(self):
        self.filter_client = "All"
        self.filter_source = "All"
        self.filter_type = "All"
        self.filter_priority = "All"
        self.filter_assignee = "All"
        self.filter_min_age = "0"
        self._load_all()

    # ------------------------------------------------------------------
    # Selection + batch
    # ------------------------------------------------------------------
    @rx.event
    def toggle_select(self, item_ref: str, checked: bool):
        if checked and item_ref not in self.selected_refs:
            self.selected_refs = self.selected_refs + [item_ref]
        elif not checked and item_ref in self.selected_refs:
            self.selected_refs = [r for r in self.selected_refs if r != item_ref]

    @rx.event
    def batch_approve(self):
        if not self.selected_refs:
            return
        # Resolve the selected generic items and surface the origin's own
        # mechanism (Module 8 never re-implements resolution).
        all_items = ac.list_flagged_items(status="open")
        chosen = [i for i in all_items if i["item_ref"] in self.selected_refs]
        result = ac.batch_action(chosen, actor=self.username)
        self.flash = (
            f"Batch approval applied to {result['applied_total']} item(s) via the "
            "originating module's own mechanism."
        )
        self.selected_refs = []
        self._load_all()

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------
    @rx.event
    def open_assign(self, item_ref: str, assignee: str, due: str):
        self.expanded_ref = item_ref
        self.assign_input = assignee
        self.due_input = due
        self.history = [
            HistoryRow(
                plain_language=h.get("plain_language") or "",
                timestamp=str(h.get("created_at") or "").replace("T", " ").split(".")[0],
            )
            for h in ac.history_for_item(item_ref)
        ]

    @rx.event
    def close_assign(self):
        self.expanded_ref = ""
        self.history = []

    def set_assign_input(self, v: str):
        self.assign_input = v

    def set_due_input(self, v: str):
        self.due_input = v

    @rx.event
    def save_assignment(self):
        try:
            ac.assign_item(
                self.expanded_ref,
                assignee=self.assign_input or None,
                due_date=self.due_input or None,
                actor=self.username,
            )
            self.flash = "Assignment saved (logged to F4). Priority was not changed."
            self.expanded_ref = ""
            self.history = []
        except ac.ActionCenterError as exc:
            self.error = str(exc)
        self._load_all()

    # ------------------------------------------------------------------
    # Acting on an item — hand off to the origin's OWN detail screen
    # ------------------------------------------------------------------
    @rx.event
    def open_in_origin(self, item_ref: str):
        from setu.state.module2_state import Module2State

        item = ac.get_flagged_item(item_ref)
        if item is None:
            self.error = "Queue item not found."
            return
        route = ac.detail_route_for_item(item)
        origin_id = route.get("origin_id")
        target = route.get("target_tab_key")
        if origin_id is None:
            self.error = "No detail screen is available for this item's origin yet."
            return
        # Route into Module 2's OWN detail screen — Module 8 renders no
        # resolution UI of its own.
        if target == "Reconciliation":
            self.selected_refs = []
            return [
                Module2State.open_exception(int(origin_id)),
                Module2State.set_section("Exception queue"),
                rx.redirect("/reconciliation"),
            ]
        self.error = "No detail screen is available for this item's origin yet."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _priority_rank(p: str) -> int:
    return {"escalated": 0, "high": 1, "normal": 2, "low": 3}.get(p, 9)


def _to_int(v: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0