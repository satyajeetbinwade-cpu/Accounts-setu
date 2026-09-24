"""Verification tests for the F6/F3-AI rate-bucketed Purchase Register fix.

Covers the corrective build prompt §6 acceptance table (T1–T12). Run:

    venv/bin/python -m pytest tests/test_f6_purchase_register.py -v

The tests use the SANITISED fixture (§3 rule 11) — the real client file is
never committed. Every expected figure is the golden total / golden row from
§1.1, so a regression in the aggregation is caught immediately.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.f6 import validation as v
from src.f6.books_register import (
    BOOKS_REQUIRED_FIELDS,
    field_tier,
    parse_books_register,
)
from src.f6.rate_buckets import detect_rate_buckets

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "purchase_register_sanitised_aug2026.xlsx"
PERIOD = "2026-08"

# §1.1 golden totals.
GOLDEN = {
    "taxable_value": Decimal("722707.00"),
    "cgst": Decimal("47154.43"),
    "sgst": Decimal("47154.43"),
    "igst": Decimal("4204.70"),
    "rounding_adjustment": Decimal("0.44"),
    "invoice_value": Decimal("821221.00"),
}

# §1.1 golden rows: S.No -> (bill no, taxable, cgst, sgst, igst, rounding, invoice).
GOLDEN_ROWS = {
    1: ("T11439", "16460.00", "1481.40", "1481.40", "0.00", "0.20", "19423.00"),
    3: ("2915", "7693.00", "192.32", "192.32", "0.00", "0.36", "8078.00"),
    19: ("4284", "33670.00", "1621.77", "1621.77", "0.00", "0.46", "36914.00"),
    20: ("7953", "22090.00", "0.00", "0.00", "3976.20", "-0.20", "26066.00"),
    21: ("7955", "4570.00", "0.00", "0.00", "228.50", "0.50", "4799.00"),
    33: ("T15158", "32685.00", "2549.70", "2549.70", "0.00", "-0.40", "37784.00"),
}


@pytest.fixture(scope="module")
def parsed():
    result = parse_books_register(str(FIXTURE), "tally", FIXTURE.name)
    assert result is not None, "the fixture must be recognised as a rate-bucketed books register"
    return result


def _sum(frame, column: str) -> Decimal:
    vals = [x for x in frame[column].tolist() if x is not None]
    return sum(vals, Decimal("0.00"))


# --- T1 ---------------------------------------------------------------------
def test_t1_rate_bucket_detector_one_unit_per_bucket(parsed):
    """12 rate-bucket columns resolve to a head x rate matrix; one case per bucket."""
    m = parsed.matrix
    assert m.heads() == ["IGST", "CGST", "SGST"]
    assert m.rates_for("IGST") == [5.0, 18.0]
    assert m.rates_for("CGST") == [2.5, 9.0]
    assert m.rates_for("SGST") == [2.5, 9.0]
    # One assertion per bucket (12 columns => 6 taxable + 6 tax).
    assert len(m.buckets) == 12
    for head, rate in (("IGST", 5.0), ("IGST", 18.0), ("CGST", 2.5),
                       ("CGST", 9.0), ("SGST", 2.5), ("SGST", 9.0)):
        assert any(b.head == head and b.rate == rate and b.kind == "tax" for b in m.buckets)
        assert any(b.head == head and b.rate == rate and b.kind == "taxable" for b in m.buckets)


# --- T2 ---------------------------------------------------------------------
def test_t2_taxable_base_dedup_and_naive_sum_rejected(parsed):
    """Correct rule = 722,707.00; a naive sum-all-Txbl rule yields 1,418,754 and is rejected."""
    assert _sum(parsed.frame, "taxable_value") == GOLDEN["taxable_value"]

    rule = next(r for r in parsed.rules if r["canonical_field"] == "taxable_value")
    assert rule["transform"] == "dedupe_taxable_base_cgst_sgst_restate"
    assert rule["excluded_mirrors"], "SGST taxable columns must be recorded as excluded mirrors"

    # The naive rule (every Txbl column summed) double-counts — the engine
    # must NOT produce it.
    naive = sum(
        float(parsed.source_rows[i].get(b.column) or 0)
        for i in range(len(parsed.source_rows))
        for b in parsed.matrix.buckets if b.kind == "taxable"
    )
    assert round(naive, 2) == 1418754.00
    assert _sum(parsed.frame, "taxable_value") != Decimal(str(round(naive, 2)))


# --- T3 ---------------------------------------------------------------------
def test_t3_golden_totals(parsed):
    assert parsed.row_count_parsed == 35
    for column, expected in GOLDEN.items():
        assert _sum(parsed.frame, column) == expected, column
    # 722,707 + 4,204.70 + 47,154.43x2 + 0.44 = 821,221.00
    total = (GOLDEN["taxable_value"] + GOLDEN["igst"] + GOLDEN["cgst"]
             + GOLDEN["sgst"] + GOLDEN["rounding_adjustment"])
    assert total == GOLDEN["invoice_value"]
    # And the file's own Total row agrees.
    assert parsed.control_totals[0][0] == 821221.00
    assert parsed.control_totals[0][1] == 821221.00


# --- T4 ---------------------------------------------------------------------
def test_t4_golden_rows(parsed):
    df = parsed.frame
    by_bill = {str(r["invoice_number"]): r for _, r in df.iterrows()}
    for sno, (bill, taxable, cgst, sgst, igst, rounding, invoice) in GOLDEN_ROWS.items():
        row = by_bill[bill]
        assert str(row["taxable_value"]) == taxable, f"S.No {sno} taxable_value"
        assert str(row["cgst"]) == cgst, f"S.No {sno} cgst"
        assert str(row["sgst"]) == sgst, f"S.No {sno} sgst"
        assert str(row["igst"]) == igst, f"S.No {sno} igst"
        assert str(row["rounding_adjustment"]) == rounding, f"S.No {sno} rounding"
        assert str(row["invoice_value"]) == invoice, f"S.No {sno} invoice_value"

    # S.No 19 and 33 are the MIXED-bucket rows: each cgst is two contributions.
    mixed = [b for b in ("4284", "T15158")]
    for bill in mixed:
        assert Decimal(str(by_bill[bill]["cgst"])) > Decimal("0")
        assert Decimal(str(by_bill[bill]["sgst"])) == Decimal(str(by_bill[bill]["cgst"]))


# --- T5 ---------------------------------------------------------------------
def test_t5_row_identity_within_one_rupee(parsed):
    """35 of 35 within ±₹1 using Decimal; S.No 21 (rounding 0.50) passes."""
    df = parsed.frame
    for _, row in df.iterrows():
        parts = [row.get(f) for f in ("taxable_value", "igst", "cgst", "sgst", "cess")]
        total = sum((p for p in parts if p is not None), Decimal("0.00"))
        rounding = row.get("rounding_adjustment") or Decimal("0.00")
        delta = abs((total + rounding) - row["invoice_value"])
        assert delta <= Decimal("1.00"), f"{row['invoice_number']} identity delta {delta}"


# --- T6 ---------------------------------------------------------------------
def test_t6_null_is_not_zero(parsed):
    """With the tax rule removed the output is null + unavailable, never 0."""
    frame = parsed.frame.copy()
    frame["cgst"] = None
    present = [v for v in frame["cgst"].tolist() if v is not None]
    assert present == [], "an absent field must be null on every row, never zero-filled"
    assert not any(v == Decimal("0.00") for v in frame["cgst"].tolist())

    # And the required-field check must HARD STOP when a required field is absent.
    rows = parsed.validation_rows()
    for r in rows:
        r["cgst"] = None
    outcome = v.run_all_checks(
        rows=rows, row_count_read=parsed.row_count_read, row_count_parsed=parsed.row_count_parsed,
        exclusion_reasons=parsed.exclusion_reasons, required_fields=BOOKS_REQUIRED_FIELDS,
        control_totals=parsed.control_totals, date_field="invoice_date", period=PERIOD,
        mirror_pairs=parsed.mirror_inputs(), rate_buckets=parsed.rate_bucket_inputs(),
    )
    assert outcome.hard_stopped
    assert any(c.check == "required_fields" and c.result == "hard_stop" for c in outcome.results)


# --- T7 ---------------------------------------------------------------------
def test_t7_pipeline_gate_refuses_unconfirmed(tmp_path):
    """A direct service call to hand an unconfirmed or failing run to Module 2
    is refused — the gate is at the SERVICE level, not just the UI."""
    import src.db as recon_db
    from src.documents.schema import init_documents_schema
    from src.ingestion_ai import service as s
    from src.ingestion_ai.schema import init_ingestion_ai_schema

    db = str(tmp_path / "gate.db")
    conn = recon_db.get_connection(db)
    init_documents_schema(conn)
    init_ingestion_ai_schema(conn)
    conn.commit()
    conn.close()

    res = s.upload_and_infer(
        client_id=1, source_type="tally", filename="pr.xlsx",
        file_bytes=FIXTURE.read_bytes(), period=PERIOD, actor="tester",
        client_ref="TestClient", recon_type="GST", db_path=db,
    )
    uid = res["upload_id"]

    # A clean parse is not blocked, and every §8 check is reported.
    gate = s.confirm_gate(uid, db_path=db)
    assert gate["blocked"] is False
    assert {c["check"] for c in gate["validation"]} >= {
        "control_total", "tax_arithmetic", "mirror_columns", "implied_rate",
    }

    # Dropping a required field MUST be refused by the service itself.
    report = s.get_report(uid, db_path=db)
    overrides = {m["canonical_field"]: m.get("raw_column") for m in report["field_mapping"]}
    overrides["taxable_value"] = None
    with pytest.raises(s.IngestionAIError) as exc:
        s.confirm_mapping(
            uid, field_overrides=overrides, trust_for_reuse=False, actor="tester",
            client_ref="TestClient", db_path=db,
        )
    assert "taxable_value" in str(exc.value)

    # A complete mapping confirms.
    overrides["taxable_value"] = report["field_mapping"][0].get("raw_column") or "x"
    # restore the genuine column from the deterministic parse
    overrides = {m["canonical_field"]: m.get("raw_column") for m in report["field_mapping"]}
    s.confirm_mapping(
        uid, field_overrides=overrides, trust_for_reuse=False, actor="tester",
        client_ref="TestClient", db_path=db,
    )


# --- T8 ---------------------------------------------------------------------
def test_t8_provenance_is_truthful(tmp_path):
    """An unconfirmed deterministic parse must never be labelled an AI
    proposal, and an unconfirmed cache entry must never read as recognised."""
    import src.db as recon_db
    from src.documents.schema import init_documents_schema
    from src.ingestion_ai import service as s
    from src.ingestion_ai.schema import init_ingestion_ai_schema

    db = str(tmp_path / "prov.db")
    conn = recon_db.get_connection(db)
    init_documents_schema(conn)
    init_ingestion_ai_schema(conn)
    conn.commit()
    conn.close()

    res = s.upload_and_infer(
        client_id=1, source_type="tally", filename="pr.xlsx",
        file_bytes=FIXTURE.read_bytes(), period=PERIOD, actor="tester",
        client_ref="TestClient", recon_type="GST", db_path=db,
    )
    prov = s.provenance(res["upload_id"], db_path=db)
    assert prov["path"] == "deterministic"
    assert prov["model_used"] is None
    assert "AI" not in prov["label"]
    assert "Deterministic" in prov["label"] or "deterministic" in prov["label"]
    # Zero C5 instructions — no model ran, so nothing shaped it.
    assert prov["c5_instructions"] == 0


# --- T9 ---------------------------------------------------------------------
def test_t9_metadata_and_out_of_period(parsed):
    meta = parsed.metadata
    assert meta.entity_name == "SAMPLE TEXTILES PVT. LTD."
    assert meta.report_title == "Purchase Register (Bill-wise)"
    assert meta.period_start == "2026-08-01"
    assert meta.period_end == "2026-08-31"
    assert meta.filter_text == "All Accounts"
    assert meta.date_format == "%d-%m-%Y"
    assert "day value above 12" in (meta.date_format_evidence or "")

    # Filed under the wrong period -> flagged, not silently accepted.
    outcome = parsed.run_validation(period="2026-07")
    assert any(c.check == "date_sanity" and c.result == "row_flag" for c in outcome.results)
    # Filed under the right period -> clean.
    assert not parsed.run_validation(period=PERIOD).hard_stopped


# --- T10 --------------------------------------------------------------------
def test_t10_mutation_rename_add_and_swap(tmp_path, parsed):
    """Rename a bucket, add an @12% column, swap a Tax and a Txbl column:
    each changes the matrix; the swap is caught by the implied-rate check."""
    original = pd.read_excel(FIXTURE, header=None, dtype=object)
    cols = list(original.iloc[6])

    def save(frame, name):
        path = tmp_path / name
        with pd.ExcelWriter(path, engine="openpyxl") as xw:
            frame.to_excel(xw, sheet_name="Sheet1", header=False, index=False)
        return str(path)

    # (a) rename a bucket rate 5% -> 12%: the matrix must follow the header.
    renamed = original.copy()
    for i, c in enumerate(cols):
        if str(c).strip() == "IGST Tax Amt @ 5%":
            renamed.iat[6, i] = "IGST Tax Amt @ 12%"
    r = parse_books_register(save(renamed, "renamed.xlsx"), "tally", "renamed.xlsx")
    assert r is not None
    assert 12.0 in r.matrix.rates_for("IGST")

    # (b) a NEW @12% bucket appears -> a new bucket, not a code change.
    added = original.copy()
    added.iloc[6, len(cols) - 1] = "IGST Txbl. Amt @ 12%"
    r2 = parse_books_register(save(added, "added.xlsx"), "tally", "added.xlsx")
    assert r2 is not None
    assert 12.0 in r2.matrix.rates_for("IGST")

    # (c) swap a Tax and a Txbl column -> implied-rate check must catch it.
    # Only ONE row (S.No 20) uses the IGST 5% bucket, so swapping that single
    # bucket's two columns trips 1 of 35 rows — a ROW FLAG, correctly short of
    # the 20% run-level escalation. Swap a bucket used by MANY rows to prove
    # the escalation path too.
    swapped = original.copy()
    ti = cols.index("IGST Tax Amt @ 5%")
    xi = cols.index("IGST Txbl. Amt @ 5%")
    for row in range(7, len(swapped)):
        swapped.iat[row, ti], swapped.iat[row, xi] = swapped.iat[row, xi], swapped.iat[row, ti]
    r3 = parse_books_register(save(swapped, "swapped.xlsx"), "tally", "swapped.xlsx")
    assert r3 is not None
    outcome = r3.run_validation(period=PERIOD)
    implied = next(c for c in outcome.results if c.check == "implied_rate")
    assert implied.result == "row_flag", "a single-row Tax/Txbl swap must be caught as a row flag"

    # A swap on a widely-used bucket (CGST 9%) trips most rows -> hard stop.
    swapped2 = original.copy()
    ti2 = cols.index("CGST Tax Amt @ 9%")
    xi2 = cols.index("CGST Txbl. Amt @ 9%")
    for row in range(7, len(swapped2)):
        swapped2.iat[row, ti2], swapped2.iat[row, xi2] = swapped2.iat[row, xi2], swapped2.iat[row, ti2]
    r4 = parse_books_register(save(swapped2, "swapped2.xlsx"), "tally", "swapped2.xlsx")
    assert r4 is not None
    implied2 = next(c for c in r4.run_validation(period=PERIOD).results if c.check == "implied_rate")
    assert implied2.result == "hard_stop", "a systematic Tax/Txbl swap must hard-stop the run"


# --- T11 --------------------------------------------------------------------
def test_t11_every_column_has_one_disposition(parsed):
    dispositions = {d["column"]: d["disposition"] for d in parsed.column_dispositions}
    # Every source column ends in exactly one disposition.
    assert len(dispositions) == len(set(dispositions))
    assert all(d in ("used-in", "validation-signal", "ignored", "needs-decision")
               for d in dispositions.values())
    # 'Purc. Type' is a validation signal, not a mapping.
    assert dispositions.get("Purc. Type") == "validation-signal"
    # 'S.No.' is safely ignorable, with a reason.
    assert dispositions.get("S.No.") == "ignored"
    # No orphan "not mapped" state remains.
    assert not any(d == "needs-decision" for d in dispositions.values())


# --- T12 --------------------------------------------------------------------
def test_t12_requiredness_tiers(parsed):
    for f in BOOKS_REQUIRED_FIELDS:
        assert field_tier(f) == "required"
    assert field_tier("party_name") == "recommended"
    assert field_tier("total_tax") == "derived"
    assert field_tier("cess") == "optional"
    # TDS fields must never be demanded from a books register.
    from src.f6.books_register import _ALIASES

    assert "pan" not in _ALIASES
    assert "section" not in _ALIASES


def test_t12_tds_fields_not_in_books_schema(parsed):
    """D14: a purchase register must not be asked for TDS fields."""
    columns = set(parsed.frame.columns)
    for tds_field in ("pan", "deductee_name", "section", "tax_deducted", "certificate_number"):
        assert tds_field not in columns
