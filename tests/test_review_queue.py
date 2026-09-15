"""Phase-3 tests: Module-3 corrective-entry workflow.

Approve / Modify / Reject / Post state machine over the review packet:
audit trail, balance re-validation on Modify, batch approval for
high-confidence + below-materiality only, Tally post gate (no write
without approval), JSON persistence round-trip.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from recon_engine.agent import auto_reconcile
from recon_engine.models import MatchStatus
from recon_engine.review_queue import (
    InvalidTransitionError,
    ReviewQueue,
    ReviewStatus,
    TallyPoster,
    UnbalancedJVError,
)
from recon_engine.sample_scenario import generate as gen_sample_docs

SAMPLES = gen_sample_docs()
CLIENT_CONFIG = {"match_rules": {"materiality_amount": 50000}}

BATCH_EXPECTED = {"INV-24002", "ITF-2024-08-112", "CN-8850"}  # matched HIGH + below


def _queue(materiality: int | None = 50000):
    cfg = {"match_rules": {"materiality_amount": materiality}} if materiality else None
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules"],
        tally_path=SAMPLES["tally"],
        client_id="acme", period="2024-08", output_tax=120000,
        client_config=cfg,
    )
    return ReviewQueue.from_packet(res["packet"]), res


# ---------------------------------------------------------------------------
# Construction from packet
# ---------------------------------------------------------------------------
def test_from_packet_builds_queue():
    q, _ = _queue()
    assert q.client_id == "acme" and q.period == "2024-08"
    assert "BL/2024/0815" in q.refs                     # corrective JV
    jv_item = q.by_ref("BL/2024/0815")
    assert jv_item.kind == "jv" and jv_item.jv is not None
    assert jv_item.jv.is_balanced()
    assert jv_item.batch_approvable is False            # low confidence
    # batch-approvable exactly the matched-HIGH-below set
    assert {i.ref for i in q.items if i.batch_approvable} == BATCH_EXPECTED
    # high priority = above-materiality or IMS Reject
    hi = {i.ref for i in q.items if i.priority == "high"}
    assert {"INV-24001", "CN-8841", "EP-INV-9033", "BL/2024/0811"} <= hi


# ---------------------------------------------------------------------------
# Approve / audit
# ---------------------------------------------------------------------------
def test_approve_jv_audit_trail():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    item.approve("partner1", "verified against supplier portal")
    assert item.status == ReviewStatus.APPROVED
    actions = [a.action for a in item.audit]
    assert actions[-1] == "approve"
    assert item.audit[-1].actor == "partner1"
    assert len(item.audit) == 2  # created + approve


# ---------------------------------------------------------------------------
# Balance gate: unbalanced JV blocks approve & modify
# ---------------------------------------------------------------------------
def test_unbalanced_blocks_approval():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    item.jv.lines[-1]["cr"] = Decimal("1")  # corrupt: dr != cr
    with pytest.raises(UnbalancedJVError):
        item.approve("partner1")
    assert item.status == ReviewStatus.PENDING


def test_modify_revalidates_balance_and_blocks_unbalanced():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    bad_lines = [dict(l) for l in item.jv.lines]
    bad_lines[0]["dr"] = Decimal(bad_lines[0]["dr"]) + Decimal("100")  # no credit
    with pytest.raises(UnbalancedJVError):
        item.modify("partner", bad_lines, "wrongly unbalanced")
    assert item.status == ReviewStatus.PENDING
    assert any(a.action == "modify_blocked" for a in item.audit)


def test_modify_balanced_then_approve():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    lines = [dict(l) for l in item.jv.lines]
    # classification shift: +100 purchase, +100 sundry credit (still balanced)
    lines[0]["dr"] = Decimal(lines[0]["dr"]) + Decimal("100")
    lines[-1]["cr"] = Decimal(lines[-1]["cr"]) + Decimal("100")
    item.modify("partner", lines, "corrected classification")
    assert item.status == ReviewStatus.MODIFIED
    item.approve("partner", "ok after modify")
    assert item.status == ReviewStatus.APPROVED
    assert [a.action for a in item.audit][-1] == "approve"


def test_reject_sets_status_and_audits():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    item.reject("partner", "duplicate with INV-24006 - holding")
    assert item.status == ReviewStatus.REJECTED
    assert item.audit[-1].action == "reject"


# ---------------------------------------------------------------------------
# Post gate
# ---------------------------------------------------------------------------
def test_post_requires_approval():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    with pytest.raises(InvalidTransitionError):
        item.post("ops")


def test_post_after_approval_with_gate():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    item.approve("partner")
    ok, msg = item.post("ops", poster=TallyPoster(dsn="TallyODBC_9000"))
    assert ok is True
    assert item.status == ReviewStatus.POSTED
    assert item.audit[-1].action == "post"


def test_post_fails_when_poster_unavailable_keeps_approved():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    item.approve("partner")
    ok, _ = item.post("ops", poster=TallyPoster(dsn=None))
    assert ok is False
    assert item.status == ReviewStatus.APPROVED
    assert any(a.action == "post_failed" for a in item.audit)


def test_invalid_transition_from_posted():
    q, _ = _queue()
    item = q.by_ref("BL/2024/0815")
    item.approve("partner")
    item.post("ops", poster=TallyPoster(dsn="x"))
    assert item.status == ReviewStatus.POSTED
    with pytest.raises(InvalidTransitionError):
        item.reject("partner")     # can't reject after posting


# ---------------------------------------------------------------------------
# Batch approval (M3 locked #2)
# ---------------------------------------------------------------------------
def test_batch_approve_only_batch_approvable():
    q, _ = _queue()
    approved = q.batch_approve("manager", "auto-approve below materiality")
    assert set(approved) == BATCH_EXPECTED
    for ref in BATCH_EXPECTED:
        assert q.by_ref(ref).status == ReviewStatus.APPROVED
    # the JV and the high-materiality / Reject items stay pending
    assert q.by_ref("BL/2024/0815").status == ReviewStatus.PENDING
    assert q.by_ref("INV-24001").status == ReviewStatus.PENDING
    assert q.by_ref("EP-INV-9033").status == ReviewStatus.PENDING
    s = q.summary()
    assert s["status"]["approved"] == len(BATCH_EXPECTED)
    assert s["ready_to_post"] == len(BATCH_EXPECTED)


def test_batch_approve_idempotent():
    q, _ = _queue()
    q.batch_approve("manager")
    second = q.batch_approve("manager")   # nothing left to approve
    assert second == []


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------
def test_save_load_roundtrip(tmp_path):
    q, _ = _queue()
    q.batch_approve("manager", "auto-approve below materiality")
    q.by_ref("BL/2024/0815").approve("partner")
    q.by_ref("INV-24001").reject("partner", "supplier mismatch confirmed")

    p = tmp_path / "queue.json"
    q.save(p)

    q2 = ReviewQueue.load(p)
    assert q2.client_id == "acme" and q2.period == "2024-08"
    assert len(q2.items) == len(q.items)
    assert {i.ref for i in q2.items} == {i.ref for i in q.items}
    # JV survives with balance intact
    jv2 = q2.by_ref("BL/2024/0815")
    assert jv2.jv is not None and jv2.jv.is_balanced()
    assert jv2.status == ReviewStatus.APPROVED
    # statuses + audit survive
    assert q2.by_ref("INV-24001").status == ReviewStatus.REJECTED
    assert q2.by_ref("INV-24002").status == ReviewStatus.APPROVED
    assert q2.by_ref("INV-24002").audit[-1].note == "auto-approve below materiality"
    # match materiality/ims flags survive
    m = q2.by_ref("INV-24001").match
    assert m.materiality == "above" and m.ims_recommendation == "Reject"


def test_agent_returns_review_queue_and_file():
    res = auto_reconcile(
        gstr2b_path=SAMPLES["gstr2b_rules_xlsx"],
        tally_path=SAMPLES["tally_xlsx"],
        client_id="acme", period="2024-08", output_tax=120000,
        client_config=CLIENT_CONFIG,
    )
    assert "review" in res
    q = res["review"]
    assert q.client_id == "acme"
    assert "BL/2024/0815" in q.refs
    path = res["reports"]["review_queue"]
    assert path.exists()
    loaded = ReviewQueue.load(path)
    assert {i.ref for i in loaded.items} == {i.ref for i in q.items}
    assert res["verification"]["pass"] is True
