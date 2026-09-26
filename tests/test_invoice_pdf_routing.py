"""F3-B routing regression tests — content-based, not extension-based.

Bug: a scanned / image-only PDF (the ``.pdf`` extension but no real text
layer) was being routed to the STRUCTURED touchpoint (text model) because
the classifier scanned raw decompressed stream bytes for ``(...)`` text
operators. A scanned page stores its pixels in those same streams, so the
decompressed image data was harvested as 100k+ characters of "text" and the
file was mis-classified, producing "every field not present".

These tests build their PDFs in-memory with PyMuPDF, so no binary fixtures
are committed. They pin BOTH directions (scanned -> visual, native -> 
structured) so the fix cannot silently regress the common native-PDF case.
"""

from __future__ import annotations

import io

import pytest

from src.invoice_extract import ai_extractor, extractor

fitz = pytest.importorskip("fitz")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")


# ---------------------------------------------------------------------------
# Fixtures — generated in-memory (no committed binaries)
# ---------------------------------------------------------------------------


def _image_only_pdf() -> bytes:
    """A 'photographed/scanned' PDF: one page holding a JPEG image and NO
    text layer — exactly the shape of the failing upload."""
    img = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(img)
    for i in range(0, 1754, 7):
        draw.line([(0, i), (1240, i)], fill=(i % 255, (i * 3) % 255, (i * 7) % 255), width=1)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buf.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


def _native_text_pdf() -> bytes:
    """A genuinely native-text PDF with a real extractable text layer."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Tax Invoice No: INV-2026-0042")
    page.insert_text((72, 130), "Vendor: Shree Balaji Hardware")
    page.insert_text((72, 160), "GSTIN: 27AABCM1234K1Z9  Total: 128400.00")
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# Content-based text-layer detection
# ---------------------------------------------------------------------------


def test_image_only_pdf_has_no_text_layer():
    assert extractor.has_text_layer(_image_only_pdf()) is False


def test_native_text_pdf_has_text_layer():
    assert extractor.has_text_layer(_native_text_pdf()) is True


def test_byte_heuristic_alone_would_misclassify_a_scan():
    """Documents the ROOT CAUSE: the old byte-scanning heuristic reports the
    scanned PDF as carrying 'meaningful text' (binary image noise), while the
    new content guard correctly rejects it."""
    data = _image_only_pdf()
    raw = extractor._pdf_text(data)
    # The old decision input was genuinely fooled...
    assert extractor._has_meaningful_text(raw) is True
    # ...and the new guard is not.
    assert extractor._looks_like_text(raw) is False


def test_looks_like_text_accepts_real_text_and_rejects_binary():
    assert extractor._looks_like_text(
        "Tax Invoice No: INV-2026-0042 Vendor Shree Balaji Hardware GSTIN 27AABCM1234K1Z9"
    )
    # Latin-1 decode of decompressed image bytes: printable enough chars to
    # pass the old length check, but no real words.
    noise = "".join(chr((i * 37) % 256) for i in range(2000))
    assert not extractor._looks_like_text(noise)


# ---------------------------------------------------------------------------
# Routing decision (the actual fix)
# ---------------------------------------------------------------------------


def test_image_only_pdf_routes_to_visual_touchpoint():
    data = _image_only_pdf()
    assert ai_extractor._route("pdf", data) == "visual"
    assert ai_extractor.touchpoint_for_path(ai_extractor._route("pdf", data)) == (
        ai_extractor.TOUCHPOINT_VISUAL
    )


def test_native_text_pdf_routes_to_structured_touchpoint():
    data = _native_text_pdf()
    assert ai_extractor._route("pdf", data) == "structured"
    assert ai_extractor.touchpoint_for_path(ai_extractor._route("pdf", data)) == (
        ai_extractor.TOUCHPOINT_STRUCTURED
    )


def test_deterministic_extract_path_matches_routing():
    """The offline fallback must agree with the AI layer's routing."""
    scan = extractor.extract(
        filename="Bill of 09.09.2026_1.pdf", file_bytes=_image_only_pdf(), source_format="pdf"
    )
    assert scan[1] == "visual"

    native = extractor.extract(
        filename="invoice.pdf", file_bytes=_native_text_pdf(), source_format="pdf"
    )
    assert native[1] == "structured"


def test_non_pdf_formats_route_by_type():
    assert ai_extractor._route("image", b"") == "visual"
    assert ai_extractor._route("excel", b"") == "structured"
    assert ai_extractor._route("word", b"") == "structured"


def test_touchpoint_keys_are_the_seeded_ones():
    assert ai_extractor.TOUCHPOINT_VISUAL == "invoice_extraction_visual"
    assert ai_extractor.TOUCHPOINT_STRUCTURED == "invoice_extraction_structured"


# ---------------------------------------------------------------------------
# End-to-end routing through extract_with_ai (which touchpoint is dispatched)
# ---------------------------------------------------------------------------


def _record_dispatches(monkeypatch):
    """Patch the LLM transport so we can observe which touchpoint was used."""
    calls: dict[str, str] = {"used": ""}
    minimal = {"fields": {}}

    def _vision(system, user, images, *, db_path=None, model_override=None, touchpoint_key=""):
        calls["used"] = touchpoint_key
        calls["kind"] = "vision"
        return minimal, 12, "test/vision-model", "{}"

    def _text(system, user, *, db_path=None, model_override=None, touchpoint_key=""):
        calls["used"] = touchpoint_key
        calls["kind"] = "text"
        return minimal, 11, "test/text-model", "{}"

    monkeypatch.setattr("src.ingestion_ai.llm.call_llm_vision_json", _vision)
    monkeypatch.setattr("src.ingestion_ai.llm.call_llm_json", _text)
    return calls


def test_extract_with_ai_dispatches_scan_to_visual_touchpoint(monkeypatch):
    calls = _record_dispatches(monkeypatch)
    _results, path, meta = ai_extractor.extract_with_ai(
        filename="Bill of 09.09.2026_1.pdf",
        file_bytes=_image_only_pdf(),
        source_format="pdf",
    )
    assert path == "visual"
    assert calls["kind"] == "vision"
    assert calls["used"] == "invoice_extraction_visual"
    assert meta["ai_used"] is True


def test_extract_with_ai_dispatches_native_pdf_to_structured_touchpoint(monkeypatch):
    calls = _record_dispatches(monkeypatch)
    _results, path, meta = ai_extractor.extract_with_ai(
        filename="invoice.pdf",
        file_bytes=_native_text_pdf(),
        source_format="pdf",
    )
    assert path == "structured"
    assert calls["kind"] == "text"
    assert calls["used"] == "invoice_extraction_structured"
    assert meta["ai_used"] is True
