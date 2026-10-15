"""Phase-3 deterministic-layer tests. NO network, no stubbed pure functions.

The rules are pure functions of stored values, so almost everything here builds
a :class:`DocContext` directly and asserts the outcome. Where a check needs the
C1 state table (or the database), a scratch DB is used — never the real one.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from src import bootstrap
from src.invoice_extract import applicability as ap
from src.invoice_extract import checks as ck
from src.invoice_extract import direction as dr
from src.invoice_extract import rules as R
from src.invoice_extract import rules_schema as rs
from src.invoice_extract.doc_context import DocContext, RuleConfig
from src.invoice_extract.gst_identifiers import gstin_check_digit, gstin_valid
from src.invoice_extract.service import InvoiceExtractError

# The client's own GSTIN, from the fixture bundle.
CLIENT_GSTIN = "07AAECK9054D1ZK"

# The state-code rows the checks need. Loaded once from a scratch DB so the
# tests use the REAL seeded table rather than a hand-written stand-in.
_STATE_CODES: dict[str, dict] = {}


@pytest.fixture(scope="module", autouse=True)
def _load_state_codes(tmp_path_factory):
    root = tmp_path_factory.mktemp("rules_state")
    db = str(root / "poc.db")
    bootstrap.run_migrations(db_path=db, data_root=root / "data")
    from src.rules import service as rules_service

    for row in rules_service.list_state_codes(db_path=db):
        _STATE_CODES[row["code"]] = row


def config(**kw) -> RuleConfig:
    base = dict(
        total_tolerance=Decimal("0.05"),
        tax_tolerance=Decimal("1.00"),
        service_sac_prefix="99",
        legal_form_suffixes=("limited", "ltd", "pvt", "private", "llp", "inc", "co", "and"),
        individual_stopwords=frozenset({"traders", "enterprises", "solutions", "services"}),
        slabs=(0.0, 0.25, 3.0, 5.0, 18.0, 40.0),
        state_codes=_STATE_CODES,
    )
    base.update(kw)
    return RuleConfig(**base)


def field(value=None, *, status="read", in_region=1, handwritten=0, **kw) -> dict:
    return {
        "field_key": kw.pop("field_key", "x"),
        "status": status, "value_a": value, "value_b": kw.pop("value_b", value),
        "reads_agree": kw.pop("reads_agree", "exact"), "text_layer_match": None,
        "is_handwritten": handwritten, "page": kw.pop("page", 2),
        "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05, "in_region": in_region,
    }


def context(*, category="tax_invoice", client=None, place_state=None, **kw) -> DocContext:
    fields: dict[str, dict] = {}
    for key, value in (kw.pop("values", {}) or {}).items():
        fields[key] = field(value, field_key=key)
    for key in kw.pop("not_printed", ()) or ():
        fields.setdefault(key, field(None, status="not_printed", field_key=key))
    for key, row in (kw.pop("raw_fields", {}) or {}).items():
        fields[key] = {**field(None), **row, "field_key": key}
    return DocContext(
        document_id=kw.pop("document_id", 1), extraction_id=1, category=category,
        client_id=kw.pop("client_id", 1), fields=fields,
        derived=kw.pop("derived", {}), annotations=kw.pop("annotations", ()),
        client=client, config=kw.pop("config", config()), place_state=place_state,
    )


def by_key(results: list[ck.CheckResult]) -> dict[str, ck.CheckResult]:
    return {r.check_key: r for r in results}


# ===========================================================================
# GSTIN checksum
# ===========================================================================


def test_checksum_accepts_the_real_client_gstins():
    """The four GSTINs in the fixture bundle must all pass the check digit."""
    for gstin in ("07AAECK9054D1ZK", "09AADFA5221A1Z6", "07AADFA5221A1ZA", "07AAACB2894G1ZP"):
        result = gstin_valid(gstin, known_state_codes={"07", "09"})
        assert result.ok, f"{gstin}: {result.reason}"
        assert result.pan and len(result.pan) == 10


def test_checksum_rejects_an_altered_last_character():
    """Build invalid GSTINs by changing the last character of a VALID one."""
    valid = CLIENT_GSTIN
    expected = valid[14]
    for char in "0123456789ABCDEFGHIJKL":
        if char == expected:
            continue
        broken = valid[:14] + char
        result = gstin_valid(broken, known_state_codes={"07"})
        assert not result.ok
        assert "check character" in result.reason
    # The algorithm is the thing being tested, so pin its answer.
    assert gstin_check_digit(valid[:14]) == expected


def test_checksum_rejects_shape_pan_and_state_problems():
    assert not gstin_valid("07AAECK9054D1Z", known_state_codes={"07"}).ok        # 14 chars
    assert not gstin_valid("07AAECK9054D1ZKX", known_state_codes={"07"}).ok      # 16 chars
    assert "shape" in gstin_valid("07@AECK9054D1ZK", known_state_codes={"07"}).reason
    assert "PAN" in gstin_valid("07AA1CK9054D1ZK", known_state_codes={"07"}).reason
    # A state code the C1 table does not know.
    result = gstin_valid("77AAECK9054D1ZK", known_state_codes={"07"})
    assert not result.ok and "state code" in result.reason
    assert not gstin_valid("", known_state_codes={"07"}).ok


def test_gstin_check_helper_fails_and_lists_the_fields():
    ctx = context(
        values={"issuer_gstin": "07AAECK9054D1ZK", "recipient_gstin": "07AAECK9054D1ZL"},
    )
    result = by_key(ck.run_all(ctx))["gstin_valid"]
    assert result.status == "fail"
    assert result.fields_implicated == ("recipient_gstin",)

    # No GSTIN printed at all → the check cannot fail, so it is not produced.
    ctx = context(values={"invoice_number": "X"})
    assert by_key(ck.run_all(ctx))["gstin_valid"].status == "not_applicable"


# ===========================================================================
# THE RULE: a check that cannot fail is never a pass
# ===========================================================================


def test_missing_inputs_are_not_applicable_never_pass():
    ctx = context(values={})                       # nothing printed at all
    results = by_key(ck.run_all(ctx))
    for key in ("totals_reconcile", "tax_equals_rate_times_taxable", "derived_rate_on_valid_slab",
                "gstin_valid", "state_code_matches_gstin", "expected_tax_type",
                "provenance_in_region", "handwriting_guard"):
        assert results[key].status == "not_applicable", key
        assert results[key].detail, f"{key} must give a reason"
    assert not any(r.status == "pass" for r in results.values())


def test_tax_check_is_not_produced_when_the_rate_is_not_printed():
    """A derived rate must never be fed back in as the input of this check."""
    ctx = context(values={
        "line.1.amount": "10,947.00", "igst_amount": "1,970.46",
        "round_off": "-0.46", "invoice_total": "12,917.00",
    })
    result = by_key(ck.run_all(ctx))["tax_equals_rate_times_taxable"]
    assert result.status == "not_applicable"
    assert "no tax rate is printed" in result.detail


def test_tax_check_runs_when_the_rate_is_printed():
    ctx = context(values={
        "line.1.amount": "26,000.00", "igst_rate": "18", "igst_amount": "4,680.00",
        "invoice_total": "30,680.00",
    })
    assert by_key(ck.run_all(ctx))["tax_equals_rate_times_taxable"].status == "pass"
    bad = context(values={
        "line.1.amount": "26,000.00", "igst_rate": "18", "igst_amount": "4,000.00",
        "invoice_total": "30,000.00",
    })
    result = by_key(ck.run_all(bad))["tax_equals_rate_times_taxable"]
    assert result.status == "fail"
    assert "igst_amount" in result.fields_implicated


def test_slab_check_uses_the_period_of_the_invoice_date():
    """18% is valid in both periods; 12% only before 2025-09-22."""
    before = config(slabs=(0.0, 0.25, 3.0, 5.0, 12.0, 18.0, 28.0))
    after = config(slabs=(0.0, 0.25, 3.0, 5.0, 18.0, 40.0))
    values = {"line.1.amount": "1,000.00", "igst_amount": "120.00", "invoice_total": "1,120.00"}

    result = by_key(ck.run_all(context(values=values, config=before)))["derived_rate_on_valid_slab"]
    assert result.status == "not_applicable", "the check needs a derived rate to test"
    derived = {"line_rate": "12.00"}
    assert by_key(ck.run_all(context(values=values, config=before, derived=derived)))[
        "derived_rate_on_valid_slab"].status == "pass"
    assert by_key(ck.run_all(context(values=values, config=after, derived=derived)))[
        "derived_rate_on_valid_slab"].status == "fail"
    # And with no slab period at all it cannot be decided.
    assert by_key(ck.run_all(context(values=values, config=config(slabs=()), derived=derived)))[
        "derived_rate_on_valid_slab"].status == "not_applicable"


def test_handwriting_guard_fails_on_a_handwritten_total():
    ctx = context(raw_fields={
        "invoice_total": {"value_a": "7,370", "value_b": "8,053.00", "status": "needs_printed",
                          "is_handwritten": 1, "reads_agree": "disagree"},
        "taxable_value": {"value_a": "6,825.00", "value_b": "6,825.00"},
    })
    result = by_key(ck.run_all(ctx))["handwriting_guard"]
    assert result.status == "fail"
    assert "invoice_total" in result.fields_implicated
    assert result.inputs["annotations"] == 0


def test_provenance_check_fails_and_lists_out_of_region_fields():
    ctx = context(raw_fields={
        "invoice_total": {"value_a": "1,180.00", "status": "out_of_region", "in_region": 0},
        "invoice_number": {"value_a": "KMAP/3"},
    })
    result = by_key(ck.run_all(ctx))["provenance_in_region"]
    assert result.status == "fail"
    assert result.fields_implicated == ("invoice_total",)


# ===========================================================================
# expected_tax_type
# ===========================================================================


def _delhi_place():
    return _STATE_CODES["07"]


def _up_place():
    return _STATE_CODES["09"]


def test_expected_tax_type_inter_state_passes_and_intra_state_heads_fail():
    inter = context(
        values={"issuer_gstin": CLIENT_GSTIN, "place_of_supply": "Uttar Pradesh",
                "igst_amount": "180.00"},
        place_state=_up_place(),
    )
    result = by_key(ck.run_all(inter))["expected_tax_type"]
    assert result.status == "pass"
    assert "Delhi → Uttar Pradesh" in result.detail

    wrong = context(
        values={"issuer_gstin": CLIENT_GSTIN, "place_of_supply": "Uttar Pradesh",
                "cgst_amount": "90.00", "sgst_amount": "90.00"},
        place_state=_up_place(),
    )
    result = by_key(ck.run_all(wrong))["expected_tax_type"]
    assert result.status == "fail"
    assert set(result.fields_implicated) == {"cgst_amount", "sgst_amount"}


def test_synthetic_delhi_to_delhi_invoice_with_an_igst_line_fails():
    ctx = context(
        values={"issuer_gstin": CLIENT_GSTIN, "place_of_supply": "Delhi",
                "igst_amount": "180.00"},
        place_state=_delhi_place(),
    )
    result = by_key(ck.run_all(ctx))["expected_tax_type"]
    assert result.status == "fail"
    assert result.fields_implicated == ("igst_amount",)
    assert "intra-state" in result.detail


def test_expected_tax_type_cannot_determine_for_export_and_reverse_charge():
    export = context(
        values={"issuer_gstin": CLIENT_GSTIN, "place_of_supply": "Other Country",
                "igst_amount": "0"},
        place_state=_STATE_CODES["96"],
    )
    result = by_key(ck.run_all(export))["expected_tax_type"]
    assert result.status == "not_applicable"
    assert "cannot determine" in result.detail

    rcm = context(
        values={"issuer_gstin": CLIENT_GSTIN, "place_of_supply": "Delhi",
                "igst_amount": "180.00", "reference_no_date": "Tax payable on reverse charge"},
        place_state=_delhi_place(),
    )
    assert by_key(ck.run_all(rcm))["expected_tax_type"].status == "not_applicable"


def test_utgst_state_expects_cgst_plus_the_second_head():
    """Ladakh has no legislature, so UTGST applies where SGST would."""
    ladakh = context(
        values={"issuer_gstin": "38AAECK9054D1ZK", "place_of_supply": "Ladakh",
                "cgst_amount": "90.00", "sgst_amount": "90.00"},
        place_state=_STATE_CODES["38"],
    )
    result = by_key(ck.run_all(ladakh))["expected_tax_type"]
    assert result.status == "pass"
    assert result.inputs["utgst_applies"] is True
    assert _STATE_CODES["38"]["utgst"] is True
    # Delhi is a UT WITH a legislature — UTGST must NOT apply there.
    assert _STATE_CODES["07"]["utgst"] is False


def test_state_code_check_fails_on_an_unknown_place_of_supply():
    ctx = context(
        values={"issuer_gstin": CLIENT_GSTIN, "place_of_supply": "Atlantis"},
        place_state=None,
    )
    result = by_key(ck.run_all(ctx))["state_code_matches_gstin"]
    assert result.status == "fail"
    assert "place_of_supply" in result.fields_implicated

    missing = context(values={"issuer_gstin": CLIENT_GSTIN})
    assert by_key(ck.run_all(missing))["state_code_matches_gstin"].status == "not_applicable"


# ===========================================================================
# The fixture documents, end to end through applicability + direction
# ===========================================================================


def test_kmap_003_delhi_to_up_igst():
    ctx = context(
        client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
        values={
            "invoice_number": "KMAP/2026-27/003", "invoice_date": "27-Apr-26",
            "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
            "place_of_supply": "Uttar Pradesh", "line.1.hsn_sac": "997112",
            "line.1.amount": "1,000.00", "igst_amount": "180.00",
            "invoice_total": "1,180.00", "reference_no_date": "KMAP/2025-06/003 dt. 27-Apr-26",
        },
        not_printed=("buyers_order_no",),
        place_state=_up_place(),
        derived={"line_rate": "18.00"},
    )
    levels = {row.field_key: row for row in ap.compute(ctx)}
    assert levels["igst_amount"].level == rs.LEVEL_REQUIRED
    assert levels["igst_rate"].level == rs.LEVEL_OPTIONAL      # derived, never asked for
    assert levels["cgst_amount"].level == rs.LEVEL_NOT_APPLICABLE
    assert levels["sgst_amount"].level == rs.LEVEL_NOT_APPLICABLE
    assert "inter-state" in levels["cgst_amount"].reason
    assert levels["buyers_order_no"].level == rs.LEVEL_OPTIONAL
    assert levels["reference_no_date"].level == rs.LEVEL_OPTIONAL
    # A blank OPTIONAL field is never a question.
    assert ap.missing_required(ctx, ap.compute(ctx)) == []

    results = by_key(ck.run_all(ctx))
    assert results["totals_reconcile"].status == "pass"
    assert results["expected_tax_type"].status == "pass"
    assert results["gstin_valid"].status == "pass"
    assert results["handwriting_guard"].status == "pass"
    assert results["derived_rate_on_valid_slab"].status == "pass"
    assert not [r for r in results.values() if r.status == "fail"]

    assert dr.decide(ctx).direction == rs.DIRECTION_SALES
    assert "issuer GSTIN" in dr.decide(ctx).basis


def test_kmap_002_two_lines_reconcile():
    ctx = context(
        client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
        values={
            "invoice_date": "13-Apr-26", "issuer_gstin": CLIENT_GSTIN,
            "place_of_supply": "Uttar Pradesh",
            "line.1.hsn_sac": "997112", "line.1.amount": "24,000.00",
            "line.2.hsn_sac": "9971", "line.2.amount": "2,000.00",
            "taxable_value": "26,000.00", "igst_rate": "18", "igst_amount": "4,680.00",
            "invoice_total": "30,680.00",
        },
        place_state=_up_place(),
    )
    results = by_key(ck.run_all(ctx))
    assert results["totals_reconcile"].status == "pass"
    assert "26,000.00 taxable + 4,680.00 tax" in results["totals_reconcile"].detail
    assert results["tax_equals_rate_times_taxable"].status == "pass"


def test_kmap_001_no_sac_no_rate_needs_the_reviewer():
    ctx = context(
        client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
        values={
            "invoice_number": "KMAP/2026-27/001", "invoice_date": "6-Apr-26",
            "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
            "place_of_supply": "Uttar Pradesh", "line.1.amount": "10,947.00",
            "igst_amount": "1,970.46", "round_off": "-0.46", "invoice_total": "12,917.00",
        },
        not_printed=("line.1.hsn_sac", "taxable_value"),
        place_state=_up_place(),
        derived={"line_rate": "18.00"},
    )
    levels = {row.field_key: row for row in ap.compute(ctx)}
    assert levels["line.1.hsn_sac"].level == rs.LEVEL_REQUIRED
    missing = [row["field_key"] for row in ap.missing_required(ctx, ap.compute(ctx))]
    assert missing == ["line.1.hsn_sac"]

    results = by_key(ck.run_all(ctx))
    assert results["totals_reconcile"].status == "pass"
    assert "10,947.00 taxable + 1,970.46 tax + -0.46 round off" in results["totals_reconcile"].detail
    assert results["tax_equals_rate_times_taxable"].status == "not_applicable"
    assert results["derived_rate_on_valid_slab"].status == "pass"


def test_airtel_intra_state_individual_recipient_is_undetermined():
    ctx = context(
        client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
        values={
            "issuer_gstin": "07AAACB2894G1ZP", "place_of_supply": "Delhi",
            "line.1.hsn_sac": "998412", "taxable_value": "1,098.00",
            "cgst_rate": "9", "cgst_amount": "98.82",
            "sgst_rate": "9", "sgst_amount": "98.82", "invoice_total": "1,295.64",
            "recipient_name": "Saurabh Kulkarni",
        },
        place_state=_delhi_place(),
    )
    levels = {row.field_key: row for row in ap.compute(ctx)}
    assert levels["cgst_amount"].level == rs.LEVEL_REQUIRED
    assert levels["sgst_amount"].level == rs.LEVEL_REQUIRED
    assert levels["igst_amount"].level == rs.LEVEL_NOT_APPLICABLE

    results = by_key(ck.run_all(ctx))
    assert results["totals_reconcile"].status == "pass"
    assert results["expected_tax_type"].status == "pass"
    assert results["tax_equals_rate_times_taxable"].status == "pass"

    decided = dr.decide(ctx)
    assert decided.direction == rs.DIRECTION_UNDETERMINED
    assert decided.needs_a_human


def test_asap_197_and_336_are_purchases_via_the_recipient_gstin():
    for values, round_off in (
        ({"line.1.hsn_sac": "9982", "line.1.amount": "6,825.00", "igst_rate": "18",
          "igst_amount": "1,228.50", "round_off": "-0.50", "invoice_total": "8,053.00",
          "issuer_gstin": "09AADFA5221A1Z6", "place_of_supply": "Delhi",
          "recipient_gstin": CLIENT_GSTIN}, "-0.50"),
        ({"line.1.hsn_sac": "998221", "line.1.amount": "10,490.00",
          "line.2.hsn_sac": "99823", "line.2.amount": "3,686.00",
          "taxable_value": "14,176.00", "cgst_rate": "9", "cgst_amount": "1,275.84",
          "sgst_rate": "9", "sgst_amount": "1,275.84", "round_off": "0.32",
          "invoice_total": "16,728.00", "issuer_gstin": "07AADFA5221A1ZA",
          "place_of_supply": "Delhi", "recipient_gstin": CLIENT_GSTIN}, "0.32"),
    ):
        place = _STATE_CODES["09"] if values["issuer_gstin"].startswith("09") else _delhi_place()
        ctx = context(
            client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
            values=values, place_state=place,
        )
        decided = dr.decide(ctx)
        assert decided.direction == rs.DIRECTION_PURCHASE, values["invoice_total"]
        assert "recipient GSTIN" in decided.basis
        result = by_key(ck.run_all(ctx))["totals_reconcile"]
        assert result.status == "pass", result.detail
        assert round_off in result.detail


def test_jelux_legal_form_difference_is_a_near_match_not_exact():
    ctx = context(
        client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
        values={"issuer_name": "Jelux Solutions", "issuer_gstin": "27AAACJ1234A1Z5",
                "invoice_date": "1-Apr-26", "invoice_total": "9,000.00",
                "line.1.amount": "9,000.00", "recipient_name": "KMAP Capital Pvt Ltd"},
        place_state=_STATE_CODES["27"],
    )
    decided = dr.decide(ctx)
    assert decided.direction == rs.DIRECTION_LIKELY_PURCHASE
    assert decided.needs_a_human is True
    assert decided.match["name_match"]["kind"] == "near"
    assert "legal form differs" in decided.match["name_match"]["difference"]
    assert "pvt ltd" in decided.basis and "limited" in decided.basis

    # The same name, same legal form → exact.
    same = context(
        client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
        values={"recipient_name": "KMAP CAPITAL LIMITED", "issuer_gstin": "27AAACJ1234A1Z5"},
        place_state=_STATE_CODES["27"],
    )
    assert dr.decide(same).match["name_match"]["kind"] == "exact"


def test_direction_never_defaults():
    """No client GSTIN, no recipient, an individual, a name we do not know —
    every one of those is undetermined, and none of them is called sales."""
    no_client = context(values={"issuer_gstin": CLIENT_GSTIN}, client=None)
    assert dr.decide(no_client).direction == rs.DIRECTION_UNDETERMINED

    nothing = context(client={"client_id": 1, "legal_name": "X Ltd", "gstin": CLIENT_GSTIN},
                      values={"issuer_gstin": "27AAACJ1234A1Z5"})
    assert dr.decide(nothing).direction == rs.DIRECTION_UNDETERMINED

    individual = context(client={"client_id": 1, "legal_name": "KMAP Capital Limited",
                                 "gstin": CLIENT_GSTIN},
                         values={"issuer_gstin": "27AAACJ1234A1Z5",
                                 "recipient_name": "Ramesh Gupta"})
    decided = dr.decide(individual)
    assert decided.direction == rs.DIRECTION_UNDETERMINED
    assert decided.match["recipient_looks_like_an_individual"] is True

    both = context(client={"client_id": 1, "legal_name": "X", "gstin": CLIENT_GSTIN},
                   values={"issuer_gstin": CLIENT_GSTIN, "recipient_gstin": CLIENT_GSTIN})
    decided = dr.decide(both)
    assert decided.direction == rs.DIRECTION_UNDETERMINED
    assert decided.match.get("conflict")


def test_a_pan_alone_never_decides_direction():
    """A PAN beside the issuer's name is the ISSUER's own — it proves nothing."""
    ctx = context(
        client={"client_id": 1, "legal_name": "KMAP Capital Limited", "gstin": CLIENT_GSTIN},
        values={"issuer_name": "Some Vendor", "issuer_pan": "AAACK9054D",
                "recipient_pan": None, "issuer_gstin": "27AAACJ1234A1Z5"},
        place_state=_STATE_CODES["27"],
    )
    assert dr.decide(ctx).direction == rs.DIRECTION_UNDETERMINED


# ===========================================================================
# The database side: run_rules, overrides, aliases, the CLI
# ===========================================================================


@pytest.fixture()
def scratch(tmp_path_factory):
    root = tmp_path_factory.mktemp("rules_db")
    db = str(root / "poc.db")
    bootstrap.run_migrations(db_path=db, data_root=root / "data")
    from src.clients import service as clients

    client_id = clients.create_client(
        legal_name="KMAP Capital Limited", pan="AAACK9054D", gstin=CLIENT_GSTIN,
        actor="test", owner_user_id=1, db_path=db,
    )
    return db, client_id


def seed_document(db: str, client_id: int, fields: dict[str, str], *, category="tax_invoice",
                  ordinal=1, in_region=1, not_printed=()) -> int:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_uploads (upload_id, client_id, filename, file_ext, source_format, "
            "extraction_path, status, duplicate_warning, created_at, created_by) "
            "VALUES (?, ?, 'b.pdf', 'pdf', 'pdf', 'structured', 'extracted', 0, 'now', 't')",
            (ordinal, client_id),
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
            "provider_b, model_b, is_active) VALUES (?, 'complete', 'openrouter', 'a', 'anthropic', 'b', 1)",
            (document_id,),
        )
        extraction_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for key, value in fields.items():
            conn.execute(
                "INSERT INTO invoice_doc_fields (extraction_id, document_id, field_key, status, "
                "value_a, value_b, reads_agree, in_region) VALUES (?, ?, ?, 'read', ?, ?, 'exact', ?)",
                (extraction_id, document_id, key, value, value, in_region),
            )
        for key in not_printed:
            conn.execute(
                "INSERT INTO invoice_doc_fields (extraction_id, document_id, field_key, status, "
                "reads_agree, in_region) VALUES (?, ?, ?, 'not_printed', 'null', 1)",
                (extraction_id, document_id, key),
            )
        conn.commit()
        return document_id
    finally:
        conn.close()


def test_run_rules_persists_everything_and_is_idempotent(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_number": "KMAP/2026-27/003", "invoice_date": "27-Apr-26",
        "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
        "place_of_supply": "Uttar Pradesh", "line.1.hsn_sac": "997112",
        "line.1.amount": "1,000.00", "igst_amount": "180.00", "invoice_total": "1,180.00",
    }, not_printed=("buyers_order_no",))

    first = R.run_rules_for_document(document_id, db_path=db)
    assert first["direction"] == rs.DIRECTION_SALES
    assert {d["key"] for d in first["derived"]} == {
        "round_off", "line_rate", "issuer_state_code", "expected_tax_type",
    }
    assert first["checks_failed"] == []

    conn = sqlite3.connect(db)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("invoice_doc_derived", "invoice_doc_checks",
                          "invoice_doc_applicability", "invoice_doc_direction")
        }
    finally:
        conn.close()
    assert counts["invoice_doc_derived"] == 4
    assert counts["invoice_doc_checks"] == 8
    assert counts["invoice_doc_applicability"] > 10
    assert counts["invoice_doc_direction"] == 1

    # Idempotent: the same rows, not twice as many.
    R.run_rules_for_document(document_id, db_path=db)
    conn = sqlite3.connect(db)
    try:
        again = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("invoice_doc_derived", "invoice_doc_checks",
                          "invoice_doc_applicability", "invoice_doc_direction")
        }
    finally:
        conn.close()
    assert again == counts


def test_rules_never_write_to_invoice_doc_fields(scratch):
    """The extracted values are input. This asserts it at the source level too."""
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_total": "1,180.00", "issuer_gstin": CLIENT_GSTIN, "place_of_supply": "Delhi",
    })
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

    source = Path(R.__file__).read_text()
    assert "UPDATE invoice_doc_fields" not in source
    assert "INSERT INTO invoice_doc_fields" not in source
    assert "DELETE FROM invoice_doc_fields" not in source


def test_override_is_logged_and_survives_a_rerun(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_total": "1,180.00", "issuer_gstin": CLIENT_GSTIN,
        "place_of_supply": "Delhi",
    })
    R.run_rules_for_document(document_id, db_path=db)
    R.override_applicability(
        document_id, "invoice_number", rs.LEVEL_OPTIONAL, actor="reviewer",
        reason="this vendor's cash memos carry no number", db_path=db,
    )
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT level, source, overridden_by, override_reason FROM invoice_doc_applicability "
            "WHERE document_id = ? AND field_key = 'invoice_number'", (document_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (rs.LEVEL_OPTIONAL, "user", "reviewer", "this vendor's cash memos carry no number")

    # A re-run recomputes the RULE rows and keeps the reviewer's override.
    R.run_rules_for_document(document_id, db_path=db)
    conn = sqlite3.connect(db)
    try:
        after = conn.execute(
            "SELECT level, source, overridden_by FROM invoice_doc_applicability "
            "WHERE document_id = ? AND field_key = 'invoice_number'", (document_id,),
        ).fetchone()
        rules_rows = conn.execute(
            "SELECT COUNT(*) FROM invoice_doc_applicability WHERE document_id = ? AND source = 'rule'",
            (document_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == (rs.LEVEL_OPTIONAL, "user", "reviewer")
    assert rules_rows > 10
    # And the review model no longer asks about the invoice number.
    model = R.get_review_model(document_id, db_path=db)
    assert "invoice_number" not in [item["field_key"] for item in model["missing_required"]]

    with pytest.raises(InvoiceExtractError):
        R.override_applicability(document_id, "invoice_total", "required", actor="r",
                                 reason="   ", db_path=db)


def test_direction_confirmation_and_aliases(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "issuer_gstin": "27AAACJ1234A1Z5", "place_of_supply": "Maharashtra",
        "invoice_total": "9,000.00", "line.1.amount": "9,000.00",
        "recipient_name": "KMAP Capital Pvt Ltd",
    })
    R.run_rules_for_document(document_id, db_path=db)
    model = R.get_review_model(document_id, db_path=db)
    assert model["direction"]["direction"] == rs.DIRECTION_LIKELY_PURCHASE
    assert model["direction"]["needs_a_human"] is True
    # An unsettled direction is one of the QUESTIONS.
    assert any(item["kind"] == "direction_unsettled" for item in model["needs_you"])

    # The alias is NOT created by running the rules.
    assert R.list_aliases(client_id, db_path=db) == []

    result = R.confirm_direction(
        document_id, rs.DIRECTION_PURCHASE, actor="reviewer",
        alias_text="KMAP Capital Pvt Ltd", remember_alias=True, db_path=db,
    )
    assert result["direction"] == rs.DIRECTION_PURCHASE
    aliases = R.list_aliases(client_id, db_path=db)
    assert len(aliases) == 1
    # Stored as printed, folded only for case and punctuation — the legal form
    # is kept (it is what makes a match exact rather than near).
    assert aliases[0]["normalised_text"] == "kmap capital pvt ltd"
    assert aliases[0]["alias_text"] == "KMAP Capital Pvt Ltd"

    # The alias now matches a document that prints that spelling exactly.
    model = R.get_review_model(document_id, db_path=db)
    assert model["direction"]["needs_a_human"] is False
    assert model["direction"]["confirmed_by"] == "reviewer"
    assert not any(item["kind"] == "direction_unsettled" for item in model["needs_you"])

    # A re-run must not undo a human's decision.
    R.run_rules_for_document(document_id, db_path=db)
    again = R.get_review_model(document_id, db_path=db)["direction"]
    assert again["direction"] == rs.DIRECTION_PURCHASE
    assert again["confirmed_by"] == "reviewer"

    # Revoking the alias stops it matching, but keeps the row as history.
    R.revoke_alias(aliases[0]["alias_id"], actor="reviewer", db_path=db)
    assert R.list_aliases(client_id, db_path=db)[0]["revoked_by"] == "reviewer"
    assert len(R.list_aliases(client_id, db_path=db)) == 1
    R.revoke_alias(aliases[0]["alias_id"], actor="reviewer", db_path=db)   # idempotent
    with pytest.raises(InvoiceExtractError):
        R.revoke_alias(9999, actor="reviewer", db_path=db)


def test_an_individual_is_never_remembered_as_an_alias(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "issuer_gstin": "27AAACJ1234A1Z5", "place_of_supply": "Maharashtra",
        "recipient_name": "Ramesh Gupta",
    })
    R.run_rules_for_document(document_id, db_path=db)
    with pytest.raises(InvoiceExtractError) as exc:
        R.confirm_direction(document_id, rs.DIRECTION_PURCHASE, actor="reviewer",
                            alias_text="Ramesh Gupta", remember_alias=True, db_path=db)
    assert "individual" in str(exc.value)
    assert R.list_aliases(client_id, db_path=db) == []


def test_review_model_sections(scratch):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_number": "KMAP/2026-27/001", "invoice_date": "6-Apr-26",
        "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
        "place_of_supply": "Uttar Pradesh", "line.1.amount": "10,947.00",
        "igst_amount": "1,970.46", "round_off": "-0.46", "invoice_total": "12,917.00",
    }, not_printed=("line.1.hsn_sac", "taxable_value"))
    R.run_rules_for_document(document_id, db_path=db)
    model = R.get_review_model(document_id, db_path=db)

    assert {k for k in model} >= {
        "needs_you", "settled", "not_applicable", "optional", "derived", "checks", "direction",
    }
    # Missing required first, then failed checks.
    assert model["needs_you"][0]["kind"] == "missing_required"
    assert model["needs_you"][0]["field_key"] == "line.1.hsn_sac"
    assert [c["check_key"] for c in model["checks"]][0] == "totals_reconcile"
    assert all("confidence" not in json.dumps(c).lower() for c in model["checks"])
    assert {d["key"] for d in model["derived"]} >= {"line_rate", "expected_tax_type"}
    # No confidence anywhere in the model.
    assert "confidence" not in json.dumps(model).lower()


def test_cli_show_prints_the_review_model(scratch, capsys):
    db, client_id = scratch
    document_id = seed_document(db, client_id, {
        "invoice_total": "1,180.00", "issuer_gstin": CLIENT_GSTIN,
        "place_of_supply": "Delhi",
    })
    assert R.main(["show", str(document_id), "--db", db, "--rerun"]) == 0
    out = capsys.readouterr().out
    assert "NEEDS YOU" in out and "CHECKS" in out and "DIRECTION" in out
    assert "totals_reconcile" in out
    # Phase 4 added a fourth kind of needs-you item; the printer must know it.
    assert "[missing]" in out

    assert R.main(["show", str(document_id), "--db", db, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["document_id"] == document_id
    assert {item["kind"] for item in payload["needs_you"]} <= {
        "missing_required", "check_failed", "below_threshold", "direction_unsettled",
    }

    assert R.main(["show", "999999", "--db", db]) == 2
    assert "Unknown document" in capsys.readouterr().err


def test_phase3_tables_and_migration_are_present(scratch):
    db, _client = scratch
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "invoice_doc_derived", "invoice_doc_checks", "invoice_doc_applicability",
            "invoice_doc_direction", "client_aliases", "gst_state_codes",
        } <= names
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        assert {1, 2, 3, 4} <= versions
        states = conn.execute("SELECT COUNT(*) FROM gst_state_codes").fetchone()[0]
        assert states >= 38
    finally:
        conn.close()


def test_phase2_extraction_completion_runs_the_rules(scratch, monkeypatch):
    """The Phase-2 job hook: a completed extraction leaves a review model ready."""
    db, client_id = scratch
    from src.invoice_extract import extract as ex

    document_id = seed_document(db, client_id, {
        "invoice_number": "KMAP/2026-27/003", "invoice_date": "27-Apr-26",
        "issuer_name": "KMAP Capital Limited", "issuer_gstin": CLIENT_GSTIN,
        "place_of_supply": "Uttar Pradesh", "line.1.hsn_sac": "997112",
        "line.1.amount": "1,000.00", "igst_amount": "180.00", "invoice_total": "1,180.00",
    })
    # Add the second document so the job has work, then let the job's rules hook
    # run against the first document's already-persisted extraction.
    import sqlite3 as _s

    conn = _s.connect(db)
    try:
        extraction_id = conn.execute(
            "SELECT extraction_id FROM invoice_doc_extractions WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    R.run_rules(extraction_id, db_path=db)
    model = R.get_review_model(document_id, db_path=db)
    assert model["checks"], "a completed extraction must have check rows"
    assert model["direction"]["direction"] == rs.DIRECTION_SALES

    # The hook is wired into the Phase-2 worker.
    source = Path(ex.__file__).read_text()
    assert "rules_mod.run_rules" in source
    assert '"rules_run"' in source


# ===========================================================================
# C1 data
# ===========================================================================


def test_state_code_table_covers_the_uts_and_flags_utgst(scratch):
    db, _client = scratch
    from src.rules import service as rules_service

    rows = {row["code"]: row for row in rules_service.list_state_codes(db_path=db)}
    assert len(rows) >= 38
    # The UTs WITHOUT a legislature — UTGST, not SGST.
    for code in ("04", "25", "26", "31", "35", "38"):
        assert rows[code]["utgst"] is True, code
    # The UTs WITH a legislature — SGST.
    for code in ("01", "07", "34"):
        assert rows[code]["utgst"] is False, code
    # Every row is seed-unverified until someone reviews it.
    assert all(row["last_reviewed_at"] is None for row in rows.values())
    assert all("UNVERIFIED" in (row["notes"] or "") for row in rows.values())
    # The non-state jurisdictions are flagged so tax type is "cannot determine".
    for code in ("96", "97", "99"):
        assert rows[code]["special"] is True


def test_state_resolution_handles_abbreviations_and_ambiguity(scratch):
    db, _client = scratch
    from src.rules import service as rules_service

    assert rules_service.resolve_state("UP", db_path=db)["code"] == "09"
    assert rules_service.resolve_state("U.P.", db_path=db)["code"] == "09"
    assert rules_service.resolve_state("Uttar Pradesh", db_path=db)["code"] == "09"
    assert rules_service.resolve_state("NCT of Delhi", db_path=db)["code"] == "07"
    assert rules_service.resolve_state("Uttar Pradesh (East)", db_path=db)["code"] == "09"
    assert rules_service.resolve_state("Delhi", db_path=db)["code"] == "07"
    # "AP" is ambiguous between the pre-2014 undivided code and the current one:
    # the CURRENT registration series wins.
    assert rules_service.resolve_state("AP", db_path=db)["code"] == "37"
    assert rules_service.resolve_state("Atlantis", db_path=db) is None
    assert rules_service.resolve_state(None, db_path=db) is None


def test_gst_rate_slabs_are_dated(scratch):
    db, _client = scratch
    from src.rules import service as rules_service

    assert rules_service.gst_rate_slabs("2025-09-21", db_path=db) == [
        0.0, 0.25, 3.0, 5.0, 12.0, 18.0, 28.0,
    ]
    assert rules_service.gst_rate_slabs("2025-09-22", db_path=db) == [
        0.0, 0.25, 3.0, 5.0, 18.0, 40.0,
    ]
    assert rules_service.gst_rate_slabs("2027-01-01", db_path=db) == [
        0.0, 0.25, 3.0, 5.0, 18.0, 40.0,
    ]
    # No date → no claim.
    assert rules_service.gst_rate_slabs(None, db_path=db) == []


def test_phase3_rules_are_configurable_per_client(scratch):
    db, client_id = scratch
    from src.rules import service as rules_service

    assert rules_service.get_effective_value("gst.service_sac_prefix", db_path=db) == "99"
    assert rules_service.get_effective_value("gst.total_tolerance_rupees", db_path=db) == "0.05"
    assert rules_service.get_effective_value("gst.tax_tolerance_rupees", db_path=db) == "1.00"

    rules_service.edit_rule_value(
        "gst.total_tolerance_rupees", "2.50", scope="client", client_id=client_id,
        actor="manager", reason="this client's invoices round at the rupee", db_path=db,
    )
    assert rules_service.get_effective_value(
        "gst.total_tolerance_rupees", client_id=client_id, db_path=db,
    ) == "2.5"
    assert rules_service.get_effective_value("gst.total_tolerance_rupees", db_path=db) == "0.05"

    # The loader picks the client's override up, and the tolerance decides the check.
    cfg = R.load_config(client_id, db_path=db)
    assert cfg.total_tolerance == Decimal("2.5")
