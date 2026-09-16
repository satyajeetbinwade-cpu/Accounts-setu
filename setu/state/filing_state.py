"""C2 state — Portal Connect & Filing.

Filing Calendar (statutory due dates from C1), Filings (prepare → Send →
outcome → acknowledgement), Connection & Sources (per-client/per-source
API/manual toggle, dormant until C4 credentials exist). Every handler calls
``src.filing.service``.

File uploads use Reflex's ``rx.upload``; the handlers are async because
``UploadFile.read()`` is a coroutine.
"""

from __future__ import annotations

from dataclasses import dataclass

import reflex as rx

from src.auth import service as auth
from src.clients import service as clients
from src.documents import service as documents
from src.filing import service as filing
from src.rules import service as rules
from setu.state.auth_state import AuthState

SOURCE_LABELS = {"gst_portal": "GST Portal", "traces": "TRACES"}

# Urgency tiers (confidence badge 3.1 adapted to days-until-due).
_URGENCY_OVERDUE_DAYS = 0
_URGENCY_SOON_DAYS = 3


@dataclass
class ClientOption:
    client_id: int
    legal_name: str


@dataclass
class CalendarRow:
    client_label: str
    obligation: str
    period: str
    due_date: str
    urgency_variant: str
    urgency_label: str


@dataclass
class FilingRow:
    filing_id: int
    obligation: str
    period: str
    client_label: str
    status: str
    status_label: str


@dataclass
class AuditRow:
    action: str
    actor: str
    detail: str
    created_at: str


@dataclass
class SourceRow:
    source: str
    label: str
    api_available: bool
    mode: str
    unavailable_reason: str


class FilingState(AuthState):
    """Portal Connect & Filing state."""

    section: str = "Filing Calendar"

    client_options: list[ClientOption] = []
    client_label_map: dict[str, str] = {}

    # calendar
    calendar: list[CalendarRow] = []
    build_client_id: int = 0
    build_period: str = ""

    # filings hub
    filings: list[FilingRow] = []
    open_count: int = 0

    # new filing form
    new_client_id: int = 0
    new_obligation: str = ""
    new_period: str = ""
    obligation_options: list[str] = []

    # filing detail
    selected_filing_id: int = 0
    detail_obligation: str = ""
    detail_period: str = ""
    detail_client: str = ""
    detail_status: str = ""
    detail_status_label: str = ""
    detail_package_doc: str = ""
    detail_outcome_note: str = ""
    detail_sent_by: str = ""
    detail_sent_at: str = ""
    detail_ack_doc: str = ""
    audit: list[AuditRow] = []

    # send
    send_confirm: bool = False
    can_send: bool = False
    send_deny_reason: str = ""

    # outcome
    outcome_choice: str = "acknowledged"
    outcome_note: str = ""

    # acknowledgement
    ack_confirm: bool = False

    # sources
    source_client_id: int = 0
    sources: list[SourceRow] = []

    flash: str = ""
    error: str = ""

    # ------------------------------------------------------------------
    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    @rx.var
    def build_client_name(self) -> str:
        for o in self.client_options:
            if o.client_id == self.build_client_id:
                return o.legal_name
        return ""

    @rx.var
    def new_client_name(self) -> str:
        for o in self.client_options:
            if o.client_id == self.new_client_id:
                return o.legal_name
        return ""

    @rx.var
    def source_client_name(self) -> str:
        for o in self.client_options:
            if o.client_id == self.source_client_id:
                return o.legal_name
        return ""

    def _role(self) -> str:
        user = auth.current_user(self.session_token or None)
        return (user or {}).get("role_name", "")

    def _client_label(self, client_id: int) -> str:
        if client_id is None:
            return "—"
        return self.client_label_map.get(str(client_id), f"Client #{client_id}")

    # ------------------------------------------------------------------
    @rx.event
    def load(self):
        if "filing.view" not in self._codes():
            return rx.redirect("/")
        self.client_options = [
            ClientOption(client_id=c["client_id"], legal_name=c["legal_name"])
            for c in clients.list_clients(include_inactive=False)
        ]
        self.client_label_map = {
            str(c["client_id"]): c["legal_name"]
            for c in clients.list_clients(include_inactive=True)
        }
        self.obligation_options = [o["obligation"] for o in rules.list_statutory_due_dates()]
        if not self.build_client_id and self.client_options:
            self.build_client_id = self.client_options[0].client_id
        if not self.new_client_id and self.client_options:
            self.new_client_id = self.client_options[0].client_id
        if not self.source_client_id and self.client_options:
            self.source_client_id = self.client_options[0].client_id
        if not self.new_obligation and self.obligation_options:
            self.new_obligation = self.obligation_options[0]
        if not self.build_period:
            self.build_period = _default_period()
        if not self.new_period:
            self.new_period = _default_period()
        self._load_all()

    def _load_all(self) -> None:
        self._load_calendar()
        self._load_filings()
        self._load_sources()
        if self.selected_filing_id:
            self._load_detail()

    def _load_calendar(self) -> None:
        self.calendar = [
            CalendarRow(
                client_label=self._client_label(e["client_id"]),
                obligation=e["obligation"],
                period=e["period"],
                due_date=e["due_date"],
                urgency_variant=_urgency_variant(e.get("days_until_due")),
                urgency_label=_urgency_label(e.get("days_until_due")),
            )
            for e in filing.list_calendar()
        ]

    def _load_filings(self) -> None:
        rows = filing.list_filings()
        self.open_count = sum(1 for r in rows if r["status"] in ("prepared", "sent"))
        self.filings = [
            FilingRow(
                filing_id=r["filing_id"],
                obligation=r["obligation"],
                period=r["period"],
                client_label=self._client_label(r["client_id"]),
                status=r["status"],
                status_label=_status_label(r["status"]),
            )
            for r in rows
        ]

    def _load_detail(self) -> None:
        rec = filing.get_filing(self.selected_filing_id)
        if rec is None:
            self.selected_filing_id = 0
            return
        self.detail_obligation = rec["obligation"]
        self.detail_period = rec["period"]
        self.detail_client = self._client_label(rec["client_id"])
        self.detail_status = rec["status"]
        self.detail_status_label = _status_label(rec["status"])
        self.detail_package_doc = (
            f"document #{rec['package_document_id']}" if rec.get("package_document_id") else ""
        )
        self.detail_outcome_note = rec.get("outcome_note") or ""
        self.detail_sent_by = rec.get("sent_by") or ""
        self.detail_sent_at = str(rec.get("sent_at") or "").replace("T", " ").split(".")[0]
        self.detail_ack_doc = (
            f"document #{rec['acknowledgement_document_id']}"
            if rec.get("acknowledgement_document_id")
            else ""
        )
        allowed, reason = filing.can_send(self._role())
        self.can_send = allowed and ("filing.send" in self._codes())
        self.send_deny_reason = reason or ("You don't have permission to send filings." if not allowed else "")
        self.audit = [
            AuditRow(
                action=a["action"],
                actor=a["actor"],
                detail=a.get("detail") or "",
                created_at=str(a["created_at"]).replace("T", " ").split(".")[0],
            )
            for a in filing.list_audit_log(rec["filing_id"])
        ]

    def _load_sources(self) -> None:
        if not self.source_client_id:
            self.sources = []
            return
        self.sources = []
        for source in filing.SOURCE_KEYS:
            avail = filing.source_availability(self.source_client_id, source)
            self.sources.append(
                SourceRow(
                    source=source,
                    label=SOURCE_LABELS.get(source, source),
                    api_available=bool(avail["api_available"]),
                    mode=avail["mode"],
                    unavailable_reason=avail.get("unavailable_reason") or "",
                )
            )

    # ------------------------------------------------------------------
    # Nav + setters
    # ------------------------------------------------------------------
    @rx.event
    def set_section(self, section: str):
        self.section = section
        self.flash = ""
        self.error = ""

    @rx.event
    def set_build_client(self, client_id: int):
        self.build_client_id = client_id

    @rx.event
    def set_build_client_by_name(self, name: str):
        self.build_client_id = self._id_for_name(name)

    def set_build_period(self, v: str):
        self.build_period = v

    def _id_for_name(self, name: str) -> int:
        for o in self.client_options:
            if o.legal_name == name:
                return o.client_id
        return self.client_options[0].client_id if self.client_options else 0

    @rx.event
    def build_calendar(self):
        try:
            filing.build_calendar(self.build_client_id, periods=[self.build_period], actor=self.username)
            self.flash = "Calendar built."
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        self._load_calendar()

    @rx.event
    def set_new_client(self, client_id: int):
        self.new_client_id = client_id

    @rx.event
    def set_new_client_by_name(self, name: str):
        self.new_client_id = self._id_for_name(name)

    @rx.event
    def set_new_obligation(self, v: str):
        self.new_obligation = v

    def set_new_period(self, v: str):
        self.new_period = v

    @rx.event
    def set_source_client(self, client_id: int):
        self.source_client_id = client_id
        self._load_sources()

    @rx.event
    def set_source_client_by_name(self, name: str):
        self.source_client_id = self._id_for_name(name)
        self._load_sources()

    @rx.event
    def set_source_mode(self, source: str, mode: str):
        try:
            filing.set_toggle(self.source_client_id, source, mode, actor=self.username)
            self.flash = "Mode saved."
        except filing.FilingError as exc:
            self.error = str(exc)
        self._load_sources()

    # ------------------------------------------------------------------
    # Prepare filing package (async upload)
    # ------------------------------------------------------------------
    @rx.event
    async def prepare_filing(self, files: list[rx.UploadFile]):
        self.flash = ""
        self.error = ""
        if not files:
            self.error = "Upload a filing package first."
            return
        f = files[0]
        data = await f.read()
        try:
            result = documents.upload_document(
                client_id=self.new_client_id,
                filename=f.filename or "package",
                file_bytes=data,
                period=self.new_period or None,
                actor=self.username,
            )
        except documents.DocumentError as exc:
            self.error = str(exc)
            return
        try:
            filing_id = filing.prepare_filing(
                client_id=self.new_client_id,
                obligation=self.new_obligation,
                period=self.new_period,
                package_document_id=result["document_id"],
                actor=self.username,
            )
        except filing.FilingError as exc:
            self.error = str(exc)
            return
        self.flash = f"Filing package prepared (filing #{filing_id})."
        self.selected_filing_id = filing_id
        self._load_all()

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------
    @rx.event
    def open_filing(self, filing_id: int):
        self.selected_filing_id = filing_id
        self.send_confirm = False
        self.outcome_choice = "acknowledged"
        self.outcome_note = ""
        self.ack_confirm = False
        self._load_detail()

    @rx.event
    def back_to_filings(self):
        self.selected_filing_id = 0
        self._load_filings()

    def set_send_confirm(self, v: bool):
        self.send_confirm = v

    @rx.event
    def send_filing(self):
        try:
            filing.send_filing(
                self.selected_filing_id, actor=self.username, actor_role=self._role()
            )
            self.flash = "Filing sent."
            self.send_confirm = False
        except filing.FilingError as exc:
            self.error = str(exc)
        self._load_detail()

    @rx.event
    def set_outcome_choice(self, v: str):
        self.outcome_choice = v

    def set_outcome_note(self, v: str):
        self.outcome_note = v

    @rx.event
    def record_outcome(self):
        try:
            filing.record_outcome(
                self.selected_filing_id,
                status=self.outcome_choice,
                outcome_note=self.outcome_note or None,
                actor=self.username,
            )
            self.flash = "Outcome recorded."
        except filing.FilingError as exc:
            self.error = str(exc)
        self._load_detail()

    def set_ack_confirm(self, v: bool):
        self.ack_confirm = v

    @rx.event
    async def confirm_acknowledgement(self, files: list[rx.UploadFile]):
        self.flash = ""
        self.error = ""
        if not files:
            self.error = "Upload the portal receipt first."
            return
        f = files[0]
        data = await f.read()
        rec = filing.get_filing(self.selected_filing_id)
        if rec is None:
            self.error = "Filing record not found."
            return
        try:
            result = documents.upload_document(
                client_id=rec["client_id"],
                filename=f.filename or "acknowledgement",
                file_bytes=data,
                period=rec["period"],
                actor=self.username,
            )
            filing.confirm_acknowledgement(
                self.selected_filing_id,
                acknowledgement_document_id=result["document_id"],
                actor=self.username,
            )
            self.flash = "Acknowledgement captured — filing marked complete."
            self.ack_confirm = False
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        self._load_detail()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_period() -> str:
    from datetime import date

    today = date.today()
    return f"{today.year}-{today.month:02d}"


def _urgency_variant(days) -> str:
    if days is None:
        return "placeholder"
    if days < _URGENCY_OVERDUE_DAYS:
        return "danger"
    if days <= _URGENCY_SOON_DAYS:
        return "ai"
    return "rule"


def _urgency_label(days) -> str:
    if days is None:
        return "No due date"
    if days < _URGENCY_OVERDUE_DAYS:
        return f"Overdue by {-days} day(s)"
    return f"Due in {days} day(s)"


def _status_label(status: str) -> str:
    return {
        "prepared": "Prepared",
        "sent": "Sent",
        "acknowledged": "Acknowledged",
        "failed": "Failed",
        "ambiguous": "Ambiguous — verify manually",
    }.get(status, status)
