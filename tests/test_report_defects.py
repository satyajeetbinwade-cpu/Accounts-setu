"""Regression tests for the Run 11 report defect fixes (25 Sep 2026 build).

Four defects were found by inspecting Run 11's three exports directly:

  §1  the Classification donut weighted its slices by each item's *difference*
      — a sub-rupee residual for a matched invoice, a gross value for a
      Not-in-Books one — so a clean 35/39 run rendered almost entirely red.
  §2  the report header showed a SUPPLIER's GSTIN (the most frequent
      counterparty), not the client's own.
  §3  the Excel export shipped formulas with NO cached values.
  §4  the "Gross invoice value on exceptions" cell summed column J (tax).
  §5  the IMS data-quality note repeated the credit-note note verbatim.

Run:

    venv/bin/python -m pytest tests/test_report_defects.py -v
"""

from __future__ import annotations

import re
import shutil

import pytest

from src.reconciliation import report_export as ex
from src.reconciliation import review

# --- synthetic model helpers -----------------------------------------------


def _record(gstin: str, invoice: str, value: float, tax: float, party: str) -> dict:
    return {
        "gstin": gstin,
        "invoice_number": invoice,
        "invoice_date": "2026-08-10",
        "party_name": party,
        "invoice_value": value,
        "taxable_value": round(value - tax, 2),
        "total_tax": tax,
        "igst": 0.0,
        "cgst": round(tax / 2, 2),
        "sgst": round(tax / 2, 2),
        "cess": 0.0,
    }


def _model(*, client_gstin: str = "", supplier_gstins: tuple[str, ...] = ("07AAXFS9006M1ZC",)) -> tuple[dict, dict]:
    """A small deterministic model: 3 matched (tiny residual) + 1 Not in Books."""
    supplier = supplier_gstins[0]
    items = []
    for i in range(3):
        portal = _record(supplier, f"M{i}", 1000.0 + i, 180.0, "Supplier A")
        books = _record(supplier, f"M{i}", 1000.02 + i, 180.0, "Supplier A")
        items.append({
            "bucket": "Matched",
            "classification": "Matched",
            "difference_type": "",
            "reviewed": True,
            "confidence_label": "High",
            "match_reason": "all agree",
            "party": "Supplier A",
            "gstin": supplier,
            "reference": f"M{i}",
            "date_label": "10 Aug 2026",
            "difference": 0.02,
            "gross_value": 0.0,
            "tax": 180.0,
            "books_value": 1000.02 + i,
            "portal_value": 1000.0 + i,
            "cause": "value_difference",
            "rounding_adjustment": 0.0,
            "books": books,
            "portal": portal,
        })
    exc = _record(supplier, "X1", 12757.26, 1946.02, "Supplier B")
    items.append({
        "bucket": "Not in Books",
        "classification": "Not in Books",
        "difference_type": "",
        "reviewed": False,
        "confidence_label": "Low",
        "match_reason": "not present in books",
        "party": "Supplier B",
        "gstin": supplier,
        "reference": "X1",
        "date_label": "12 Aug 2026",
        "difference": 12757.26,
        "gross_value": 12757.26,
        "tax": 1946.02,
        "books_value": 0.0,
        "portal_value": 12757.26,
        "cause": "missing_in_books",
        "rounding_adjustment": 0.0,
        "books": {},
        "portal": exc,
    })
    total_tax = round(sum(i["tax"] for i in items), 2)
    exc_tax = round(sum(i["tax"] for i in items if i["bucket"] == "Not in Books"), 2)
    slices = []
    for name in ("Matched", "Amount Difference", "Not in Books", "Not in Portal"):
        bucket_items = [i for i in items if i["bucket"] == name]
        slices.append({
            "key": name,
            "name": name,
            "count": len(bucket_items),
            "value": round(sum(i["tax"] for i in bucket_items), 2),
            "color_role": "rule" if name == "Matched" else "danger",
        })
    model = {
        "recon_type": "GST",
        "client_gstin": client_gstin,
        "items": items,
        "classification_slices": slices,
        "itc": None,
        "suppliers": [{"party": "Supplier B", "value": 12757.26, "count": 1}],
        "kpi": {
            "itc_at_stake_tax": exc_tax,
            "itc_at_stake_display": review.format_money(exc_tax),
            "itc_at_stake_pct": round(exc_tax / total_tax * 100, 2),
            "itc_at_stake_pct_display": f"{round(exc_tax / total_tax * 100, 2):.2f}%",
            "period_itc_total": total_tax,
            "period_itc_total_display": review.format_money(total_tax),
            "total_count": len(items),
            "matched_count": 3,
            "exception_count": 1,
            "reviewed_count": 3,
            "gross_value": 12757.26,
            "gross_value_display": review.format_money(12757.26),
        },
    }
    run = {
        "client": "Test Client",
        "period": "2026-08",
        "recon_type": "GST",
        "source_file_names": ["gstr2b.xlsx", "books.xlsx"],
        "caveats": [],
        "run_notes": [],
    }
    return run, model


def _export_data(monkeypatch, *, client_gstin: str = "", supplier_gstins=("07AAXFS9006M1ZC",)) -> dict:
    run, model = _model(client_gstin=client_gstin, supplier_gstins=supplier_gstins)
    monkeypatch.setattr(ex.run_model, "load_run_model", lambda run_id, db_path=None: (run, model))
    return ex.build_export_data(1)


# --- §1 classification donut weighting -------------------------------------


def _row(classification: str, books: dict, portal: dict) -> dict:
    return {
        "result_id": 1,
        "fingerprint": "fp",
        "classification": classification,
        "difference_type": None,
        "reviewed": 0,
        "confidence_band": "High",
        "confidence_score": 100,
        "match_reason": "x",
        "itc_at_risk": 0.0,
        "gross_value": 0.0,
        "books_record": books,
        "portal_record": portal,
        "reviewer_note": None,
        "overridden_fields": [],
    }


def test_build_review_model_slices_weight_by_tax():
    """The shared review model (Review screen AND exports) weights every
    classification slice by the bucket's total TAX, never by the mixed
    residual/gross `difference` that inverted the Run 11 donut."""
    supplier = "07AAXFS9006M1ZC"
    rows = [
        _row("Matched",
             _record(supplier, f"M{i}", 1000.02 + i, 180.0, "Supplier A"),
             _record(supplier, f"M{i}", 1000.0 + i, 180.0, "Supplier A"))
        for i in range(3)
    ]
    rows.append(_row("Not in Books", {}, _record(supplier, "X1", 12757.26, 1946.02, "Supplier B")))

    model = review.build_review_model({"recon_type": "GST", "period": "2026-08"}, rows)
    by_name = {s["name"]: s for s in model["classification_slices"]}
    assert by_name["Matched"]["count"] == 3
    assert by_name["Matched"]["value"] == pytest.approx(540.0)
    assert by_name["Not in Books"]["count"] == 1
    assert by_name["Not in Books"]["value"] == pytest.approx(1946.02)
    # The old bug summed each item's `difference` (≈0.06 for matched) — that
    # is a residual, not a total, and must not drive the slice.
    assert by_name["Matched"]["value"] != pytest.approx(0.06)


def test_export_data_every_slice_weights_by_tax(monkeypatch):
    data = _export_data(monkeypatch)
    by_name = {s["name"]: s for s in data["classification_slices"]}
    # Two buckets survive (count > 0); every slice value is a tax total.
    assert set(by_name) == {"Matched", "Not in Books"}
    assert by_name["Matched"]["value"] == 540.0
    assert by_name["Not in Books"]["value"] == 1946.02


def test_export_donut_arcs_and_legend_use_count():
    """The classification donut drives BOTH the arc and the legend from the
    count basis, so a 3/4 matched run shows a large majority ring."""
    slices = [{"name": "Matched", "count": 35, "value": 98513.46, "color_role": "rule"},
              {"name": "Not in Books", "count": 4, "value": 1946.02, "color_role": "danger"}]
    svg = ex._donut_svg(slices, center_label="Records", center_value="39",
                        value_key="count", value_fmt=lambda v: f"{int(round(v))}")
    dashes = [float(m) for m in re.findall(r'stroke-dasharray="([0-9.]+) ', svg)]
    circ = 2 * 3.141592653589793 * 70.0
    assert dashes[0] / circ > 0.85, "Matched must occupy the large majority of the ring"
    assert dashes[1] / circ < 0.15
    assert "Matched<strong>35</strong>" in svg
    assert "Not in Books<strong>4</strong>" in svg


# --- §2 header GSTIN --------------------------------------------------------


def test_header_gstin_comes_from_client_record(monkeypatch):
    data = _export_data(monkeypatch, client_gstin="07AAACP9472A1ZJ")
    assert data["gstin"] == "07AAACP9472A1ZJ"
    supplier_gstins = {i["gstin"] for i in data["invoices"] if i["gstin"]}
    assert data["gstin"] not in supplier_gstins


def test_header_gstin_never_falls_back_to_a_supplier(monkeypatch):
    """A client record with no GSTIN leaves the field blank — it must never
    borrow a transaction row's GSTIN."""
    data = _export_data(monkeypatch, client_gstin="")
    assert data["gstin"] == ""


# --- §3 cached values / §4 column K ----------------------------------------


def test_xlsx_gross_formula_uses_column_k_and_drops_the_caveat(monkeypatch, tmp_path):
    monkeypatch.setattr(ex, "_recalculate_workbook", lambda path: None)
    data = _export_data(monkeypatch, client_gstin="07AAACP9472A1ZJ")
    out = ex.write_xlsx(data, tmp_path / "report.xlsx")

    from openpyxl import load_workbook

    ws = load_workbook(out, data_only=False)["Overview"]
    row = next(r for r in range(1, 40)
               if str(ws.cell(row=r, column=1).value or "").startswith("Gross invoice value"))
    formula = ws.cell(row=row, column=2).value
    assert "'GST Invoices'!$K$2:$K$100000" in formula
    assert "'GST Invoices'!$J$2:$J$100000" not in formula
    # The self-caveat note is gone once the formula is correct.
    assert ws.cell(row=row, column=3).value in (None, "")


@pytest.mark.skipif(
    not (shutil.which("soffice") or shutil.which("libreoffice")),
    reason="LibreOffice unavailable — cannot bake cached values",
)
def test_xlsx_ships_cached_values(monkeypatch, tmp_path):
    data = _export_data(monkeypatch, client_gstin="07AAACP9472A1ZJ")
    out = ex.write_xlsx(data, tmp_path / "report_cached.xlsx")

    from openpyxl import load_workbook

    ws = load_workbook(out, data_only=True)["Overview"]
    values = {
        str(ws.cell(row=r, column=1).value): ws.cell(row=r, column=2).value
        for r in range(1, 40)
    }
    gross = next(v for k, v in values.items() if k.startswith("Gross invoice value"))
    assert gross == pytest.approx(12757.26, abs=0.01)
    # The headline is the tax across EVERY exception bucket, not the Not-in-Books
    # bucket alone. On this fixture every exception IS Not in Books, so the value
    # is unchanged — the point is the binding, and the label now says so.
    assert values["ITC at stake (tax on exceptions)"] == pytest.approx(1946.02, abs=0.01)
    assert values["Invoices"] == 4
    assert values["Matched"] == 3
    # A cached-value-only reader must never see a blank KPI.
    kpi_labels = ("ITC at stake", "Period ITC", "Invoices", "Matched", "Exceptions")
    assert all(values.get(k) not in (None, "") for k in values
               if any(k.startswith(p) for p in kpi_labels))


# --- §5 data-quality note text ---------------------------------------------


def test_quality_notes_have_distinct_why_and_how():
    items = [{
        "cause": "missing_in_books",
        "rounding_adjustment": 0.0,
    }]
    run_notes = [
        {"code": "credit_notes_not_reconciled", "title": "12 credit note(s) were not reconciled",
         "detail": "The portal file carries 12 credit note(s)."},
        {"code": "ims_coverage", "title": "IMS action status covers 80 portal document(s)",
         "detail": "IMS statuses on the supplied export: No Action 80."},
    ]
    notes = review.data_quality_notes(
        items, recon_type="GST", source_files=["gstr2b.xlsx"], run_notes=run_notes,
    )
    pairs = [(n.why, n.how) for n in notes]
    assert len(pairs) == len(set(pairs)), "every note must have its own Why/How text"
    ims = next(n for n in notes if n.key == "run_note_ims_coverage")
    cn = next(n for n in notes if n.key == "run_note_credit_notes_not_reconciled")
    assert (ims.why, ims.how) != (cn.why, cn.how)
    assert "IMS status" in ims.why
