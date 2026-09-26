"""F3-B AI extraction layer — reads invoices in ANY format via a model.

This is the real AI layer the module was always meant to have. The
deterministic ``extractor.py`` remains as an offline fallback (and as the
fast path for clean tabular exports), but every upload now goes through
this layer first so that images, scanned PDFs, native PDFs, Word and Excel
are all read the same way and mapped onto the SAME canonical field set.

Routing (mirrors the two C3-ext touchpoints the module already declares):

  * VISUAL     — images (JPG/PNG) and scanned PDFs (no usable text layer).
                 The page image(s) are sent to a vision-capable model.
  * STRUCTURED — native-text PDF, Word, Excel/CSV. The extracted text /
                 table content is sent to a text model.

Both paths return the identical per-field shape the rest of the module
already consumes, so nothing downstream changes:

    {
      "field_name": str,
      "extracted_value": str | None,
      "confidence": int | None,      # None => genuinely absent ("not present")
      "source_location": str | None,
      "is_present": bool,
    }

Honesty rules (unchanged from extractor.py, and enforced in the prompt):
  * A field the model cannot find is returned with ``is_present=False`` and
    ``confidence=None`` — "not present", NEVER invented.
  * Confidence is per-field, never a single document-level score.
  * The model is told to copy values verbatim from the document; it is not
    asked to compute or infer anything.

If the AI layer is unavailable (no key, network error, unparseable reply)
the caller falls back to the deterministic extractor — this module never
raises for a model failure, it reports it so the UI can be honest about
which path produced the values.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Optional

from src.invoice_extract import extractor
from src.invoice_extract.seed import CANONICAL_FIELD_KEYS

# The two touchpoints this module routes to (see seed.SEED_TOUCHPOINTS).
TOUCHPOINT_VISUAL = "invoice_extraction_visual"
TOUCHPOINT_STRUCTURED = "invoice_extraction_structured"


def touchpoint_for_path(path: str) -> str:
    """The C3-ext touchpoint key a routing decision dispatches to. This is
    the single mapping from the content-based ``visual``/``structured``
    decision to the model assignment that is actually used (and stored per
    upload as ``extraction_path``)."""
    return TOUCHPOINT_VISUAL if path == "visual" else TOUCHPOINT_STRUCTURED

# Cap how much text we send for structured files — invoices are small, and
# a runaway sheet shouldn't blow the context window.
_MAX_TEXT_CHARS = 24000
# Cap pages rendered for a scanned PDF (multi-page invoices are rare; the
# first few pages carry the header + totals).
_MAX_PDF_PAGES = 5

_SYSTEM_PROMPT = (
    "You are an invoice data-extraction engine for an Indian accounting firm. "
    "You read a single invoice (image, scanned PDF, native PDF, Word or Excel) "
    "and return its fields as strict JSON. "
    "Rules you MUST follow:\n"
    "1. Copy values VERBATIM from the document. Never compute, infer, round or "
    "invent a value. If a field is not present in the document, return null for "
    "its value and 0 for its confidence.\n"
    "2. Give a per-field confidence from 0-100 reflecting how clearly you could "
    "read that specific field. Do NOT give one document-level score.\n"
    "3. Amounts: return the number as printed, without currency symbols or "
    "thousands separators (e.g. 9345.60). Tax RATES (CGST/SGST/IGST): return "
    "just the percentage number (e.g. 9 for 9%, 2.5 for 2.5%) — never a "
    "fraction and never with a '%' sign. Dates: return as printed.\n"
    "4. GSTIN must be the 15-character GSTIN of the SELLER/vendor (the party "
    "issuing the invoice), not the buyer.\n"
    "5. Respond with JSON ONLY — no prose, no markdown fences."
)

_FIELD_GUIDE = {
    "invoice_number": "the invoice/bill number",
    "invoice_date": "the invoice date",
    "vendor_name": "the seller/supplier name (the party issuing the invoice)",
    "vendor_gstin": "the seller's 15-character GSTIN",
    "hsn_sac": "the HSN or SAC code of the first line item",
    "item_description": "the description of the first line item",
    "quantity": "the quantity of the first line item",
    "rate": "the unit rate of the first line item",
    "taxable_value": "the total taxable value (before tax)",
    "cgst_amount": "the CGST amount",
    "cgst_rate": "the CGST rate as a percentage (e.g. 9 for 9%)",
    "sgst_amount": "the SGST amount",
    "sgst_rate": "the SGST rate as a percentage (e.g. 9 for 9%)",
    "igst_amount": "the IGST amount",
    "igst_rate": "the IGST rate as a percentage (e.g. 18 for 18%)",
    "total_tax": "the total tax (CGST+SGST+IGST)",
    "invoice_total": "the grand total / invoice total",
    "place_of_supply": "the place of supply",
    "reference_po": "the reference / PO number",
}


def _user_prompt(
    *, filename: str, source_format: str, text: Optional[str] = None, with_bbox: bool = False,
) -> str:
    def _line(k: str) -> str:
        guide = _FIELD_GUIDE[k]
        if with_bbox:
            return (
                f'  "{k}": {{"value": <string or null>, "confidence": <0-100>, '
                f'"source": "<where you found it, e.g. \'header\', \'totals block\', \'line item 1\'>", '
                f'"bbox": {{"x": <0-1>, "y": <0-1>, "w": <0-1>, "h": <0-1>, "page": <1-based page number>}}}}'
                f"   // {guide}"
            )
        return (
            f'  "{k}": {{"value": <string or null>, "confidence": <0-100>, '
            f'"source": "<where you found it, e.g. \'header\', \'totals block\', \'line item 1\'>"}}'
            f"   // {guide}"
        )

    fields = "\n".join(_line(k) for k in CANONICAL_FIELD_KEYS)
    parts = [
        f"Extract the fields below from this invoice (filename: {filename!r}, "
        f"format: {source_format}).",
        "",
        "Return exactly this JSON shape:",
        "{",
        '  "fields": {',
        fields,
        "  }",
        "}",
    ]
    if with_bbox:
        parts += [
            "",
            "For each field you DO find, also return a bounding box locating it on the "
            "page image: x/y are the top-left corner and w/h the size, all as FRACTIONS "
            "of the page width/height (a number between 0 and 1 — NOT pixels), and page "
            "is the 1-based page number. "
            "If you cannot confidently localize a field, OMIT its bbox entirely — "
            "never guess coordinates.",
        ]
    if text is not None:
        parts += [
            "",
            "The invoice's extracted text content follows between the markers. "
            "Read the fields from it:",
            "----- BEGIN INVOICE TEXT -----",
            text[:_MAX_TEXT_CHARS],
            "----- END INVOICE TEXT -----",
        ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_with_ai(
    *, filename: str, file_bytes: bytes, source_format: str, db_path=None,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Extract every canonical field using the AI layer.

    Returns ``(field_results, extraction_path, meta)`` where ``meta`` is::

        {
          "ai_used": bool,
          "model_id": str | None,
          "latency_ms": int | None,
          "ai_error": str | None,   # human-readable reason AI was not used
        }

    Never raises for a model failure — the caller decides whether to fall
    back to the deterministic extractor.
    """
    from src.ingestion_ai import llm

    path = _route(source_format, file_bytes)
    touchpoint = touchpoint_for_path(path)

    try:
        if path == "visual":
            images = _images_for(filename, file_bytes, source_format)
            if not images:
                raise llm.LLMError("config_error|no renderable page image for this file")
            parsed, latency_ms, model_id, _raw = llm.call_llm_vision_json(
                _SYSTEM_PROMPT,
                _user_prompt(filename=filename, source_format=source_format, with_bbox=True),
                images,
                db_path=db_path,
                touchpoint_key=touchpoint,
            )
        else:
            text = _text_for(filename, file_bytes, source_format)
            parsed, latency_ms, model_id, _raw = llm.call_llm_json(
                _SYSTEM_PROMPT,
                _user_prompt(filename=filename, source_format=source_format, text=text),
                db_path=db_path,
                touchpoint_key=touchpoint,
            )
    except Exception as exc:  # noqa: BLE001 — any failure => caller falls back
        return [], path, {
            "ai_used": False,
            "model_id": None,
            "latency_ms": None,
            "ai_error": _humanize_error(exc),
        }

    results = _normalize_results(parsed, with_bbox=(path == "visual"))
    if path == "visual":
        # Some vision models return pixel coordinates despite being asked for
        # fractions. Normalize them against the actual page image so the
        # highlight overlay still lands correctly.
        _rescale_pixel_bboxes(results, images)
    return results, path, {
        "ai_used": True,
        "model_id": model_id,
        "latency_ms": latency_ms,
        "ai_error": None,
    }


# ---------------------------------------------------------------------------
# Routing + content preparation
# ---------------------------------------------------------------------------


def _route(source_format: str, file_bytes: bytes) -> str:
    """visual vs structured, matching extractor.extraction_path_for().

    Classification is by ACTUAL CONTENT, never by file extension alone. For
    a PDF that means probing whether it truly carries an extractable text
    layer: a native-text PDF goes STRUCTURED, an image-only / scanned PDF
    (the ``.pdf`` extension but no real text) goes VISUAL.
    """
    if source_format == "image":
        return "visual"
    if source_format == "pdf":
        return "structured" if extractor.has_text_layer(file_bytes) else "visual"
    return "structured"


def _touchpoint_model(touchpoint: str, *, db_path=None) -> Optional[str]:
    """The model assigned to this touchpoint in the central registry, or
    None to let the LLM client use its own configured default."""
    try:
        from src.ai_models import service as ai_models

        return ai_models.effective_model(touchpoint, db_path=db_path)
    except Exception:  # noqa: BLE001
        return None


def _images_for(filename: str, file_bytes: bytes, source_format: str) -> list[tuple[str, bytes]]:
    """Render the file to one or more (mime, bytes) page images for the
    vision path. Images pass through; scanned PDFs are rasterised."""
    if source_format == "image":
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        mime = "image/png" if ext == "png" else "image/jpeg"
        return [(mime, file_bytes)]
    if source_format == "pdf":
        return _render_pdf_pages(file_bytes)
    return []


def _render_pdf_pages(file_bytes: bytes) -> list[tuple[str, bytes]]:
    """Rasterise a PDF's pages to PNG using PyMuPDF. Returns [] if PyMuPDF
    isn't available or the file can't be opened (caller then falls back)."""
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        return []
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001
        return []
    pages: list[tuple[str, bytes]] = []
    try:
        for page in doc[: _MAX_PDF_PAGES]:
            pix = page.get_pixmap(dpi=150)
            pages.append(("image/png", pix.tobytes("png")))
    except Exception:  # noqa: BLE001
        return pages
    finally:
        doc.close()
    return pages


def _text_for(filename: str, file_bytes: bytes, source_format: str) -> str:
    """The text content sent for the structured path."""
    if source_format == "pdf":
        return extractor._pdf_text(file_bytes)
    if source_format == "word":
        return extractor._docx_text(file_bytes, filename)
    if source_format == "excel":
        df = extractor._read_table(file_bytes)
        if df is None or df.empty:
            return ""
        return extractor._flatten_sheet_to_text(df)
    return ""


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------


def _normalize_results(parsed: dict[str, Any], *, with_bbox: bool = False) -> list[dict[str, Any]]:
    """Turn the model's ``{"fields": {...}}`` reply into the module's
    canonical per-field result list. Any field the model omitted or left
    null is reported "not present" — never invented.

    ``with_bbox`` is True only for the VISUAL path (the model was asked for
    coordinates). A missing or malformed bbox is dropped rather than
    guessed — a wrong highlight is worse than no highlight.
    """
    raw_fields = parsed.get("fields") if isinstance(parsed, dict) else None
    if not isinstance(raw_fields, dict):
        raw_fields = {}

    out: list[dict[str, Any]] = []
    for key in CANONICAL_FIELD_KEYS:
        entry = raw_fields.get(key)
        value, confidence, source, bbox = _read_entry(entry, with_bbox=with_bbox)
        if value is None or value == "":
            out.append(_absent(key))
            continue
        row: dict[str, Any] = {
            "field_name": key,
            "extracted_value": value,
            "confidence": confidence,
            "source_location": source or "AI extraction",
            "is_present": True,
        }
        if bbox is not None:
            row.update(bbox)
        out.append(row)
    return out


def _read_entry(
    entry: Any, *, with_bbox: bool = False,
) -> tuple[Optional[str], Optional[int], Optional[str], Optional[dict[str, Any]]]:
    """Accept either the documented ``{"value","confidence","source"}``
    object or a bare scalar (some models simplify), and coerce to
    (value, confidence, source, bbox). ``bbox`` is a validated
    ``{bbox_x,bbox_y,bbox_w,bbox_h,bbox_page}`` dict or None."""
    if entry is None:
        return None, None, None, None
    if isinstance(entry, dict):
        value = entry.get("value")
        confidence = entry.get("confidence")
        source = entry.get("source")
        raw_bbox = entry.get("bbox")
    else:
        value, confidence, source, raw_bbox = entry, None, None, None

    bbox = _validate_bbox(raw_bbox) if with_bbox else None

    if value is None:
        return None, None, source if isinstance(source, str) else None, bbox
    value = str(value).strip()
    if value == "" or value.lower() in ("null", "none", "n/a", "na", "-"):
        return None, None, source if isinstance(source, str) else None, bbox

    conf: Optional[int]
    try:
        conf = int(round(float(confidence)))
    except (TypeError, ValueError):
        conf = None
    if conf is not None:
        conf = max(0, min(100, conf))
    return value, conf, source if isinstance(source, str) else None, bbox


def _validate_bbox(raw: Any) -> Optional[dict[str, Any]]:
    """Coerce a model-supplied bbox into a usable box, or None.

    Accepts either 0-1 fractions or pixel coordinates (some vision models
    answer in pixels despite the prompt) — ``_rescale_pixel_bboxes``
    normalizes the latter against the real page size afterwards. Rejects
    non-finite / negative / zero-size boxes: a hallucinated box would
    mislead the reviewer, so an unusable one is dropped (the field simply
    has no highlight).
    """
    if not isinstance(raw, dict):
        return None
    try:
        x = float(raw.get("x"))
        y = float(raw.get("y"))
        w = float(raw.get("w"))
        h = float(raw.get("h"))
    except (TypeError, ValueError):
        return None
    for v in (x, y, w, h):
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
            return None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return None
    try:
        page = int(raw.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1
    return {"bbox_x": x, "bbox_y": y, "bbox_w": w, "bbox_h": h, "bbox_page": page}


def _rescale_pixel_bboxes(results: list[dict[str, Any]], images: list[tuple[str, bytes]]) -> None:
    """Normalize any bbox that came back in PIXELS rather than fractions.

    The prompt asks for 0-1 fractions, but some vision models answer in pixel
    coordinates anyway. A box whose x/y/w/h exceed 1 is unambiguously pixels,
    so divide by the page image's real dimensions. Boxes already in 0-1 are
    left untouched. Mutates ``results`` in place; a box that still can't be
    normalized is dropped rather than guessed.
    """
    if not images:
        return
    try:
        from PIL import Image
        from io import BytesIO

        with Image.open(BytesIO(images[0][1])) as img:
            page_w, page_h = img.size
    except Exception:  # noqa: BLE001 — no PIL / unreadable image => can't rescale
        return
    if not page_w or not page_h:
        return
    for f in results:
        if f.get("bbox_x") is None:
            continue
        x, y = f["bbox_x"], f["bbox_y"]
        w, h = f["bbox_w"], f["bbox_h"]
        if max(x, y, w, h) <= 1.0:
            continue  # already fractions
        nx, ny = x / page_w, y / page_h
        nw, nh = w / page_w, h / page_h
        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0 and 0.0 < nw <= 1.0 and 0.0 < nh <= 1.0):
            # Still unusable after rescaling — drop it rather than mislead.
            for k in ("bbox_x", "bbox_y", "bbox_w", "bbox_h", "bbox_page"):
                f.pop(k, None)
            continue
        f["bbox_x"], f["bbox_y"] = nx, ny
        f["bbox_w"], f["bbox_h"] = nw, nh


def _absent(field: str) -> dict[str, Any]:
    return {
        "field_name": field,
        "extracted_value": None,
        "confidence": None,
        "source_location": None,
        "is_present": False,
    }


def _humanize_error(exc: Exception) -> str:
    """Turn an LLMError's ``type|detail`` message into a short, honest
    reason for the UI. Never leaks the API key (it isn't in the message)."""
    msg = str(exc)
    if "|" in msg:
        kind, detail = msg.split("|", 1)
        kind = kind.strip()
        detail = detail.strip()
        if kind == "config_error":
            return f"AI not configured — {detail}"
        if kind == "auth_error":
            return "AI provider rejected the API key."
        if kind == "rate_limit":
            return "AI provider rate limit reached — try again shortly."
        if kind == "timeout":
            return "AI request timed out."
        if kind == "parse_error":
            return "AI reply could not be parsed."
        return f"AI error — {detail[:200]}"
    return f"AI error — {msg[:200]}"


# ---------------------------------------------------------------------------
# Preview helper (used by the review screen for scanned PDFs)
# ---------------------------------------------------------------------------


def first_page_png(file_bytes: bytes) -> Optional[bytes]:
    """Render a PDF's first page to PNG for the review screen's source
    preview. Returns None when PyMuPDF is unavailable or the file can't be
    opened."""
    pages = _render_pdf_pages(file_bytes)
    return pages[0][1] if pages else None


def data_uri(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _strip_currency(value: str) -> str:
    """Not applied to extracted values (they are stored verbatim) — kept
    for callers that want a numeric form. Exposed for tests."""
    return re.sub(r"[^\d.\-]", "", value)
