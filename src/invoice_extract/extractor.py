"""F3-B extraction engine — the AI extraction stand-in.

Per the F3-B build prompt this module is the invoice digitization engine:
a fixed canonical field set, extracted with a PER-FIELD confidence score
(never a single document-level score), routed by source format into one
of two model touchpoints:

  * VISUAL touchpoint     — images, scanned / image-based PDFs. In a real
                            deployment this calls a vision-capable model
                            (C3-ext touchpoint `invoice_extraction_visual`).
  * STRUCTURED touchpoint — native-text PDF, Excel, Word. Text/table
                            parsing (C3-ext touchpoint
                            `invoice_extraction_structured`).

This repo has no LLM budget for a per-file model call — consistent with
the rest of the codebase (see src/documents/service.py's
classify_document() and src/ingestion_ai/mapper.py, which are the same
kind of deliberately-simple stand-in for a real AI call). This module is
that stand-in: it reads the GENUINE text signal available in each format
(Excel/CSV cells, DOCX body text, a PDF's text layer, or filename/embedded
text for images), then applies label/pattern matching to populate the
canonical fields.

Honesty notes (core business rules from the build prompt):
  * A field with NO matching signal in the source is left ``is_present=False``
    with ``confidence=None`` — "not present", NEVER invented/fabricated.
  * Confidence is per-field and reflects how cleanly the signal matched:
    strict pattern/header matches score high (>=90, auto-accepted);
    free-text fields (vendor name, line description) and fuzzy header
    matches score in the review band (<90), routing the upload into this
    module's Review Queue. That band is deliberately realistic — vendor
    names and line descriptions are the genuinely hard fields.
  * The observed band is only ever compared against the ADMIN-CONFIGURABLE
    threshold in service.py — this module never hardcodes 90.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import Any, Optional

import pandas as pd
from rapidfuzz import fuzz

from src.invoice_extract.seed import CANONICAL_FIELD_KEYS

# ---------------------------------------------------------------------------
# Format detection (the four accepted source formats)
# ---------------------------------------------------------------------------

IMAGE_EXTS = {"jpg", "jpeg", "png"}
PDF_EXTS = {"pdf"}
EXCEL_EXTS = {"xls", "xlsx", "csv"}
WORD_EXTS = {"doc", "docx"}

# Header-alias vocabulary per canonical field, used only for STRUCTURED
# files (column-header matching). Mirrors ingestion_ai/mapper.py's approach.
_COLUMN_ALIASES: dict[str, list[str]] = {
    "invoice_number": ["invoice no", "invoice number", "inv no", "bill no", "voucher no", "voucher number", "invoice #"],
    "invoice_date": ["invoice date", "date", "bill date", "voucher date"],
    "vendor_name": ["vendor", "supplier", "party name", "party ledger name", "seller", "vendor name", "supplier name"],
    "vendor_gstin": ["gstin", "vendor gstin", "party gstin", "gst no", "gst number"],
    "hsn_sac": ["hsn", "sac", "hsn sac", "hsn code", "sac code"],
    "item_description": ["description", "item", "particulars", "item description", "product", "line description"],
    "quantity": ["qty", "quantity", "nos", "units"],
    "rate": ["rate", "unit rate", "price", "unit price"],
    "taxable_value": ["taxable value", "taxable amount", "subtotal", "sub total", "net amount", "taxable"],
    "cgst_amount": ["cgst", "cgst amount", "central tax"],
    "sgst_amount": ["sgst", "sgst amount", "state tax"],
    "igst_amount": ["igst", "igst amount", "integrated tax"],
    "total_tax": ["total tax", "tax amount", "gst amount", "total gst"],
    "invoice_total": ["invoice total", "grand total", "total amount", "amount payable", "total", "voucher total"],
    "place_of_supply": ["place of supply", "pos", "supply state"],
    "reference_po": ["po no", "po number", "reference", "ref no", "order no", "purchase order"],
}

# Header-alias score floor below which a column isn't considered a match at
# all (the field is then "not present", never a guess).
_COLUMN_FLOOR = 45

# Label regexes + a confidence for text-based (PDF/Word/image) extraction.
# Fields whose values are inherently free-text (vendor name, description)
# carry a deliberately lower confidence — see the module docstring.
_GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d])\b")
_DATE_RE = re.compile(r"\b(\d{1,4}[\-/\.]\d{1,2}[\-/\.]\d{1,4})\b")
_AMOUNT = r"([\d][\d,]*(?:\.\d{1,2})?)"

# (field, regex, confidence_when_matched, source_location_label)
# Free-text captures (vendor name, description) use a lookahead that stops
# at the next known label so they don't swallow the following field.
_NEXT_LABEL = (
    r"(?=\s+(?:GSTIN|GST\s*No|Invoice|Inv\b|Date|HSN|SAC|Description|Particulars|"
    r"Item|Qty|Quantity|Rate|Taxable|Sub\s*Total|Subtotal|CGST|SGST|IGST|Total|"
    r"Place\s*of\s*Supply|P\.?\s*O\.?|Reference|Ref)\b|$)"
)
_TEXT_LABEL_RULES: list[tuple[str, str, int, str]] = [
    ("invoice_number", rf"(?:invoice|inv|bill|voucher)\s*(?:no|number|#)\.?\s*[:\-]?\s*([A-Za-z0-9\-/]+)", 95, "text match"),
    ("invoice_date", rf"(?:invoice\s*date|bill\s*date|date)\s*[:\-]?\s*{_DATE_RE.pattern}", 93, "text match"),
    ("vendor_gstin", _GSTIN_RE.pattern, 96, "text match"),
    ("vendor_name", rf"(?:vendor|supplier|seller|party|billed?\s*from|from)\s*[:\-]\s*([A-Za-z0-9&.,'\- ]{{3,60}}?){_NEXT_LABEL}", 82, "text match"),
    ("hsn_sac", r"(?:hsn|sac)\s*(?:code)?\s*[:\-]?\s*(\d{4,8})", 94, "text match"),
    ("item_description", rf"(?:description|particulars|item)\s*[:\-]\s*([A-Za-z0-9&.,'\-()/% ]{{3,80}}?){_NEXT_LABEL}", 76, "text match"),
    ("quantity", rf"(?:qty|quantity|nos)\.?\s*[:\-]?\s*{_AMOUNT}", 92, "text match"),
    ("rate", rf"\brate\s*[:\-]?\s*(?:rs\.?\s*)?{_AMOUNT}", 92, "text match"),
    ("taxable_value", rf"(?:taxable\s*(?:value|amount)|sub\s*total|subtotal|net\s*amount)\s*[:\-]?\s*(?:rs\.?\s*)?{_AMOUNT}", 93, "text match"),
    ("cgst_amount", rf"cgst(?:\s*(?:amount|@\s*[\d.]+%))?\s*[:\-]?\s*(?:rs\.?\s*)?{_AMOUNT}", 93, "text match"),
    ("sgst_amount", rf"sgst(?:\s*(?:amount|@\s*[\d.]+%))?\s*[:\-]?\s*(?:rs\.?\s*)?{_AMOUNT}", 93, "text match"),
    ("igst_amount", rf"igst(?:\s*(?:amount|@\s*[\d.]+%))?\s*[:\-]?\s*(?:rs\.?\s*)?{_AMOUNT}", 93, "text match"),
    ("total_tax", rf"total\s*tax\s*[:\-]?\s*(?:rs\.?\s*)?{_AMOUNT}", 93, "text match"),
    ("invoice_total", rf"(?:invoice\s*total|grand\s*total|total\s*amount|amount\s*payable)\s*[:\-]?\s*(?:rs\.?\s*)?{_AMOUNT}", 94, "text match"),
    ("place_of_supply", rf"place\s*of\s*supply\s*[:\-]\s*([A-Za-z][A-Za-z ]{{1,40}}?){_NEXT_LABEL}", 90, "text match"),
    ("reference_po", r"(?:p\.?\s*o\.?|purchase\s*order|reference|ref)\s*(?:no|number|#)?\.?\s*[:\-]?\s*([A-Za-z0-9\-/]{2,30})", 92, "text match"),
]

# Fallback rules applied ONLY when the labelled rule above found nothing —
# for the two highest-signal fields that frequently appear as bare tokens
# (e.g. in a filename or a scanned header). Confidence sits in the review
# band on purpose: a bare token is weaker evidence than a labelled match,
# so a human confirms it. Never applied to a field that already matched.
_FALLBACK_RULES: list[tuple[str, str, int, str]] = [
    ("invoice_number", r"(?<![A-Za-z0-9])((?:INV|BILL|VCH)[\-/]?\d{2,}|[A-Z]{2,4}[\-/]\d{3,})(?![A-Za-z0-9])", 85, "token match"),
    ("invoice_date", r"(?<![A-Za-z0-9])(\d{1,4}[\-/\.]\d{1,2}[\-/\.]\d{1,4})(?![A-Za-z0-9])", 84, "token match"),
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def source_format_for(filename: str) -> str:
    """Map a filename to one of the four accepted source-format tags."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in EXCEL_EXTS:
        return "excel"
    if ext in WORD_EXTS:
        return "word"
    return "other"


def extraction_path_for(source_format: str, *, has_text_layer: bool = True) -> str:
    """Route by source format to one of the two C3-ext touchpoints:
    image / image-based PDF -> 'visual'; native-text PDF / Excel / Word ->
    'structured'. A PDF with no usable text layer is image-based."""
    if source_format == "image":
        return "visual"
    if source_format == "pdf":
        return "structured" if has_text_layer else "visual"
    if source_format in ("excel", "word"):
        return "structured"
    return "structured"


def extract(
    *, filename: str, file_bytes: bytes, source_format: str,
) -> tuple[list[dict[str, Any]], str]:
    """Extract every canonical field, with a per-field confidence.

    Returns ``(field_results, extraction_path)`` where each field result is:
        {
          "field_name": str,
          "extracted_value": str | None,
          "confidence": int | None,      # None => genuinely absent ("not present")
          "source_location": str | None, # page / cell / row reference
          "is_present": bool,
        }
    """
    if source_format == "excel":
        return _extract_structured_excel(file_bytes), "structured"
    if source_format == "word":
        text = _docx_text(file_bytes, filename)
        return _extract_from_text(text), "structured"
    if source_format == "pdf":
        text = _pdf_text(file_bytes)
        if _has_meaningful_text(text):
            return _extract_from_text(text), "structured"
        # Image-based (scanned) PDF — no usable text layer: visual path.
        signal = _visual_signal(filename, file_bytes, text)
        return _extract_from_text(signal), "visual"
    if source_format == "image":
        signal = _visual_signal(filename, file_bytes, "")
        return _extract_from_text(signal), "visual"
    # Unknown/other — nothing extracted; every field not present.
    return _all_absent(), "structured"


# ---------------------------------------------------------------------------
# STRUCTURED path — Excel / CSV column-header matching
# ---------------------------------------------------------------------------


def _extract_structured_excel(file_bytes: bytes) -> list[dict[str, Any]]:
    df = _read_table(file_bytes)
    if df is None or df.empty:
        return _all_absent()

    headers = [str(h) for h in df.columns]
    # Use the first data row as the representative value for each matched
    # column (invoices are typically one row; multi-row line items take the
    # first line, with the full column available for export).
    first_row = df.iloc[0]

    results: list[dict[str, Any]] = []
    used_headers: set[str] = set()
    for field in CANONICAL_FIELD_KEYS:
        aliases = _COLUMN_ALIASES.get(field, [])
        best_header, best_score = None, 0
        for h in headers:
            if h in used_headers:
                continue
            for alias in aliases:
                score = fuzz.token_sort_ratio(_norm(h), _norm(alias))
                if score > best_score:
                    best_header, best_score = h, score
        if best_header is None or best_score < _COLUMN_FLOOR:
            results.append(_absent(field))
            continue
        used_headers.add(best_header)
        raw_val = first_row.get(best_header)
        val = _clean_cell(raw_val)
        if val is None:
            results.append(_absent(field))
            continue
        # Confidence = header-alias score, mapped into a realistic band:
        # an exact header match scores 96; a looser alias match scores lower
        # (and can fall into the review band, routing the upload for review).
        if best_score >= 100:
            conf = 96
        elif best_score >= 90:
            conf = 88 + (best_score - 90)  # 88-97
        else:
            conf = int(best_score)
        results.append({
            "field_name": field,
            "extracted_value": val,
            "confidence": conf,
            "source_location": f"column '{best_header}'",
            "is_present": True,
        })
    return results


def _read_table(file_bytes: bytes) -> Optional[pd.DataFrame]:
    ext = _sniff_ext(file_bytes)
    try:
        if ext == "csv":
            return pd.read_csv(BytesIO(file_bytes), dtype=str, keep_default_na=False)
        return pd.read_excel(BytesIO(file_bytes), dtype=str)
    except Exception:  # noqa: BLE001
        try:
            return pd.read_csv(BytesIO(file_bytes), dtype=str, keep_default_na=False)
        except Exception:  # noqa: BLE001
            return None


def _sniff_ext(file_bytes: bytes) -> str:
    # xlsx/xls are zip/OLE; everything else is treated as CSV.
    if file_bytes[:4] == b"PK\x03\x04":
        return "xlsx"
    if file_bytes[:4] == b"\xd0\xcf\x11\xe0":
        return "xls"
    return "csv"


def _clean_cell(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none", "nat"):
        return None
    return s


# ---------------------------------------------------------------------------
# Text paths (PDF text layer / Word body / visual signal)
# ---------------------------------------------------------------------------


def _extract_from_text(text: str) -> list[dict[str, Any]]:
    if not text or not text.strip():
        return _all_absent()
    flat = re.sub(r"\s+", " ", text)

    results: list[dict[str, Any]] = []
    found: set[str] = set()
    for field, pattern, conf, loc in _TEXT_LABEL_RULES:
        m = re.search(pattern, flat, re.IGNORECASE)
        if not m:
            results.append(_absent(field))
            continue
        raw = m.group(1).strip().rstrip(".,;:")
        if not raw:
            results.append(_absent(field))
            continue
        found.add(field)
        results.append({
            "field_name": field,
            "extracted_value": raw,
            "confidence": conf,
            "source_location": loc,
            "is_present": True,
        })

    # Fallback pass for the two highest-signal fields, only where the
    # labelled rule found nothing (e.g. a bare token in a filename).
    by_name = {r["field_name"]: r for r in results}
    for field, pattern, conf, loc in _FALLBACK_RULES:
        if field in found:
            continue
        m = re.search(pattern, flat, re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1).strip().rstrip(".,;:")
        if not raw:
            continue
        by_name[field] = {
            "field_name": field,
            "extracted_value": raw,
            "confidence": conf,
            "source_location": loc,
            "is_present": True,
        }
    return [by_name[f] for f in CANONICAL_FIELD_KEYS]


def _docx_text(file_bytes: bytes, filename: str) -> str:
    """Extract body text from a .docx (a zip with word/document.xml).
    Legacy .doc (binary) yields nothing usable — treated as no signal."""
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        # Strip tags; keep text runs separated.
        text = re.sub(r"</w:p>", "\n", xml)
        text = re.sub(r"<[^>]+>", " ", text)
        return text
    except Exception:  # noqa: BLE001
        return ""


def _pdf_text(file_bytes: bytes) -> str:
    """Best-effort extraction of a PDF's text layer: decompress content
    streams and pull parenthesized text operators. A PDF with no
    decompressible text is treated as image-based (scanned)."""
    import zlib

    parts: list[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", file_bytes, re.S):
        raw = m.group(1)
        try:
            dec = zlib.decompress(raw)
        except Exception:  # noqa: BLE001
            dec = raw
        s = dec.decode("latin-1", "ignore")
        for t in re.finditer(r"\((?:[^()\\]|\\.)*\)", s):
            frag = t.group(0)[1:-1]
            frag = frag.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
            if frag.strip():
                parts.append(frag)
    return " ".join(parts)


def _has_meaningful_text(text: str) -> bool:
    return len(re.sub(r"[^A-Za-z0-9]", "", text or "")) >= 20


def _visual_signal(filename: str, file_bytes: bytes, pdf_text: str) -> str:
    """The genuine text signal available to the VISUAL path without an OCR
    step: the filename (which in real workflows frequently carries vendor +
    invoice no + date), any PDF text layer, and short printable runs
    embedded in the bytes. No field is ever synthesized from a hash — a
    field only appears if its value genuinely exists in this signal."""
    pieces: list[str] = [filename, filename.replace("_", " ").replace("-", " ")]
    if pdf_text:
        pieces.append(pdf_text)
    # Pull printable ASCII runs (e.g. embedded XMP/EXIF text) as extra signal.
    runs = re.findall(rb"[ -~]{6,}", file_bytes)
    joined = " ".join(r.decode("latin-1", "ignore") for r in runs[:40])
    # Keep only token-ish content to avoid noise.
    pieces.append(" ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9&.,:\-/ ]{2,}", joined))[:2000])
    return " ".join(pieces)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).strip().lower()).strip()


def _absent(field: str) -> dict[str, Any]:
    return {
        "field_name": field,
        "extracted_value": None,
        "confidence": None,
        "source_location": None,
        "is_present": False,
    }


def _all_absent() -> list[dict[str, Any]]:
    return [_absent(f) for f in CANONICAL_FIELD_KEYS]