"""Regression tests for two Invoice Extraction defects (branch
``invoice-extract-gate-fixes``).

BUG 1 — the export gate and the review screen disagreed about tax RATE fields.
``_satisfied_by_derivation()`` waived every ``RATE_FIELD_KEYS`` member
unconditionally, so ``cgst_rate`` / ``sgst_rate`` / ``igst_rate`` never gated an
upload, while the review screen still rendered them as flagged with Resolve
inputs. An upload could therefore reach status ``exported`` with visibly
unresolved rate fields.

The fix has two halves, both locked in here:

1. ONE source of truth. ``service.get_fields()`` now attaches a
   ``requires_resolution`` flag per field, computed by ``_requires_resolution()``
   — the same code path ``get_reviewable_fields()`` and the export gate use. The
   UI (``_to_field_row``) renders its flags/Resolve inputs from that key alone.

2. The relaxation is narrowed: a flagged tax half is satisfied ONLY when every
   dependency of its derivation spec is available (rate needs amount + taxable;
   amount needs rate + taxable). A rate the model never found can no longer pass
   silently.

BUG 2 — an exported upload rendered editable controls. ``resolve_field()`` /
``re_extract_upload()`` / ``discard_upload()`` all refuse on an exported upload,
but the "Extracted fields" card was gated only on ``can_review``. The review
screen now renders read-only when ``is_read_only`` is true (status == exported).

ALSO — a present value with a missing confidence was labelled "not present" by
``status_label``. It is now "present — unverified", and stays flagged.

Run:

    venv/bin/python -m pytest tests/test_invoice_extract_gate.py -v
"""

from __future__ import annotations

import json
import unittest.mock as mock

import pytest

from src import db as recon_db
from src.invoice_extract import db as idb
from src.invoice_extract import service as ie
from src.invoice_extract.seed import CANONICAL_FIELD_KEYS

# High-confidence fields every fixture starts from — deliberately WITHOUT any
# tax rate or amount, so each test states exactly which tax half it supplies.
_BASE = {
    "invoice_number": ("SBE/0191", 95),
    "invoice_date": ("12-Sep-2026", 93),
    "vendor_name": ("Shree Balaji Hardware", 95),
    "vendor_gstin": ("27AABCM1234K1Z9", 96),
    "place_of_supply": ("Maharashtra", 90),
    "reference_po": ("PO-7781", 92),
    "hsn_sac": ("7308", 94),
    "item_description": ("MS Pipe", 95),
    "quantity": ("10", 92),
    "rate": ("430", 92),
    "taxable_value": ("12,900.00", 93),
    "total_tax": ("2,322.00", 93),
    "invoice_total": ("15,222.00", 94),
}

_AMOUNTS_ONLY = {"cgst_amount": ("1,161.00", 93), "sgst_amount": ("1,161.00", 93),
                 "igst_amount": ("0", 93)}


def _seed(
    tmp_path, *, name="scratch.db", overrides=None, absent=(), unscored=(), status="needs_review",
):
    """Create a scratch F3-B DB with ONE upload.

    ``overrides`` supplies (value, confidence) pairs; ``absent`` stores a field
    as genuinely not present; ``unscored`` stores a PRESENT value with NO
    confidence (the "present — unverified" case).
    """
    db_path = str(tmp_path / name)
    ie.init_invoice_extract(db_path)
    values = {**_BASE, **(overrides or {})}

    conn = recon_db.get_connection(db_path)
    upload_id = idb.create_upload(
        conn, client_id=1, filename="inv.xlsx", file_ext="xlsx", source_format="excel",
        extraction_path="structured", batch_id=None, status=status,
        duplicate_warning=False, actor="admin", ai_used=True, ai_model="test-model",
    )
    for key in CANONICAL_FIELD_KEYS:
        if key in absent:
            value, conf, present = None, None, False
        elif key in unscored:
            value, conf, present = (values[key][0] if key in values else "X"), None, True
        elif key in values:
            value, conf, present = values[key][0], values[key][1], True
        else:
            value, conf, present = None, None, False
        idb.insert_field(conn, upload_id=upload_id, result={
            "field_name": key, "extracted_value": value, "confidence": conf,
            "source_location": "test", "is_present": present,
        })
    conn.close()
    return db_path, upload_id


def _reviewable(db_path, upload_id) -> set[str]:
    return {r["field_name"] for r in ie.get_reviewable_fields(upload_id, db_path=db_path)}


# ===========================================================================
# BUG 1 — the derivation relaxation, narrowed
# ===========================================================================


def test_underivable_rate_head_gates(tmp_path):
    """A rate with neither its amount counterpart nor the taxable value on the
    document is NOT satisfiable — it must gate (it used to be waived)."""
    db_path, uid = _seed(tmp_path)
    rev = _reviewable(db_path, uid)
    assert {"cgst_rate", "sgst_rate", "igst_rate"} <= rev


def test_derivable_rate_head_does_not_gate(tmp_path):
    """An amounts-only invoice still derives its rates (9% on 12,900 = 1,161),
    so those rate heads stay out of the review queue."""
    db_path, uid = _seed(tmp_path, overrides=_AMOUNTS_ONLY)
    rev = _reviewable(db_path, uid)
    assert rev == set()


def test_amount_head_without_rate_counterpart_gates(tmp_path):
    """The amount half of a head whose rate counterpart (and taxable) is absent
    is not derivable, so it gates the export."""
    db_path, uid = _seed(tmp_path, overrides={"cgst_amount": ("1,161.00", 93),
                                              "sgst_amount": ("1,161.00", 93)})
    rev = _reviewable(db_path, uid)
    assert "igst_amount" in rev
    # ...and the rate half of that same head now gates too (the fix).
    assert "igst_rate" in rev


def test_only_rate_heads_unresolved_blocks_confirm_and_export(tmp_path):
    """The exact defect shape: the tax AMOUNTS are present and auto-accepted,
    the tax RATES are absent and NOT derivable (the amounts aren't usable
    numbers, so no rate can be computed from them + taxable value). Only the
    three rate heads remain unresolved — and the upload can no longer be
    confirmed or exported."""
    # Amounts present (and high confidence) but NOT numeric: nothing to derive a
    # rate from, so the rate heads are the only unresolved fields.
    db_path, uid = _seed(tmp_path, overrides={
        "cgst_amount": ("N/A", 93), "sgst_amount": ("N/A", 93), "igst_amount": ("N/A", 93),
    })
    assert _reviewable(db_path, uid) == {"cgst_rate", "sgst_rate", "igst_rate"}

    ok, reason = ie.is_exportable(uid, db_path=db_path)
    assert ok is False and "still need resolution" in reason

    with pytest.raises(ie.InvoiceExtractError):
        ie.confirm_upload(uid, actor="admin", db_path=db_path)
    with pytest.raises(ie.InvoiceExtractError):
        ie.generate_export(upload_ids=[uid], actor="admin", fmt="xlsx", db_path=db_path)


# ===========================================================================
# BUG 1 — one source of truth
# ===========================================================================


def test_get_fields_exposes_requires_resolution_for_every_field(tmp_path):
    db_path, uid = _seed(tmp_path, overrides=_AMOUNTS_ONLY)
    fields = ie.get_fields(uid, db_path=db_path)
    assert fields, "expected fields"
    for f in fields:
        assert isinstance(f["requires_resolution"], bool)


def test_get_fields_and_reviewable_fields_never_disagree(tmp_path):
    """For a sample of each status, the per-field flag and the reviewable list
    are the same decision — the screen and the gate cannot drift."""
    samples = [
        (_AMOUNTS_ONLY, "extracted"),                    # everything derivable / clear
        ({"cgst_amount": ("N/A", 93), "sgst_amount": ("N/A", 93), "igst_amount": ("N/A", 93)}, "needs_review"),
        ({"cgst_amount": ("1,161.00", 93), "sgst_amount": ("1,161.00", 93)}, "confirmed"),   # igst missing
        ({"cgst_amount": ("1,161.00", 93), "sgst_amount": ("1,161.00", 93)}, "exported"),
    ]
    for i, (overrides, status) in enumerate(samples):
        db_path, uid = _seed(tmp_path, name=f"agree{i}.db", overrides=overrides, status=status)
        fields = ie.get_fields(uid, db_path=db_path)
        flagged = {f["field_name"] for f in fields if f["requires_resolution"]}
        reviewable = {r["field_name"] for r in ie.get_reviewable_fields(uid, db_path=db_path)}
        assert flagged == reviewable, f"disagreement for status={status}"


def test_reviewable_list_is_literal_subset_of_get_fields(tmp_path):
    db_path, uid = _seed(tmp_path, overrides={"cgst_amount": ("1,161.00", 93)})
    by_name = {f["field_name"]: f for f in ie.get_fields(uid, db_path=db_path)}
    for r in ie.get_reviewable_fields(uid, db_path=db_path):
        assert by_name[r["field_name"]]["requires_resolution"] is True


# ===========================================================================
# ALSO — present but unscored is not "not present"
# ===========================================================================


def test_present_without_confidence_is_labelled_unverified(tmp_path):
    tagged = ie._apply_threshold(
        [{"field_name": "vendor_name", "is_present": True, "confidence": None}], 90,
    )[0]
    assert tagged["status_label"] == "present — unverified"
    assert tagged["review_kind"] == "unverified"
    assert tagged["reviewable"] is True

    absent = ie._apply_threshold(
        [{"field_name": "vendor_name", "is_present": False, "confidence": None}], 90,
    )[0]
    assert absent["status_label"] == "not present"
    assert absent["review_kind"] == "not_present"


def test_field_row_labels_present_unscored_as_unverified(tmp_path):
    import setu.state.invoice_extract_state as st

    db_path, uid = _seed(tmp_path, unscored=("vendor_name",))
    field = next(f for f in ie.get_fields(uid, db_path=db_path) if f["field_name"] == "vendor_name")
    assert bool(field["is_present"]) and field["confidence"] is None
    assert field["requires_resolution"] is True

    class _Fake:
        detail_threshold = 90
        field_inputs: dict = {}
        selected_upload_id = 0

    with mock.patch.object(st.ie, "suggested_value", lambda *a, **k: None):
        row = st.InvoiceExtractState._to_field_row(_Fake(), field)
    assert row.unverified is True
    assert row.not_present is False
    assert row.badge_pct == "present — unverified"
    assert row.flagged is True


# ===========================================================================
# BUG 2 — an exported upload is read-only, and is blocked server-side
# ===========================================================================


def test_exported_upload_blocks_resolve_reextract_and_discard(tmp_path):
    db_path, uid = _seed(tmp_path, status="exported", overrides=_AMOUNTS_ONLY)

    with pytest.raises(ie.InvoiceExtractError):
        ie.resolve_field(uid, field_name="vendor_name", resolved_value="Acme", actor="admin", db_path=db_path)
    with pytest.raises(ie.InvoiceExtractError):
        ie.re_extract_upload(uid, actor="admin", db_path=db_path)
    with pytest.raises(ie.InvoiceExtractError):
        ie.discard_upload(uid, actor="admin", db_path=db_path)


def test_read_only_predicate_is_exported_only():
    import setu.state.invoice_extract_state as st

    assert st.read_only_for_status("exported") is True
    for status in ("needs_review", "confirmed", "extracted"):
        assert st.read_only_for_status(status) is False


def _cond_nodes(node, out):
    if isinstance(node, dict):
        if "cond_state" in node:
            out.append(node)
        for v in node.values():
            _cond_nodes(v, out)
    elif isinstance(node, list):
        for v in node:
            _cond_nodes(v, out)
    return out


def _rendered_with_guard(component, needles, guard="is_read_only"):
    """True when, in the rendered tree, every ``needle`` control sits inside a
    Cond whose condition references ``guard``."""
    render = component.render()
    nodes = _cond_nodes(render, [])
    for needle in needles:
        owners = [
            n for n in nodes
            if needle in json.dumps(n.get("true_value")) or needle in json.dumps(n.get("false_value"))
        ]
        assert owners, f"no conditional renders {needle!r}"
        assert any(guard in str(n.get("cond_state", "")) for n in owners), \
            f"{needle!r} is not guarded by {guard!r}"


def test_field_row_guards_resolve_and_edit_behind_is_read_only():
    import setu.state.invoice_extract_state as st
    from setu.views.invoice_extract_page import _field_row

    flagged = st.FieldRow(
        field_name="cgst_rate", label="CGST Rate %", extracted_value="", effective_value="",
        source_location="p.5", confidence=0, has_confidence=False, is_present=False,
        resolved=False, flagged=True, auto_accepted=False, not_present=True,
        resolved_value="", badge_pct="not present", input_value="",
    )
    settled = st.FieldRow(
        field_name="invoice_total", label="Invoice Total", extracted_value="1,180.00",
        effective_value="1,180.00", source_location="p.5", confidence=95, has_confidence=True,
        is_present=True, resolved=False, flagged=False, auto_accepted=True, not_present=False,
        resolved_value="", badge_pct="AI — 95%", input_value="1,180.00",
    )
    # Both build, and the Resolve/Edit controls are conditional on is_read_only.
    _rendered_with_guard(_field_row(flagged), ["Resolve"])
    _rendered_with_guard(_field_row(settled), ["Edit"])


def test_review_screen_read_only_message_is_guarded():
    from setu.views.invoice_extract_page import _review_screen

    render = json.dumps(_review_screen().render(), default=str)
    assert "is_read_only" in render
    assert "generating a new batch" in render
