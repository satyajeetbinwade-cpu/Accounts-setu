"""Phase-3 review queue: Approve / Modify / Reject / Post workflow (Module 3).

A deterministic state machine over the review packet. No LLM here.

Transitions (documented, enforced):
    PENDING  --approve--> APPROVED      (JV items: requires is_balanced)
    PENDING  --modify-->  MODIFIED      (JV items: re-validates balance; blocks on
                                          unbalanced lines -> UnbalancedJVError)
    MODIFIED --approve--> APPROVED      (balance re-checked again)
    MODIFIED --reject-->  REJECTED
    APPROVED --post-----> POSTED        (only via the Tally post gate; poster
                                          must confirm the write)
    APPROVED --modify--> MODIFIED       (re-open for correction, balance re-checked)

Batch approval (M3 locked #2) applies ONLY to items tagged
batch_approvable: matched HIGH confidence AND below the materiality gate.
Nothing else is auto-approved -- above-threshold and IMS-Reject items stay
pending for individual review (PRD M2 locked #3).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .models import Confidence, DraftedJV, Invoice, MatchResult, MatchStatus, ReviewPacket
from .normalizer import make_invoice


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    MODIFIED = "modified"
    REJECTED = "rejected"
    POSTED = "posted"


class InvalidTransitionError(Exception):
    """An action is not allowed from the item's current status."""

    def __init__(self, ref: str, status: str, action: str):
        self.ref, self.status, self.action = ref, status, action
        super().__init__(f"cannot '{action}' item {ref!r} in status {status!r}")


class UnbalancedJVError(Exception):
    """A JV with dr != cr cannot be approved or modified/accepted."""

    def __init__(self, ref: str):
        super().__init__(f"JV for {ref!r} is unbalanced - action blocked")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
@dataclass
class AuditEntry:
    action: str
    actor: str
    note: str = ""
    at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Tally post gate (stub by design - engine never writes without approval)
# ---------------------------------------------------------------------------
class TallyPoster:
    """Posting gateway. Real ODBC write is deferred; the GATE is the point.

    post() only ever runs AFTER the item is APPROVED/MODIFIED, and only
    returns ok=True when a posting target is configured. The engine itself
    never calls Tally -- posting is an explicit reviewer action.
    """

    def __init__(self, dsn: str | None = None, available: bool | None = None):
        self._dsn = dsn
        self._available = bool(dsn) if available is None else available

    @classmethod
    def from_settings(cls, settings) -> "TallyPoster":
        return cls(dsn=settings.tally_odbc_dsn)

    def post(self, jv: DraftedJV | None) -> tuple[bool, str]:
        if jv is None or not jv.is_balanced():
            return False, "JV unbalanced - refusing to post"
        if not self._available:
            return False, (f"Tally ODBC unavailable (DSN={self._dsn or '(none)'})"
                           " - not posted")
        return True, "posted to Tally (stub - ODBC write deferred)"


# ---------------------------------------------------------------------------
# Queue item
# ---------------------------------------------------------------------------
@dataclass
class ReviewItem:
    ref: str
    kind: str                                  # "jv" | "exception"
    status: ReviewStatus = ReviewStatus.PENDING
    priority: str = "normal"                   # "high" | "normal"
    batch_approvable: bool = False
    jv: Optional[DraftedJV] = None
    match: Optional[MatchResult] = None
    assigned_to: str = ""
    due_date: str = ""
    audit: list[AuditEntry] = field(default_factory=list)

    # -- helpers ------------------------------------------------------------
    def _require_balanced(self) -> None:
        if self.kind == "jv" and self.jv is not None and not self.jv.is_balanced():
            raise UnbalancedJVError(self.ref)

    def _log(self, action: str, actor: str, note: str = "") -> None:
        self.audit.append(AuditEntry(action=action, actor=actor, note=note))

    # -- workflow actions ---------------------------------------------------
    def approve(self, actor: str, note: str = "") -> "ReviewItem":
        self._require_balanced()
        if self.status in (ReviewStatus.PENDING, ReviewStatus.MODIFIED,
                           ReviewStatus.APPROVED):
            self.status = ReviewStatus.APPROVED
            self._log("approve", actor, note)
            return self
        raise InvalidTransitionError(self.ref, self.status.value, "approve")

    def modify(self, actor: str, lines: list[dict],
               note: str = "") -> "ReviewItem":
        if self.kind != "jv" or self.jv is None:
            raise InvalidTransitionError(self.ref, self.status.value, "modify")
        if self.status in (ReviewStatus.REJECTED, ReviewStatus.POSTED):
            raise InvalidTransitionError(self.ref, self.status.value, "modify")
        self.jv.lines = lines
        if not self.jv.is_balanced():
            self._log("modify_blocked", actor,
                      "unbalanced lines rejected (dr != cr)")
            raise UnbalancedJVError(self.ref)
        # re-open after a correction from APPROVED, or finish a draft modify
        self.status = ReviewStatus.MODIFIED
        self._log("modify", actor, note)
        return self

    def reject(self, actor: str, note: str = "") -> "ReviewItem":
        if self.status in (ReviewStatus.PENDING, ReviewStatus.MODIFIED,
                           ReviewStatus.APPROVED):
            self.status = ReviewStatus.REJECTED
            self._log("reject", actor, note)
            return self
        raise InvalidTransitionError(self.ref, self.status.value, "reject")

    def post(self, actor: str, poster: TallyPoster | None = None,
             note: str = "") -> tuple[bool, str]:
        """Post an APPROVED/MODIFIED JV to Tally via the gate. None otherwise."""
        if self.status not in (ReviewStatus.APPROVED, ReviewStatus.MODIFIED):
            raise InvalidTransitionError(self.ref, self.status.value, "post")
        self._require_balanced()
        poster = poster or TallyPoster(dsn=None)
        ok, msg = poster.post(self.jv)
        if ok:
            self.status = ReviewStatus.POSTED
            self._log("post", actor, note or msg)
        else:
            self._log("post_failed", actor, msg)
        return ok, msg

    # -- serialization ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "status": self.status.value,
            "priority": self.priority,
            "batch_approvable": self.batch_approvable,
            "jv": self.jv.to_dict() if self.jv else None,
            "match": self.match.to_dict() if self.match else None,
            "assigned_to": self.assigned_to,
            "due_date": self.due_date,
            "audit": [a.to_dict() for a in self.audit],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReviewItem":
        return cls(
            ref=d["ref"],
            kind=d["kind"],
            status=ReviewStatus(d["status"]),
            priority=d.get("priority", "normal"),
            batch_approvable=d.get("batch_approvable", False),
            jv=_jv_from_dict(d["jv"]) if d.get("jv") else None,
            match=_match_from_dict(d["match"]) if d.get("match") else None,
            assigned_to=d.get("assigned_to", ""),
            due_date=d.get("due_date", ""),
            audit=[AuditEntry(**a) for a in d.get("audit", [])],
        )


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------
class ReviewQueue:
    """A review round over one client/period packet (Module 3 workflow)."""

    def __init__(self, client_id: str = "", period: str = "",
                 items: list[ReviewItem] | None = None,
                 created_at: str | None = None):
        self.client_id = client_id
        self.period = period
        self.items: list[ReviewItem] = items or []
        self.created_at = created_at or datetime.utcnow().isoformat()

    # -- construction -------------------------------------------------------
    @classmethod
    def from_packet(cls, packet: ReviewPacket,
                    actor: str = "system") -> "ReviewQueue":
        """Build the queue from a review packet.

        Batch-approvable = matched + HIGH confidence + below materiality.
        High priority    = above materiality OR IMS Reject.
        Corrective JVs (Not in Books) become 'jv' items -- low confidence,
        never batch-approvable, must pass individual review.
        """
        q = cls(client_id=packet.client_id, period=packet.period)
        jv_by_ref = {j.exception_ref: j for j in packet.drafted_jvs}
        for e in packet.exceptions:
            ref = e.portal_invoice.invoice_no
            jv = jv_by_ref.get(ref)
            kind = "jv" if jv is not None else "exception"
            batch = kind == "exception" and (
                e.status == MatchStatus.MATCHED
                and e.confidence == Confidence.HIGH
                and e.materiality == "below")
            priority = ("high" if (e.materiality == "above"
                                   or e.ims_recommendation == "Reject")
                        else "normal")
            item = ReviewItem(ref=ref, kind=kind, priority=priority,
                              batch_approvable=batch, jv=jv, match=e)
            item._log("created", actor, "queue item generated from review packet")
            q.items.append(item)
        return q

    # -- convenience --------------------------------------------------------
    def by_ref(self, ref: str) -> ReviewItem | None:
        return next((i for i in self.items if i.ref == ref), None)

    @property
    def refs(self) -> list[str]:
        return [i.ref for i in self.items]

    # -- workflow -----------------------------------------------------------
    def batch_approve(self, actor: str, note: str = "") -> list[str]:
        """Approve only batch-approvable items (M3 locked #2)."""
        approved: list[str] = []
        for item in self.items:
            if item.batch_approvable and item.status == ReviewStatus.PENDING:
                item.approve(actor, note)
                approved.append(item.ref)
        return approved

    # -- reporting ----------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        counts = {s.value: 0 for s in ReviewStatus}
        by_priority = {"high": 0, "normal": 0}
        for i in self.items:
            counts[i.status.value] += 1
            by_priority[i.priority] += 1
        return {
            "client_id": self.client_id,
            "period": self.period,
            "total": len(self.items),
            "status": counts,
            "priority": by_priority,
            "batch_approvable": sum(1 for i in self.items
                                    if i.batch_approvable),
            "ready_to_post": sum(1 for i in self.items
                                 if i.status in (ReviewStatus.APPROVED,
                                                 ReviewStatus.MODIFIED)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "period": self.period,
            "created_at": self.created_at,
            "summary": self.summary(),
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReviewQueue":
        return cls(client_id=d["client_id"], period=d["period"],
                   items=[ReviewItem.from_dict(i) for i in d["items"]],
                   created_at=d.get("created_at"))

    # -- persistence --------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2),
                        encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "ReviewQueue":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Rehydration helpers (from serialized dicts)
# ---------------------------------------------------------------------------
def _jv_from_dict(d: dict[str, Any]) -> DraftedJV:
    return DraftedJV(
        exception_ref=d["exception_ref"],
        voucher_date=datetime.fromisoformat(d["voucher_date"]).date(),
        narration=d.get("narration", ""),
        lines=[{**l, "dr": Decimal(l["dr"]), "cr": Decimal(l["cr"])}
               for l in d.get("lines", [])],
        rationale=d.get("rationale", ""),
    )


def _invoice_from_dict(d: dict[str, Any]) -> Invoice:
    return make_invoice(
        gstin=d["gstin"], invoice_no=d["invoice_no"], date=d["date"],
        taxable_value=d.get("taxable_value", "0"),
        cgst=d.get("cgst", "0"), sgst=d.get("sgst", "0"),
        igst=d.get("igst", "0"), source=d.get("source", ""),
        status=d.get("status", ""),
        reverse_charge=d.get("reverse_charge", False),
        blocked_credit=d.get("blocked_credit", False),
        reference=d.get("reference", ""),
    )


def _match_from_dict(d: dict[str, Any]) -> MatchResult:
    book = None
    if d.get("book_invoice"):
        book = _invoice_from_dict(d["book_invoice"])
        book.invoice_no = d["book_invoice"].get("invoice_no", book.invoice_no)
        book.source = "tally"
    return MatchResult(
        portal_invoice=_invoice_from_dict(d["portal_invoice"]),
        book_invoice=book,
        status=MatchStatus(d.get("status", "not_in_books")),
        confidence=Confidence(d.get("confidence", "low")),
        match_score=d.get("match_score", 0.0),
        edit_distance=d.get("edit_distance", 99),
        date_delta=d.get("date_delta", 99),
        value_delta_pct=d.get("value_delta_pct"),
        reasons=d.get("reasons", []),
        evidence=d.get("evidence", {}),
        materiality=d.get("materiality", "none"),
        ims_recommendation=d.get("ims_recommendation", "pending"),
    )


# ---------------------------------------------------------------------------
# CLI: run a review round over a persisted queue
# ---------------------------------------------------------------------------
def _build_cli_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog="recon_engine.review_queue",
        description="Approve/Modify/Reject/Post round over a persisted queue.",
    )
    p.add_argument("queue", help="path to review_queue.json")
    p.add_argument("--approve", nargs="*", default=None,
                   help="refs to approve (default: none)")
    p.add_argument("--reject", nargs="*", default=None,
                   help="refs to reject")
    p.add_argument("--post", nargs="*", default=None,
                   help="refs to post (must be approved/modified)")
    p.add_argument("--batch", action="store_true",
                   help="batch-approve batch-approvable items")
    p.add_argument("--actor", default="reviewer",
                   help="actor id for the audit trail")
    p.add_argument("--note", default="", help="note recorded on each action")
    p.add_argument("--out", default=None,
                   help="optional path to save the updated queue")
    return p


def main(argv: list[str] | None = None) -> int:
    import sys
    args = _build_cli_parser().parse_args(argv)
    queue = ReviewQueue.load(args.queue)
    done: list[str] = []

    if args.approve:
        for ref in args.approve:
            item = queue.by_ref(ref)
            if item is None:
                print(f"!! unknown ref {ref!r}"); return 2
            item.approve(args.actor, args.note)
            done.append(f"approve {ref}")
    if args.reject:
        for ref in args.reject:
            item = queue.by_ref(ref)
            if item is None:
                print(f"!! unknown ref {ref!r}"); return 2
            item.reject(args.actor, args.note)
            done.append(f"reject {ref}")
    if args.post:
        for ref in args.post:
            item = queue.by_ref(ref)
            if item is None:
                print(f"!! unknown ref {ref!r}"); return 2
            ok, msg = item.post(args.actor, note=args.note)
            print(f"   post {ref}: {'OK - ' + msg if ok else 'NOT POSTED - ' + msg}")
            done.append(f"post {ref}")
    if args.batch:
        approved = queue.batch_approve(args.actor, args.note)
        done.append(f"batch approve {len(approved)}: {', '.join(approved) or '(none)'}")

    s = queue.summary()
    print(f"REVIEW ROUND  |  client={queue.client_id} period={queue.period}")
    print(f"  actions: {'; '.join(done) or '(none)'}")
    print(f"  status: {s['status']}  ready_to_post={s['ready_to_post']}")
    if args.out:
        queue.save(args.out)
        print(f"  saved -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
