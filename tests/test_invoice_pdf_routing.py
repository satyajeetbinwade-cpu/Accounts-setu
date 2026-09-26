"""F3-B routing regression tests — content-based, not extension-based.

Bug 1: a scanned / image-only PDF (the ``.pdf`` extension but no real text
layer) was being routed to the STRUCTURED touchpoint (text model) because the
classifier scanned raw decompressed stream bytes for ``(...)`` text operators.
A scanned page stores its pixels in those same streams, so the decompressed
image data was harvested as 100k+ characters of "text" and the file was
mis-classified, producing "every field not present".

Bug 2 (LST_1042.pdf): the OPPOSITE drift. The routing probe and the prompt
builder used two different PDF readers. ``has_text_layer`` (PyMuPDF) correctly
reported a text layer, but ``_pdf_text`` — the reader that builds the structured
prompt — only understands LITERAL ``(...) Tj`` strings, while modern writers
store glyphs as HEX strings (``[<546178...>]TJ``). The structured call was
therefore dispatched with an EMPTY document: all 16 fields came back "not
present" under a badge that still said "AI / Structured touchpoint". The fix is
to decide the path from the SAME text the structured call would send.

These tests build their PDFs in-memory, so no binary fixtures are committed.
They pin ALL THREE directions: scan -> visual, literal-text -> structured, and
hex-encoded text -> visual (never an empty structured prompt).
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


def _hex_text_pdf() -> bytes:
    """A native-text PDF written by PyMuPDF/MuPDF — the LST_1042.pdf shape.

    PyMuPDF stores the glyphs as HEX strings inside a ``TJ`` array::

        /helv 11 Tf [<54617820496e766f696365204e6f3a...>]TJ

    so PyMuPDF itself reads the text back perfectly, while the stdlib scraper
    (which only understands literal ``(...) Tj``) sees nothing. This is the
    fixture that proves the two readers can disagree.
    """
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Tax Invoice No: INV-2026-0042")
    page.insert_text((72, 130), "Vendor: Shree Balaji Hardware")
    page.insert_text((72, 160), "GSTIN: 27AABCM1234K1Z9  Total: 128400.00")
    data = doc.tobytes()
    doc.close()
    return data


def _literal_text_pdf() -> bytes:
    """A PDF whose text is stored as LITERAL string objects — the shape the
    structured reader can actually recover (and therefore serve)."""
    text = "Tax Invoice No: INV-2026-0042  Vendor: Shree Balaji Hardware Total: 128400.00"
    content = ("BT /F1 12 Tf 72 720 Td (" + text + ") Tj ET").encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


# ---------------------------------------------------------------------------
# Content-based text-layer detection
# ---------------------------------------------------------------------------


def test_image_only_pdf_has_no_text_layer():
    assert extractor.has_text_layer(_image_only_pdf()) is False


def test_hex_text_pdf_still_has_a_text_layer():
    """The PyMuPDF probe is right — this file DOES carry a text layer. That is
    exactly why the routing must not rely on this probe alone."""
    assert extractor.has_text_layer(_hex_text_pdf()) is True


def test_hex_text_pdf_is_not_servable_structurally():
    """Root cause of LST_1042.pdf: a text layer the structured READER cannot
    recover (hex strings vs literal strings), so the prompt would be empty."""
    data = _hex_text_pdf()
    assert extractor._pdf_text(data) == ""
    assert extractor.can_serve_pdf_structurally(data) is False


def test_literal_text_pdf_has_text_layer():
    assert extractor.has_text_layer(_literal_text_pdf()) is True
    assert extractor.can_serve_pdf_structurally(_literal_text_pdf()) is True


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


def test_literal_text_pdf_routes_to_structured_touchpoint():
    data = _literal_text_pdf()
    assert ai_extractor._route("pdf", data) == "structured"
    assert ai_extractor.touchpoint_for_path(ai_extractor._route("pdf", data)) == (
        ai_extractor.TOUCHPOINT_STRUCTURED
    )


def test_hex_text_pdf_routes_to_visual_touchpoint():
    """LST_1042.pdf: a text PDF this reader cannot serve must go to VISION,
    never to structured with an empty document."""
    data = _hex_text_pdf()
    assert ai_extractor._route("pdf", data) == "visual"
    assert ai_extractor.touchpoint_for_path(ai_extractor._route("pdf", data)) == (
        ai_extractor.TOUCHPOINT_VISUAL
    )


def test_deterministic_extract_path_matches_routing():
    """The offline fallback must agree with the AI layer's routing."""
    scan = extractor.extract(
        filename="Bill of 09.09.2026_1.pdf", file_bytes=_image_only_pdf(), source_format="pdf"
    )
    assert scan[1] == "visual"

    # A text PDF the structured reader CAN read stays structured...
    literal = extractor.extract(
        filename="invoice.pdf", file_bytes=_literal_text_pdf(), source_format="pdf"
    )
    assert literal[1] == "structured"

    # ...and a hex-encoded text PDF it CANNOT read goes visual, so the fields
    # are never reported "not present" just because the text was invisible.
    hexed = extractor.extract(
        filename="LST_1042.pdf", file_bytes=_hex_text_pdf(), source_format="pdf"
    )
    assert hexed[1] == "visual"


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


def test_extract_with_ai_dispatches_literal_pdf_to_structured_touchpoint(monkeypatch):
    calls = _record_dispatches(monkeypatch)
    _results, path, meta = ai_extractor.extract_with_ai(
        filename="invoice.pdf",
        file_bytes=_literal_text_pdf(),
        source_format="pdf",
    )
    assert path == "structured"
    assert calls["kind"] == "text"
    assert calls["used"] == "invoice_extraction_structured"
    assert meta["ai_used"] is True


def test_extract_with_ai_dispatches_hex_text_pdf_to_visual_touchpoint(monkeypatch):
    """The LST_1042.pdf regression end-to-end: the VISION touchpoint is
    dispatched (not structured with an empty document)."""
    calls = _record_dispatches(monkeypatch)
    _results, path, meta = ai_extractor.extract_with_ai(
        filename="LST_1042.pdf",
        file_bytes=_hex_text_pdf(),
        source_format="pdf",
    )
    assert path == "visual"
    assert calls["kind"] == "vision"
    assert calls["used"] == "invoice_extraction_visual"
    assert meta["ai_used"] is True
