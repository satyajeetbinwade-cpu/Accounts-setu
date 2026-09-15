"""Core domain models for the reconciliation engine."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class MatchStatus(str, Enum):
    MATCHED = "matched"
    NOT_IN_BOOKS = "not_in_books"      # in portal, not in Tally
    NOT_IN_PORTAL = "not_in_portal"    # in Tally, not in portal
    AMOUNT_DIFF = "amount_diff"        # matched on GSTIN+invoice but value differs


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Invoice:
    """A normalised supplier invoice (from portal or Tally)."""
    gstin: str
    invoice_no: str
    date: date
    taxable_value: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")
    igst: Decimal = Decimal("0")
    source: str = ""                  # "portal" | "tally"
    status: str = ""                  # supplier ITC status for portal rows
    reverse_charge: bool = False      # RCM supply  - ITC not claimable
    blocked_credit: bool = False      # Section 17(5) - ITC blocked
    reference: str = ""               # original ref / registry id

    @property
    def total(self) -> Decimal:
        return self.taxable_value + self.tax_amount

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        for k in ("taxable_value", "tax_amount", "cgst", "sgst", "igst"):
            d[k] = str(d[k])
        return d


@dataclass
class MatchResult:
    portal_invoice: Invoice
    book_invoice: Optional[Invoice] = None
    status: MatchStatus = MatchStatus.NOT_IN_BOOKS
    confidence: Confidence = Confidence.LOW
    match_score: float = 0.0          # higher = better
    edit_distance: int = 99
    date_delta: int = 99
    value_delta_pct: Optional[float] = None   # None if unmatched
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    materiality: str = "none"          # above | below | none (vs materiality gate)
    ims_recommendation: str = "pending"  # Accept | Reject | Pending | N/A

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["confidence"] = self.confidence.value
        d["materiality"] = self.materiality
        d["ims_recommendation"] = self.ims_recommendation
        d["portal_invoice"] = self.portal_invoice.to_dict()
        d["book_invoice"] = self.book_invoice.to_dict() if self.book_invoice else None
        return d


@dataclass
class ITCComputation:
    eligible_itc: Decimal = Decimal("0")
    ineligible_itc: Decimal = Decimal("0")
    eligible_count: int = 0
    ineligible_count: int = 0
    gstr3b_draft: dict[str, Decimal | str] = field(default_factory=dict)
    itc_position: dict[str, Decimal | str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        position = {k: (str(v) if isinstance(v, Decimal) else v)
                    for k, v in self.itc_position.items()}
        return {
            "eligible_itc": str(self.eligible_itc),
            "ineligible_itc": str(self.ineligible_itc),
            "eligible_count": self.eligible_count,
            "ineligible_count": self.ineligible_count,
            "itc_position": position,
            "gstr3b_draft": {k: (str(v) if isinstance(v, Decimal) else v)
                             for k, v in self.gstr3b_draft.items()},
        }


@dataclass
class DraftedJV:
    """A corrective entry draft, always balanced (dr == cr)."""
    exception_ref: str
    voucher_date: date
    narration: str
    lines: list[dict[str, str | Decimal]] = field(default_factory=list)  # [{account, dr, cr}]
    rationale: str = ""

    def is_balanced(self) -> bool:
        total_dr = sum(Decimal(l.get("dr", 0)) for l in self.lines)
        total_cr = sum(Decimal(l.get("cr", 0)) for l in self.lines)
        return total_dr == total_cr

    def to_dict(self) -> dict[str, Any]:
        return {
            "exception_ref": self.exception_ref,
            "voucher_date": self.voucher_date.isoformat(),
            "narration": self.narration,
            "lines": [
                {**l, "dr": str(l.get("dr", 0)), "cr": str(l.get("cr", 0))}
                for l in self.lines
            ],
            "rationale": self.rationale,
            "balanced": self.is_balanced(),
        }


@dataclass
class ReviewPacket:
    """The gate review packet: draft + evidence + confidence + explanation."""
    client_id: str
    period: str
    stage: int = 2
    packet_type: str = "recon_gst_2b"
    summary: dict[str, Any] = field(default_factory=dict)
    exceptions: list[MatchResult] = field(default_factory=list)
    itc: Optional[ITCComputation] = None
    drafted_jvs: list[DraftedJV] = field(default_factory=list)
    ai_summary: str = ""
    validation: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "period": self.period,
            "stage": self.stage,
            "packet_type": self.packet_type,
            "summary": self.summary,
            "exceptions": [e.to_dict() for e in self.exceptions],
            "itc": self.itc.to_dict() if self.itc else None,
            "drafted_jvs": [j.to_dict() for j in self.drafted_jvs],
            "ai_summary": self.ai_summary,
            "validation": self.validation,
            "created_at": self.created_at,
        }
