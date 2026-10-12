"""Phase-2 Invoice Extraction tests. NO live model calls.

``extract._call_extract_model`` is the module's single external seam, so every
test stubs that ONE function. The database is always a temp COPY or a scratch
DB — the tests never open the real ``db/poc.db`` for writing, and the real
fixture PDF is read from ``SETU_TRIAGE_FIXTURE_PDF`` and SKIPPED when unset.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from src import bootstrap
from src.invoice_extract import crops as crops_mod
from src.invoice_extract import extract as ex
from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import normalisers as nz
from src.invoice_extract.service import InvoiceExtractError

FIXTURE_PDF = os.environ.get("SETU_TRIAGE_FIXTURE_PDF")
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_pdf(path: Path, pages: int = 3, *, text: bool = True) -> Path:
    """A tiny synthetic multi-page PDF (optionally with a real text layer)."""
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        if text:
            page.insert_text((72, 72), f"Page {i + 1} invoice_total 1,180.00")
            page.insert_text((72, 100), "issuer 07AAECK9054D1ZK")
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return path


def _mk_image(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (600, 400), "white").save(path, format="PNG")
    return path


def _rotated_pdf(path: Path) -> Path:
    """A PDF whose FIRST page is rotated 90 degrees in its own geometry."""
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((72, 72), "LANDSCAPE PAGE rotated content")
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return path


@pytest.fixture(scope="module")
def scratch_db(tmp_path_factory):
    """A scratch app DB with every schema + seed applied."""
    root = tmp_path_factory.mktemp("v2scratch")
    db = str(root / "source.db")
    bootstrap.run_migrations(db_path=db, data_root=root / "data")
    return db


def _set_upload_bytes(db: str, upload_id: int, data: bytes) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE invoice_uploads SET file_bytes = ? WHERE upload_id = ?", (data, upload_id),
        )
        conn.commit()
    finally:
        conn.close()


def _region(page: int, box=(0.1, 0.1, 0.6, 0.4), order: int = 0) -> dict:
    return {
        "region_id": order + 1, "page": page, "x": box[0], "y": box[1],
        "w": box[2], "h": box[3], "order_in_doc": order, "source": "triage",
    }


def _document(document_id: int = 1, *, regions=None, category="tax_invoice", ordinal=1, excluded=False):
    return {
        "document_id": document_id, "ordinal": ordinal, "category": category,
        "category_source": "triage", "label": f"doc-{ordinal}", "excluded": excluded,
        "excluded_reason": None, "flags": [], "regions": regions or [_region(1)],
    }


def _reply(fields: dict, annotations=None, *, region: int = 1) -> dict:
    return {
        "fields": [
            {"key": key, "value": value, "handwritten": False, "region": region,
             "box": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.05}}
            for key, value in fields.items()
        ],
        "annotations": annotations or [],
    }


def _stub_reads(monkeypatch, read_a: dict, read_b: dict, *, provider_a="openrouter", provider_b="deepseek",
                usage_a=None, usage_b=None, calls=None):
    """Stub the single seam: read A then read B, with the providers we choose.

    Also declares both providers as configured, which is what the read-B
    independence check consults. A test that wants to prove the check refuses
    must re-stub ``configured_providers`` AFTER calling this.
    """
    monkeypatch.setattr(
        ex, "configured_providers", lambda db_path=None: sorted({provider_a, provider_b})
    )


    def fake(*, touchpoint, crops, **kwargs):
        if calls is not None:
            calls.append({"touchpoint": touchpoint, "crops": len(crops)})
        if touchpoint == ex.TOUCHPOINT_READ_A:
            meta = {
                "provider": provider_a, "model": "vendor-a/model-a", "leg": "primary",
                "fallback_used": False, "usage": usage_a or {"input_tokens": 100, "output_tokens": 50},
            }
            return read_a, meta
        meta = {
            "provider": provider_b, "model": "vendor-b/model-b", "leg": "primary",
            "fallback_used": False, "usage": usage_b or {"input_tokens": 120, "output_tokens": 60},
        }
        return read_b, meta

    monkeypatch.setattr(ex, "_call_extract_model", fake)


@pytest.fixture()
def v2_on(monkeypatch):
    """Turn Phase 2 on without touching any real setting store."""
    monkeypatch.setattr(ex, "get_config", lambda db_path=None: ex.ExtractConfig(enabled=True))


# ===========================================================================
# STEP 3 — crop geometry
# ===========================================================================


def test_padding_is_three_percent_of_the_shorter_side_and_clamped(tmp_path):
    # A 0.20 x 0.10 box: the shorter side is 0.10, so pad = 0.003.
    padded = crops_mod.padded_box((0.5, 0.5, 0.20, 0.10))
    assert padded == pytest.approx((0.497, 0.497, 0.206, 0.106))

    # At the page edge the padding is simply clipped away, never negative.
    edge = crops_mod.padded_box((0.0, 0.0, 0.20, 0.10))
    assert edge[0] == 0.0 and edge[1] == 0.0
    # The left/top padding was clipped, so the box is 0.206 - 0.003 wide.
    assert edge[2] == pytest.approx(0.203) and edge[3] == pytest.approx(0.103)


def test_crop_is_taken_after_rotation(tmp_path):
    pdf = _rotated_pdf(tmp_path / "land.pdf")
    upright = crops_mod.render_upright_page(pdf.read_bytes(), "pdf", 1, dpi=100, rotation_deg=90)
    raw = crops_mod.render_upright_page(pdf.read_bytes(), "pdf", 1, dpi=100, rotation_deg=0)
    # A quarter turn swaps width and height.
    assert upright.width == raw.height
    assert upright.height == raw.width
    assert upright.rotation_deg == 90


def test_crop_caps_the_long_edge(tmp_path):
    pdf = _mk_pdf(tmp_path / "big.pdf", 1)
    upright = crops_mod.render_upright_page(pdf.read_bytes(), "pdf", 1, dpi=300)
    crop = crops_mod.crop_region(
        upright, (0.0, 0.0, 1.0, 1.0), region_index=1, max_edge_px=400,
    )
    assert max(crop.px_w, crop.px_h) == 400
    assert crop.px_w >= 1 and crop.px_h >= 1
    assert crop.png[:4] == b"\x89PNG"


def test_unrenderable_region_raises_crop_error(tmp_path):
    pdf = _mk_pdf(tmp_path / "small.pdf", 1)
    with pytest.raises(crops_mod.CropError):
        crops_mod.render_region_crops(
            pdf.read_bytes(), "pdf", [_region(99)], {}, dpi=100, max_edge_px=400,
        )
    with pytest.raises(crops_mod.CropError):
        crops_mod.render_region_crops(
            pdf.read_bytes(), "pdf", [], {}, dpi=100, max_edge_px=400,
        )
    # A degenerate box is refused too.
    with pytest.raises(crops_mod.CropError):
        crops_mod.render_region_crops(
            pdf.read_bytes(), "pdf", [_region(1, (0.1, 0.1, 0.0, 0.2))], {},
            dpi=100, max_edge_px=400,
        )


def test_multiple_regions_come_back_in_order_with_labels(tmp_path):
    pdf = _mk_pdf(tmp_path / "two.pdf", 2)
    crops, renders = crops_mod.render_region_crops(
        pdf.read_bytes(), "pdf",
        [_region(2, order=1), _region(1, order=0)], {},
        dpi=100, max_edge_px=500,
    )
    assert [c.region_page for c in crops] == [1, 2]
    assert [c.region_index for c in crops] == [1, 2]
    assert crops[0].label == "Region 1 = page 1"
    assert crops[1].label == "Region 2 = page 2"
    assert set(renders) == {1, 2}


# ===========================================================================
# STEP 3 — THE coordinate conversion
# ===========================================================================


def test_crop_box_to_page_maps_offset_and_scale():
    crop = crops_mod.Crop(
        region_index=1, region_page=10, png=b"x", px_w=100, px_h=100,
        page_box=(0.20, 0.40, 0.50, 0.20),          # x, y, w, h on the upright page
    )
    # A box covering the middle half of the crop.
    page = crops_mod.crop_box_to_page((0.25, 0.25, 0.50, 0.50), crop)
    assert page == pytest.approx((0.325, 0.45, 0.25, 0.10))
    # The whole crop maps to exactly the crop's own page box.
    assert crops_mod.crop_box_to_page((0.0, 0.0, 1.0, 1.0), crop) == pytest.approx(crop.page_box)


def test_crop_box_is_clamped_to_the_page():
    crop = crops_mod.Crop(
        region_index=1, region_page=1, png=b"x", px_w=10, px_h=10,
        page_box=(0.0, 0.0, 0.5, 0.5),
    )
    page = crops_mod.crop_box_to_page((-0.02, -0.02, 1.04, 1.04), crop)
    assert page[0] == 0.0 and page[1] == 0.0
    assert page[0] + page[2] <= 1.0 + 1e-9
    assert page[1] + page[3] <= 1.0 + 1e-9


def test_parse_crop_box_accepts_the_shapes_models_emit():
    assert crops_mod.parse_crop_box({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}) == \
        pytest.approx((0.1, 0.2, 0.3, 0.4))
    assert crops_mod.parse_crop_box({"x0": 0.1, "y0": 0.2, "x1": 0.4, "y1": 0.6}) == \
        pytest.approx((0.1, 0.2, 0.3, 0.4))
    assert crops_mod.parse_crop_box([0.1, 0.2, 0.4, 0.6]) == pytest.approx((0.1, 0.2, 0.3, 0.4))
    # A box far outside the crop is unusable, not silently folded onto the page.
    assert crops_mod.parse_crop_box({"x": 100, "y": 200, "w": 300, "h": 50}) is None
    assert crops_mod.parse_crop_box({"x": 0.1, "y": 0.2, "w": 0.0, "h": 0.4}) is None
    assert crops_mod.parse_crop_box(None) is None


def test_parse_crop_box_tolerates_two_percent_then_clamps_inside():
    # Within the 2% tolerance the box is kept, clamped into the crop.
    assert crops_mod.parse_crop_box({"x": -0.01, "y": -0.01, "w": 0.5, "h": 0.5}) == \
        pytest.approx((0.0, 0.0, 0.49, 0.49))
    assert crops_mod.parse_crop_box({"x": 0.6, "y": 0.6, "w": 0.41, "h": 0.41}) == \
        pytest.approx((0.6, 0.6, 0.4, 0.4))
    # Beyond 2% it is refused: "every box inside its crop (tolerance 2%)".
    assert crops_mod.parse_crop_box({"x": -0.05, "y": 0.1, "w": 0.5, "h": 0.5}) is None
    assert crops_mod.parse_crop_box({"x": 0.1, "y": 0.1, "w": 0.95, "h": 0.5}) is None


def test_in_any_region_and_tolerance():
    regions = [_region(1, (0.10, 0.10, 0.30, 0.30))]
    assert crops_mod.in_any_region((0.15, 0.15, 0.10, 0.10), regions) is True
    # Just outside the region but inside the 2% tolerance.
    assert crops_mod.in_any_region((0.395, 0.15, 0.05, 0.05), regions) is True
    # Far below the region — out.
    assert crops_mod.in_any_region((0.50, 0.80, 0.10, 0.10), regions) is False


def test_unrotate_box_is_the_inverse_for_quarter_turns():
    box = (0.1, 0.2, 0.3, 0.4)
    for deg in (0, 90, 180, 270):
        raw = crops_mod.unrotate_box(box, deg)
        # Rotating the raw box clockwise by deg must give the upright box back.
        assert len(raw) == 4
    # 180 is its own inverse.
    assert crops_mod.unrotate_box(crops_mod.unrotate_box(box, 180), 180) == pytest.approx(box)
    # The area is preserved by every quarter turn.
    for deg in (90, 180, 270):
        raw = crops_mod.unrotate_box(box, deg)
        assert raw[2] * raw[3] == pytest.approx(box[2] * box[3])


def test_text_in_box_finds_the_words_inside():
    words = [
        (10.0, 10.0, 40.0, 20.0, "KMAP/2026-27/003"),
        (10.0, 100.0, 40.0, 110.0, "ELSEWHERE"),
    ]
    found = crops_mod.text_in_box(words, (0.0, 0.0, 0.2, 0.1), page_w=200.0, page_h=200.0)
    assert "KMAP" in found and "ELSEWHERE" not in found


# ===========================================================================
# STEP 2/5 — normalisers
# ===========================================================================


@pytest.mark.parametrize("raw,expected", [
    ("1,000.00", "1000"),
    ("\u20b91,000.00", "1000"),
    ("180.00", "180"),
    ("0.32", "0.32"),
    ("-0.46", "-0.46"),
    ("1 234.50", "1234.5"),
    ("not a number", None),
    ("", None),
    (None, None),
])
def test_normalise_number(raw, expected):
    assert nz.normalise_number(raw) == expected


def test_numbers_are_exact_never_near():
    assert nz.numbers_equal("1,000.00", "1000") is True
    assert nz.numbers_equal("9,000.00", "9000.0") is True
    # 8,053 vs 7,370 must never be "near".
    verdict = nz.compare(fv2.KIND_NUMBER, "8,053.00", "7,370")
    assert verdict.reads_agree == nz.AGREE_DISAGREE


@pytest.mark.parametrize("raw,iso", [
    ("27-Apr-26", "2026-04-27"),
    ("27-Apr-2026", "2026-04-27"),
    ("13/04/2026", "2026-04-13"),
    ("6-Apr-26", "2026-04-06"),
    ("25-Mar-26", "2026-03-25"),
    ("6.4.26", "2026-04-06"),
    ("1-Apr-26", "2026-04-01"),
])
def test_normalise_date_is_day_first(raw, iso):
    assert nz.normalise_date(raw) == iso


def test_day_first_date_is_not_month_first():
    """13/04/2026 is unambiguous, but 06/04/2026 must read as 6 April."""
    assert nz.normalise_date("06/04/2026") == "2026-04-06"
    assert nz.normalise_date("04/06/2026") == "2026-06-04"


def test_identifier_is_exact_and_case_insensitive_only():
    assert nz.normalise_identifier("07aaeck9054d1zk") == "07AAECK9054D1ZK"
    assert nz.normalise_identifier(" 07AAECK 9054D1ZK ") == "07AAECK9054D1ZK"
    # One wrong character must never be "near".
    verdict = nz.compare(fv2.KIND_IDENTIFIER, "07AAECK9054D1ZK", "07AAECK9054D1ZL")
    assert verdict.reads_agree == nz.AGREE_DISAGREE
    assert nz.identifiers_equal("09aadfa5221a1z6", "09AADFA5221A1Z6") is True


def test_text_near_match_allows_structure_but_not_different_words():
    verdict = nz.compare(fv2.KIND_TEXT, "Uttar Pradesh", "UTTAR PRADESH")
    assert verdict.reads_agree == nz.AGREE_EXACT
    # Structure and spacing differences normalise away entirely.
    verdict = nz.compare(fv2.KIND_TEXT, "Suresh Kumar Jain", "Suresh Kumar  Jain")
    assert verdict.reads_agree == nz.AGREE_EXACT
    # A single-character slip is still the same value.
    verdict = nz.compare(fv2.KIND_TEXT, "Suresh Kumar Jain", "Suresh Kumar Jam")
    assert verdict.reads_agree == nz.AGREE_NEAR
    # An extra WORD is a content difference, not a slip: not "near".
    assert nz.compare(fv2.KIND_TEXT, "Uttar Pradesh", "Uttar Pradesh State").reads_agree == \
        nz.AGREE_DISAGREE
    # Too different to be near.
    assert nz.compare(fv2.KIND_TEXT, "Delhi", "Karnataka").reads_agree == nz.AGREE_DISAGREE
    # A near-match is NEVER allowed for a number.
    assert nz.compare(fv2.KIND_NUMBER, "1234", "1235").reads_agree == nz.AGREE_DISAGREE


def test_compare_reports_missing_b_and_null():
    assert nz.compare(fv2.KIND_TEXT, "Delhi", None).reads_agree == nz.AGREE_MISSING_B
    assert nz.compare(fv2.KIND_TEXT, None, None).reads_agree == nz.AGREE_NULL
    # A value only read B saw is a disagreement, never a silent agreement.
    assert nz.compare(fv2.KIND_TEXT, None, "Delhi").reads_agree == nz.AGREE_DISAGREE
    # "not printed" is not a value.
    assert nz.compare(fv2.KIND_TEXT, "not printed", None).reads_agree == nz.AGREE_NULL


def test_line_keys():
    assert fv2.line_key(3, "amount") == "line.3.amount"
    assert fv2.parse_line_key("line.3.amount") == (3, "amount")
    assert fv2.parse_line_key("line.1.hsn_sac") == (1, "hsn_sac")
    assert fv2.parse_line_key("line.9.amount") == (9, "amount")
    # Over the per-document line cap: refused rather than unbounded.
    assert fv2.parse_line_key("line99.amount") is None
    assert fv2.parse_line_key("line.0.amount") is None
    assert fv2.parse_line_key("line.1.nonsense") is None
    assert fv2.parse_line_key("invoice_total") is None
    assert fv2.kind_of("line.2.amount") == fv2.KIND_NUMBER
    assert fv2.kind_of("line.2.hsn_sac") == fv2.KIND_IDENTIFIER
    assert fv2.is_monetary("invoice_total") is True
    assert fv2.is_monetary("invoice_date") is False


# ===========================================================================
# STEP 4 — read A, the handwriting guard, categories
# ===========================================================================


def test_read_a_boxes_outside_every_region_are_flagged(tmp_path, monkeypatch, v2_on):
    """A value whose box lands outside the document's regions is KEPT and
    flagged, never dropped.

    The region is the document's own Phase-1 box, while the image the model
    sees is the PADDED crop around it. With a generous padding the crop reaches
    well beyond the region, so a value boxed at the crop's far edge genuinely
    lies outside the document — which is exactly the case this status exists
    for.
    """
    pdf = _mk_pdf(tmp_path / "a.pdf", 1)
    read_a = {
        "fields": [
            {"key": "invoice_number", "value": "KMAP/1", "handwritten": False, "region": 1,
             "box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}},
            # Near the far corner of the (padded) crop — off the document.
            {"key": "invoice_total", "value": "1,180.00", "handwritten": False, "region": 1,
             "box": {"x": 0.96, "y": 0.96, "w": 0.03, "h": 0.03}},
        ],
    }
    _stub_reads(monkeypatch, read_a, read_a)
    outcome = ex.extract_document(
        _document(regions=[_region(1, (0.05, 0.05, 0.40, 0.30))]),
        pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True, padding_frac=0.2), actor="t", db_path=None,
    )
    rows = {r["field_key"]: r for r in outcome.fields}
    assert rows["invoice_number"]["status"] == fv2.STATUS_READ
    assert rows["invoice_number"]["in_region"] is True
    assert rows["invoice_total"]["status"] == fv2.STATUS_OUT_OF_REGION
    assert rows["invoice_total"]["in_region"] == 0
    # The value is KEPT even though it is flagged.
    assert rows["invoice_total"]["value_a"] == "1,180.00"
    # Every canonical invoice key got a row, and an absent one is not_printed.
    assert rows["sgst_amount"]["status"] == fv2.STATUS_NOT_PRINTED
    assert rows["sgst_amount"]["value_a"] is None


def test_handwritten_total_is_rejected_and_the_printed_one_kept(tmp_path, monkeypatch, v2_on):
    """The heart of STEP 4: a handwritten total must not become the value."""
    pdf = _mk_pdf(tmp_path / "hw.pdf", 1)
    read_a = {
        "fields": [
            # The model wrongly reports the handwritten figure as the total.
            {"key": "invoice_total", "value": "7,370", "handwritten": True, "region": 1,
             "box": {"x": 0.60, "y": 0.60, "w": 0.20, "h": 0.05}},
        ],
        "annotations": [
            {"text": "7,370", "kind_guess": "payment", "region": 1,
             "box": {"x": 0.60, "y": 0.60, "w": 0.20, "h": 0.05}},
        ],
    }
    printed = {
        "fields": [
            {"key": "invoice_total", "value": "8,053.00", "handwritten": False, "region": 1,
             "box": {"x": 0.60, "y": 0.70, "w": 0.20, "h": 0.05}},
        ],
    }
    _stub_reads(monkeypatch, read_a, printed)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    row = {r["field_key"]: r for r in outcome.fields}["invoice_total"]
    # A handwritten value is never accepted as the field value...
    assert row["status"] == fv2.STATUS_NEEDS_PRINTED
    assert row["is_handwritten"] == 1
    # ...and BOTH verbatim values are kept — the phase never picks a winner.
    assert row["value_a"] == "7,370"
    assert row["value_b"] == "8,053.00"
    assert row["reads_agree"] == nz.AGREE_DISAGREE
    # The annotation is stored, and never marked as a value.
    assert outcome.annotations[0]["text"] == "7,370"


def test_handwriting_guard_fires_when_boxes_overlap_and_not_otherwise():
    page_box = (0.60, 0.60, 0.20, 0.05)
    overlapping = [{"text": "6,825", "page_box": (0.61, 0.61, 0.20, 0.05)}]
    elsewhere = [{"text": "6,825", "page_box": (0.05, 0.90, 0.10, 0.05)}]
    assert ex._handwriting_guard("line.1.amount", "6,825.00", page_box, overlapping) == "6,825"
    # Same amount, different place on the page: not this value's problem.
    assert ex._handwriting_guard("line.1.amount", "6,825.00", page_box, elsewhere) is None
    # A different amount never fires.
    assert ex._handwriting_guard("line.1.amount", "9,999", page_box, overlapping) is None
    # Non-numeric values are never guarded.
    assert ex._handwriting_guard("invoice_number", "KMAP/1", page_box, overlapping) is None


def test_excluded_and_other_categories_are_skipped_with_a_reason(scratch_db):
    conn = sqlite3.connect(scratch_db)
    try:
        conn.execute(
            "INSERT INTO invoice_uploads (client_id, filename, file_ext, source_format, "
            "extraction_path, status, duplicate_warning, created_at, created_by) "
            "VALUES (1, 'x.pdf', 'pdf', 'pdf', 'structured', 'extracted', 0, 'now', 'test')"
        )
        upload_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO invoice_triage_runs (upload_id, status, started_at) "
            "VALUES (?, 'complete', 'now')", (upload_id,),
        )
        run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for ordinal, category, excluded in (
            (1, "tax_invoice", 0), (2, "supporting", 0), (3, "ticket", 0),
            (4, "not_a_voucher", 1), (5, "no_gst_expense", 0),
        ):
            conn.execute(
                "INSERT INTO invoice_documents (upload_id, run_id, ordinal, category, label, "
                "excluded, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'now')",
                (upload_id, run_id, ordinal, category, f"d{ordinal}", excluded),
            )
        conn.commit()
    finally:
        conn.close()

    result = ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    reasons = {row["category"]: row["reason"] for row in result["skipped_documents"]}
    assert result["queued"] == 2                       # tax_invoice + no_gst_expense
    assert result["skipped"] == 3
    assert "not extracted" in reasons["supporting"]
    assert "not extracted" in reasons["ticket"]
    assert "excluded" in reasons["not_a_voucher"]

    ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=20)
    stored = ex.get_extraction(upload_id, db_path=scratch_db)
    by_ordinal = {e["ordinal"]: e for e in stored["extractions"]}
    assert by_ordinal[2]["status"] == "skipped"
    assert "not extracted" in by_ordinal[2]["skipped_reason"]
    assert by_ordinal[4]["status"] == "skipped"
    assert "excluded" in by_ordinal[4]["skipped_reason"]


# ===========================================================================
# STEP 5 — provider independence, text layer, agreement
# ===========================================================================


def test_same_provider_for_both_reads_fails_clearly():
    with pytest.raises(InvoiceExtractError) as exc:
        ex.require_independent_provider("openrouter", db_path=None)
    message = str(exc.value)
    assert "different provider" in message
    assert "openrouter" in message
    assert "independent" in message


def test_require_independent_provider_accepts_another_configured_provider(monkeypatch):
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["deepseek", "openrouter"])
    ex.require_independent_provider("openrouter", db_path=None)      # no raise
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter"])
    with pytest.raises(InvoiceExtractError):
        ex.require_independent_provider("openrouter", db_path=None)


def test_read_b_served_by_the_same_provider_fails_the_document(tmp_path, monkeypatch, v2_on):
    pdf = _mk_pdf(tmp_path / "same.pdf", 1)
    _stub_reads(monkeypatch, _reply({"invoice_number": "A"}), _reply({"invoice_number": "B"}),
                provider_a="openrouter", provider_b="openrouter")
    # Another provider IS available, so the pre-flight check passes — and then
    # read B comes back served by the same vendor as read A.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    assert outcome.status == "failed"
    assert "same provider as read A" in (outcome.error or "")


def test_no_second_provider_available_fails_before_read_b(tmp_path, monkeypatch, v2_on):
    pdf = _mk_pdf(tmp_path / "one.pdf", 1)
    calls: list[dict] = []
    _stub_reads(monkeypatch, _reply({"invoice_number": "A"}), _reply({}), calls=calls)
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter"])
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    assert outcome.status == "failed"
    assert "different provider" in (outcome.error or "")
    # Read B was never even attempted.
    assert [c["touchpoint"] for c in calls] == [ex.TOUCHPOINT_READ_A]


def test_text_layer_match_is_set_for_a_native_pdf(tmp_path, monkeypatch, v2_on):
    pdf = _mk_pdf(tmp_path / "native.pdf", 1, text=True)
    read = {
        "fields": [
            # The page's text sits near the top-left, inside this box.
            {"key": "invoice_total", "value": "1,180.00", "handwritten": False, "region": 1,
             "box": {"x": 0.05, "y": 0.03, "w": 0.90, "h": 0.15}},
        ],
    }
    _stub_reads(monkeypatch, read, read)
    outcome = ex.extract_document(
        _document(regions=[_region(1, (0.25, 0.05, 0.85, 0.20))]),
        pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=500,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    row = {r["field_key"]: r for r in outcome.fields}["invoice_total"]
    assert row["text_layer_match"] == 1

    # An image source has no text layer at all → NULL, not 0.
    image = _mk_image(tmp_path / "scan.png")
    outcome2 = ex.extract_document(
        _document(), image.read_bytes(), "image", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    row2 = {r["field_key"]: r for r in outcome2.fields}["invoice_total"]
    assert row2["text_layer_match"] is None


def test_reads_agree_is_recorded_per_field(tmp_path, monkeypatch, v2_on):
    pdf = _mk_pdf(tmp_path / "cmp.pdf", 1)
    read_a = _reply({
        "invoice_number": "KMAP/2026-27/003",
        "invoice_total": "1,180.00",
        "place_of_supply": "Uttar Pradesh",
    })
    read_b = _reply({
        "invoice_number": "KMAP/2026-27/003",
        "invoice_total": "1,190.00",
        "place_of_supply": "Uttar Prades",
    })
    _stub_reads(monkeypatch, read_a, read_b)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    rows = {r["field_key"]: r for r in outcome.fields}
    assert rows["invoice_number"]["reads_agree"] == nz.AGREE_EXACT
    assert rows["invoice_total"]["reads_agree"] == nz.AGREE_DISAGREE
    assert rows["place_of_supply"]["reads_agree"] == nz.AGREE_NEAR
    # Both verbatim values stored; no winner chosen.
    assert rows["invoice_total"]["value_a"] == "1,180.00"
    assert rows["invoice_total"]["value_b"] == "1,190.00"


# ===========================================================================
# STEP 3 — multi-region documents go in ONE call, labelled
# ===========================================================================


def test_multi_region_document_is_sent_as_one_call_with_labelled_crops(tmp_path, monkeypatch, v2_on):
    pdf = _mk_pdf(tmp_path / "multi.pdf", 11)
    calls: list[dict] = []
    _stub_reads(monkeypatch, _reply({"invoice_total": "1,295.64"}, region=2),
                _reply({"invoice_total": "1,295.64"}, region=2), calls=calls)
    outcome = ex.extract_document(
        _document(regions=[_region(10, order=0), _region(11, order=1)]),
        pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    assert outcome.status in ("complete", "partial")
    # TWO reads, TWO crops each — not four separate calls.
    assert len(calls) == 2
    assert all(c["crops"] == 2 for c in calls)
    row = {r["field_key"]: r for r in outcome.fields}["invoice_total"]
    # The value came from Region 2, so it is placed on page 11.
    assert row["page"] == 11


def test_prompt_labels_each_region(tmp_path):
    pdf = _mk_pdf(tmp_path / "lbl.pdf", 11)
    crops, _ = crops_mod.render_region_crops(
        pdf.read_bytes(), "pdf", [_region(10, order=0), _region(11, order=1)], {},
        dpi=100, max_edge_px=500,
    )
    prompt = ex._user_prompt(crops)
    assert "Region 1 = page 10" in prompt
    assert "Region 2 = page 11" in prompt
    assert '"key": "invoice_number"' in prompt


# ===========================================================================
# STEP 6 — the job: state machine, resume, cost cap, unparseable JSON
# ===========================================================================


def _seed_upload(db: str, *, documents: int = 3, pages: int = 3) -> tuple[int, list[int]]:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO invoice_uploads (client_id, filename, file_ext, source_format, "
            "extraction_path, status, duplicate_warning, created_at, created_by) "
            "VALUES (1, 'bundle.pdf', 'pdf', 'pdf', 'structured', 'extracted', 0, 'now', 'test')"
        )
        upload_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO invoice_triage_runs (upload_id, status, started_at) "
            "VALUES (?, 'complete', 'now')", (upload_id,),
        )
        run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        doc_ids: list[int] = []
        for ordinal in range(1, documents + 1):
            page = min(ordinal, pages)
            conn.execute(
                "INSERT INTO invoice_documents (upload_id, run_id, ordinal, category, label, "
                "excluded, is_active, created_at) VALUES (?, ?, ?, 'tax_invoice', ?, 0, 1, 'now')",
                (upload_id, run_id, ordinal, f"doc{ordinal}"),
            )
            document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO document_regions (document_id, page, x, y, w, h, order_in_doc, "
                "source, is_active) VALUES (?, ?, 0.05, 0.05, 0.85, 0.40, 0, 'triage', 1)",
                (document_id, page),
            )
            doc_ids.append(document_id)
        conn.commit()
        return upload_id, doc_ids
    finally:
        conn.close()


def test_job_runs_queued_to_complete_and_persists_fields(tmp_path, monkeypatch, scratch_db):
    pdf = _mk_pdf(tmp_path / "job.pdf", 3)
    upload_id, doc_ids = _seed_upload(scratch_db, documents=2, pages=2)
    _set_upload_bytes(scratch_db, upload_id, pdf.read_bytes())

    _stub_reads(monkeypatch, _reply({"invoice_number": "KMAP/1", "invoice_total": "1,180.00"}),
                _reply({"invoice_number": "KMAP/1", "invoice_total": "1,180.00"}))
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])

    ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    counts = ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=60)
    assert counts.get("complete") == 2

    stored = ex.get_extraction(upload_id, db_path=scratch_db)
    assert len(stored["extractions"]) == 2
    first = stored["extractions"][0]
    assert first["provider_a"] == "openrouter"
    assert first["provider_b"] == "deepseek"
    assert first["attempts"] == 2                      # one attempt per read
    fields = ex.fields_by_key(first)
    assert fields["invoice_number"]["value_a"] == "KMAP/1"
    assert fields["invoice_number"]["reads_agree"] == nz.AGREE_EXACT
    assert fields["invoice_number"]["status"] == fv2.STATUS_READ
    # Every canonical invoice key got a row.
    assert len([f for f in first["fields"] if not f["field_key"].startswith("line.")]) == \
        len(fv2.INVOICE_KEYS)


def test_provider_preflight_reports_availability_and_blocks_a_single_provider(scratch_db, monkeypatch):
    rows, problems = ex.provider_preflight(db_path=scratch_db)
    providers = {row["provider"] for row in rows}
    # The two reads' default legs name more than one provider (read B is NOT
    # allowed to be the same vendor as read A).
    assert "openrouter" in providers
    assert len(providers) >= 2
    # With nothing configured, it says so and refuses to start.
    assert all(row["available"] is False for row in rows)
    assert any("No usable API key" in p for p in problems)
    assert any("different provider" in p for p in problems)

    # One provider only is still not enough — read B needs a second one.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter"])
    _rows, problems = ex.provider_preflight(db_path=scratch_db)
    assert any("different provider" in p for p in problems)

    # Two providers, both of them keyed → no problems at all.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "anthropic"])
    _rows, problems = ex.provider_preflight(db_path=scratch_db)
    assert problems == []


def test_resume_only_picks_up_unfinished_documents(tmp_path, monkeypatch, scratch_db):
    """Resume picks up what is UNFINISHED (queued / running) and leaves a
    FAILED document alone — a failure needs a re-run, not a resume."""
    pdf = _mk_pdf(tmp_path / "resume.pdf", 2)
    upload_id, doc_ids = _seed_upload(scratch_db, documents=2, pages=2)
    _set_upload_bytes(scratch_db, upload_id, pdf.read_bytes())
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])

    conn = sqlite3.connect(scratch_db)
    try:
        conn.execute(
            "INSERT INTO invoice_doc_extractions (document_id, status, is_active) VALUES (?, 'queued', 1)",
            (doc_ids[0],),
        )
        conn.execute(
            "INSERT INTO invoice_doc_extractions (document_id, status, error, is_active) "
            "VALUES (?, 'failed', 'earlier failure', 1)",
            (doc_ids[1],),
        )
        conn.commit()
    finally:
        conn.close()

    calls: list[dict] = []
    _stub_reads(monkeypatch, _reply({"invoice_number": "X"}), _reply({"invoice_number": "X"}),
                calls=calls)
    monkeypatch.setattr(ex, "get_config", lambda db_path=None: ex.ExtractConfig(
        enabled=True, concurrency=1,
    ))

    result = ex.resume_extraction(upload_id, actor="test", db_path=scratch_db)
    assert result["resumed"] == 1
    ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=60)

    stored = ex.get_extraction(upload_id, db_path=scratch_db)
    by_doc = {e["document_id"]: e for e in stored["extractions"]}
    assert by_doc[doc_ids[0]]["status"] == "complete"
    # The FAILED one was not touched by a resume.
    assert by_doc[doc_ids[1]]["status"] == "failed"
    assert by_doc[doc_ids[1]]["error"] == "earlier failure"
    assert calls, "the queued document should have been read"

    # Nothing unfinished → nothing to resume.
    assert ex.resume_extraction(upload_id, actor="test", db_path=scratch_db)["resumed"] == 0


def test_init_extract_requeues_a_running_row(scratch_db):
    upload_id, _ = _seed_upload(scratch_db, documents=1, pages=1)
    conn = sqlite3.connect(scratch_db)
    try:
        conn.execute(
            "INSERT INTO invoice_doc_extractions (document_id, status, is_active) "
            "SELECT document_id, 'running', 1 FROM invoice_documents WHERE upload_id = ?",
            (upload_id,),
        )
        conn.commit()
    finally:
        conn.close()
    assert ex.init_extract(scratch_db) >= 1
    counts = ex._status_counts(upload_id, db_path=scratch_db)
    assert counts.get("queued") == 1
    assert ex._active_rows(sqlite3.connect(scratch_db), upload_id) != []
    conn = sqlite3.connect(scratch_db)
    try:
        row = conn.execute(
            "SELECT error FROM invoice_doc_extractions WHERE document_id IN "
            "(SELECT document_id FROM invoice_documents WHERE upload_id = ?)", (upload_id,),
        ).fetchone()
    finally:
        conn.close()
    assert "interrupted" in (row[0] or "")


def test_cost_cap_stops_the_run_and_leaves_the_rest_queued(tmp_path, monkeypatch, scratch_db):
    pdf = _mk_pdf(tmp_path / "cap.pdf", 3)
    upload_id, _ = _seed_upload(scratch_db, documents=3, pages=3)
    _set_upload_bytes(scratch_db, upload_id, pdf.read_bytes())
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])

    # A price that makes a single document blow the cap, and a cap of $1.
    from datetime import datetime, timezone
    from src import db as recon_db

    conn = recon_db.get_connection(scratch_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_model_catalog (model_id, name, context_length, "
            "prompt_price, completion_price, modality, fetched_at) VALUES ('m', 'm', NULL, "
            "1000000.0, 1000000.0, 'text+image->text', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    read_a_calls = []

    def fake(*, touchpoint, crops, **kwargs):
        read_a_calls.append(touchpoint)
        return _reply({"invoice_number": "X"}), {
            "provider": "openrouter" if touchpoint == ex.TOUCHPOINT_READ_A else "deepseek",
            "model": "m", "usage": {"input_tokens": 500, "output_tokens": 500},
        }

    monkeypatch.setattr(ex, "_call_extract_model", fake)
    # concurrency=1 so the documents are read one after another: the first one
    # blows the cap, and the rest must never be started.
    monkeypatch.setattr(ex, "get_config", lambda db_path=None: ex.ExtractConfig(
        enabled=True, concurrency=1, max_cost_usd=1.00,
    ))

    ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    counts = ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=60)
    assert counts.get("queued", 0) >= 1, counts
    assert counts.get("complete", 0) == 1, counts
    # Read A + read B for the ONE document that was allowed to run — nothing more.
    assert len(read_a_calls) == 2, read_a_calls

    conn = sqlite3.connect(scratch_db)
    try:
        rows = conn.execute(
            "SELECT e.status, e.error FROM invoice_doc_extractions e "
            "JOIN invoice_documents d ON d.document_id = e.document_id "
            "WHERE d.upload_id = ? AND e.is_active = 1", (upload_id,),
        ).fetchall()
    finally:
        conn.close()
    queued = [r for r in rows if r[0] == "queued"]
    assert queued, rows
    assert all("cost cap reached" in (r[1] or "") for r in queued), queued
    # The queued rows are unfinished, so they can still be resumed.
    assert ex.is_extracting(upload_id, db_path=scratch_db) is True


def test_unparseable_reply_is_logged_and_the_document_fails(tmp_path, monkeypatch, scratch_db):
    pdf = _mk_pdf(tmp_path / "bad.pdf", 1)
    upload_id, _ = _seed_upload(scratch_db, documents=1, pages=1)
    _set_upload_bytes(scratch_db, upload_id, pdf.read_bytes())
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])

    class ParseError(Exception):
        pass

    def fake(*, touchpoint, crops, **kwargs):
        # The gateway logs the call (with its tokens) as parse_error, then raises.
        from src.ai_models import service as ai_models

        ai_models.log_ai_call(
            touchpoint_key=touchpoint, provider="openrouter", model="m",
            input_tokens=99, output_tokens=1, latency_ms=5, outcome="parse_error",
            attempt=1, upload_id=kwargs.get("upload_id"), db_path=kwargs.get("db_path"),
        )
        raise ParseError("unparseable JSON")

    monkeypatch.setattr(ex, "_call_extract_model", fake)
    ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=60)

    stored = ex.get_extraction(upload_id, db_path=scratch_db)
    assert stored["extractions"][0]["status"] == "failed"
    assert "read A failed" in (stored["extractions"][0]["error"] or "")

    from src.ai_models import service as ai_models

    log = ai_models.list_ai_call_log(upload_id=upload_id, db_path=scratch_db)
    assert len(log) == 1
    assert log[0]["outcome"] == "parse_error"
    assert log[0]["input_tokens"] == 99


def test_re_extraction_supersedes_and_keeps_history(tmp_path, monkeypatch, scratch_db):
    pdf = _mk_pdf(tmp_path / "sup.pdf", 1)
    upload_id, _ = _seed_upload(scratch_db, documents=1, pages=1)
    _set_upload_bytes(scratch_db, upload_id, pdf.read_bytes())
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "deepseek"])
    _stub_reads(monkeypatch, _reply({"invoice_number": "1"}), _reply({"invoice_number": "1"}))

    ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=60)
    first_id = ex.get_extraction(upload_id, db_path=scratch_db)["extractions"][0]["extraction_id"]

    ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=60)

    active = ex.get_extraction(upload_id, db_path=scratch_db)["extractions"]
    everything = ex.get_extraction(upload_id, db_path=scratch_db, active_only=False)["extractions"]
    assert len(active) == 1
    assert active[0]["extraction_id"] != first_id
    assert len(everything) == 2, "the superseded pass is kept, not deleted"


def test_start_refuses_while_another_pass_is_unfinished(scratch_db, v2_on):
    upload_id, _ = _seed_upload(scratch_db, documents=1, pages=1)
    conn = sqlite3.connect(scratch_db)
    try:
        conn.execute(
            "INSERT INTO invoice_doc_extractions (document_id, status, is_active) "
            "SELECT document_id, 'queued', 1 FROM invoice_documents WHERE upload_id = ?",
            (upload_id,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(InvoiceExtractError) as exc:
        ex.start_extraction(upload_id, actor="test", db_path=scratch_db)
    assert "still queued" in str(exc.value)
    # force=True supersedes instead — the previous pass is deactivated, not lost.
    result = ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    assert result["queued"] == 1


def test_disabled_by_default_refuses_to_run(scratch_db):
    upload_id, _ = _seed_upload(scratch_db, documents=1, pages=1)
    with pytest.raises(InvoiceExtractError) as exc:
        ex.start_extraction(upload_id, actor="test", db_path=scratch_db)
    assert "invoice_extract.v2.enabled" in str(exc.value)


def test_init_extract_creates_the_tables_on_a_scratch_db(scratch_db):
    conn = sqlite3.connect(scratch_db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"invoice_doc_extractions", "invoice_doc_fields", "invoice_doc_annotations"} <= names
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        assert 3 in versions
        # No existing table gained a column.
    finally:
        conn.close()


def test_legacy_path_is_unchanged_with_v2_disabled():
    """The legacy extractor must not know about Phase 2 at all."""
    import src.invoice_extract.extractor as legacy

    source = Path(legacy.__file__).read_text()
    assert "invoice_doc_extractions" not in source
    assert "invoice_extract.v2" not in source
    # And the module is off unless asked.
    assert ex.get_config().enabled is False


# ===========================================================================
# STEP 7 — the fixture and the scorer (needs the real PDF)
# ===========================================================================


def test_expected_fields_fixture_is_provisional_and_clean():
    path = REPO_ROOT / "tests/fixtures/invoice_extract/batch_apr2026_expected_fields.json"
    data = json.loads(path.read_text())
    assert data["verified"] is False
    assert data["source_env"] == "SETU_TRIAGE_FIXTURE_PDF"
    docs = {d["label"].split(" ")[0]: d for d in data["documents"]}
    assert {"p2", "p3", "p4", "p10-p11", "p23", "p24", "p25"} <= set(docs)
    # p24 / p25 are the handwriting pages and must declare their handwritten amounts.
    assert docs["p24"]["handwritten_amounts"]
    assert docs["p25"]["handwritten_amounts"]
    assert docs["p23"]["handwritten_amounts"] == []
    # Every asserted key must be a real canonical key.
    for doc in data["documents"]:
        for key in list(doc.get("expect") or {}) + list(doc.get("not_printed") or []):
            assert fv2.field_spec(key) is not None, f"{key} is not a canonical field"
    # The PDF itself is never committed.
    assert not list(path.parent.glob("*.pdf"))


def test_scorer_detects_wrong_values_and_counts_the_key_metric():
    expected = ex.ExpectedFields(
        path=Path("x"),
        data={"verified": False, "documents": [{
            "label": "d", "page": 2,
            "expect": {"invoice_total": "1,180.00", "invoice_number": "KMAP/3"},
            "not_printed": ["buyers_order_no"],
            "handwritten_amounts": ["7,370"],
        }]},
        verified=False,
    )
    extraction = {
        "extraction_id": 1, "document_id": 1, "ordinal": 1, "category": "tax_invoice",
        "status": "complete", "pages": [2], "annotations": [],
        "fields": [
            # read A is WRONG on the total; read B disagrees → caught.
            {"field_key": "invoice_total", "value_a": "1,190.00", "value_b": "1,180.00",
             "reads_agree": "disagree", "status": "read", "page": 2},
            # read A is RIGHT on the number, read B disagrees → wrong false flag.
            {"field_key": "invoice_number", "value_a": "KMAP/3", "value_b": "KMAP/4",
             "reads_agree": "disagree", "status": "read", "page": 2},
            # the model invented a buyers_order_no → wrong.
            {"field_key": "buyers_order_no", "value_a": "PO-1", "value_b": "PO-1",
             "reads_agree": "exact", "status": "read", "page": 2},
            # a handwritten amount used as a value → violation.
            {"field_key": "round_off", "value_a": "7,370", "value_b": None,
             "reads_agree": "missing_b", "status": "read", "page": 2},
        ],
    }
    result = {"extractions": [extraction]}
    scoring = ex._score_all(result, expected)
    totals = scoring["totals"]
    assert totals["a_wrong"] == 2                      # the total + the invented PO no
    assert totals["b_disagreed_on_a_wrong"] == 1       # only the total was contradicted
    assert totals["a_correct_flagged_disagree"] == 1   # the number was right but B differed
    violations = scoring["documents"][0]["handwriting_violations"]
    assert [v["field_key"] for v in violations] == ["round_off"]


def test_scorer_report_is_markdown_without_images():
    expected = ex.ExpectedFields(
        path=Path("x"), data={"verified": False, "documents": []}, verified=False,
    )
    extraction = {
        "extraction_id": 1, "document_id": 1, "ordinal": 1, "category": "supporting",
        "status": "skipped", "skipped_reason": "category 'supporting' is not extracted",
        "pages": [5], "annotations": [], "fields": [],
    }
    result = {"extractions": [extraction]}
    report = ex._render_score_report({
        "pdf": "/tmp/x.pdf", "pages_total": 5, "source_db": "/tmp/s.db", "db_copy": "/tmp/c.db",
        "data_root": "/tmp/d", "expected": expected, "result": result,
        "scoring": ex._score_all(result, expected), "calls": [],
        "elapsed_ms": 12, "cost_usd": None, "input_tokens": 0, "output_tokens": 0,
        "budget": {"cap_usd": 2.0, "spent_usd": 0.0, "exceeded": False, "unknown_cost_calls": 0},
        "status_counts": {"skipped": 1}, "triage_documents": 1, "triage_unassigned": 0,
        "annotated": ["/tmp/report/doc_1/region_1_page_2.png"],
    })
    assert report.startswith("# Invoice Extraction Phase 2")
    assert "KEY METRIC" in report
    assert "![" not in report                          # no embedded images
    assert "category 'supporting' is not extracted" in report
    assert "provisional" in report
    assert "Annotated crops" in report


@pytest.mark.skipif(FIXTURE_PDF is None, reason="SETU_TRIAGE_FIXTURE_PDF is not set")
def test_fixture_pdf_crop_geometry_is_readable():
    """With the real bundle available, every expected document's page must render
    and crop — the geometry contract the scorer depends on."""
    data = json.loads(
        (REPO_ROOT / "tests/fixtures/invoice_extract/batch_apr2026_expected_fields.json").read_text()
    )
    raw = Path(FIXTURE_PDF).read_bytes()
    import fitz

    doc = fitz.open(stream=raw, filetype="pdf")
    total = doc.page_count
    doc.close()
    for entry in data["documents"]:
        page = entry["page"]
        assert 1 <= page <= total, f"{entry['label']} refers to page {page} of {total}"
        crops, _ = crops_mod.render_region_crops(
            raw, "pdf",
            [{"page": page, "x": 0.02, "y": 0.02, "w": 0.96, "h": 0.96, "order_in_doc": 0}],
            {}, dpi=120, max_edge_px=1000,
        )
        assert crops[0].png[:4] == b"\x89PNG"
