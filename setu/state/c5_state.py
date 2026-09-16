"""C5 state — AI Instruction & Knowledge Library.

Instruction library (search/filter/entry/versions), approval queue, conflict
surfacing. Every handler calls ``src.c5.service``.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.c5 import service as c5
from src.clients import service as clients
from setu.state.auth_state import AuthState


@dataclass
class TouchpointOption:
    key: str
    label: str


@dataclass
class InstructionRow:
    instruction_id: int
    title: str
    explanation: str
    scope: str
    client_id: int
    is_active: bool
    tags: list[str]
    tag_labels: list[str]


@dataclass
class VersionRow:
    change_summary: str
    title: str
    created_at: str
    changed_by: str
    is_current: bool


@dataclass
class ProposalRow:
    proposal_id: int
    title: str
    explanation: str
    submitted_by: str
    submitted_at: str
    source: str
    tags: list[str]


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class ConflictGroup:
    touchpoint: str
    scope: str
    names: list[str]


class C5State(AuthState):
    """AI Instruction & Knowledge Library state."""

    section: str = "Instruction Library"

    touchpoints: list[TouchpointOption] = []
    instructions: list[InstructionRow] = []
    client_options: list[ClientOption] = []
    conflicts: list[ConflictGroup] = []

    # library filters
    search: str = ""
    scope_filter: str = "All"

    # create/edit form
    show_form: bool = False
    editing_id: int = 0
    form_title: str = ""
    form_explanation: str = ""
    form_tags: list[str] = []
    form_scope: str = "firm"
    form_client_id: int = 0
    form_summary: str = ""
    suggest_pattern: str = ""
    draft: str = ""

    # detail
    selected_id: int = 0
    versions: list[VersionRow] = []

    # approval queue
    proposals: list[ProposalRow] = []
    prop_title: dict[str, str] = {}
    prop_explanation: dict[str, str] = {}

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    @rx.var
    def can_manage(self) -> bool:
        return "c5.manage" in self._codes()

    @rx.var
    def has_conflicts(self) -> bool:
        return len(self.conflicts) > 0

    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "c5.view" not in self._codes():
            return rx.redirect("/")
        self.touchpoints = [
            TouchpointOption(key=t["key"], label=t["label"]) for t in c5.list_touchpoints()
        ]
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self._load_all()

    def _load_all(self) -> None:
        self._load_instructions()
        self._load_conflicts()
        self._load_proposals()
        if self.selected_id:
            self._load_versions()

    def _tp_map(self) -> dict[str, str]:
        return {t.key: t.label for t in self.touchpoints}

    def _load_instructions(self) -> None:
        tp_map = self._tp_map()
        rows = c5.list_instructions()
        if self.search:
            s = self.search.lower()
            rows = [
                i
                for i in rows
                if s in (i["title"] or "").lower() or s in (i["explanation"] or "").lower()
            ]
        if self.scope_filter == "Firm-wide":
            rows = [i for i in rows if i["scope"] == "firm"]
        elif self.scope_filter == "Client-specific":
            rows = [i for i in rows if i["scope"] == "client"]
        self.instructions = [
            InstructionRow(
                instruction_id=i["instruction_id"],
                title=i["title"],
                explanation=i.get("explanation") or "",
                scope=i["scope"],
                client_id=i.get("client_id") or 0,
                is_active=bool(i["is_active"]),
                tags=i.get("tags") or [],
                tag_labels=[tp_map.get(t, t) for t in (i.get("tags") or [])],
            )
            for i in rows
        ]

    def _load_versions(self) -> None:
        if not self.selected_id:
            self.versions = []
            return
        raw = c5.instruction_versions(self.selected_id)
        self.versions = [
            VersionRow(
                change_summary=v.get("change_summary") or "",
                title=v.get("title") or "",
                created_at=str(v.get("created_at") or "").split("T")[0],
                changed_by=v.get("changed_by") or "",
                is_current=(idx == 0),
            )
            for idx, v in enumerate(raw)
        ]

    def _load_conflicts(self) -> None:
        self.conflicts = [
            ConflictGroup(
                touchpoint=g["touchpoint"],
                scope=g["scope"],
                names=[i["title"] for i in g["instructions"]],
            )
            for g in c5.conflicting_groups()
        ]

    def _load_proposals(self) -> None:
        self.proposals = [
            ProposalRow(
                proposal_id=p["proposal_id"],
                title=p["title"],
                explanation=p.get("explanation") or "",
                submitted_by=p.get("submitted_by") or "",
                submitted_at=str(p.get("submitted_at") or "").split("T")[0],
                source=p.get("source") or "",
                tags=p.get("tags") or [],
            )
            for p in c5.list_proposals(status="pending")
        ]
        for p in self.proposals:
            self.prop_title.setdefault(str(p.proposal_id), p.title)
            self.prop_explanation.setdefault(str(p.proposal_id), p.explanation)

    # ------------------------------------------------------------------
    # Nav + filters
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    def set_search(self, v: str):
        self.search = v
        self._load_instructions()

    def set_scope_filter(self, v: str):
        self.scope_filter = v
        self._load_instructions()

    # ------------------------------------------------------------------
    # Form
    # ------------------------------------------------------------------
    @rx.event
    def toggle_form(self):
        self.show_form = not self.show_form
        if self.show_form:
            self.editing_id = 0
            self.form_title = ""
            self.form_explanation = ""
            self.form_tags = []
            self.form_scope = "firm"
            self.form_client_id = 0
            self.form_summary = ""
            self.draft = ""

    def set_form_title(self, v: str):
        self.form_title = v

    def set_form_explanation(self, v: str):
        self.form_explanation = v

    def set_form_tags(self, v: list[str]):
        self.form_tags = v

    @rx.event
    def toggle_form_tag(self, key: str):
        if key in self.form_tags:
            self.form_tags = [k for k in self.form_tags if k != key]
        else:
            self.form_tags = [*self.form_tags, key]

    def set_form_scope(self, v: str):
        self.form_scope = v

    def set_form_client_id(self, v: int):
        self.form_client_id = v

    def set_form_summary(self, v: str):
        self.form_summary = v

    def set_suggest_pattern(self, v: str):
        self.suggest_pattern = v

    @rx.event
    def suggest_wording(self):
        try:
            self.draft = c5.suggest_wording(self.suggest_pattern)
        except c5.C5Error as exc:
            self.error = str(exc)

    @rx.event
    def use_draft(self):
        """Copy the AI draft into the explanation field. Manual review is
        still required before saving — never auto-saved as active."""
        self.form_explanation = self.draft
        self.draft = ""

    @rx.event
    def save_instruction(self):
        self.error = ""
        self.flash = ""
        try:
            if self.editing_id:
                c5.edit_instruction(
                    self.editing_id,
                    title=self.form_title,
                    explanation=self.form_explanation,
                    scope=self.form_scope,
                    client_id=self.form_client_id if self.form_scope == "client" else None,
                    touchpoint_keys=self.form_tags,
                    change_summary=self.form_summary,
                    actor=self.username,
                )
                self.flash = "Instruction updated — new version recorded."
            else:
                c5.create_instruction(
                    title=self.form_title,
                    explanation=self.form_explanation,
                    scope=self.form_scope,
                    client_id=self.form_client_id if self.form_scope == "client" else None,
                    touchpoint_keys=self.form_tags,
                    actor=self.username,
                )
                self.flash = "Instruction created."
        except c5.C5Error as exc:
            self.error = str(exc)
            return
        self.show_form = False
        self.editing_id = 0
        self._load_all()

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------
    @rx.event
    def open_instruction(self, instruction_id: int):
        self.selected_id = 0 if self.selected_id == instruction_id else instruction_id
        self._load_versions()

    @rx.event
    def edit_instruction(self, instruction_id: int):
        inst = c5.get_instruction(instruction_id)
        if inst is None:
            return
        self.editing_id = instruction_id
        self.show_form = True
        self.form_title = inst["title"]
        self.form_explanation = inst["explanation"]
        self.form_tags = list(inst.get("tags") or [])
        self.form_scope = inst["scope"]
        self.form_client_id = inst.get("client_id") or 0
        self.form_summary = ""

    @rx.event
    def toggle_active(self, instruction_id: int, active: bool):
        c5.set_instruction_active(instruction_id, active, actor=self.username)
        self._load_all()

    # ------------------------------------------------------------------
    # Approval queue
    # ------------------------------------------------------------------
    def set_prop_title(self, proposal_id: int, v: str):
        self.prop_title[str(proposal_id)] = v

    def set_prop_explanation(self, proposal_id: int, v: str):
        self.prop_explanation[str(proposal_id)] = v

    @rx.event
    def approve_proposal(self, proposal_id: int):
        try:
            c5.approve_proposal(
                proposal_id,
                title=self.prop_title.get(str(proposal_id), ""),
                explanation=self.prop_explanation.get(str(proposal_id), ""),
                actor=self.username,
            )
            self.flash = "Proposal approved and activated."
        except c5.C5Error as exc:
            self.error = str(exc)
        self._load_all()

    @rx.event
    def reject_proposal(self, proposal_id: int):
        c5.reject_proposal(proposal_id, actor=self.username)
        self.flash = "Proposal rejected."
        self._load_all()