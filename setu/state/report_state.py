"""Module 8 — Visual Reconciliation Report state.

Presentation-layer state over Module 2's existing output. Every dataset here
is computed by ``src.action_center.report`` (read-only aggregation) — no new
data model, no new storage, no matching logic. The State only shapes that
output for the charts, the detail table and the evidence modal, and holds the
click-to-filter selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import reflex as rx

from src.action_center import report as report_svc
from src.auth import service as auth
from setu.foundation import tokens as t
from setu.state.auth_state import AuthState

# Foundation semantic roles → token hex. Charts NEVER introduce a new hex.
_ROLE_COLOR = {
    "rule": t.Color.RULE.value,
    "ai": t.Color.AI.value,
    "danger": t.Color.DANGER.value,
    "accent": t.Color.ACCENT.value,
    "neutral": t.Color.NEUTRAL.value,
}


@dataclass
class PeriodOption:
    label: str
    client_folder: str
    period: str
    has_gst: bool
    has_tds: bool


@dataclass
class DetailRow:
    result_id: int
    side: str
    classification: str
    bucket: str
    difference_type: str
    party: str
    reference: str
    date: str
    value: str
    books_value: str
    portal_value: str
    confidence_band: str
    match_reason: str


@dataclass
class FieldRow:
    label: str
    value: str


@dataclass
class QualityNote:
    title: str
    detail: str
    kind: str


@dataclass
class CheckRow:
    label: str
    result: str
    detail: str


class ReportState(AuthState):
    """The visual reconciliation report."""

    # selection
    period_options: list[PeriodOption] = []
    selected_label: str = ""
    loaded: bool = False
    error: str = ""

    # header
    client_name: str = ""
    period: str = ""
    generated_at: str = ""
    has_gst: bool = False
    has_tds: bool = False

    # KPI row
    match_rate: str = "—"
    matched_count: int = 0
    total_count: int = 0
    exception_count: int = 0
    exceptions_value: str = "—"
    itc_eligible: str = "—"
    itc_eligible_available: bool = False
    net_payable: str = "—"
    net_payable_available: bool = False
    net_payable_reason: str = ""

    # charts — GST
    gst_slices: list[dict[str, Any]] = []
    gst_slice_colors: list[str] = []
    itc_slices: list[dict[str, Any]] = []
    itc_slice_colors: list[str] = []
    itc_note: str = ""

    # charts — TDS
    tds_slices: list[dict[str, Any]] = []
    tds_slice_colors: list[str] = []
    tds_deposit_bars: list[dict[str, Any]] = []
    tds_deposit_colors: list[str] = []

    # charts — shared
    supplier_bars: list[dict[str, Any]] = []
    trend_bars: list[dict[str, Any]] = []
    trend_available: bool = False
    trend_message: str = ""

    # narrative
    narrative_text: str = ""
    narrative_source: str = ""
    narrative_guard_passed: bool = True
    narrative_guard_note: str = ""
    ai_available: bool = False

    # data quality + integrity
    quality_notes: list[QualityNote] = []
    f5_checks: list[CheckRow] = []
    verification_checks: list[CheckRow] = []
    integrity_overall: str = "—"

    # filters (click-to-filter)
    active_side: str = ""      # "" | "GST" | "TDS"
    active_bucket: str = ""    # "" = all
    active_party: str = ""
    filter_label: str = "All records"

    # detail table
    detail_rows: list[DetailRow] = []
    detail_count: int = 0

    # evidence modal
    modal_open: bool = False
    modal_title: str = ""
    modal_classification: str = ""
    modal_match_reason: str = ""
    modal_books: list[FieldRow] = []
    modal_portal: list[FieldRow] = []
    modal_deltas: list[FieldRow] = []

    # raw report (kept for re-filtering without recompute)
    _report: dict[str, Any] = {}

    # ------------------------------------------------------------------
    def _codes(self) -> set[str]:
        user = auth.current_user(self.session_token or None)
        return auth.effective_permissions(user) if user else set()

    @rx.event
    def load(self):
        if (deny := self._gate("action_center.view")):
            return rx.redirect(deny)
        self.period_options = [
            PeriodOption(
                label=p["label"],
                client_folder=p["client_folder"],
                period=p["period"],
                has_gst="GST" in p["recon_types"],
                has_tds="TDS" in p["recon_types"],
            )
            for p in report_svc.list_report_periods()
        ]
        if not self.selected_label and self.period_options:
            # Prefer a period that has both GST and TDS so the full report shows.
            both = next((p for p in self.period_options if p.has_gst and p.has_tds), None)
            self.selected_label = (both or self.period_options[0]).label
        self._build()

    @rx.event
    def select_period(self, label: str):
        self.selected_label = label
        self.active_side = ""
        self.active_bucket = ""
        self._build()

    def _build(self) -> None:
        self.error = ""
        self.loaded = False
        opt = next((p for p in self.period_options if p.label == self.selected_label), None)
        if opt is None:
            return
        try:
            rep = report_svc.build_period_report(opt.client_folder, opt.period)
        except report_svc.ReportError as exc:
            self.error = str(exc)
            return
        self._report = rep
        self.loaded = True

        self.client_name = rep["client_name"]
        self.period = rep["period"]
        self.generated_at = rep["generated_at"]
        self.has_gst = rep["has_gst"]
        self.has_tds = rep["has_tds"]

        kpi = rep["kpi"]
        self.match_rate = f"{kpi['match_rate']}%"
        self.matched_count = kpi["matched_count"]
        self.total_count = kpi["total_count"]
        self.exception_count = kpi["exception_count"]
        self.exceptions_value = _money(kpi["exceptions_value"])
        self.itc_eligible_available = kpi.get("itc_eligible") is not None
        self.itc_eligible = _money(kpi["itc_eligible"]) if self.itc_eligible_available else "—"
        self.net_payable_available = False
        self.net_payable = "—"
        self.net_payable_reason = (
            "No output-tax / GSTR-3B source is part of this reconciliation, so net payable "
            "cannot be computed from these files."
        )

        # GST charts
        gst = rep.get("gst")
        if gst:
            self.gst_slices = gst["classification_slices"]
            self.gst_slice_colors = [_ROLE_COLOR.get(s["color_role"], t.Color.NEUTRAL.value) for s in self.gst_slices]
            self.itc_slices = (gst.get("gst") or {}).get("itc_slices") or []
            self.itc_slice_colors = [_ROLE_COLOR.get(s["color_role"], t.Color.NEUTRAL.value) for s in self.itc_slices]
            self.itc_note = (gst.get("gst") or {}).get("itc_note") or ""
        else:
            self.gst_slices = []
            self.gst_slice_colors = []
            self.itc_slices = []
            self.itc_slice_colors = []
            self.itc_note = ""

        # TDS charts
        tds = rep.get("tds")
        if tds:
            self.tds_slices = tds["classification_slices"]
            self.tds_slice_colors = [_ROLE_COLOR.get(s["color_role"], t.Color.NEUTRAL.value) for s in self.tds_slices]
            self.tds_deposit_bars = (tds.get("tds") or {}).get("deducted_vs_deposited") or []
            self.tds_deposit_colors = [_ROLE_COLOR.get(s["color_role"], t.Color.NEUTRAL.value) for s in self.tds_deposit_bars]
        else:
            self.tds_slices = []
            self.tds_slice_colors = []
            self.tds_deposit_bars = []
            self.tds_deposit_colors = []

        # Shared charts
        self.supplier_bars = rep["suppliers"][:10]
        trend = rep["trend"]
        self.trend_available = trend["available"]
        self.trend_message = trend["message"]
        if trend["available"]:
            self.trend_bars = [
                {"name": f"{trend['prior_period']} (prior)", "rate": trend["prior_rate"]},
                {"name": f"{self.period} (this)", "rate": trend["this_rate"]},
            ]
        else:
            self.trend_bars = []

        # Narrative
        nar = rep["narrative"]
        self.narrative_text = nar["text"]
        self.narrative_source = nar["source"]
        self.narrative_guard_passed = nar["guard"]["passed"]
        self.narrative_guard_note = nar.get("ai_error") or ""

        # Data quality + integrity
        self.quality_notes = [
            QualityNote(title=n["title"], detail=n["detail"], kind=n["kind"])
            for n in rep["data_quality"]["notes"]
        ]
        self.f5_checks = [CheckRow(**c) for c in rep["integrity"]["f5"]]
        self.verification_checks = [CheckRow(**c) for c in rep["integrity"]["verification"]]
        self.integrity_overall = rep["integrity"]["overall"]

        self._apply_filter()

    # ------------------------------------------------------------------
    # Click-to-filter
    # ------------------------------------------------------------------
    @rx.event
    def filter_bucket(self, payload: dict):
        """A chart segment was clicked. The payload is the sector/bar's own
        data object, which carries the bucket `key` and the `side`."""
        if not isinstance(payload, dict):
            return
        key = payload.get("key") or payload.get("name") or ""
        side = payload.get("side") or ""
        kind = payload.get("kind") or "bucket"
        if kind == "party":
            # Supplier bar → filter the table to that party's exceptions.
            if key == self.active_party:
                self.active_party = ""
            else:
                self.active_party = str(key)
                self.active_bucket = "__exceptions__"
                self.active_side = ""
        elif kind == "itc":
            self.active_side = "GST"
            self.active_bucket = ""
            self.active_party = ""
        elif kind == "tds_deposit":
            self.active_side = "TDS"
            self.active_bucket = ""
            self.active_party = ""
        else:
            # Classification donut → toggle the bucket.
            if key == self.active_bucket and side == self.active_side:
                self.active_bucket = ""
                self.active_side = ""
            else:
                self.active_bucket = str(key)
                self.active_side = str(side)
                self.active_party = ""
        self._apply_filter()

    @rx.event
    def filter_kpi(self, which: str):
        self.active_party = ""
        if which == "match":
            self.active_bucket = ""
            self.active_side = ""
        elif which == "itc":
            self.active_side = "GST"
            self.active_bucket = ""
        elif which == "exceptions":
            self.active_side = ""
            self.active_bucket = "__exceptions__"
        elif which == "net":
            return  # no data — the card explains why
        self._apply_filter()

    @rx.event
    def clear_filter(self):
        self.active_bucket = ""
        self.active_side = ""
        self.active_party = ""
        self._apply_filter()

    def _apply_filter(self) -> None:
        rep = self._report
        if not rep:
            self.detail_rows = []
            self.detail_count = 0
            return

        rows: list[DetailRow] = []
        for side_key, side_label in (("gst", "GST"), ("tds", "TDS")):
            side = rep.get(side_key)
            if not side:
                continue
            if self.active_side and self.active_side != side_label:
                continue
            for r in side["records"]:
                if self.active_bucket == "__exceptions__":
                    if r["bucket"] == "Matched":
                        continue
                elif self.active_bucket and r["bucket"] != self.active_bucket:
                    continue
                if self.active_party and r["party"] != self.active_party:
                    continue
                rows.append(DetailRow(
                    result_id=r["result_id"],
                    side=side_label,
                    classification=r["classification"],
                    bucket=r["bucket"],
                    difference_type=r["difference_type"],
                    party=r["party"] or "—",
                    reference=r["reference"] or "—",
                    date=r["date"] or "—",
                    value=_money(r["value"]),
                    books_value=_money(r["books_value"]),
                    portal_value=_money(r["portal_value"]),
                    confidence_band=r["confidence_band"],
                    match_reason=r["match_reason"],
                ))
        self.detail_rows = rows
        self.detail_count = len(rows)

        if self.active_party:
            self.filter_label = f"Party: {self.active_party}"
        elif self.active_bucket == "__exceptions__":
            self.filter_label = "All exceptions"
        elif self.active_bucket:
            self.filter_label = f"{self.active_bucket}" + (f" · {self.active_side}" if self.active_side else "")
        elif self.active_side:
            self.filter_label = f"All {self.active_side} records"
        else:
            self.filter_label = "All records"

    # ------------------------------------------------------------------
    # Evidence modal
    # ------------------------------------------------------------------
    @rx.event
    def open_evidence(self, result_id: int, side: str):
        rep = self._report
        side_key = "gst" if side == "GST" else "tds"
        side_rep = rep.get(side_key)
        if not side_rep:
            return
        rec = next((r for r in side_rep["records"] if r["result_id"] == result_id), None)
        if rec is None:
            return
        self.modal_open = True
        self.modal_title = f"{side} · {rec['reference'] or 'record'} · {rec['party'] or '—'}"
        self.modal_classification = rec["classification"] + (
            f" — {rec['difference_type']}" if rec["difference_type"] else ""
        )
        self.modal_match_reason = rec["match_reason"]
        self.modal_books = [FieldRow(label=f["label"], value=f["value"]) for f in rec["books_fields"]]
        self.modal_portal = [FieldRow(label=f["label"], value=f["value"]) for f in rec["portal_fields"]]
        self.modal_deltas = _deltas(rec)

    @rx.event
    def close_evidence(self):
        self.modal_open = False

    def set_modal_open(self, v: bool):
        self.modal_open = bool(v)

    # ------------------------------------------------------------------
    @rx.event
    def draft_narrative(self):
        """Explicitly request the AI-drafted narrative. It is guard-verified;
        on any failure the deterministic text is kept."""
        if not self._report:
            return
        nar = report_svc.generate_narrative(self._report, allow_ai=True)
        self.narrative_text = nar["text"]
        self.narrative_source = nar["source"]
        self.narrative_guard_passed = nar["guard"]["passed"]
        self.narrative_guard_note = nar.get("ai_error") or ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _money(v) -> str:
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _deltas(rec: dict[str, Any]) -> list[FieldRow]:
    """Match deltas between the books and portal sides, for the evidence modal.

    Only comparable record fields are considered — raw passthrough fields
    (original row JSON, source file/type) are not meaningful deltas.
    """
    _SKIP = {"original row", "source file", "source type"}
    books = {f["label"]: f["value"] for f in rec["books_fields"]}
    portal = {f["label"]: f["value"] for f in rec["portal_fields"]}
    out: list[FieldRow] = []
    for label in sorted(set(books) | set(portal)):
        if label in _SKIP:
            continue
        b = books.get(label, "—")
        p = portal.get(label, "—")
        if b != p:
            out.append(FieldRow(label=label, value=f"books {b}  ·  portal {p}"))
    return out
