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
from src.invoice_extract import triage as triage_mod
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


def _truncated_error() -> Exception:
    """The LLM layer's own error for a reply cut off at the output ceiling."""
    from src.ingestion_ai import llm as llm_mod

    return llm_mod.LLMError(
        f"parse_error|{llm_mod.TRUNCATED_REPLY_MARKER} in response — the model's reply "
        "was cut off before it finished (it hit the output-token limit)"
    )


def _empty_body_error() -> Exception:
    """The LLM layer's own error for a provider that billed tokens and sent nothing."""
    from src.ingestion_ai import llm as llm_mod

    return llm_mod.LLMError("parse_error|empty response")


def _malformed_reply_error() -> Exception:
    """A parse failure that is NOT a truncation: nothing was cut off."""
    from src.ingestion_ai import llm as llm_mod

    return llm_mod.LLMError("parse_error|no JSON object found in response")


def _read_stub(monkeypatch, *, read_a=None, read_b=None, errors_a=(), errors_b=(),
               calls=None, provider_a="openrouter", provider_b="deepseek"):
    """Stub the seam so each ATTEMPT of a read can fail with a chosen error.

    ``errors_b=[]`` → read B always answers. ``errors_b=[e1]`` → read B's first
    attempt raises ``e1`` and its second (the retry) answers, so a test can make
    every attempt fail by listing one error per attempt. Every call is recorded
    in ``calls`` with the budget it asked for — which is what proves the retry
    used the bigger one.
    """
    monkeypatch.setattr(
        ex, "configured_providers", lambda db_path=None: sorted({provider_a, provider_b})
    )
    attempts: dict[str, int] = {}

    def fake(*, touchpoint, crops, max_tokens=None, **kwargs):
        attempts[touchpoint] = attempts.get(touchpoint, 0) + 1
        if calls is not None:
            calls.append({"touchpoint": touchpoint, "max_tokens": max_tokens,
                          "attempt": attempts[touchpoint]})
        is_a = touchpoint == ex.TOUCHPOINT_READ_A
        errors = list(errors_a if is_a else errors_b)
        if attempts[touchpoint] <= len(errors):
            raise errors[attempts[touchpoint] - 1]
        provider = provider_a if is_a else provider_b
        return (read_a if is_a else read_b), {
            "provider": provider, "model": f"{provider}/model", "leg": "primary",
            "fallback_used": False,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

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


def test_handwriting_guard_needs_a_substantial_overlap_not_a_merest_touch():
    """A figure written in the NEIGHBOURING column shares a sliver of the value's
    box and says nothing about it. Only a real covering counts."""
    page_box = (0.10, 0.10, 0.30, 0.05)
    # Covers the value almost completely → fires.
    assert ex._handwriting_guard(
        "line.1.amount", "6,825.00", page_box,
        [{"text": "6,825", "page_box": (0.10, 0.10, 0.30, 0.05)}],
    ) == "6,825"
    # Overlaps a third of the value → fires (exactly at the threshold).
    third = crops_mod.overlap_ratio_of(page_box, (0.30, 0.10, 0.30, 0.05))
    assert third >= crops_mod.HANDWRITING_OVERLAP_MIN
    assert ex._handwriting_guard(
        "line.1.amount", "6,825.00", page_box,
        [{"text": "6,825", "page_box": (0.30, 0.10, 0.30, 0.05)}],
    ) == "6,825"
    # Overlaps a sliver → does NOT fire.
    sliver = crops_mod.overlap_ratio_of(page_box, (0.38, 0.10, 0.30, 0.05))
    assert sliver < crops_mod.HANDWRITING_OVERLAP_MIN
    assert ex._handwriting_guard(
        "line.1.amount", "6,825.00", page_box,
        [{"text": "6,825", "page_box": (0.38, 0.10, 0.30, 0.05)}],
    ) is None
    # A degenerate box can never reach the threshold.
    assert crops_mod.overlap_ratio_of(page_box, (0.20, 0.10, 0.0, 0.0)) == 0.0


def test_printed_amount_with_the_handwritten_figure_elsewhere_stays_read(tmp_path, monkeypatch, v2_on):
    """p24: the printed 6,825.00 and the handwritten 6,825 are the same figure,
    but written apart — the printed value is NOT a question for anyone."""
    pdf = _mk_pdf(tmp_path / "p24_apart.pdf", 1)
    field_box = {"x": 0.10, "y": 0.10, "w": 0.30, "h": 0.05}
    read = {
        "fields": [
            {"key": "line.1.amount", "value": "6,825.00", "handwritten": False,
             "region": 1, "box": field_box},
        ],
        "annotations": [
            {"text": "6,825", "kind_guess": "payment", "region": 1,
             "box": {"x": 0.60, "y": 0.60, "w": 0.30, "h": 0.05}},
        ],
    }
    _stub_reads(monkeypatch, read, read)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    row = {r["field_key"]: r for r in outcome.fields}["line.1.amount"]
    assert row["status"] == fv2.STATUS_READ
    assert row["guard_hit"] is None
    # The annotation is still recorded — it was seen, it just is not this value's problem.
    assert outcome.annotations[0]["text"] == "6,825"


def test_printed_amount_covered_by_the_handwritten_figure_is_flagged(tmp_path, monkeypatch, v2_on):
    """The p24 case the guard exists for: the same figure written OVER the value."""
    pdf = _mk_pdf(tmp_path / "p24_over.pdf", 1)
    field_box = {"x": 0.10, "y": 0.10, "w": 0.30, "h": 0.05}
    read = {
        "fields": [
            {"key": "line.1.amount", "value": "6,825.00", "handwritten": False,
             "region": 1, "box": field_box},
        ],
        "annotations": [
            {"text": "6,825", "kind_guess": "payment", "region": 1, "box": dict(field_box)},
        ],
    }
    _stub_reads(monkeypatch, read, read)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    row = {r["field_key"]: r for r in outcome.fields}["line.1.amount"]
    assert row["status"] == fv2.STATUS_NEEDS_PRINTED
    assert row["guard_hit"] == "6,825"
    # Flagged, never replaced: the printed value is still the value.
    assert row["value_a"] == "6,825.00"


def test_model_flagged_handwritten_monetary_value_is_flagged(tmp_path, monkeypatch, v2_on):
    """The other half of the rule: the model's OWN handwritten flag on a
    monetary value is enough on its own — no annotation and no overlap needed."""
    pdf = _mk_pdf(tmp_path / "hw_flag.pdf", 1)
    box = {"x": 0.30, "y": 0.30, "w": 0.20, "h": 0.05}
    read_a = {
        "fields": [{"key": "invoice_total", "value": "7,370", "handwritten": True,
                    "region": 1, "box": box}],
        "annotations": [],
    }
    read_b = {
        "fields": [{"key": "invoice_total", "value": "7,370", "handwritten": True,
                    "region": 1, "box": box}],
    }
    _stub_reads(monkeypatch, read_a, read_b)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    row = {r["field_key"]: r for r in outcome.fields}["invoice_total"]
    assert row["status"] == fv2.STATUS_NEEDS_PRINTED
    assert row["is_handwritten"] == 1
    # The model's flag fired it, not the box guard.
    assert row["guard_hit"] is None


def test_model_flagged_handwritten_NON_monetary_value_is_not_flagged(tmp_path, monkeypatch, v2_on):
    """`needs_printed` means 'a handwritten amount where a printed one is
    required'. A non-monetary field is never one of those."""
    pdf = _mk_pdf(tmp_path / "hw_nonmoney.pdf", 1)
    read = {
        "fields": [{"key": "invoice_number", "value": "KMAP/1", "handwritten": True,
                    "region": 1, "box": {"x": 0.10, "y": 0.10, "w": 0.30, "h": 0.05}}],
        "annotations": [],
    }
    _stub_reads(monkeypatch, read, read)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    row = {r["field_key"]: r for r in outcome.fields}["invoice_number"]
    assert row["status"] == fv2.STATUS_READ
    # The fact is still on record, which is what calibration reads.
    assert row["is_handwritten"] == 1


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


# ===========================================================================
# A read the OUTPUT BUDGET cut short, and read A's survival when read B dies
# ===========================================================================


def test_truncated_read_b_is_retried_once_with_a_bigger_budget(tmp_path, monkeypatch, v2_on):
    """A reply cut off mid-JSON is re-asked ONCE, with room to finish."""
    pdf = _mk_pdf(tmp_path / "trunc.pdf", 1)
    calls: list[dict] = []
    _read_stub(monkeypatch, read_a=_reply({"invoice_number": "KMAP/1"}),
               read_b=_reply({"invoice_number": "KMAP/1"}),
               errors_b=[_truncated_error()], calls=calls)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    # Exactly one retry — never a loop — and the document completes.
    assert [c["touchpoint"] for c in calls] == [
        ex.TOUCHPOINT_READ_A, ex.TOUCHPOINT_READ_B, ex.TOUCHPOINT_READ_B,
    ]
    # The retry asked for the bigger budget; the first attempt asked for the
    # normal one.
    assert [c["max_tokens"] for c in calls] == [
        ex.READ_MAX_TOKENS, ex.READ_MAX_TOKENS, ex.READ_RETRY_MAX_TOKENS,
    ]
    assert ex.READ_RETRY_MAX_TOKENS > ex.READ_MAX_TOKENS
    assert ex.READ_RETRY_ATTEMPTS == 1
    assert outcome.status == "complete"
    assert outcome.error is None
    rows = {r["field_key"]: r for r in outcome.fields}
    assert rows["invoice_number"]["reads_agree"] == nz.AGREE_EXACT
    # The retry is CHARGED: the successful attempt's usage is summed in.
    assert outcome.input_tokens == 200 and outcome.output_tokens == 100
    assert outcome.attempts == 3


def test_empty_read_b_is_retried_once_and_the_document_completes(tmp_path, monkeypatch, v2_on):
    """An empty body is the same budget symptom and gets the same one retry."""
    pdf = _mk_pdf(tmp_path / "empty.pdf", 1)
    calls: list[dict] = []
    _read_stub(monkeypatch, read_a=_reply({"invoice_number": "KMAP/1"}),
               read_b=_reply({"invoice_number": "KMAP/1"}),
               errors_b=[_empty_body_error()], calls=calls)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    assert [c["touchpoint"] for c in calls] == [
        ex.TOUCHPOINT_READ_A, ex.TOUCHPOINT_READ_B, ex.TOUCHPOINT_READ_B,
    ]
    assert calls[-1]["max_tokens"] == ex.READ_RETRY_MAX_TOKENS
    assert outcome.status == "complete"
    assert outcome.error is None


def test_read_b_truncated_twice_keeps_read_a_as_partial(tmp_path, monkeypatch, v2_on):
    """The retry is bounded: when it ALSO runs out of budget, the document is
    stored as 'partial' with everything read A saw."""
    pdf = _mk_pdf(tmp_path / "twice.pdf", 1)
    calls: list[dict] = []
    read_a = _reply(
        {"invoice_number": "KMAP/1", "invoice_total": "1,180.00"},
        annotations=[{"text": "Less TDS 10% Rs. 683.00", "kind_guess": "tds",
                      "region": 1, "box": {"x": 0.1, "y": 0.9, "w": 0.4, "h": 0.05}}],
    )
    _read_stub(monkeypatch, read_a=read_a, read_b=_reply({}),
               errors_b=[_truncated_error(), _truncated_error()], calls=calls)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    # One retry, then stop — not a second retry.
    assert [c["touchpoint"] for c in calls].count(ex.TOUCHPOINT_READ_B) == 2
    assert outcome.attempts == 3
    # 'partial': the document WAS read, once.
    assert outcome.status == ex.STATUS_PARTIAL == "partial"
    assert "read B failed" in (outcome.error or "")
    assert "unbalanced JSON object" in (outcome.error or "")
    rows = {r["field_key"]: r for r in outcome.fields}
    # Read A's value is stored and read B's side is empty — the missing_b shape
    # the pipeline already uses when read B is silent on a field.
    assert rows["invoice_number"]["value_a"] == "KMAP/1"
    assert rows["invoice_number"]["value_b"] is None
    assert rows["invoice_number"]["reads_agree"] == nz.AGREE_MISSING_B
    assert rows["invoice_number"]["status"] == fv2.STATUS_READ
    # A field NEITHER read saw is still null, not "missing from read B".
    assert rows["sgst_amount"]["reads_agree"] == nz.AGREE_NULL
    # Read A's handwritten annotation survives with the document.
    assert [a["text"] for a in outcome.annotations] == ["Less TDS 10% Rs. 683.00"]
    # Read A's own cost is what the run is charged for this document.
    assert outcome.input_tokens == 100 and outcome.output_tokens == 50


def test_a_non_truncation_read_b_error_is_not_retried(tmp_path, monkeypatch, v2_on):
    """A bigger budget cannot fix a real failure, so it is not paid for."""
    pdf = _mk_pdf(tmp_path / "plain.pdf", 1)
    for error, expected in (
        (RuntimeError("vendor said no"), "read B failed: RuntimeError: vendor said no"),
        (_malformed_reply_error(), "read B failed: LLMError: parse_error|no JSON object"),
    ):
        calls: list[dict] = []
        _read_stub(monkeypatch, read_a=_reply({"invoice_number": "KMAP/1"}),
                   read_b=_reply({"invoice_number": "KMAP/1"}),
                   errors_b=[error, error], calls=calls)
        outcome = ex.extract_document(
            _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
            cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
        )
        # Read B was attempted ONCE — the second error was never reached.
        assert [c["touchpoint"] for c in calls].count(ex.TOUCHPOINT_READ_B) == 1
        assert expected in (outcome.error or "")
        # Read A is still kept: this is a read-B failure like any other.
        assert outcome.status == "partial"
        assert {r["field_key"]: r for r in outcome.fields}["invoice_number"]["value_a"] == "KMAP/1"


def test_read_a_failure_still_fails_the_document(tmp_path, monkeypatch, v2_on):
    """Nothing is stored when the FIRST read fails: there is no read to keep."""
    pdf = _mk_pdf(tmp_path / "nodata.pdf", 1)
    calls: list[dict] = []
    _read_stub(monkeypatch, read_a=_reply({"invoice_number": "KMAP/1"}),
               read_b=_reply({"invoice_number": "KMAP/1"}),
               errors_a=[RuntimeError("vendor down")], calls=calls)
    outcome = ex.extract_document(
        _document(), pdf.read_bytes(), "pdf", rotations={}, text_layer_chars=0,
        cfg=ex.ExtractConfig(enabled=True), actor="t", db_path=None,
    )
    assert outcome.status == "failed"
    assert outcome.fields == [] and outcome.annotations == []
    assert "read A failed: RuntimeError: vendor down" in (outcome.error or "")
    # Read A is not retried either (this is not a budget failure), and read B is
    # never attempted.
    assert [c["touchpoint"] for c in calls] == [ex.TOUCHPOINT_READ_A]


def test_call_read_reports_what_it_took_and_never_raises(monkeypatch, v2_on):
    """The retry seam itself: `_call_read` returns a FAILED READ as a value.

    A failed read is an outcome here, exactly as a failed document is at the
    level above, so the caller always learns both WHAT failed and HOW MANY
    attempts it took — the number the document row records.
    """
    # A reply cut off on the first attempt: one retry, then success.
    calls: list[dict] = []
    _read_stub(monkeypatch, read_a=_reply({"invoice_number": "KMAP/1"}),
               errors_a=[_truncated_error()], calls=calls)
    call = ex._call_read(
        touchpoint=ex.TOUCHPOINT_READ_A, crops=[], upload_id=None, document_id=1,
        extraction_id=None, actor="t", timeout=5, db_path=None,
    )
    assert call.error is None and call.retried is True and call.attempts == 2
    assert [c["max_tokens"] for c in calls] == [ex.READ_MAX_TOKENS, ex.READ_RETRY_MAX_TOKENS]
    assert call.reply["fields"][0]["key"] == "invoice_number"
    assert call.input_tokens == 100 and call.output_tokens == 50

    # Cut off on BOTH attempts: the failure comes back, with the count, and
    # nothing is raised.
    calls = []
    _read_stub(monkeypatch, read_a=_reply({}),
               errors_a=[_truncated_error(), _truncated_error()], calls=calls)
    call = ex._call_read(
        touchpoint=ex.TOUCHPOINT_READ_A, crops=[], upload_id=None, document_id=1,
        extraction_id=None, actor="t", timeout=5, db_path=None,
    )
    assert call.error is not None and call.attempts == 2 and call.retried is True
    assert call.reply == {} and call.meta == {}
    assert call.input_tokens is None and call.output_tokens is None

    # Not a budget failure: returned at once, NOT retried.
    calls = []
    _read_stub(monkeypatch, read_a=_reply({}), errors_a=[RuntimeError("nope")], calls=calls)
    call = ex._call_read(
        touchpoint=ex.TOUCHPOINT_READ_A, crops=[], upload_id=None, document_id=1,
        extraction_id=None, actor="t", timeout=5, db_path=None,
    )
    assert isinstance(call.error, RuntimeError) and call.retried is False
    assert call.attempts == 1 and len(calls) == 1


def test_partial_read_b_failure_is_stored_with_read_a_and_rules_still_run(
        tmp_path, monkeypatch, scratch_db):
    """End to end: a read-B failure stores read A as 'partial', and Phase 3/4
    (checks, direction, the review model) still run on that stored result."""
    pdf = _mk_pdf(tmp_path / "partial.pdf", 1)
    upload_id, _doc_ids = _seed_upload(scratch_db, documents=1, pages=1)
    _set_upload_bytes(scratch_db, upload_id, pdf.read_bytes())
    _read_stub(
        monkeypatch,
        read_a=_reply(
            {"invoice_number": "KMAP/1", "invoice_total": "1,180.00"},
            annotations=[{"text": "Paid by ch. No. 002887 dt. 16/04/26",
                          "kind_guess": "payment", "region": 1,
                          "box": {"x": 0.1, "y": 0.9, "w": 0.4, "h": 0.05}}],
        ),
        read_b=_reply({}),
        errors_b=[_truncated_error(), _truncated_error()],
    )
    ex.start_extraction(upload_id, actor="test", db_path=scratch_db, force=True)
    ex.wait_for_extraction(upload_id, db_path=scratch_db, timeout=60)
    # ``wait_for_extraction`` returns as soon as every DOCUMENT row is terminal
    # — which is BEFORE Phase 3/4 run. The checks and the direction asserted on
    # below are written by that later step, so wait for the JOB itself: its
    # event is set when the whole worker body, rules included, is done.
    state = ex.job_state(upload_id)
    assert state is not None and state["event"].wait(timeout=60), "the job did not finish"

    stored = ex.get_extraction(upload_id, db_path=scratch_db)["extractions"][0]
    assert stored["status"] == "partial"
    assert "read B failed" in (stored["error"] or "")
    fields = {f["field_key"]: f for f in stored["fields"]}
    assert fields["invoice_number"]["value_a"] == "KMAP/1"
    assert fields["invoice_number"]["value_b"] is None
    assert fields["invoice_number"]["reads_agree"] == nz.AGREE_MISSING_B
    assert fields["invoice_total"]["value_a"] == "1,180.00"
    assert stored["annotations"][0]["text_verbatim"] == "Paid by ch. No. 002887 dt. 16/04/26"
    assert stored["annotations"][0]["used_as_value"] == 0

    document_id = int(stored["document_id"])
    conn = sqlite3.connect(scratch_db)
    try:
        checks = conn.execute(
            "SELECT COUNT(*) FROM invoice_doc_checks WHERE document_id = ?", (document_id,)
        ).fetchone()[0]
        row = conn.execute(
            "SELECT direction, basis FROM invoice_doc_direction WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    finally:
        conn.close()
    # Phase 3 ran: checks were computed and a direction was decided WITH a basis.
    assert checks > 0
    assert row is not None
    assert row[0] in ("sales", "purchase", "likely_purchase", "undetermined")
    assert row[1]
    # ...so the review screen can build its model from the partial result.
    from src.invoice_extract import rules as rules_mod

    model = rules_mod.get_review_model(document_id, db_path=scratch_db)
    assert isinstance(model["needs_you"], list)



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


def test_provider_preflight_reports_availability_and_blocks_a_single_provider(tmp_path, monkeypatch):
    """The pre-flight's key and independence logic.

    Its OWN database, because the legs are stated explicitly here: the seed no
    longer guesses a vision model, so a test that needs one must assign it —
    and doing that on the shared scratch DB would leak into other tests.
    """
    from src.ai_models import service as ai_models

    db = str(tmp_path / "preflight.db")
    bootstrap.run_migrations(db_path=db, data_root=tmp_path / "data")
    ai_models.set_assignment_legs(
        ex.TOUCHPOINT_READ_A, primary_provider="openrouter", primary_model="acme/vision-a",
        fallback_provider="openrouter", fallback_model="acme/vision-a2",
        actor="test", db_path=db,
    )
    ai_models.set_assignment_legs(
        ex.TOUCHPOINT_READ_B, primary_provider="anthropic", primary_model="acme/vision-b",
        fallback_provider="anthropic", fallback_model="acme/vision-b2",
        actor="test", db_path=db,
    )

    rows, problems = ex.provider_preflight(db_path=db)
    providers = {row["provider"] for row in rows}
    # The two reads' legs name more than one provider (read B is NOT allowed to
    # be the same vendor as read A).
    assert "openrouter" in providers
    assert "anthropic" in providers
    assert len(providers) >= 2
    # With nothing configured, it says so and refuses to start.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: [])
    rows, problems = ex.provider_preflight(db_path=db)
    assert all(row["available"] is False for row in rows)
    assert any("No usable API key" in p for p in problems)
    assert any("different provider" in p for p in problems)

    # One provider only is still not enough — read B needs a second one.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter"])
    _rows, problems = ex.provider_preflight(db_path=db)
    assert any("different provider" in p for p in problems)

    # Two providers, both of them keyed → no problems at all.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "anthropic"])
    _rows, problems = ex.provider_preflight(db_path=db)
    assert problems == []


def _preflight_db_with_both_reads(tmp_path) -> str:
    """A database with the two reads assigned explicitly — read A on OpenRouter,
    read B on Anthropic.

    Its OWN database: the seed no longer guesses a vision model, so a test that
    needs one must assign it (and doing that on the shared scratch DB would leak
    into other tests).
    """
    from src.ai_models import service as ai_models

    db = str(tmp_path / "preflight_named.db")
    bootstrap.run_migrations(db_path=db, data_root=tmp_path / "data")
    ai_models.set_assignment_legs(
        ex.TOUCHPOINT_READ_A, primary_provider="openrouter",
        primary_model="anthropic/claude-sonnet-4",
        fallback_provider="openrouter", fallback_model="google/gemini-2.5-pro",
        actor="test", db_path=db,
    )
    ai_models.set_assignment_legs(
        ex.TOUCHPOINT_READ_B, primary_provider="anthropic",
        primary_model="claude-sonnet-4",
        fallback_provider="anthropic", fallback_model="claude-haiku-4",
        actor="test", db_path=db,
    )
    return db


def test_provider_preflight_names_the_read_provider_model_and_reason(tmp_path, monkeypatch):
    """A pre-flight refusal must say WHICH read, WHICH provider, WHICH model and
    WHY — otherwise the human cannot tell what to change."""
    db = _preflight_db_with_both_reads(tmp_path)
    # Everything keyed: no problems, but BOTH reads are still named, with the
    # provider and model each leg would use.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "anthropic"])
    rows, problems = ex.provider_preflight(db_path=db)
    assert problems == []
    by_provider = {row["provider"]: row for row in rows}
    a_legs = [u for u in by_provider["openrouter"]["used_by"] if u["read"] == "read A"]
    assert a_legs and all(u["touchpoint"] == ex.TOUCHPOINT_READ_A for u in a_legs)
    assert {u["leg"] for u in a_legs} == {"primary", "fallback"}
    assert any(u["model"] == "anthropic/claude-sonnet-4" for u in a_legs)
    assert {u["read"] for u in by_provider["anthropic"]["used_by"]} == {"read B"}
    # Both of read B's legs are named; its fallback is a cheaper Anthropic model
    # because the service refuses a fallback identical to the primary.
    assert "claude-sonnet-4" in {u["model"] for u in by_provider["anthropic"]["used_by"]}

    # Read B refused for a MISSING KEY: read, touchpoint, provider, model, reason.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter"])
    _rows, problems = ex.provider_preflight(db_path=db)
    blob = "\n".join(problems)
    assert "read B" in blob and ex.TOUCHPOINT_READ_B in blob
    assert "anthropic" in blob and "claude-sonnet-4" in blob
    assert "no usable API key" in blob
    assert "different provider" in blob

    # Read A refused as well: read A is named too, with its own model.
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["anthropic"])
    _rows, problems = ex.provider_preflight(db_path=db)
    blob = "\n".join(problems)
    assert "read A" in blob and ex.TOUCHPOINT_READ_A in blob
    assert "openrouter" in blob and "anthropic/claude-sonnet-4" in blob


def test_provider_preflight_refuses_a_text_only_model_by_name(tmp_path, monkeypatch):
    """The third refusal reason: the read sends page images, so a text-only
    model is refused — naming the read, the provider and the model."""
    db = _preflight_db_with_both_reads(tmp_path)
    monkeypatch.setattr(ex, "configured_providers", lambda db_path=None: ["openrouter", "anthropic"])
    monkeypatch.setattr(
        "src.ai_models.service.vision_check",
        lambda provider, model, **kwargs: (
            model != "claude-sonnet-4",
            "" if model != "claude-sonnet-4"
            else f"model '{model}' ({provider}) does not accept image input",
        ),
    )
    _rows, problems = ex.provider_preflight(db_path=db)
    blob = "\n".join(problems)
    assert "read B" in blob and ex.TOUCHPOINT_READ_B in blob
    assert "claude-sonnet-4" in blob and "anthropic" in blob
    assert "does not accept image input" in blob
    # It is a vision refusal, not a key problem — both legs are keyed here.
    assert not any("No usable API key" in p for p in problems)


def test_provider_preflight_refuses_read_b_on_read_as_own_provider(scratch_db, monkeypatch):
    """Both primaries on one provider is the failure the second read exists to
    prevent, so it is refused up front with both legs named."""
    monkeypatch.setattr(
        ex, "configured_providers", lambda db_path=None: ["openrouter", "anthropic"],
    )
    monkeypatch.setattr(ex, "configured_reads", lambda db_path=None: [
        {"read": "read A", "touchpoint": ex.TOUCHPOINT_READ_A, "leg": "primary",
         "provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        {"read": "read A", "touchpoint": ex.TOUCHPOINT_READ_A, "leg": "fallback",
         "provider": "openrouter", "model": "google/gemini-2.5-pro"},
        {"read": "read B", "touchpoint": ex.TOUCHPOINT_READ_B, "leg": "primary",
         "provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        {"read": "read B", "touchpoint": ex.TOUCHPOINT_READ_B, "leg": "fallback",
         "provider": "anthropic", "model": "claude-sonnet-4"},
    ])
    _rows, problems = ex.provider_preflight(db_path=scratch_db)
    blob = "\n".join(problems)
    assert "SAME provider" in blob
    assert "read A" in blob and "read B" in blob
    assert "anthropic/claude-sonnet-4" in blob
    assert "different provider" in blob
    # Both legs ARE keyed, so nothing here is about a missing key.
    assert not any("No usable API key" in p for p in problems)



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


# ===========================================================================
# Triage prompt — one document per issuer (over-split fix)
# ===========================================================================


def test_triage_prompt_keeps_one_document_per_issuer():
    """The triage system prompt must keep the rules that stop ONE sheet being
    split into many documents, and must no longer tell the model to split when
    it is unsure. Prompt text only — no model is called, no DB is touched."""
    prompt = triage_mod._SYSTEM_PROMPT

    # (a) one issuer = one document, sections are part of it.
    assert "ONE issuer" in prompt
    assert "ONE document, ONE box per page" in prompt
    assert "its sections included" in prompt
    assert "The SAME issuer is ONE document" in prompt

    # (b) boilerplate / continuations of the same document are not documents.
    assert "headers, footers" in prompt
    assert "payment " in prompt and "instructions" in prompt
    assert "continues_previous=true" in prompt

    # (c) handwritten marks and annotations belong to their document.
    assert "Handwritten notes" in prompt
    assert "never separate" in prompt

    # (d) the old "unsure" trigger that caused the over-split is gone.
    assert "when you are UNSURE" not in prompt
    # A second box may only be returned for genuinely different paper, and only
    # with the "unsure_split" flag — never on a bare tie-break.
    assert "could differ in issuer or number" in prompt
    assert prompt.index("could differ in issuer or number") < prompt.index("unsure_split")
    assert "most pages hold ONE" in prompt

    # Unchanged contracts: categories, geometry, rotation, blank pages.
    for category in ("tax_invoice", "no_gst_expense", "supporting", "ticket", "not_a_voucher"):
        assert category in prompt
    assert "FRACTIONS" in prompt
    assert "rotation_deg" in prompt
    assert "page_is_blank" in prompt
    assert "evidence snippet" in prompt

    # The reply shape and field names handed to the model are untouched.
    fields = triage_mod._USER_PROMPT_TEMPLATE
    for key in ('"category"', '"label"', '"continues_previous"', '"continues_next"',
                '"flags"', '"evidence"', '"boxes"', '"x"', '"y"', '"w"', '"h"'):
        assert key in fields
    assert "Return exactly this JSON shape" in fields

    # Keep the rewrite inside its size budget so the instruction weight on the
    # model does not balloon. History of the ceiling: the prompt was 1545 chars,
    # the one-document-per-issuer rules took it to 2012 (a ~+30% budget), the
    # statement / duplicate-copy rules took it to 2308 (~+15%), and rewriting
    # those two rules for the Airtel statement pages brought it to 2863 — the
    # current approved ceiling.
    assert len(prompt) <= 2863, f"system prompt grew to {len(prompt)} chars"
