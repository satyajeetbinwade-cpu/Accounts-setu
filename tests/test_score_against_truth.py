"""Tests for scripts/score_against_truth.py — synthetic key + synthetic database.

The scorer is read-only, so these tests build a throwaway sqlite file with the
same table names the real database uses, and a tiny answer key, then assert on
the returned structures (and on the CLI writing its report).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "score_against_truth.py"
_spec = importlib.util.spec_from_file_location("score_against_truth", _SCRIPT)
scorer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scorer)


SCHEMA = """
CREATE TABLE invoice_uploads (upload_id INTEGER PRIMARY KEY, filename TEXT);
CREATE TABLE invoice_triage_runs (run_id INTEGER PRIMARY KEY, upload_id INTEGER, status TEXT);
CREATE TABLE invoice_documents (
    document_id INTEGER PRIMARY KEY, upload_id INTEGER, run_id INTEGER, ordinal INTEGER,
    category TEXT, category_source TEXT DEFAULT 'triage', label TEXT,
    excluded INTEGER DEFAULT 0, excluded_reason TEXT, flags_json TEXT,
    is_active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE document_regions (
    region_id INTEGER PRIMARY KEY, document_id INTEGER, page INTEGER,
    x REAL, y REAL, w REAL, h REAL, order_in_doc INTEGER, source TEXT, is_active INTEGER DEFAULT 1);
CREATE TABLE invoice_doc_extractions (
    extraction_id INTEGER PRIMARY KEY, document_id INTEGER, status TEXT,
    skipped_reason TEXT, is_active INTEGER DEFAULT 1);
CREATE TABLE invoice_doc_fields (
    field_id INTEGER PRIMARY KEY, extraction_id INTEGER, document_id INTEGER, field_key TEXT,
    status TEXT, value_a TEXT, value_b TEXT, reads_agree TEXT, is_handwritten INTEGER DEFAULT 0);
CREATE TABLE invoice_doc_annotations (
    annotation_id INTEGER PRIMARY KEY, extraction_id INTEGER, document_id INTEGER, page INTEGER,
    text_verbatim TEXT, kind_guess TEXT, used_as_value INTEGER DEFAULT 0);
CREATE TABLE invoice_doc_checks (
    check_id INTEGER PRIMARY KEY, extraction_id INTEGER, document_id INTEGER,
    check_key TEXT, status TEXT, detail TEXT);
CREATE TABLE invoice_triage_pages (
    run_id INTEGER, page INTEGER, status TEXT, duplicate_of_page INTEGER, PRIMARY KEY (run_id, page));
CREATE TABLE document_region_log (
    id INTEGER PRIMARY KEY, document_id INTEGER, region_id INTEGER, action TEXT,
    before_json TEXT, after_json TEXT, actor TEXT, at TEXT);
CREATE TABLE invoice_doc_derived (
    derived_id INTEGER PRIMARY KEY, extraction_id INTEGER, document_id INTEGER,
    key TEXT, value TEXT, formula TEXT);
"""


def make_db(path: Path, documents) -> str:
    """``documents``: list of dicts with pages/category/label/excluded/fields/status."""
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        con.execute("INSERT INTO invoice_uploads (upload_id, filename) VALUES (5, 'synthetic.pdf')")
        con.execute("INSERT INTO invoice_triage_runs (run_id, upload_id, status) VALUES (1, 5, 'complete')")
        for index, doc in enumerate(documents, start=1):
            did = index
            con.execute(
                "INSERT INTO invoice_documents (document_id, upload_id, run_id, ordinal, category, label, "
                "excluded, excluded_reason, flags_json, is_active) VALUES (?,5,1,?,?,?,?,?,'[]',1)",
                (did, index, doc["category"], doc.get("label", "doc %d" % did),
                 1 if doc.get("excluded") else 0, doc.get("excluded_reason")),
            )
            for order, page in enumerate(doc.get("pages", [])):
                con.execute(
                    "INSERT INTO document_regions (document_id, page, x, y, w, h, order_in_doc, source, is_active) "
                    "VALUES (?,?,0,0,1,1,?,'triage',1)", (did, page, order))
            status = doc.get("status")
            if status:
                con.execute(
                    "INSERT INTO invoice_doc_extractions (extraction_id, document_id, status, skipped_reason, is_active) "
                    "VALUES (?,?,?,?,1)", (did, did, status, doc.get("skipped_reason")))
                for key, value in (doc.get("fields") or {}).items():
                    con.execute(
                        "INSERT INTO invoice_doc_fields (extraction_id, document_id, field_key, status, "
                        "value_a, value_b, reads_agree, is_handwritten) VALUES (?,?,?,?,?,?,'exact',0)",
                        (did, did, key, "read" if value else "not_printed", value, value))
            for page, duplicate_of in (doc.get("duplicates") or {}).items():
                con.execute("INSERT INTO invoice_triage_pages (run_id, page, status, duplicate_of_page) "
                            "VALUES (1,?, 'ok', ?)", (int(page), int(duplicate_of)))
        con.commit()
    finally:
        con.close()
    return str(path)


def truth_doc(doc_id, pages, expected, **kw):
    entry = {"id": doc_id, "pages": pages, "expected_category": expected, "extract": kw.pop("extract", True),
             "doc_type": kw.pop("doc_type", doc_id), "fields": kw.pop("fields", {})}
    entry.update(kw)
    return entry


def truth_file(path: Path, documents) -> str:
    payload = {"meta": {"name": "synthetic", "pdf": "synthetic.pdf", "pages_total": 9, "how_to_score": []},
               "documents": documents}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.fixture()
def tiny(tmp_path):
    """Three Setu documents: a right match, a wrong category, an extra page 9."""
    db = make_db(tmp_path / "poc.db", [
        {"pages": [1], "category": "tax_invoice", "label": "KMAP/2026-27/001", "status": "complete",
         "fields": {"invoice_number": "KMAP/2026-27/001", "invoice_date": "18 Apr 2026",
                    "invoice_total": "₹ 1,180.00"}},
        {"pages": [2], "category": "supporting", "label": "Something else", "status": "skipped",
         "skipped_reason": "category 'supporting' is not extracted"},
        {"pages": [9], "category": "supporting", "label": "Banner", "status": "skipped"},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T01", [1], "tax_invoice", doc_type="KMAP/2026-27/001 invoice",
                  fields={"invoice_number": {"value": "KMAP/2026-27/001", "source": "printed"},
                          "invoice_date": {"value": "2026-04-18", "source": "printed"},
                          "invoice_total": {"value": 1180.00, "source": "printed"}}),
        truth_doc("T02", [2], "tax_invoice", doc_type="a tax invoice",
                  fields={"invoice_total": {"value": 500.00, "source": "printed"}}),
        truth_doc("T03", [3], "supporting", extract=False, doc_type="a receipt"),
    ])
    return {"db": db, "truth": truth}


def load_all(paths):
    con = scorer.open_read_only(paths["db"])
    try:
        schema = scorer.discover_schema(con)
        schema["columns"] = {name: scorer._columns(con, name) for name in schema.values()}
        setu = scorer.load_setu(con, schema, 5)
    finally:
        con.close()
    truth = scorer.load_truth(paths["truth"])
    triage = scorer.score_triage(truth, setu)
    return setu, truth, triage


def test_right_match_scores_pages_category_and_normalised_values(tiny):
    setu, truth, triage = load_all(tiny)
    row = triage["rows"][0]
    assert row["matched"] == 1 and row["pages_ok"] is True
    assert row["category_ok"] is True
    # T01 and T02 both sit on the right page; only T01 has the right category.
    assert triage["totals"]["pages_right"] == 2
    assert triage["totals"]["category_right"] == 1

    extraction = scorer.score_extraction(truth, setu, triage)
    report = extraction["documents"][0]
    by_field = {f["field"]: f for f in report["fields"]}
    # amount normalisation: '₹ 1,180.00' == 1180.00
    assert by_field["invoice_total"]["status"] == "right"
    # date normalisation: '18 Apr 2026' == 2026-04-18
    assert by_field["invoice_date"]["status"] == "right"
    assert extraction["totals"]["wrong"] == 0
    # T01 itself is clean; the only hard failure is T02, whose category is wrong.
    assert len(extraction["hard_failures"]) == 1 and "T02" in extraction["hard_failures"][0]


def test_wrong_category_is_reported_and_a_lost_extraction_is_a_hard_failure(tiny):
    setu, truth, triage = load_all(tiny)
    row = triage["rows"][1]
    assert row["matched"] == 2
    assert row["pages_ok"] is True
    assert row["category_ok"] is False
    assert triage["totals"]["category_right"] == 1
    assert triage["totals"]["category_checked"] == 2

    extraction = scorer.score_extraction(truth, setu, triage)
    hard = " ".join(extraction["hard_failures"])
    assert "T02" in hard and "not extracted" in hard


def test_missing_truth_document_and_extra_setu_document(tiny):
    setu, truth, triage = load_all(tiny)
    missing = [r for r in triage["rows"] if r["matched"] is None]
    assert [r["id"] for r in missing] == ["T03"]
    assert triage["totals"]["missing"] == 1
    assert [d.pages for d in triage["extras"]] == [{9}]
    assert triage["totals"]["extra_documents"] == 1


def test_handwritten_figure_used_as_a_value_is_a_hard_failure(tmp_path):
    db = make_db(tmp_path / "poc.db", [
        {"pages": [1], "category": "no_gst_expense", "label": "Jelux Solutions Invoice 01", "status": "complete",
         "fields": {"invoice_total": "8820", "line.1.amount": "9000"}},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T16", [1], "no_gst_expense", doc_type="Jelux Solutions Invoice",
                  fields={"invoice_total": {"value": 9000.00, "source": "printed"}},
                  handwritten_must_not_be_values=[180, 8820]),
    ])
    setu, truth, triage = load_all({"db": db, "truth": truth})
    extraction = scorer.score_extraction(truth, setu, triage)
    assert any("8820" in item for item in extraction["hard_failures"])
    assert any("handwritten figure" in item for item in extraction["hard_failures"])


def test_empty_counts_as_correct_for_a_handwritten_only_value(tmp_path):
    db = make_db(tmp_path / "poc.db", [
        {"pages": [1], "category": "no_gst_expense", "label": "Ajay General Store slip", "status": "complete",
         "fields": {"issuer_name": "AJAY GENERAL STORE", "invoice_total": None}},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T02", [1], "no_gst_expense", doc_type="Ajay General Store milk slip",
                  fields={"issuer_name": {"value": "Ajay General Store", "source": "printed"},
                          "invoice_total": {"value": 570.00, "source": "handwritten"}}),
    ])
    setu, truth, triage = load_all({"db": db, "truth": truth})
    extraction = scorer.score_extraction(truth, setu, triage)
    by_field = {f["field"]: f for f in extraction["documents"][0]["fields"]}
    assert by_field["invoice_total"]["status"] == "right"
    assert "empty counts as correct" in by_field["invoice_total"]["detail"]
    assert extraction["totals"]["empty"] == 0
    assert extraction["handwriting_warnings"] == []


def test_numeric_and_date_normalisation_helpers():
    assert scorer.norm_number("₹ 1,180.00") == 1180.0
    assert scorer.norm_number("(-)0.46") == -0.46
    assert scorer.norm_number(1098.0) == 1098.0
    assert scorer.norm_date("18 Apr 2026") == "2026-04-18"
    assert scorer.norm_date("18/04/26") == "2026-04-18"
    assert scorer.norm_date("2026-04-18") == "2026-04-18"
    assert scorer.norm_date("not a date") is None
    assert scorer.norm_text("Ajay  General-Store!") == "ajaygeneralstore"
    assert scorer.values_equal("invoice_total", 1180.00, "₹ 1,180.00")
    assert scorer.values_equal("invoice_date", "2026-04-18", "18 Apr 2026")
    assert scorer.values_equal("issuer_gstin", "07AAECK9054D1ZK", "07 aaeck9054d1zk")
    assert not scorer.values_equal("issuer_gstin", "07AAECK9054D1ZK", "07AAECK9054D1ZL")
    assert not scorer.values_equal("invoice_total", 1180.00, 1180.01)


def test_arithmetic_check_flags_an_operand_setu_never_read(tmp_path):
    db = make_db(tmp_path / "poc.db", [
        {"pages": [1], "category": "tax_invoice", "label": "KMAP/2026-27/003", "status": "complete",
         "fields": {"taxable_value": "1000.00", "igst_amount": "180.00", "invoice_total": "1180.00"}},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T04", [1], "tax_invoice", doc_type="KMAP tax invoice",
                  fields={"taxable_value": {"value": 1000.00, "source": "printed"},
                          "igst_amount": {"value": 180.00, "source": "printed"},
                          "invoice_total": {"value": 1180.00, "source": "printed"}},
                  checks=["1000 x 18% = 180", "1000 + 180 = 1180", "inter-state 07 to 09, so IGST"]),
    ])
    setu, truth, triage = load_all({"db": db, "truth": truth})
    extraction = scorer.score_extraction(truth, setu, triage)
    checks = {c["text"]: c["status"] for c in extraction["documents"][0]["checks"]}
    assert checks["1000 x 18% = 180"] == "pass"
    assert checks["1000 + 180 = 1180"] == "pass"
    assert checks["inter-state 07 to 09, so IGST"] == "pass"


def test_exclusion_beside_the_right_document_is_not_a_loss(tmp_path):
    """Two documents on page 1: excluding one must not read as losing the other."""
    db = make_db(tmp_path / "poc.db", [
        {"pages": [1], "category": "not_a_voucher", "label": "DTDC Z58078291",
         "excluded": True, "excluded_reason": "not_a_voucher", "status": "skipped",
         "skipped_reason": "the document is excluded (not_a_voucher)"},
        {"pages": [1], "category": "no_gst_expense", "label": "Ajay General Store slip", "status": "complete",
         "fields": {"issuer_name": "AJAY GENERAL STORE"}},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T01", [1], "no_gst_expense", extract="ca_to_decide",
                  acceptable_categories=["no_gst_expense", "supporting", "not_a_voucher"],
                  doc_type="DTDC courier consignment note"),
        truth_doc("T02", [1], "no_gst_expense", doc_type="Ajay General Store milk slip",
                  fields={"issuer_name": {"value": "Ajay General Store", "source": "printed"}}),
    ])
    setu, truth, triage = load_all({"db": db, "truth": truth})
    assert triage["totals"]["wrongly_excluded"] == 0
    # T01 is the CA's call: Setu's choice is reported, never marked wrong.
    t01 = triage["rows"][0]
    assert t01["category_ok"] is None and t01["category_ok"] is not False
    assert "CA to decide" in " ".join(t01["notes"])


def test_a_page_in_an_excluded_document_is_the_keys_acceptable_alternative(tmp_path):
    """T14: pages 17-19, with page 19 excluded as its own banner document."""
    db = make_db(tmp_path / "poc.db", [
        {"pages": [17, 18], "category": "ticket", "label": "Air India Ticket", "status": "skipped"},
        {"pages": [19], "category": "not_a_voucher", "label": "Star Alliance banner",
         "excluded": True, "excluded_reason": "not_a_voucher", "status": "skipped"},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T14", [17, 18, 19], "ticket", extract=False, doc_type="Air India e-ticket itinerary",
                  pages_not_separate=[18, 19],
                  acceptable_alternative="page 19 may be a separate EXCLUDED not_a_voucher"),
    ])
    setu, truth, triage = load_all({"db": db, "truth": truth})
    row = triage["rows"][0]
    assert row["pages_ok"] is False
    assert row["pages_ok_acceptable"] is True
    assert triage["totals"]["pages_right"] == 0
    assert triage["totals"]["pages_right_or_acceptable"] == 1
    assert triage["totals"]["wrongly_excluded"] == 0


def test_a_missing_page_that_became_its_own_document_is_not_accepted(tmp_path):
    """The baseline's T20: pages 28 and 29 became two ordinary documents."""
    db = make_db(tmp_path / "poc.db", [
        {"pages": [27], "category": "supporting", "label": "Credit Limit Report", "status": "skipped"},
        {"pages": [28], "category": "supporting", "label": "Payable Card Statement Due", "status": "skipped"},
        {"pages": [29], "category": "supporting", "label": "Payable Card Statement", "status": "skipped",
         "duplicates": {29: 28}},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T20", [27, 28, 29], "supporting", extract=False, doc_type="HDFC card report",
                  pages_not_separate=[28, 29]),
    ])
    setu, truth, triage = load_all({"db": db, "truth": truth})
    row = triage["rows"][0]
    assert row["pages_ok"] is False
    assert row["pages_ok_acceptable"] is False
    assert triage["totals"]["pages_right_or_acceptable"] == 0

    p29 = next(c for c in scorer.specific_checks(truth, setu, triage) if c["text"].startswith("p29 copy"))
    assert p29["ok"] is False
    assert "NOT excluded" in p29["text"]
    assert "duplicate_of_page=28" in p29["text"]


def test_page_29_excluded_as_a_copy_of_28_passes_the_named_check(tmp_path):
    db = make_db(tmp_path / "poc.db", [
        {"pages": [27, 28], "category": "supporting", "label": "HDFC card report", "status": "skipped"},
        {"pages": [29], "category": "supporting", "label": "HDFC card report", "excluded": True,
         "excluded_reason": "duplicate of page 28 — the page hash matches that earlier page",
         "status": "skipped", "duplicates": {29: 28}},
    ])
    truth = truth_file(tmp_path / "truth.json", [
        truth_doc("T20", [27, 28, 29], "supporting", extract=False, doc_type="HDFC card report"),
    ])
    setu, truth, triage = load_all({"db": db, "truth": truth})
    p29 = next(c for c in scorer.specific_checks(truth, setu, triage) if c["text"].startswith("p29 copy"))
    assert p29["ok"] is True
    assert "EXCLUDED as a copy" in p29["text"]


def test_cli_writes_the_full_report_and_prints_at_most_25_lines(tiny, tmp_path, capsys):
    out = tmp_path / "score.txt"
    summary, out_path = scorer.run(tiny["db"], 5, tiny["truth"], str(out))
    assert out_path == str(out)
    assert len(summary) <= 25
    text = out.read_text(encoding="utf-8")
    assert "TRIAGE PER TRUTH DOCUMENT" in text
    assert "EXCLUDED SETU DOCUMENTS" in text
    assert "NAMED CHECKS" in text
    assert "OPEN CA QUESTIONS" in text
    assert "PAYMENT LINKS" in text
    assert "HARD FAILURES" in text
    assert "T01 pp[1]" in text


def test_database_is_opened_read_only(tiny):
    con = scorer.open_read_only(tiny["db"])
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE nope (x INTEGER)")
    finally:
        con.close()


def test_page_one_split_is_scored_by_the_split_and_tolerates_a_typo(tmp_path):
    """Three documents on page 1 is the finding; a one-letter label typo is not."""
    db = make_db(tmp_path / "poc.db", [
        {"pages": [1], "category": "no_gst_expense", "label": "DDTC Express Consignment Slip"},
        {"pages": [1], "category": "no_gst_expense", "label": "Ajay General Store Slip"},
        {"pages": [1], "category": "no_gst_expense", "label": "Cash Memo AGGARWAL TRADING CO"},
    ])
    truth_path = truth_file(tmp_path / "truth.json", [
        truth_doc("T01", [1], "no_gst_expense", extract="ca_to_decide", doc_type="DTDC courier note"),
        truth_doc("T02", [1], "no_gst_expense", extract=False, doc_type="Ajay General Store slip"),
        truth_doc("T03", [1], "no_gst_expense", extract=False, doc_type="Aggarwal Trading cash memo"),
    ])
    setu, key, triage = load_all({"db": db, "truth": truth_path})
    p1 = next(c for c in scorer.specific_checks(key, setu, triage) if c["text"].startswith("p1 documents"))
    assert p1["ok"] is True
    assert "near-miss DTDC" in p1["text"]
    assert "Ajay" in p1["text"] and "Aggarwal" in p1["text"]

    one = make_db(tmp_path / "one.db", [
        {"pages": [1], "category": "no_gst_expense", "label": "Ajay General Store Slip"},
    ])
    setu2, key2, triage2 = load_all({"db": one, "truth": truth_path})
    p1b = next(c for c in scorer.specific_checks(key2, setu2, triage2) if c["text"].startswith("p1 documents"))
    assert p1b["ok"] is False
    assert "1 on page 1" in p1b["text"]
