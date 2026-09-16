"""Module 2 state — Reconciliation Engine (2A GST / 2B TDS / 2C Other).

AI-FIRST: the exception detail leads with the AI analysis (issue summary,
probable causes, suggested fix, reasoning, confidence) before the raw
books-vs-portal comparison. The TDS narration classifier is the third active
AI touchpoint. Every handler calls ``src.module2.service`` / ``src.ai_analysis``
— no business logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src import ai_analysis, db
from src.auth import service as auth
from src.clients import service as clients
from src.module2 import service as m2
from setu.state.auth_state import AuthState

SUB_TYPE_LABELS = {"2A": "2A — GST", "2B": "2B — TDS", "2C": "2C — Other"}
CLASSIFICATION_ORDER = ["Amount Difference", "Not in Portal", "Not in Books"]


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class ExceptionRow:
    exception_id: int
    classification: str
    difference_type: str
    sub_type_label: str
    priority: str
    status: str
    confidence_score: int
    confidence_band: str
    confidence_source: str
    above_materiality: bool
    match_reason: str
    recommendation: str


@dataclass
class RecordField:
    label: str
    value: str


@dataclass
class ChainStage:
    label: str
    state: str
    detail: str


@dataclass
class ConfigRow:
    sub_type: str
    label: str
    match_keys: str
    tolerance: str
    date_tolerance: str
    fuzzy_threshold: str
    materiality: str


class Module2State(AuthState):
    """Reconciliation Engine state."""

    section: str = "Reconciliation overview"

    client_options: list[ClientOption] = []
    client_id: int = 0

    # overview
    eligible_credit: str = ""
    eligible_period: str = ""
    total_itc: str = ""
    matched_itc: str = ""
    at_risk_itc: str = ""
    blocked_itc: str = ""
    reverse_charge_itc: str = ""
    has_credit: bool = False
    total_exceptions: int = 0
    open_exceptions: int = 0
    escalated_exceptions: int = 0
    by_classification: dict[str, int] = {}
    by_sub_type: dict[str, int] = {}

    # queue
    filter_sub_type: str = "All"
    filter_classification: str = "All"
    exceptions: list[ExceptionRow] = []

    # detail
    selected_exception_id: int = 0
    detail_classification: str = ""
    detail_difference_type: str = ""
    detail_sub_type: str = ""
    detail_sub_type_label: str = ""
    detail_status: str = ""
    detail_priority: str = ""
    detail_confidence_score: int = 0
    detail_confidence_band: str = ""
    detail_confidence_source: str = ""
    detail_above_materiality: bool = False
    detail_materiality: str = ""
    detail_match_reason: str = ""
    detail_recommendation: str = ""
    detail_recommendation_reason: str = ""
    books_fields: list[RecordField] = []
    portal_fields: list[RecordField] = []
    chain_stages: list[ChainStage] = []

    # AI analysis (AI-first)
    ai_eligible: bool = False
    ai_status: str = ""  # "" | success | failed
    ai_issue_summary: str = ""
    ai_causes: list[str] = []
    ai_suggested_fix: str = ""
    ai_reasoning: str = ""
    ai_confidence: str = ""
    ai_model: str = ""
    ai_data_sufficient: bool = True
    ai_error_type: str = ""
    ai_error_detail: str = ""
    ai_cached: bool = False
    ai_route_hint: str = ""

    # resolve
    resolve_note: str = ""
    can_resolve: bool = False

    # matching configuration
    configs: list[ConfigRow] = []

    # TDS narration classifier (AI touchpoint)
    narration: str = ""
    tds_section: str = ""
    tds_rate: str = ""
    tds_reasoning: str = ""
    tds_available: bool = False
    tds_c5_used: bool = False

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    @rx.var
    def count_amount_difference(self) -> int:
        return self.by_classification.get("Amount Difference", 0)

    @rx.var
    def count_not_in_portal(self) -> int:
        return self.by_classification.get("Not in Portal", 0)

    @rx.var
    def count_not_in_books(self) -> int:
        return self.by_classification.get("Not in Books", 0)

    def _ai_config(self):
        try:
            return ai_analysis.load_ai_config()
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "module2.view" not in self._codes():
            return rx.redirect("/")
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        if not self.client_id and self.client_options:
            self.client_id = self.client_options[0].client_id
        self.can_resolve = "module2.exceptions.resolve" in self._codes()
        self._load_all()

    def _load_all(self) -> None:
        self._load_overview()
        self._load_queue()
        self._load_configs()
        if self.selected_exception_id:
            self._load_detail()

    def _load_overview(self) -> None:
        if not self.client_id:
            return
        summary = m2.exception_summary(client_id=self.client_id)
        self.total_exceptions = summary["total"]
        self.open_exceptions = summary["open"]
        self.escalated_exceptions = summary["escalated"]
        self.by_classification = summary["by_classification"]
        self.by_sub_type = summary["by_sub_type"]

        credit = m2.eligible_credit_for_client(self.client_id)
        if credit is None:
            self.has_credit = False
            self.eligible_credit = ""
            return
        self.has_credit = True
        self.eligible_period = credit.get("period") or ""
        self.eligible_credit = _money(credit["eligible_credit"])
        self.total_itc = _money(credit["total_itc_claimed"])
        self.matched_itc = _money(credit["matched_itc"])
        self.at_risk_itc = _money(credit["at_risk_itc"])
        self.blocked_itc = _money(credit["blocked_credit_itc"])
        self.reverse_charge_itc = _money(credit["reverse_charge_itc"])

    def _load_queue(self) -> None:
        if not self.client_id:
            self.exceptions = []
            return
        rows = m2.list_exceptions(
            client_id=self.client_id,
            sub_type=None if self.filter_sub_type == "All" else self.filter_sub_type,
            classification=None if self.filter_classification == "All" else self.filter_classification,
        )
        self.exceptions = [
            ExceptionRow(
                exception_id=e["exception_id"],
                classification=e["classification"],
                difference_type=e.get("difference_type") or "",
                sub_type_label=SUB_TYPE_LABELS.get(e["sub_type"], e["sub_type"]),
                priority=e["priority"],
                status=e["status"],
                confidence_score=int(e.get("confidence_score") or 0),
                confidence_band=e.get("confidence_band") or "",
                confidence_source=e.get("confidence_source") or "rule",
                above_materiality=bool(e.get("above_materiality")),
                match_reason=e.get("match_reason") or "",
                recommendation=e.get("recommendation") or "",
            )
            for e in rows
        ]

    def _load_configs(self) -> None:
        self.configs = [
            ConfigRow(
                sub_type=c["sub_type"],
                label=c["label"],
                match_keys=", ".join(c.get("match_keys") or []),
                tolerance=f"±{c['tolerance_absolute']} / {c['tolerance_percent']}%",
                date_tolerance=f"{c['date_tolerance_days']}d",
                fuzzy_threshold=str(int(c["fuzzy_threshold"])),
                materiality=_money(c["materiality_threshold"]),
            )
            for c in m2.list_matching_key_configs()
        ]

    def _load_detail(self) -> None:
        exc = m2.get_exception(self.selected_exception_id)
        if exc is None:
            self.selected_exception_id = 0
            return
        self.detail_classification = exc["classification"]
        self.detail_difference_type = exc.get("difference_type") or ""
        self.detail_sub_type = exc["sub_type"]
        self.detail_sub_type_label = SUB_TYPE_LABELS.get(exc["sub_type"], exc["sub_type"])
        self.detail_status = exc["status"]
        self.detail_priority = exc["priority"]
        self.detail_confidence_score = int(exc.get("confidence_score") or 0)
        self.detail_confidence_band = exc.get("confidence_band") or ""
        self.detail_confidence_source = exc.get("confidence_source") or "rule"
        self.detail_above_materiality = bool(exc.get("above_materiality"))
        self.detail_materiality = _money(exc.get("materiality_threshold") or 0)
        self.detail_match_reason = exc.get("match_reason") or ""
        self.detail_recommendation = exc.get("recommendation") or ""
        self.detail_recommendation_reason = exc.get("recommendation_reason") or ""

        evidence = exc.get("evidence") or {}
        self.books_fields = _record_fields(evidence.get("books_record"))
        self.portal_fields = _record_fields(evidence.get("portal_record"))

        self.chain_stages = [
            ChainStage(
                label=s["stage_label"],
                state=s["stage_state"],
                detail=s.get("detail") or "",
            )
            for s in m2.tds_chain_for_exception(self.selected_exception_id)
        ]

        self._load_ai(exc)

    def _load_ai(self, exc: dict) -> None:
        """Load the AI analysis state for the current exception (AI-first)."""
        config = self._ai_config()
        self.ai_eligible = bool(
            config and ai_analysis.is_analyzable_classification(exc["classification"], config)
        )
        self.ai_status = ""
        self.ai_issue_summary = ""
        self.ai_causes = []
        self.ai_suggested_fix = ""
        self.ai_reasoning = ""
        self.ai_confidence = ""
        self.ai_model = ""
        self.ai_data_sufficient = True
        self.ai_error_type = ""
        self.ai_error_detail = ""
        self.ai_cached = False
        self.ai_route_hint = ""
        if not self.ai_eligible or config is None:
            return
        model_id, _reason = ai_analysis.select_model(
            exc["classification"], exc.get("difference_type"), config
        )
        self.ai_route_hint = model_id
        result_id = exc.get("result_id")
        if not result_id:
            return
        conn = db.get_connection()
        try:
            latest = ai_analysis.get_latest_analysis(conn, int(result_id))
        finally:
            conn.close()
        if latest is None:
            return
        self._apply_ai(latest, cached=False)

    def _apply_ai(self, rec: dict, *, cached: bool) -> None:
        self.ai_status = rec.get("status") or ""
        self.ai_cached = cached
        self.ai_model = rec.get("model_used") or ""
        if self.ai_status == "success":
            self.ai_issue_summary = rec.get("issue_summary") or ""
            causes = rec.get("probable_causes")
            self.ai_causes = causes if isinstance(causes, list) else []
            self.ai_suggested_fix = rec.get("suggested_fix") or ""
            self.ai_reasoning = rec.get("reasoning") or ""
            self.ai_confidence = str(rec.get("confidence") or "")
            ds = rec.get("data_sufficient")
            self.ai_data_sufficient = not (ds is False or ds == 0)
        else:
            self.ai_error_type = rec.get("error_type") or "api_error"
            self.ai_error_detail = rec.get("error_detail") or ""

    # ------------------------------------------------------------------
    # Nav + setters
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def set_client(self, client_id: int):
        self.client_id = client_id
        self.selected_exception_id = 0
        self._load_all()

    @rx.event
    def set_client_by_name(self, name: str):
        for o in self.client_options:
            if o.legal_name == name:
                self.client_id = o.client_id
                break
        self.selected_exception_id = 0
        self._load_all()

    @rx.event
    def set_filter_sub_type(self, v: str):
        self.filter_sub_type = v
        self._load_queue()

    @rx.event
    def set_filter_classification(self, v: str):
        self.filter_classification = v
        self._load_queue()

    @rx.event
    def goto_classification(self, cls: str):
        self.filter_classification = cls
        self.section = "Exception queue"
        self._load_queue()

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------
    @rx.event
    def open_exception(self, exception_id: int):
        self.selected_exception_id = exception_id
        self.resolve_note = ""
        self._load_detail()

    @rx.event
    def back_to_queue(self):
        self.selected_exception_id = 0
        self._load_queue()

    def set_resolve_note(self, v: str):
        self.resolve_note = v

    @rx.event
    def resolve(self, resolution: str):
        try:
            m2.resolve_exception(
                self.selected_exception_id,
                resolution=resolution,
                note=self.resolve_note or None,
                actor=self.username,
            )
            self.flash = f"Exception {resolution}."
            self.selected_exception_id = 0
        except m2.Module2Error as exc:
            self.error = str(exc)
        self._load_all()

    # ------------------------------------------------------------------
    # AI analysis (AI-first)
    # ------------------------------------------------------------------
    @rx.event
    def analyze(self, force: bool = False):
        self.flash = ""
        self.error = ""
        exc = m2.get_exception(self.selected_exception_id)
        if exc is None or not exc.get("result_id"):
            self.error = "No match result is linked to this exception."
            return
        config = self._ai_config()
        if config is None:
            self.error = "AI configuration could not be loaded."
            return
        conn = db.get_connection()
        try:
            out = ai_analysis.analyze_result(conn, int(exc["result_id"]), config, force=force)
        except ai_analysis.AIAnalysisError as exc2:
            self.error = str(exc2)
            return
        finally:
            conn.close()
        self._apply_ai(out, cached=bool(out.get("_cached")))
        if self.ai_status == "success":
            self.flash = "AI analysis complete." if not self.ai_cached else "AI analysis loaded from cache."
        else:
            self.flash = ""

    # ------------------------------------------------------------------
    # TDS narration classifier (AI touchpoint)
    # ------------------------------------------------------------------
    def set_narration(self, v: str):
        self.narration = v

    @rx.event
    def classify_narration(self):
        self.flash = ""
        self.error = ""
        out = m2.classify_tds_from_narration(self.narration, client_id=self.client_id or None)
        self.tds_available = bool(out.get("available"))
        self.tds_section = out.get("section") or ""
        rate = out.get("rate")
        self.tds_rate = f"{rate}%" if rate is not None else "—"
        self.tds_reasoning = out.get("reasoning") or ""
        self.tds_c5_used = bool(out.get("c5_context_used"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _money(v) -> str:
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _record_fields(record) -> list[RecordField]:
    if not isinstance(record, dict):
        return []
    out: list[RecordField] = []
    for k, v in record.items():
        if k.startswith("_") or v in (None, ""):
            continue
        out.append(RecordField(label=str(k).replace("_", " "), value=str(v)))
    return out
