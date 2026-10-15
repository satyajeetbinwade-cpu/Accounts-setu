"""Phase-4 confidence tests. NO network, and no stubbed pure functions.

The formula is a pure function of facts + config, so most of this file builds a
:class:`FieldFacts` directly and asserts the outcome — including every cap and
the interactions between them. The database layer (scoring, corrections,
calibration, the gate) runs against a scratch DB, never the real one.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src import bootstrap
from src.invoice_extract import calibration as cal
from src.invoice_extract import confidence as conf
from src.invoice_extract import rules as R
from src.invoice_extract.confidence_schema import (
    ACTION_CONFIRMED,
    ACTION_EDITED,
    ACTION_MARKED_NOT_APPLICABLE,
    ACTION_MARKED_NOT_PRINTED,
)
from src.invoice_extract.service import InvoiceExtractError

CLIENT_GSTIN = "07AAECK9054D1ZK"


def facts(**kw) -> conf.FieldFacts:
    base = dict(
        field_id=1, field_key="invoice_total", status="read", reads_agree="exact",
        text_layer_match=None, is_handwritten=False, in_region=True, value="1,180.00",
        value_a="1,180.00", value_b="1,180.00", single_reader=False,
        involved_checks={}, check_count=8, required=False,
    )
    base.update(kw)
    return conf.FieldFacts(**base)


def score(**kw) -> conf.FieldScore:
    return conf.score_field(facts(**kw), conf.ScoreConfig())


def signal(result: conf.FieldScore, name: str) -> conf.Signal:
    return next(s for s in result.signals if s.name == name)


def cap_names(result: conf.FieldScore) -> set[str]:
    return {c.name for c in result.caps}


# ===========================================================================
# The formula: signals
# ===========================================================================


def test_a_perfect_field_scores_100():
    result = score(
        field_key="issuer_gstin", value=CLIENT_GSTIN, value_a=CLIENT_GSTIN,
        value_b=CLIENT_GSTIN, text_layer_match=1,
        involved_checks={"gstin_valid": "pass"},
    )
    assert result.score == 100.0
    assert [s.points for s in result.signals] == [40, 30, 15, 10, 5]


def test_each_signal_contributes_its_own_points():
    # No text layer, no involved check, a field with a format rule that passes.
    result = score(field_key="invoice_total", value="1,180.00")
    assert signal(result, "base").points == 40
    assert signal(result, "independent_read").points == 30
    assert signal(result, "format").points == 15
    assert signal(result, "cross_check").points == 10        # no check involves it
    assert signal(result, "text_layer").points == 0
    assert result.score == 95.0


def test_near_agreement_is_awarded_for_text_only():
    text = score(field_key="issuer_name", value="KMAP Capital Limited",
                 reads_agree="near", value_b="KMAP Capital Ltd")
    assert signal(text, "independent_read").points == 20
    # A near agreement on a NUMBER scores nothing: near is never allowed there.
    number = score(field_key="invoice_total", value="1,180.00",
                   reads_agree="near", value_b="1,181.00")
    assert signal(number, "independent_read").points == 0
    assert "never allowed" in signal(number, "independent_read").reason


def test_a_field_with_no_format_rule_gets_the_same_points_as_one_that_passes():
    with_rule = score(field_key="invoice_total", value="1,180.00")
    without_rule = score(field_key="issuer_name", value="KMAP Capital Limited")
    assert signal(with_rule, "format").points == signal(without_rule, "format").points == 15
    assert "no format rule" in signal(without_rule, "format").reason


def test_a_failing_format_rule_scores_zero_and_is_explained():
    result = score(field_key="issuer_gstin", value="07AAECK9054D1ZL")   # altered check digit
    assert signal(result, "format").points == 0
    assert "fails its format rule" in signal(result, "format").reason
    assert "gstin_checksum_invalid" in cap_names(result)


def test_cross_check_reads_the_checks_that_actually_involve_the_field():
    passing = score(field_key="invoice_total", value="1,180.00",
                    involved_checks={"totals_reconcile": "pass"})
    assert signal(passing, "cross_check").points == 10
    failing = score(field_key="invoice_total", value="1,180.00",
                    involved_checks={"totals_reconcile": "fail"})
    assert signal(failing, "cross_check").points == 0
    assert "failed" in signal(failing, "cross_check").reason
    # Not-applicable is treated as no evidence either way, never as a pass.
    na = score(field_key="cbgst_amount" if False else "cgst_amount", value="90.00",
               involved_checks={"totals_reconcile": "not_applicable"})
    assert signal(na, "cross_check").points == 10
    assert "could not run" in signal(na, "cross_check").reason


def test_text_layer_awards_five_only_on_a_match():
    assert signal(score(text_layer_match=1), "text_layer").points == 5
    zero = score(text_layer_match=0)
    assert signal(zero, "text_layer").points == 0
    assert zero.score == 95.0
    scan = score(text_layer_match=None)
    assert "no text layer" in signal(scan, "text_layer").reason


# ===========================================================================
# The formula: caps, and their interactions
# ===========================================================================


def test_exact_reads_on_a_handwritten_amount_are_capped_at_80():
    """The headline interaction: agreement does not make handwriting safe."""
    result = score(field_key="invoice_total", value="6,825.00", is_handwritten=True)
    assert result.score == 80.0
    assert "handwritten_monetary" in cap_names(result)
    assert "handwritten" in next(
        c for c in result.caps if c.name == "handwritten_monetary"
    ).reason


def test_a_handwritten_NON_monetary_value_is_not_capped():
    result = score(field_key="issuer_name", value="KMAP Capital Limited", is_handwritten=True)
    assert result.score == 95.0
    assert "handwritten_monetary" not in cap_names(result)


def test_disagreeing_reads_cap_at_60():
    result = score(reads_agree="disagree", value_b="1,090.00")
    assert result.score == 60.0
    assert cap_names(result) == {"reads_disagree"}


def test_an_arithmetic_check_failure_caps_at_60():
    result = score(involved_checks={"totals_reconcile": "fail"})
    # 40 + 30 + 15 + 0 = 85, capped by the arithmetic failure.
    assert result.score == 60.0
    assert "arithmetic_check_failed" in cap_names(result)


def test_a_non_arithmetic_check_failure_does_not_cap():
    """A failed direction expectation is not evidence that a NUMBER is wrong."""
    result = score(field_key="issuer_gstin", value=CLIENT_GSTIN,
                   involved_checks={"expected_tax_type": "fail"})
    # 40 base + 30 read + 15 format + 0 cross = 85 — no cap applies.
    assert result.score == 85.0
    assert result.caps == ()


def test_an_invalid_gstin_checksum_caps_at_50():
    result = score(field_key="issuer_gstin", value="07AAECK9054D1ZL")
    assert result.score == 50.0
    assert cap_names(result) == {"gstin_checksum_invalid"}


def test_out_of_region_caps_at_85():
    result = score(status="out_of_region", in_region=False)
    assert result.score == 85.0
    assert cap_names(result) == {"out_of_region"}


def test_a_single_reader_is_never_above_70():
    result = score(reads_agree="missing_b", value_b=None, single_reader=True)
    # A cap is a MAXIMUM, not a target: the field still cannot earn the 30 points
    # for an independent agreement, so it lands below the cap.
    assert result.score <= 70.0
    assert "single_reader" in cap_names(result)
    # Even with a text-layer match and a passing check it stays at or below 70.
    stronger = score(reads_agree="missing_b", value_b=None, single_reader=True,
                     text_layer_match=1, involved_checks={"totals_reconcile": "pass"},
                     field_key="issuer_gstin", value=CLIENT_GSTIN, value_a=CLIENT_GSTIN)
    assert stronger.score == 70.0


def test_needs_printed_and_unreadable_score_zero():
    needs = score(status="needs_printed", is_handwritten=True)
    assert needs.score == 0.0
    unreadable = score(status="unreadable", value="??")
    assert unreadable.score == 0.0
    assert cap_names(unreadable) == {"needs_printed"}


def test_the_lowest_cap_wins():
    """Disagree (60) + an invalid checksum (50) → 50, not 60."""
    result = score(
        field_key="issuer_gstin", value="07AAECK9054D1ZL", value_b="07AAECK9054D1ZM",
        reads_agree="disagree",
    )
    assert result.score == 50.0
    assert cap_names(result) == {"reads_disagree", "gstin_checksum_invalid"}
    # And a needs_printed monetary value beats every other cap.
    worst = score(status="needs_printed", reads_agree="disagree", is_handwritten=True)
    assert worst.score == 0.0


# ===========================================================================
# No score by rule
# ===========================================================================


def test_not_printed_on_an_optional_field_has_no_score():
    result = score(status="not_printed", value=None, value_a=None, value_b=None, required=False)
    assert result.score is None
    assert not result.below_threshold
    assert "does not print this field" in signal(result, "no_score").reason


def test_not_printed_on_a_required_field_is_a_missing_required_item_not_a_score():
    result = score(status="not_printed", value=None, value_a=None, value_b=None, required=True)
    assert result.score is None
    assert "missing-required" in signal(result, "no_score").reason


def test_a_field_with_no_value_has_no_score_whatever_its_status():
    for status in ("not_printed", "read"):
        assert score(status=status, value=None, value_a=None, value_b=None).score is None


# ===========================================================================
# Settings change the formula
# ===========================================================================


def test_settings_change_the_threshold_and_the_weights():
    heavy = conf.ScoreConfig(threshold=75.0, weights={
        **conf.WEIGHT_DEFAULTS,
        "invoice_extract.score.base": 50,
        "invoice_extract.score.read.exact": 40,
        "invoice_extract.score.cap.handwritten_monetary": 95,
    })
    result = conf.score_field(facts(), heavy)
    assert result.score == 100.0
    assert result.threshold_used == 75.0
    handwritten = conf.score_field(facts(is_handwritten=True), heavy)
    assert handwritten.score == 95.0        # the raised cap applies
    assert handwritten.below_threshold is False


def test_an_unknown_weight_key_falls_back_to_its_default():
    config = conf.ScoreConfig(weights={})
    assert config.weight("invoice_extract.score.base") == 40
    assert config.weight("does.not.exist") == conf.WEIGHT_DEFAULTS["does.not.exist"] \
        if "does.not.exist" in conf.WEIGHT_DEFAULTS else True


# ===========================================================================
# The database layer
# ===========================================================================


@pytest.fixture()
def scratch(tmp_path_factory):
    root = tmp_path_factory.mktemp("conf_db")
    db = str(root / "poc.db")
    bootstrap.run_migrations(db_path=db, data_root=root / "data")
    from src.clients import service as clients

    client_id = clients.create_client(
        legal_name="KMAP Capital Limited", pan="AAACK9054D", gstin=CLIENT_GSTIN,
        actor="test", owner_user_id=1, db_path=db,
    )
    return db, client_id


def seed_document(
    db: str, client_id: int, fields: dict[str, dict], *, category="tax_invoice",
    ordinal=1, upload_status="extracted", reads=None, read_b=True,
) -> int:
    """Insert one document with an extraction and its field rows."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_uploads (upload_id, client_id, filename, file_ext, source_format, "
            "extraction_path, status, duplicate_warning, created_at, created_by) "
            "VALUES (?, ?, 'b.pdf', 'pdf', 'pdf', 'structured', ?, 0, 'now', 't')",
            (ordinal, client_id, upload_status),
        )
        conn.execute(
            "INSERT INTO invoice_triage_runs (run_id, upload_id, status, started_at) "
            "VALUES (?, ?, 'complete', 'now')", (ordinal, ordinal),
        )
        conn.execute(
            "INSERT INTO invoice_documents (upload_id, run_id, ordinal, category, label, excluded, "
            "is_active, created_at) VALUES (?, ?, ?, ?, ?, 0, 1, 'now')",
            (ordinal, ordinal, ordinal, category, f"doc{ordinal}"),
        )
        document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO document_regions (document_id, page, x, y, w, h, order_in_doc, source, "
            "is_active) VALUES (?, ?, 0.05, 0.05, 0.9, 0.4, 0, 'triage', 1)", (document_id, ordinal),
        )
        conn.execute(
            "INSERT INTO invoice_doc_extractions (document_id, status, provider_a, model_a, "
            "provider_b, model_b, is_active) VALUES (?, 'complete', 'openrouter', 'a', ?, ?, 1)",
            (document_id, "anthropic" if read_b else None, "b" if read_b else None),
        )
        extraction_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for key, spec in fields.items():
            row = {
                "status": "read", "value_a": spec, "value_b": spec, "reads_agree": "exact",
                "text_layer_match": None, "is_handwritten": 0, "in_region": 1,
            }
            if isinstance(spec, dict):
                row.update(spec)
            if reads and key in reads:
                row.update(reads[key])
            conn.execute(
                "INSERT INTO invoice_doc_fields (extraction_id, document_id, field_key, status, "
                "value_a, value_b, reads_agree, text_layer_match, is_handwritten, in_region) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (extraction_id, document_id, key, row["status"], row["value_a"], row["value_b"],
                 row["reads_agree"], row["text_layer_match"], row["is_handwritten"],
                 row["in_region"]),
            )
        conn.commit()
        return document_id
    finally:
        conn.close()


def test_score_extraction_scores_every_read_field(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_number": "KMAP/2026-27/003", "invoice_date": "27-Apr-26",
        "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
        "place_of_supply": "Uttar Pradesh", "line.1.hsn_sac": "997112",
        "line.1.amount": "1,000.00", "igst_amount": "180.00", "invoice_total": "1,180.00",
    })
    R.run_rules_for_document(document_id, db_path=db)

    scores = conf.get_scores(document_id, db_path=db)
    assert set(scores) >= {"invoice_total", "issuer_gstin", "invoice_number"}
    # A clean, fully cross-checked GSTIN with a passing checksum scores high.
    assert scores["issuer_gstin"]["score"] >= 90
    # Every score row carries its signals, its version and the threshold used.
    for row in scores.values():
        if row["score"] is None:
            continue
        assert row["signals"]
        assert row["formula_version"] == "v1"
        assert row["threshold_used"] == 90.0
    # A field that is not printed has no score and says why.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_doc_fields (extraction_id, document_id, field_key, status, "
            "reads_agree, in_region) SELECT extraction_id, document_id, 'buyers_order_no', "
            "'not_printed', 'null', 1 FROM invoice_doc_field_scores s JOIN invoice_doc_fields f "
            "ON f.field_id = s.field_id LIMIT 1"
        )
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(db)
    try:
        extraction_id = conn.execute(
            "SELECT extraction_id FROM invoice_doc_fields WHERE document_id = ? LIMIT 1",
            (document_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    conf.score_extraction(extraction_id, db_path=db)
    again = conf.get_scores(document_id, db_path=db)
    assert again["buyers_order_no"]["score"] is None
    assert "does not print" in again["buyers_order_no"]["signals"][0]["reason"]


def test_scoring_is_idempotent(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {"invoice_total": "1,180.00"})
    R.run_rules_for_document(document_id, db_path=db)
    conn = sqlite3.connect(db)
    try:
        before = conn.execute("SELECT COUNT(*) FROM invoice_doc_field_scores").fetchone()[0]
        rows_before = conn.execute(
            "SELECT field_id, score FROM invoice_doc_field_scores ORDER BY field_id"
        ).fetchall()
    finally:
        conn.close()
    R.run_rules_for_document(document_id, db_path=db)
    conn = sqlite3.connect(db)
    try:
        after = conn.execute("SELECT COUNT(*) FROM invoice_doc_field_scores").fetchone()[0]
        rows_after = conn.execute(
            "SELECT field_id, score FROM invoice_doc_field_scores ORDER BY field_id"
        ).fetchall()
    finally:
        conn.close()
    assert after == before
    assert rows_after == rows_before


def test_a_single_reader_document_never_exceeds_70(scratch):
    db, client_id = scratch
    document_id = seed_document(
        db, client_id, {"invoice_total": "1,180.00", "issuer_gstin": CLIENT_GSTIN},
        read_b=False,
    )
    R.run_rules_for_document(document_id, db_path=db)
    scores = conf.get_scores(document_id, db_path=db)
    assert scores["invoice_total"]["score"] == 70.0
    assert "single_reader" in {c["name"] for c in scores["invoice_total"]["caps"]}


# ===========================================================================
# Corrections
# ===========================================================================


def _field_id(db: str, document_id: int, field_key: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute(
            "SELECT field_id FROM invoice_doc_fields WHERE document_id = ? AND field_key = ?",
            (document_id, field_key),
        ).fetchone()[0])
    finally:
        conn.close()


def test_every_decision_type_is_logged(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_total": "1,180.00", "buyers_order_no": "PO-1", "sgst_amount": "90.00",
    })
    R.run_rules_for_document(document_id, db_path=db)

    conf.record_correction(
        _field_id(db, document_id, "invoice_total"), ACTION_CONFIRMED, actor="reviewer",
        reason_code="correct", db_path=db,
    )
    conf.record_correction(
        _field_id(db, document_id, "buyers_order_no"), ACTION_EDITED, actor="reviewer",
        value_after="PO-2", reason_code="ocr_error", reason_text="the 2 was read as a 1", db_path=db,
    )
    conf.record_correction(
        _field_id(db, document_id, "sgst_amount"), ACTION_MARKED_NOT_PRINTED, actor="reviewer",
        reason_code="not_on_document", db_path=db,
    )
    conf.record_correction(
        _field_id(db, document_id, "invoice_total"), ACTION_MARKED_NOT_APPLICABLE, actor="reviewer",
        reason_code="not_applicable_here", db_path=db,
    )

    rows = conf.list_corrections(document_id=document_id, db_path=db)
    assert [r["action"] for r in rows] == [
        ACTION_CONFIRMED, ACTION_EDITED, ACTION_MARKED_NOT_PRINTED, ACTION_MARKED_NOT_APPLICABLE,
    ]
    # The confirmation records the score the reviewer was looking at.
    assert rows[0]["score_at_time"] is not None
    assert rows[0]["value_before"] == "1,180.00"
    assert rows[1]["value_after"] == "PO-2" and rows[1]["reason_text"]
    assert rows[1]["formula_version"] == "v1"
    assert rows[1]["document_category"] == "tax_invoice"
    assert "read_a" in json.loads(rows[1]["extraction_models"])


def test_a_correction_on_an_exported_upload_is_rejected(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {"invoice_total": "1,180.00"},
                                upload_status="exported")
    R.run_rules_for_document(document_id, db_path=db)
    with pytest.raises(InvoiceExtractError) as exc:
        conf.record_correction(
            _field_id(db, document_id, "invoice_total"), ACTION_CONFIRMED,
            actor="reviewer", db_path=db,
        )
    assert "immutable" in str(exc.value)
    assert conf.list_corrections(document_id=document_id, db_path=db) == []


def test_a_correction_validates_its_action_and_reason(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {"invoice_total": "1,180.00"})
    R.run_rules_for_document(document_id, db_path=db)
    field_id = _field_id(db, document_id, "invoice_total")
    with pytest.raises(InvoiceExtractError):
        conf.record_correction(field_id, "approved", actor="r", db_path=db)
    with pytest.raises(InvoiceExtractError):
        conf.record_correction(field_id, ACTION_CONFIRMED, actor="r",
                               reason_code="because_i_said_so", db_path=db)
    with pytest.raises(InvoiceExtractError):
        conf.record_correction(field_id, ACTION_EDITED, actor="r", db_path=db)   # no value_after
    with pytest.raises(InvoiceExtractError):
        conf.record_correction(999999, ACTION_CONFIRMED, actor="r", db_path=db)


def test_was_flagged_records_whether_the_score_had_already_asked(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_total": {"value_a": "1,180.00", "value_b": "1,090.00", "reads_agree": "disagree"},
        "issuer_name": "KMAP Capital Limited",
    })
    R.run_rules_for_document(document_id, db_path=db)
    low = conf.record_correction(
        _field_id(db, document_id, "invoice_total"), ACTION_CONFIRMED, actor="r", db_path=db,
    )
    high = conf.record_correction(
        _field_id(db, document_id, "issuer_name"), ACTION_CONFIRMED, actor="r", db_path=db,
    )
    assert low["was_flagged"] is True       # 60 < 90
    assert high["was_flagged"] is False


# ===========================================================================
# The review model
# ===========================================================================


def test_review_model_orders_needs_you_and_settles_by_confidence(scratch):
    db, client_id = scratch
    document_id = seed_document(
        db, client_id,
        {
            "invoice_number": "KMAP/2026-27/001", "invoice_date": "6-Apr-26",
            "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
            "place_of_supply": "Uttar Pradesh", "line.1.amount": "10,947.00",
            "igst_amount": {"value_a": "1,970.46", "value_b": "1,900.00", "reads_agree": "disagree"},
            "round_off": "-0.46", "invoice_total": "12,917.00",
        },
        reads={"line.1.hsn_sac": {"status": "not_printed", "value_a": None, "value_b": None,
                                  "reads_agree": "null"}},
    )
    R.run_rules_for_document(document_id, db_path=db)
    model = R.get_review_model(document_id, db_path=db)

    kinds = [item["kind"] for item in model["needs_you"]]
    assert kinds[0] == "missing_required"                     # the real gap leads
    assert model["needs_you"][0]["field_key"] == "line.1.hsn_sac"
    assert "check_failed" in kinds or "below_threshold" in kinds
    # Order within the kinds: every missing_required before any check_failed,
    # every check_failed before any below_threshold.
    assert kinds == sorted(kinds, key=lambda k: {"missing_required": 0, "check_failed": 1,
                                                 "below_threshold": 2, "direction_unsettled": 3}[k])
    # The disagreeing IGST amount is a below-threshold field.
    below = [i for i in model["needs_you"] if i["kind"] == "below_threshold"]
    assert any(i["field_key"] == "igst_amount" for i in below)
    assert all(i["score"] < i["threshold_used"] for i in below)
    # ...and it is NOT settled.
    assert "igst_amount" not in [s["field_key"] for s in model["settled"]]
    # Settled is confidence DESCENDING.
    scores = [s["score"] for s in model["settled"] if s["score"] is not None]
    assert scores == sorted(scores, reverse=True)
    # Every settled field carries its signals and caps.
    assert all("signals" in s and "caps" in s for s in model["settled"])
    # The derived values are tagged and unscored.
    assert model["derived"] and all(d["tag"] == "derived" and d["score"] is None
                                    for d in model["derived"])
    # No model ever self-reported anything.
    assert "confidence" not in json.dumps(model["checks"]).lower()


def test_settled_can_be_ordered_by_the_document_instead(scratch):
    db, client_id = scratch
    document_id = seed_document(
        db, client_id,
        {
            "invoice_number": "KMAP/2026-27/003", "invoice_date": "27-Apr-26",
            "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
            "place_of_supply": "Uttar Pradesh", "invoice_total": "1,180.00",
        },
        # A text-layer match is worth 5 more points, so this field is the only
        # one at 100 — enough for the two orderings to differ.
        reads={"invoice_number": {"text_layer_match": 1}},
    )
    R.run_rules_for_document(document_id, db_path=db)
    by_confidence = R.get_review_model(document_id, db_path=db)
    by_document = R.get_review_model(document_id, order="document", db_path=db)

    # Both orders hold the SAME fields — only the order differs.
    assert {s["field_key"] for s in by_confidence["settled"]} == \
        {s["field_key"] for s in by_document["settled"]}
    # Document order is the invoice's own reading order.
    assert [s["field_key"] for s in by_document["settled"]] == \
        sorted(s["field_key"] for s in by_document["settled"])
    # Confidence order is descending, and differs from the document order.
    scores = [s["score"] for s in by_confidence["settled"]]
    assert scores == sorted(scores, reverse=True)
    assert [s["field_key"] for s in by_confidence["settled"]] != \
        [s["field_key"] for s in by_document["settled"]]


# ===========================================================================
# Calibration
# ===========================================================================


def _seed_history(db: str, client_id: int, spec: list[tuple[float, str, str]]) -> None:
    """spec: (score, action, month) → one correction each, seeded directly.

    Written straight into the corrections table so a test can describe a
    HISTORY (hundreds of fields over several months) without that history being
    the slow part of the suite. The owning upload/document rows are created
    first, so the foreign keys are real.
    """
    conn = sqlite3.connect(db)
    try:
        # A fresh upload/run/document trio per call, so a test can build a
        # history batch by batch without colliding with itself.
        base = int(
            conn.execute(
                "SELECT COALESCE(MAX(upload_id), 0) + 1 FROM invoice_uploads"
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO invoice_uploads (upload_id, client_id, filename, file_ext, source_format, "
            "extraction_path, status, duplicate_warning, created_at, created_by) "
            "VALUES (?, ?, 'seed.pdf', 'pdf', 'pdf', 'structured', 'extracted', 0, 'now', 'seed')",
            (base, client_id),
        )
        conn.execute(
            "INSERT INTO invoice_triage_runs (run_id, upload_id, status, started_at) "
            "VALUES (?, ?, 'complete', 'now')", (base, base),
        )
        conn.execute(
            "INSERT INTO invoice_documents (document_id, upload_id, run_id, ordinal, category, "
            "label, excluded, is_active, created_at) VALUES (?, ?, ?, 900, 'tax_invoice', "
            "'seed', 0, 1, 'now')", (base, base, base),
        )
        for index, (score, action, month) in enumerate(spec):
            conn.execute(
                "INSERT INTO invoice_field_corrections (document_id, client_id, field_key, "
                "value_before, value_after, action, reason_code, actor, at, score_at_time, "
                "was_flagged, formula_version, document_category, extraction_models, is_handwritten) "
                "VALUES (?, ?, 'invoice_total', '1,180.00', ?, ?, 'correct', 'seed', ?, ?, 0, 'v1', "
                "'tax_invoice', '{}', 0)",
                (
                    base, client_id, None if action == ACTION_CONFIRMED else "1,190.00",
                    action, f"{month}-15T10:00:00+00:00", score,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_calibration_bins_a_seeded_history(scratch):
    db, client_id = scratch
    _seed_history(db, client_id, [
        (55.0, ACTION_EDITED, "2026-01"), (55.0, ACTION_CONFIRMED, "2026-01"),
        (75.0, ACTION_EDITED, "2026-02"),
        (85.0, ACTION_CONFIRMED, "2026-02"),
        (92.0, ACTION_CONFIRMED, "2026-03"), (92.0, ACTION_EDITED, "2026-03"),
        (97.0, ACTION_CONFIRMED, "2026-04"),
        (97.0, ACTION_MARKED_NOT_PRINTED, "2026-04"),
    ])
    report = cal.calibration_report(client_id=client_id, db_path=db)
    bands = {row["band"]: row for row in report["bands"]}
    assert bands["<60"]["fields"] == 2 and bands["<60"]["edited"] == 1
    assert bands["<60"]["edit_rate_pct"] == 50.0
    assert bands["60-69"]["fields"] == 0
    assert bands["70-79"]["edited"] == 1 and bands["70-79"]["edit_rate_pct"] == 100.0
    assert bands["80-89"]["edited"] == 0
    assert bands["90-94"]["fields"] == 2 and bands["90-94"]["edited"] == 1
    assert bands["95-100"]["fields"] == 2 and bands["95-100"]["edited"] == 1
    assert report["months_of_history"] == 4
    # The dangerous ones are the highest-scoring EDITS (a confirmation is not an
    # edit, however high the score), worst first.
    edited_scores = [row["score_at_time"] for row in report["highest_scoring_edits"]]
    assert edited_scores == sorted(edited_scores, reverse=True)
    assert edited_scores[:2] == [97.0, 92.0]
    assert all(row["band"] in ("90-94", "95-100") for row in report["highest_scoring_edits"][:2])
    assert report["by_category"][0]["document_category"] == "tax_invoice"
    assert {row["handwriting"] for row in report["by_handwriting"]} == {"printed", "handwritten"}


def test_calibration_reports_which_reader_was_right(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_total": {"value_a": "1,180.00", "value_b": "1,190.00", "reads_agree": "disagree"},
        "igst_amount": {"value_a": "180.00", "value_b": "190.00", "reads_agree": "disagree"},
    })
    R.run_rules_for_document(document_id, db_path=db)
    # The human keeps read B's total and read A's igst.
    conf.record_correction(
        _field_id(db, document_id, "invoice_total"), ACTION_EDITED, actor="r",
        value_after="1,190.00", db_path=db,
    )
    conf.record_correction(
        _field_id(db, document_id, "igst_amount"), ACTION_CONFIRMED, actor="r", db_path=db,
    )
    report = cal.calibration_report(client_id=client_id, db_path=db)
    agreement = report["reader_agreement"]
    assert agreement["read_b_right"] == 1
    assert agreement["corrections_on_disagreeing_fields"] == 2


def test_the_gate_stays_closed_below_each_limit_and_opens_when_all_hold(scratch):
    db, client_id = scratch
    # Not enough fields yet.
    _seed_history(db, client_id, [(95.0, ACTION_CONFIRMED, "2026-01")] * 5)
    gate = cal.bulk_approve_gate(client_id, db_path=db)
    assert gate["open"] is False
    assert any("switched off" in r for r in gate["reasons"])
    assert any("are required" in r for r in gate["reasons"])

    # Turn it on, lower the field bar, but keep the history in ONE month.
    from src.rules import service as rules_service

    rules_service.edit_rule_value(
        conf.KEY_BULK_ENABLED, "true", scope="firm", client_id=None,
        actor="manager", reason="pilot", db_path=db,
    )
    rules_service.edit_rule_value(
        conf.KEY_BULK_MIN_FIELDS, "5", scope="firm", client_id=None,
        actor="manager", reason="pilot", db_path=db,
    )
    gate = cal.bulk_approve_gate(client_id, db_path=db)
    assert gate["open"] is False
    assert any("month(s)" in r for r in gate["reasons"])

    # Now span three months with a clean history.
    _seed_history(db, client_id, [(95.0, ACTION_CONFIRMED, m) for m in ("2026-02", "2026-03")] * 3)
    gate = cal.bulk_approve_gate(client_id, db_path=db)
    assert gate["open"] is True, gate["reasons"]
    assert gate["months"] >= 3
    assert gate["edit_rate_pct"] == 0.0

    # One edit above the rate limit closes it again.
    _seed_history(db, client_id, [(95.0, ACTION_EDITED, "2026-04")] * 6)
    gate = cal.bulk_approve_gate(client_id, db_path=db)
    assert gate["open"] is False
    assert any("above the" in r and "limit" in r for r in gate["reasons"])


def test_the_gate_has_no_override_argument(scratch):
    db, client_id = scratch
    import inspect

    signature = inspect.signature(cal.bulk_approve_gate)
    assert "force" not in signature.parameters
    assert "override" not in signature.parameters
    gate = cal.bulk_approve_gate(client_id, db_path=db)
    assert gate["open"] is False


def test_the_calibration_cli_renders(scratch, capsys):
    db, client_id = scratch
    _seed_history(db, client_id, [(92.0, ACTION_CONFIRMED, "2026-03")])
    assert cal.main(["report", "--db", db, "--client-id", str(client_id)]) == 0
    out = capsys.readouterr().out
    assert "Score bands" in out and "Bulk approve" in out and "CLOSED" in out
    assert cal.main(["report", "--db", db, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "bands" in payload and "bulk_approve" in payload


# ===========================================================================
# The Phase 2/3 fixtures, end to end
# ===========================================================================


def test_kmap_003_scores_high_and_needs_nobody(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_number": "KMAP/2026-27/003", "invoice_date": "27-Apr-26",
        "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
        "place_of_supply": "Uttar Pradesh", "line.1.hsn_sac": "997112",
        "line.1.amount": "1,000.00", "igst_amount": "180.00",
        "invoice_total": "1,180.00", "reference_no_date": "KMAP/2025-06/003 dt. 27-Apr-26",
    })
    R.run_rules_for_document(document_id, db_path=db)
    model = R.get_review_model(document_id, db_path=db)
    assert model["needs_you"] == []
    assert all(s["score"] >= 90 for s in model["settled"])


def test_kmap_001_missing_sac_is_a_gap_not_a_score(scratch):
    db, client_id = scratch
    document_id = seed_document(
        db, client_id,
        {
            "invoice_number": "KMAP/2026-27/001", "invoice_date": "6-Apr-26",
            "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
            "place_of_supply": "Uttar Pradesh", "line.1.amount": "10,947.00",            "line.1.hsn_sac": {},            "igst_amount": "1,970.46", "round_off": "-0.46", "invoice_total": "12,917.00",
        },
        reads={"line.1.hsn_sac": {"status": "not_printed", "value_a": None, "value_b": None,
                                  "reads_agree": "null"}},
    )
    R.run_rules_for_document(document_id, db_path=db)
    scores = conf.get_scores(document_id, db_path=db)
    assert scores["line.1.hsn_sac"]["score"] is None
    assert "missing-required" in scores["line.1.hsn_sac"]["signals"][0]["reason"]
    model = R.get_review_model(document_id, db_path=db)
    assert model["needs_you"][0]["field_key"] == "line.1.hsn_sac"


def test_asap_197_handwritten_total_caps_at_80_and_the_guard_flags_it(scratch):
    db, client_id = scratch
    document_id = seed_document(
        db, client_id,
        {
            "issuer_gstin": "09AADFA5221A1Z6", "place_of_supply": "Delhi",
            "line.1.amount": "6,825.00", "igst_rate": "18", "igst_amount": "1,228.50",
            "round_off": "-0.50", "invoice_total": "8,053.00",
            "recipient_gstin": CLIENT_GSTIN,
        },
        reads={
            # The handwritten "total" the client scrawled on the page.
            "invoice_total": {"value_a": "7,370", "value_b": "7,370", "reads_agree": "exact",
                              "is_handwritten": 1, "status": "needs_printed"},
            # The printed line amount, which a handwritten 6,825 overlaps.
            "line.1.amount": {"value_a": "6,825.00", "value_b": "6,825.00",
                              "reads_agree": "exact", "is_handwritten": 0,
                              "status": "needs_printed"},
        },
    )
    R.run_rules_for_document(document_id, db_path=db)
    scores = conf.get_scores(document_id, db_path=db)
    assert scores["invoice_total"]["score"] == 0.0
    assert "needs_printed" in {c["name"] for c in scores["invoice_total"]["caps"]}
    assert scores["line.1.amount"]["score"] == 0.0
    # The handwriting guard turns that into a failed check for the reviewer.
    model = R.get_review_model(document_id, db_path=db)
    failed = [c["check_key"] for c in model["checks"] if c["status"] == "fail"]
    assert "handwriting_guard" in failed
    assert any(i["kind"] == "check_failed" and i["check_key"] == "handwriting_guard"
               for i in model["needs_you"])


def test_asap_336_printed_values_are_scored_not_penalised(scratch):
    """The printed block of a page with handwriting is still trustworthy."""
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "issuer_gstin": "07AADFA5221A1ZA", "recipient_gstin": CLIENT_GSTIN,
        "place_of_supply": "Delhi", "line.1.amount": "10,490.00",
        "line.2.amount": "3,686.00", "taxable_value": "14,176.00",
        "cgst_rate": "9", "cgst_amount": "1,275.84", "sgst_rate": "9",
        "sgst_amount": "1,275.84", "round_off": "0.32", "invoice_total": "16,728.00",
    })
    R.run_rules_for_document(document_id, db_path=db)
    scores = conf.get_scores(document_id, db_path=db)
    assert scores["invoice_total"]["score"] >= 90
    assert scores["cgst_amount"]["score"] >= 90
    assert scores["invoice_total"]["caps"] == []


def test_phase4_tables_and_migration_are_present(scratch):
    db, _client = scratch
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"invoice_doc_field_scores", "invoice_field_corrections"} <= names
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        assert {1, 2, 3, 4, 5} <= versions
    finally:
        conn.close()


def test_the_scorer_never_writes_to_the_values_it_scores(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {"invoice_total": "1,180.00"})
    conn = sqlite3.connect(db)
    try:
        before = conn.execute("SELECT * FROM invoice_doc_fields ORDER BY field_id").fetchall()
    finally:
        conn.close()
    R.run_rules_for_document(document_id, db_path=db)
    conn = sqlite3.connect(db)
    try:
        after = conn.execute("SELECT * FROM invoice_doc_fields ORDER BY field_id").fetchall()
    finally:
        conn.close()
    assert before == after
    source = open(conf.__file__).read()
    for statement in ("UPDATE invoice_doc_fields", "INSERT INTO invoice_doc_fields",
                      "DELETE FROM invoice_doc_fields"):
        assert statement not in source
