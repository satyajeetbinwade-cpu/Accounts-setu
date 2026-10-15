"""Invoice Extraction **triage** (Phase 1) — "one file, many documents".

Today one uploaded file is one record and the visual path reads only the first
five pages. This module is the foundation for the model that replaces that: a
page-and-region document model plus a triage pass that finds EVERY document in
a file, categorises it, and records where it sits on the page.

WHAT THIS MODULE DOES (and does NOT):
  * It reads one rendered page at a time and asks the model to find each
    separate document on it, categorise it, box it, label it, say whether it
    continues onto the next page, and quote a short evidence snippet.
  * It NEVER extracts field values. Field extraction, the review screen,
    direction detection and export are untouched by this phase.
  * It is OFF by default (``invoice_extract.triage.enabled`` = false) and
    nothing in the normal upload path calls it. Only the CLI / service API do.

Invariants this module guarantees:
  * NOTHING VANISHES — every page ends up either inside at least one document
    region or in the unassigned list with a reason (see ``_assert_coverage``).
  * A page whose reply is empty or invalid is marked FAILED, never silently
    empty (retry once, then fail; the run becomes "partial").
  * A wrong merge is worse than a wrong split, so the prompt tells the model to
    SPLIT when unsure and flag ``unsure_split`` — later checks can catch a bad
    split, but a merge mixes values between invoices.
  * ``excluded`` defaults to true ONLY for ``not_a_voucher`` and is always
    user-reversible; a category never auto-excludes anything else.

CLI::

    python -m src.invoice_extract.triage run <upload_id>
    python -m src.invoice_extract.triage score <pdf>

The ``score`` command is a LIVE scoring harness: it runs triage against a COPY
of the database and a temporary data root (never the real ``db/`` or ``data/``),
compares the result with the provisional expected bundle, and writes a Markdown
report plus annotated page PNGs to the ignored report directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src import db as recon_db
from src.ai_models import service as ai_models
from src.invoice_extract import db as idb
from src.invoice_extract.service import InvoiceExtractError, _connect

TOUCHPOINT = "invoice_triage"

# The five categories, in the order the prompt lists them. ``not_a_voucher`` is
# the only one that defaults to excluded.
CATEGORIES: tuple[str, ...] = (
    "tax_invoice",
    "no_gst_expense",
    "supporting",
    "ticket",
    "not_a_voucher",
)
DEFAULT_EXCLUDED_CATEGORY = "not_a_voucher"

#: The flag the model uses for a page that repeats a page already read in the
#: same bundle — a COPY of the same paper, not a continuation of it. Named here
#: because the prompt asks for it by name AND ``_stitch`` reads it back, so the
#: two must stay in step (the prompt interpolates this constant).
DUPLICATE_PAGE_FLAG = "duplicate_page"

CATEGORY_DEFINITIONS: dict[str, str] = {
    "tax_invoice": "a GST tax invoice — it shows a GSTIN and/or CGST/SGST/IGST tax.",
    "no_gst_expense": "an expense document with no GSTIN and no tax shown (e.g. a plain bill or cash memo).",
    "supporting": "a statement, receipt, email, letter or report — evidence that is not itself an invoice.",
    "ticket": "a travel ticket (air, rail, bus).",
    "not_a_voucher": "something that SAYS it is not a tax invoice, or that has no accounting content at all "
                     "(e.g. a courier docket, a packing slip, a blank/banner page).",
}

# A valid box must have a positive width and height of at least this fraction of
# the page — a hairline or a point is not a document.
MIN_BOX_SIDE = 0.005
# Two pages whose 64-bit average hashes differ by at most this many bits are
# near-identical. Flag only — never delete or merge.
DUPLICATE_HAMMING = 6

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "invoice_triage"
DEFAULT_EXPECTED = REPO_ROOT / "tests" / "fixtures" / "invoice_extract" / "batch_apr2026_expected.json"

# Settings keys (C3 flat key/value store). Defaults match the task spec.
KEY_ENABLED = "invoice_extract.triage.enabled"
KEY_DPI = "invoice_extract.triage.dpi"
KEY_MAX_LONG_EDGE = "invoice_extract.triage.max_long_edge_px"
KEY_CONCURRENCY = "invoice_extract.triage.concurrency"
KEY_TIMEOUT = "invoice_extract.triage.timeout_seconds"
KEY_RETRIES = "invoice_extract.triage.retries"
KEY_MAX_PAGES = "invoice_extract.triage.max_pages"

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_COMPLETE = "complete"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_INTERRUPTED = "interrupted"

# Terminal states — a run in one of these is finished (or was abandoned).
RUN_TERMINAL = frozenset({RUN_COMPLETE, RUN_PARTIAL, RUN_FAILED, RUN_INTERRUPTED})

# Triage asks the gateway to retry a transient failure on the SAME leg (429 /
# timeout / 5xx, exponential backoff honouring Retry-After) before failing over.
TRIAGE_RETRY_POLICY: dict[str, Any] = {"attempts": 3, "base_delay": 1.0, "max_delay": 10.0, "jitter": 0.5}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict_row(cur: sqlite3.Cursor, row: Any) -> Optional[dict[str, Any]]:
    """A dict for one row. The shared app connection has no ``sqlite3.Row``
    factory, so convert via the cursor's own description."""
    if row is None:
        return None
    return {c[0]: row[i] for i, c in enumerate(cur.description)}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriageConfig:
    enabled: bool = False
    dpi: int = 110
    max_long_edge_px: int = 1600
    concurrency: int = 4
    timeout_seconds: int = 120
    retries: int = 1
    max_pages: int = 200

    def as_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dpi": self.dpi,
            "max_long_edge_px": self.max_long_edge_px,
            "concurrency": self.concurrency,
            "timeout_seconds": self.timeout_seconds,
            "retries": self.retries,
            "max_pages": self.max_pages,
        }


def _setting_int(key: str, default: int, db_path) -> int:
    try:
        from src.settings import service as settings

        return int(settings.get_setting_int(key, default=default, db_path=db_path))
    except Exception:  # noqa: BLE001 — settings must never break triage
        return default


def _setting_str(key: str, default: Optional[str], db_path) -> Optional[str]:
    try:
        from src.settings import service as settings

        return settings.get_setting_str(key, default=default, db_path=db_path)
    except Exception:  # noqa: BLE001
        return default


def _setting_bool(key: str, default: bool, db_path) -> bool:
    raw = _setting_str(key, None, db_path)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def get_config(*, db_path=None) -> TriageConfig:
    """The triage settings, all read from C3 with the spec's defaults."""
    return TriageConfig(
        enabled=_setting_bool(KEY_ENABLED, False, db_path),
        dpi=max(36, _setting_int(KEY_DPI, 110, db_path)),
        max_long_edge_px=max(200, _setting_int(KEY_MAX_LONG_EDGE, 1600, db_path)),
        concurrency=max(1, _setting_int(KEY_CONCURRENCY, 4, db_path)),
        timeout_seconds=max(10, _setting_int(KEY_TIMEOUT, 120, db_path)),
        retries=max(0, _setting_int(KEY_RETRIES, 1, db_path)),
        max_pages=max(1, _setting_int(KEY_MAX_PAGES, 200, db_path)),
    )


# ---------------------------------------------------------------------------
# The per-page prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a document-triage engine for an Indian accounting firm. You see "
    "ONE page image from a scanned bundle: a page may hold several documents, or "
    "one may span pages. Do NOT read field values — find the documents, say "
    "what each IS and where it sits. Respond with JSON only.\n"
    "Rules:\n"
    "1. A document is everything ONE issuer printed under one document number or "
    "title, its sections included: ONE document, ONE box per page it occupies, "
    "covering it fully.\n"
    "2. Terms, fare rules, baggage allowances, hazardous-goods notices, "
    "promotional/in-flight text, bill summaries, plan/service details, payment "
    "instructions, headers, footers, banners belong to their document, never "
    "returned separately. A page of only that material continues that document.\n"
    "3. Handwritten notes, stamps and signatures on or beside a document are part "
    "of it, never separate.\n"
    "4. Split a page only for clearly different pieces of paper, or a different "
    "issuer or document number — e.g. a courier slip, a shop slip and a cash memo "
    "on one sheet. The SAME issuer is ONE document; most pages hold ONE, more "
    "than three is rare — double-check. If two items could differ in issuer or "
    "number, return TWO and add the flag \"unsure_split\".\n"
    "5. Categorise each as exactly one of: tax_invoice, no_gst_expense, "
    "supporting, ticket, not_a_voucher. Statements and summaries — bank, card, "
    "loan, credit-limit, payable, telecom and utility monthly or summary "
    "statements, ledgers, passbooks — are supporting, or not_a_voucher if only "
    "a notice, NEVER tax_invoice/no_gst_expense even with charges or GST; a page "
    "saying it is a summary, is not a tax invoice, or that input tax credit is "
    "not available on it is never tax_invoice. One bill seen twice — as a "
    "statement and as the real Tax Invoice (titled Tax Invoice, with the "
    "issuer's GST registration number and a tax breakup) — is tax_invoice only "
    "for the real one: the statement is supporting, its history tables, payment "
    "details and advertisements included.\n"
    "6. Give each a SHORT label read from the document (e.g. an invoice number, "
    "or \"Ajay General Store slip\").\n"
    "7. continues_next=true when the totals, signature or closing section are NOT "
    "on this page; continues_previous=true when this page has no issuer header or "
    "number of its own and continues a table or totals.\n"
    "8. rotation_deg: clockwise degrees (0, 90, 180, 270) to turn the page "
    "upright; boxes are on the UPRIGHT page.\n"
    "9. Boxes: x, y and w, h as FRACTIONS of the page width/height (0 to 1, "
    "top-left).\n"
    "10. Include a SHORT quoted evidence snippet for the category.\n"
    "11. A blank page: set page_is_blank and return no documents (rule-2 material "
    "is not blank).\n"
    f"12. A page repeating an earlier page EXACTLY and adding nothing new (same "
    f"issuer, number and figures) is a COPY: return it with "
    f"continues_previous=false, flagged \"{DUPLICATE_PAGE_FLAG}\". Continuation "
    f"pages, history tables, payment details and adverts are NOT copies.\n"
)

_USER_PROMPT_TEMPLATE = (
    "Page {page} of {total}. Find every document on this page.\n"
    "\n"
    "Category meanings:\n"
    "{cats}\n"
    "\n"
    "Return exactly this JSON shape:\n"
    "{{\n"
    '  "rotation_deg": 0,\n'
    '  "page_is_blank": false,\n'
    '  "documents": [\n'
    "    {{\n"
    '      "category": "tax_invoice",\n'
    '      "label": "<short label read from the document>",\n'
    '      "continues_previous": false,\n'
    '      "continues_next": false,\n'
    '      "flags": [],\n'
    '      "evidence": "<short quoted snippet>",\n'
    '      "boxes": [{{"x": 0.1, "y": 0.05, "w": 0.8, "h": 0.3}}]\n'
    "    }}\n"
    "  ]\n"
    "}}"
)


def _user_prompt(page: int, total: int) -> str:
    cats = "\n".join(f"  - {k}: {v}" for k, v in CATEGORY_DEFINITIONS.items())
    return _USER_PROMPT_TEMPLATE.format(page=page, total=total, cats=cats)


# ---------------------------------------------------------------------------
# Page rendering + signals
# ---------------------------------------------------------------------------


@dataclass
class RenderedPage:
    page: int
    png: bytes
    px_w: int
    px_h: int
    text_layer_chars: int
    content_hash: str
    grey: Any = None  # PIL.Image (L), kept for blank detection


def _pdf_page_count(file_bytes: bytes) -> int:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def _average_hash(grey: Any) -> str:
    """Deterministic 64-bit average hash of a normalised greyscale page, as hex."""
    small = grey.convert("L").resize((8, 8))
    pixels = list(small.tobytes())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, p in enumerate(pixels):
        if p > avg:
            bits |= 1 << i
    return f"{bits:016x}"


def _looks_blank(grey: Any) -> bool:
    """A page with essentially no dark ink is blank. A cheap deterministic
    guard checked at full resolution (downscaling can erase faint content)."""
    hist = grey.convert("L").histogram()
    total = sum(hist) or 1
    dark = sum(hist[:200])
    return (dark / total) < 0.001


def _page_total_and_text(file_bytes: bytes, source_format: str) -> tuple[int, int]:
    """(page_count, text_layer_chars) without rendering."""
    if source_format == "pdf":
        import fitz

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            text = "".join((doc.load_page(i).get_text("text") or "") for i in range(doc.page_count))
            return int(doc.page_count), len(text)
        finally:
            doc.close()
    return 1, 0


def _render_page(file_bytes: bytes, source_format: str, page: int, cfg: TriageConfig) -> RenderedPage:
    """Render ONE page to PNG at the configured dpi, clamped by max long edge.

    Only ever one page in memory. The greyscale copy feeds the content hash and
    the blank guard.
    """
    from io import BytesIO

    from PIL import Image

    if source_format == "pdf":
        import fitz

        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            fitz_page = doc.load_page(page - 1)
            text = fitz_page.get_text("text") or ""
            pix = fitz_page.get_pixmap(dpi=cfg.dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
    else:  # image — the whole file is the single page
        img = Image.open(BytesIO(file_bytes)).convert("RGB")
        text = ""

    w, h = img.size
    long_edge = max(w, h)
    if long_edge > cfg.max_long_edge_px:
        scale = cfg.max_long_edge_px / long_edge
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    grey = img.convert("L")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return RenderedPage(
        page=page,
        png=buf.getvalue(),
        px_w=img.size[0],
        px_h=img.size[1],
        text_layer_chars=len(text),
        content_hash=_average_hash(grey),
        grey=grey,
    )


# ---------------------------------------------------------------------------
# Response normalisation + validation (the ONE canonical box conversion)
# ---------------------------------------------------------------------------


def _canonical_box(raw: Any, px_w: int, px_h: int) -> Optional[tuple[float, float, float, float]]:
    """Convert a model-supplied box to canonical ``(x, y, w, h)`` — top-left
    origin, normalised 0-1 — or None when it is unusable.

    This is the ONE place a box's convention is converted. It accepts the
    shapes models actually emit:
      * ``{"x","y","w","h"}`` (our requested form),
      * ``{"x0","y0","x1","y1"}`` / ``{"left","top","right","bottom"}`` corners,
      * ``[x0, y0, x1, y1]`` or ``[x, y, w, h]`` lists.
    Values may be 0-1 fractions, 0-1000 (Gemini-style), or pixels; the scale is
    inferred from the magnitude and, for pixels, the rendered page size.
    """
    if raw is None:
        return None

    x0 = y0 = x1 = y1 = None
    if isinstance(raw, dict):
        if {"x", "y", "w", "h"} <= set(raw):
            try:
                x0, y0 = float(raw["x"]), float(raw["y"])
                w, h = float(raw["w"]), float(raw["h"])
                x1, y1 = x0 + w, y0 + h
            except (TypeError, ValueError):
                return None
        else:
            x0 = raw.get("x0", raw.get("left", raw.get("xmin")))
            y0 = raw.get("y0", raw.get("top", raw.get("ymin")))
            x1 = raw.get("x1", raw.get("right", raw.get("xmax")))
            y1 = raw.get("y1", raw.get("bottom", raw.get("ymax")))
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            a, b, c, d = (float(v) for v in raw)
        except (TypeError, ValueError):
            return None
        # Convention: [x0, y0, x1, y1] (top-left, bottom-right).
        x0, y0, x1, y1 = a, b, c, d
    else:
        return None

    try:
        x0, y0, x1, y1 = float(x0), float(y0), float(x1), float(y1)
    except (TypeError, ValueError):
        return None
    for v in (x0, y0, x1, y1):
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
            return None

    # Normalise the scale. corners may come as x0/x1 rather than x0/w.
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0

    m = max(abs(x1), abs(y1))
    if m > 1.5:
        if m <= 1000.0 and px_w and px_h:
            # Ambiguous between 0-1000 and pixels; prefer 0-1000 only when the
            # page is much larger than the coords (typical of a 0-1000 grid).
            if m <= 1000.0 and (px_w <= 1.5 * max(px_w, px_h)):
                x0, y0, x1, y1 = x0 / 1000.0, y0 / 1000.0, x1 / 1000.0, y1 / 1000.0
            else:
                x0, y0, x1, y1 = x0 / px_w, y0 / px_h, x1 / px_w, y1 / px_h
        else:
            x0, y0, x1, y1 = x0 / 1000.0, y0 / 1000.0, x1 / 1000.0, y1 / 1000.0

    x0 = min(max(x0, 0.0), 1.0)
    y0 = min(max(y0, 0.0), 1.0)
    x1 = min(max(x1, 0.0), 1.0)
    y1 = min(max(y1, 0.0), 1.0)
    w, h = x1 - x0, y1 - y0
    if w < MIN_BOX_SIDE or h < MIN_BOX_SIDE:
        return None
    return (round(x0, 6), round(y0, 6), round(w, 6), round(h, 6))


_CATEGORY_ALIASES: dict[str, str] = {
    "tax invoice": "tax_invoice",
    "invoice": "tax_invoice",
    "gst invoice": "tax_invoice",
    "no gst expense": "no_gst_expense",
    "no_gst_expense": "no_gst_expense",
    "expense": "no_gst_expense",
    "bill": "no_gst_expense",
    "cash memo": "no_gst_expense",
    "supporting": "supporting",
    "supporting_document": "supporting",
    "statement": "supporting",
    "receipt": "supporting",
    "ticket": "ticket",
    "travel ticket": "ticket",
    "not_a_voucher": "not_a_voucher",
    "not a voucher": "not_a_voucher",
    "courier": "not_a_voucher",
    "docket": "not_a_voucher",
}


def _normalize_category(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    key = str(raw).strip().lower().replace("-", "_")
    if key in CATEGORIES:
        return key
    spaced = key.replace("_", " ")
    if spaced in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[spaced]
    return _CATEGORY_ALIASES.get(key)


def _norm_rotation(raw: Any) -> int:
    try:
        deg = int(round(float(raw)))
    except (TypeError, ValueError):
        return 0
    deg %= 360
    return deg if deg in (0, 90, 180, 270) else 0


class _InvalidPageResponse(Exception):
    """The model's page reply was empty or malformed for a non-blank page."""


@dataclass
class NormalizedDoc:
    category: str
    label: str
    continues_previous: bool
    continues_next: bool
    flags: list[str]
    evidence: str
    boxes: list[tuple[float, float, float, float]]


def _normalize_page(raw: Any, rendered: RenderedPage) -> tuple[int, bool, list[NormalizedDoc]]:
    """Validate + canonicalise one page reply.

    Raises ``_InvalidPageResponse`` when the reply is not a JSON object, a
    category is unknown, or a non-blank page yields no valid document. The
    caller retries once, then marks the page failed — never silently empty.
    """
    if not isinstance(raw, dict):
        raise _InvalidPageResponse("reply was not a JSON object")

    rotation = _norm_rotation(raw.get("rotation_deg"))
    is_blank = bool(raw.get("page_is_blank") or raw.get("is_blank"))
    raw_docs = raw.get("documents")
    if raw_docs is None:
        raw_docs = raw.get("docs") or []
    if not isinstance(raw_docs, list):
        raise _InvalidPageResponse("'documents' was not a list")

    docs: list[NormalizedDoc] = []
    for entry in raw_docs:
        if not isinstance(entry, dict):
            raise _InvalidPageResponse("a document entry was not an object")
        category = _normalize_category(entry.get("category"))
        if category is None:
            raise _InvalidPageResponse(f"unknown category {entry.get('category')!r}")
        raw_boxes = entry.get("boxes")
        if raw_boxes is None:
            raw_boxes = [entry.get("box")] if entry.get("box") is not None else []
        if not isinstance(raw_boxes, list):
            raise _InvalidPageResponse("'boxes' was not a list")
        boxes: list[tuple[float, float, float, float]] = []
        for rb in raw_boxes:
            box = _canonical_box(rb, rendered.px_w, rendered.px_h)
            if box is None:
                raise _InvalidPageResponse("a box was outside [0,1] or had no size")
            boxes.append(box)
        if not boxes:
            raise _InvalidPageResponse("a document had no boxes")
        flags = [str(f) for f in (entry.get("flags") or []) if str(f).strip()]
        docs.append(
            NormalizedDoc(
                category=category,
                label=str(entry.get("label") or "").strip()[:200],
                continues_previous=bool(entry.get("continues_previous")),
                continues_next=bool(entry.get("continues_next")),
                flags=flags,
                evidence=str(entry.get("evidence") or "").strip()[:400],
                boxes=boxes,
            )
        )

    if not docs and not is_blank:
        raise _InvalidPageResponse("no documents found on a non-blank page")
    return rotation, is_blank, docs


# ---------------------------------------------------------------------------
# The single external seam (tests stub this)
# ---------------------------------------------------------------------------


def _call_page_model(
    png: bytes,
    *,
    page: int,
    total: int,
    cfg: TriageConfig,
    db_path,
    run_id: int,
    actor: str,
    upload_id: Optional[int] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One vision call through the gateway (so fallback events are recorded).

    Uses the triage retry policy: transient failures (429 / timeout / 5xx)
    retry on the SAME leg with backoff before failing over to the fallback leg.

    This is the ONE place triage touches a model, which keeps the whole test
    suite free of live calls: stub this function.
    """
    from src.ai_models import gateway

    return gateway.call_touchpoint_json(
        TOUCHPOINT,
        _SYSTEM_PROMPT,
        _user_prompt(page, total),
        images=[("image/png", png)],
        max_tokens=2000,
        temperature=0.0,
        actor=actor,
        timeout=cfg.timeout_seconds,
        reference_type="triage_run",
        reference_id=run_id,
        upload_id=upload_id,
        retry_policy=TRIAGE_RETRY_POLICY,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Per-page processing (rendering + models + rotation re-ask + validation)
# ---------------------------------------------------------------------------


@dataclass
class PageOutcome:
    page: int
    status: str                      # ok | failed
    rotation_deg: int = 0
    is_blank: bool = False
    duplicate_of_page: Optional[int] = None
    content_hash: str = ""
    text_layer_chars: int = 0
    attempts: int = 0
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    fallback_used: bool = False
    cost_usd: Optional[float] = None


def _rotate_png(png: bytes, clockwise_deg: int) -> bytes:
    from io import BytesIO

    from PIL import Image

    if clockwise_deg % 360 == 0:
        return png
    img = Image.open(BytesIO(png))
    # PIL rotates counter-clockwise for positive angles; we need clockwise.
    rotated = img.rotate(-clockwise_deg, expand=True)
    buf = BytesIO()
    rotated.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _process_page(
    *,
    page: int,
    total: int,
    file_bytes: bytes,
    source_format: str,
    cfg: TriageConfig,
    db_path,
    run_id: int,
    actor: str,
    upload_id: Optional[int] = None,
) -> PageOutcome:
    """Render, call, validate, re-ask on rotation, retry once. Never raises."""
    rendered = _render_page(file_bytes, source_format, page, cfg)
    outcome = PageOutcome(
        page=page,
        status="failed",
        content_hash=rendered.content_hash,
        text_layer_chars=rendered.text_layer_chars,
    )
    if rendered.grey is not None and _looks_blank(rendered.grey):
        # Deterministic blank guard — a page with no ink is blank regardless of
        # what the model says (and needs no model call at all).
        outcome.status = "ok"
        outcome.is_blank = True
        outcome.result = {"rotation_deg": 0, "page_is_blank": True, "documents": []}
        return outcome

    attempts_allowed = 1 + max(0, cfg.retries)
    last_error: Optional[str] = None

    for attempt in range(1, attempts_allowed + 1):
        outcome.attempts = attempt
        try:
            parsed, meta = _call_page_model(
                rendered.png, page=page, total=total, cfg=cfg, db_path=db_path,
                run_id=run_id, actor=actor, upload_id=upload_id,
            )
        except Exception as exc:  # noqa: BLE001 — a failed page, not a crash
            last_error = f"{type(exc).__name__}: {exc}"
            _accumulate_usage(outcome, None)
            continue

        _accumulate_usage(outcome, meta)
        try:
            rotation, is_blank, docs = _normalize_page(parsed, rendered)
        except _InvalidPageResponse as exc:
            last_error = f"invalid response: {exc}"
            continue

        # Rotation re-ask: boxes must be measured on the UPRIGHT page.
        if rotation:
            try:
                rotated_png = _rotate_png(rendered.png, rotation)
            except Exception as exc:  # noqa: BLE001
                last_error = f"could not rotate page: {exc}"
                continue
            try:
                parsed2, meta2 = _call_page_model(
                    rotated_png, page=page, total=total, cfg=cfg, db_path=db_path,
                    run_id=run_id, actor=actor, upload_id=upload_id,
                )
                _accumulate_usage(outcome, meta2)
                _, is_blank, docs = _normalize_page(parsed2, rendered)
            except Exception as exc:  # noqa: BLE001
                last_error = f"rotation re-ask failed: {type(exc).__name__}: {exc}"
                continue

        outcome.status = "ok"
        outcome.rotation_deg = rotation
        outcome.is_blank = is_blank
        outcome.error = None
        outcome.result = {
            "rotation_deg": rotation,
            "page_is_blank": is_blank,
            "documents": [
                {
                    "category": d.category,
                    "label": d.label,
                    "continues_previous": d.continues_previous,
                    "continues_next": d.continues_next,
                    "flags": list(d.flags),
                    "evidence": d.evidence,
                    "boxes": [{"x": b[0], "y": b[1], "w": b[2], "h": b[3]} for b in d.boxes],
                }
                for d in docs
            ],
        }
        return outcome

    outcome.status = "failed"
    outcome.error = last_error or "page could not be read"
    outcome.result = None
    return outcome


def _accumulate_usage(outcome: PageOutcome, meta: Optional[dict[str, Any]]) -> None:
    if not meta:
        return
    usage = meta.get("usage") or {}
    if usage.get("input_tokens") is not None:
        outcome.input_tokens = (outcome.input_tokens or 0) + int(usage["input_tokens"])
    if usage.get("output_tokens") is not None:
        outcome.output_tokens = (outcome.output_tokens or 0) + int(usage["output_tokens"])
    if meta.get("provider"):
        outcome.provider = meta.get("provider")
    if meta.get("model"):
        outcome.model = meta.get("model")
    if meta.get("fallback_used"):
        outcome.fallback_used = True


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _hamming_hex(a: str, b: str) -> int:
    if not a or not b:
        return 999
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 999


def _detect_duplicates(outcomes: list[PageOutcome]) -> None:
    """Flag duplicate_of_page (earliest near-identical earlier page). Flag only."""
    for i, outcome in enumerate(outcomes):
        for earlier in outcomes[:i]:
            if not earlier.content_hash or not outcome.content_hash:
                continue
            if _hamming_hex(earlier.content_hash, outcome.content_hash) <= DUPLICATE_HAMMING:
                outcome.duplicate_of_page = earlier.page
                break


# ---------------------------------------------------------------------------
# Deterministic stitching
# ---------------------------------------------------------------------------


@dataclass
class _Doc:
    ordinal: int
    category: str
    label: str
    flags: set[str]
    regions: list[dict[str, Any]]
    first_page: int
    last_page: int
    #: Set when the document must NOT be counted or extracted even though its
    #: category is extractable — today only a duplicate COPY of an earlier page.
    #: ``_persist_documents`` turns this into excluded=1 + this reason.
    excluded_reason: Optional[str] = None


def _pick_open(open_docs: list[_Doc], label: str) -> Optional[_Doc]:
    """The nearest open document, or a label match when several are open."""
    if label:
        for d in open_docs:
            if d.label and d.label.lower() == label.lower():
                return d
    if not open_docs:
        return None
    # Nearest = the latest document (highest last_page, then highest ordinal).
    return max(open_docs, key=lambda d: (d.last_page, d.ordinal))


def _region(page: int, box: tuple[float, float, float, float], order: int, source: str = "triage") -> dict[str, Any]:
    x, y, w, h = box
    return {"page": page, "x": x, "y": y, "w": w, "h": h, "order_in_doc": order, "source": source}


def _duplicate_copy_of(
    outcome: PageOutcome, *, flagged: bool,
) -> tuple[Optional[int], Optional[str]]:
    """(the earlier page this page copies, why) — or ``(None, None)``.

    A page is only treated as a copy when the MODEL says so (``DUPLICATE_PAGE_FLAG``)
    AND the hash detector independently flagged this page against an earlier page.

    NOTHING else counts as support. In particular the document's LABEL is not
    evidence: it comes from the same model that set the flag, and continuation
    pages legitimately repeat the label of the document they continue (pages 7
    and 9 of the real bundle are one statement and share its label). Nothing
    acts on the hash alone either — the 64-bit average hash is not reliable (it
    calls pages 2 and 3 near-identical, and they are different invoices), so a
    bare hash match keeps the old behaviour: a flag on the page row, nothing
    more. And nothing acts on the model flag alone.
    """
    if not flagged:
        return None, None
    if outcome.duplicate_of_page is not None and outcome.duplicate_of_page < outcome.page:
        return outcome.duplicate_of_page, "the page hash matches that earlier page"
    return None, None


def _stitch(pages: list[PageOutcome]) -> tuple[list[_Doc], list[dict[str, Any]]]:
    """Stitch per-page results into documents, deterministically.

    Returns ``(documents, unassigned)`` where ``unassigned`` is
    ``[{"page": int, "reason": str}]``. Coverage is asserted at the end.
    """
    docs: list[_Doc] = []
    unassigned: list[dict[str, Any]] = []

    def _covered_pages() -> set[int]:
        return {r["page"] for d in docs for r in d.regions}

    for outcome in pages:
        prev_open = [d for d in docs if d.last_page == outcome.page - 1]

        if outcome.status != "ok" or outcome.result is None:
            unassigned.append({"page": outcome.page, "reason": "page_failed"})
            continue

        result = outcome.result
        raw_docs = result.get("documents") or []
        is_blank = bool(result.get("page_is_blank"))

        if not raw_docs:
            # Blank or banner-only page. It attaches to the open document when
            # the page is a continuation; otherwise it is recorded unassigned.
            continuation = bool(result.get("continues_previous"))
            if continuation and prev_open:
                target = _pick_open(prev_open, "")
                if target is not None:
                    order = len(target.regions)
                    target.regions.append(_region(outcome.page, (0.0, 0.0, 1.0, 1.0), order))
                    target.last_page = outcome.page
                    continue
            unassigned.append({
                "page": outcome.page,
                "reason": "blank" if is_blank else "no_document_detected",
            })
            continue

        for entry in raw_docs:
            category = str(entry.get("category") or "").strip()
            label = str(entry.get("label") or "").strip()
            flags = {str(f) for f in (entry.get("flags") or [])}
            boxes = [
                (float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"]))
                for b in (entry.get("boxes") or [])
            ]
            cont_prev = bool(entry.get("continues_previous"))

            duplicate_of, duplicate_why = _duplicate_copy_of(
                outcome, flagged=DUPLICATE_PAGE_FLAG in flags,
            )
            if duplicate_of is not None:
                # A COPY of paper this bundle already carries: the model said so
                # AND the hash detector agrees. It is NOT a continuation
                # (attaching it would put the same page inside a document twice)
                # and NOT an extractable document (it would be read and counted
                # twice), so it becomes its own EXCLUDED document whose reason
                # names the page it copies. Checked BEFORE the continuation
                # branch on purpose: a model that marks a copy as a continuation
                # must not get it attached anyway.
                copy = _Doc(
                    ordinal=len(docs) + 1,
                    category=category,
                    label=label,
                    flags=set(flags),
                    regions=[],
                    first_page=outcome.page,
                    last_page=outcome.page,
                    excluded_reason=f"duplicate of page {duplicate_of} — {duplicate_why}",
                )
                for b in boxes:
                    copy.regions.append(_region(outcome.page, b, len(copy.regions)))
                docs.append(copy)
                continue

            if cont_prev:
                target = _pick_open(prev_open, label) if prev_open else None
                if target is not None:
                    if target.category != category:
                        target.flags.add("category_disagrees")
                    for b in boxes:
                        target.regions.append(_region(outcome.page, b, len(target.regions)))
                    target.last_page = outcome.page
                    continue
                # An explicit continuation with nothing to attach to.
                flags = flags | {"orphan_continuation"}

            doc = _Doc(
                ordinal=len(docs) + 1,
                category=category,
                label=label,
                flags=set(flags),
                regions=[],
                first_page=outcome.page,
                last_page=outcome.page,
            )
            for b in boxes:
                doc.regions.append(_region(outcome.page, b, len(doc.regions)))
            docs.append(doc)

    # COVERAGE INVARIANT: every page is in a region or in unassigned.
    covered = _covered_pages()
    assigned_unassigned = {u["page"] for u in unassigned}
    for outcome in pages:
        if outcome.page not in covered and outcome.page not in assigned_unassigned:
            unassigned.append({"page": outcome.page, "reason": "uncovered"})
    _assert_coverage(pages, docs, unassigned)
    return docs, unassigned


def _assert_coverage(pages: list[PageOutcome], docs: list[_Doc], unassigned: list[dict[str, Any]]) -> None:
    covered = {r["page"] for d in docs for r in d.regions}
    accounted = covered | {u["page"] for u in unassigned}
    missing = sorted({p.page for p in pages} - accounted)
    if missing:
        raise AssertionError(f"triage coverage invariant violated — pages not accounted for: {missing}")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _create_run(
    conn: sqlite3.Connection, upload_id: int, cfg: TriageConfig, pages_total: int,
    status: str = RUN_QUEUED,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO invoice_triage_runs
            (upload_id, status, pages_total, pages_ok, pages_failed, settings_json, started_at)
        VALUES (?, ?, ?, 0, 0, ?, ?)
        """,
        (upload_id, status, pages_total, json.dumps(cfg.as_json()), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute("UPDATE invoice_triage_runs SET status = ? WHERE run_id = ?", (status, run_id))
    conn.commit()


def _count_fallback_pages(conn: sqlite3.Connection, run_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM invoice_triage_pages WHERE run_id = ? AND fallback_used = 1",
        (run_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _persist_page(conn: sqlite3.Connection, run_id: int, outcome: PageOutcome) -> None:
    conn.execute(
        """
        INSERT INTO invoice_triage_pages
            (run_id, page, status, rotation_deg, is_blank, duplicate_of_page,
             content_hash, text_layer_chars, attempts, error, result_json, model, fallback_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, page) DO UPDATE SET
            status=excluded.status, rotation_deg=excluded.rotation_deg,
            is_blank=excluded.is_blank, duplicate_of_page=excluded.duplicate_of_page,
            content_hash=excluded.content_hash, text_layer_chars=excluded.text_layer_chars,
            attempts=excluded.attempts, error=excluded.error, result_json=excluded.result_json,
            model=excluded.model, fallback_used=excluded.fallback_used
        """,
        (
            run_id, outcome.page, outcome.status, int(outcome.rotation_deg),
            int(outcome.is_blank), outcome.duplicate_of_page,
            outcome.content_hash, int(outcome.text_layer_chars), int(outcome.attempts),
            outcome.error, json.dumps(outcome.result) if outcome.result is not None else None,
            outcome.model, int(bool(outcome.fallback_used)),
        ),
    )
    conn.commit()


def _update_run_progress(conn: sqlite3.Connection, run_id: int, pages_ok: int, pages_failed: int) -> None:
    conn.execute(
        "UPDATE invoice_triage_runs SET pages_ok = ?, pages_failed = ? WHERE run_id = ?",
        (pages_ok, pages_failed, run_id),
    )
    conn.commit()


def _finalize_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    allowed = {
        "status", "provider", "model", "input_tokens", "output_tokens", "cost_usd",
        "error", "finished_at", "pages_ok", "pages_failed", "fallback_pages",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(
        f"UPDATE invoice_triage_runs SET {cols} WHERE run_id = ?",
        (*sets.values(), run_id),
    )
    conn.commit()


def _documents_have_user_edits(conn: sqlite3.Connection, upload_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT document_id, label, category_source FROM invoice_documents "
        "WHERE upload_id = ? AND is_active = 1",
        (upload_id,),
    ).fetchall()
    protected: list[dict[str, Any]] = []
    for r in rows:
        doc_id = int(r[0])
        user_region = conn.execute(
            "SELECT 1 FROM document_regions WHERE document_id = ? AND source = 'user' AND is_active = 1 LIMIT 1",
            (doc_id,),
        ).fetchone()
        if (r[2] or "triage") == "user" or user_region is not None:
            protected.append({"document_id": doc_id, "label": r[1], "reason": "user-edited"})
    return protected


def _deactivate_active_documents(conn: sqlite3.Connection, upload_id: int) -> None:
    """Make every currently-active document for this upload inactive history.

    Called immediately before inserting a freshly-stitched set (a new run, or a
    retry that re-stitches the same run), so the active set is always exactly
    the latest stitch — never an accumulation.
    """
    conn.execute(
        "UPDATE invoice_documents SET is_active = 0 WHERE upload_id = ? AND is_active = 1",
        (upload_id,),
    )
    conn.execute(
        "UPDATE document_regions SET is_active = 0 WHERE document_id IN "
        "(SELECT document_id FROM invoice_documents WHERE upload_id = ? AND is_active = 0)",
        (upload_id,),
    )
    conn.commit()


def _persist_documents(
    conn: sqlite3.Connection, *, upload_id: int, run_id: int, docs: list[_Doc], unassigned: list[dict[str, Any]], actor: str,
) -> None:
    for doc in docs:
        excluded_reason = doc.excluded_reason or (
            DEFAULT_EXCLUDED_CATEGORY if doc.category == DEFAULT_EXCLUDED_CATEGORY else None
        )
        excluded = 1 if excluded_reason else 0
        cur = conn.execute(
            """
            INSERT INTO invoice_documents
                (upload_id, run_id, ordinal, category, category_source, label,
                 excluded, excluded_reason, flags_json, is_active, created_at)
            VALUES (?, ?, ?, ?, 'triage', ?, ?, ?, ?, 1, ?)
            """,
            (
                upload_id, run_id, doc.ordinal, doc.category, doc.label, excluded,
                excluded_reason, json.dumps(sorted(doc.flags)), _now(),
            ),
        )
        document_id = int(cur.lastrowid)
        if doc.excluded_reason:
            # One decision line per duplicate copy, in the SAME audit table the
            # unassigned pages already use (``page_unassigned``) — no new table,
            # nothing to migrate. The reason is repeated here because this log
            # is what a reviewer reads to see WHY a page was left out.
            conn.execute(
                """
                INSERT INTO document_region_log
                    (document_id, region_id, action, before_json, after_json, actor, at)
                VALUES (?, NULL, 'page_duplicate_excluded', NULL, ?, ?, ?)
                """,
                (
                    document_id,
                    json.dumps({"page": doc.first_page, "reason": doc.excluded_reason}),
                    actor, _now(),
                ),
            )
        for region in doc.regions:
            cur2 = conn.execute(
                """
                INSERT INTO document_regions
                    (document_id, page, x, y, w, h, order_in_doc, source, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    document_id, region["page"], region["x"], region["y"], region["w"],
                    region["h"], region["order_in_doc"], region.get("source", "triage"),
                ),
            )
            conn.execute(
                """
                INSERT INTO document_region_log
                    (document_id, region_id, action, before_json, after_json, actor, at)
                VALUES (?, ?, 'triage_create', NULL, ?, ?, ?)
                """,
                (document_id, int(cur2.lastrowid), json.dumps(region), actor, _now()),
            )
    for item in unassigned:
        conn.execute(
            """
            INSERT INTO document_region_log
                (document_id, region_id, action, before_json, after_json, actor, at)
            VALUES (NULL, NULL, 'page_unassigned', NULL, ?, ?, ?)
            """,
            (json.dumps(item), actor, _now()),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


def _probe_pages(upload: dict[str, Any], file_bytes: bytes, cfg: TriageConfig) -> tuple[int, int]:
    source_format = upload.get("source_format")
    if source_format == "pdf":
        pages_total, text_chars = _page_total_and_text(file_bytes, "pdf")
    elif source_format == "image":
        pages_total, text_chars = 1, 0
    else:
        raise InvoiceExtractError(
            f"Triage works on PDF and image files only; this upload is '{source_format}'."
        )
    if pages_total > cfg.max_pages:
        raise InvoiceExtractError(
            f"This file has {pages_total} pages, over the triage limit of {cfg.max_pages}. "
            "Raise invoice_extract.triage.max_pages (or split the file) — triage never "
            "silently skips pages."
        )
    return pages_total, text_chars


def _load_upload(conn: sqlite3.Connection, upload_id: int) -> tuple[dict[str, Any], bytes]:
    upload = idb.get_upload(conn, upload_id)
    if upload is None:
        raise InvoiceExtractError("Upload not found.")
    data = idb.get_upload_bytes(conn, upload_id)
    if not data:
        raise InvoiceExtractError("The original file bytes for this upload are no longer stored.")
    return upload, data


def _run_pages(
    *, file_bytes: bytes, source_format: str, total: int, cfg: TriageConfig,
    db_path, run_id: int, actor: str, upload_id: Optional[int] = None,
    pages: Optional[list[int]] = None,
) -> list[PageOutcome]:
    """Process pages concurrently; return outcomes in page order.

    Each page is rendered by ITS OWN worker task and released immediately, so
    the peak number of page images in memory is ``cfg.concurrency`` — never the
    whole file. The gateway's vision semaphore caps the simultaneous model
    calls independently.
    """
    page_numbers = pages if pages is not None else list(range(1, total + 1))
    outcomes: dict[int, PageOutcome] = {}

    def _work(pno: int) -> PageOutcome:
        return _process_page(
            page=pno, total=total, file_bytes=file_bytes, source_format=source_format,
            cfg=cfg, db_path=db_path, run_id=run_id, actor=actor, upload_id=upload_id,
        )

    with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
        futures = {pool.submit(_work, pno): pno for pno in page_numbers}
        for fut in as_completed(futures):
            pno = futures[fut]
            try:
                outcome = fut.result()
            except Exception as exc:  # noqa: BLE001 — defensive; _process_page never raises
                outcome = PageOutcome(page=pno, status="failed", error=f"{type(exc).__name__}: {exc}")
            outcomes[pno] = outcome

    return [outcomes[p] for p in sorted(outcomes)]


def _maybe_supersede(conn: sqlite3.Connection, upload_id: int, *, force: bool) -> None:
    protected = _documents_have_user_edits(conn, upload_id)
    if protected and not force:
        names = ", ".join(str(p["label"] or p["document_id"]) for p in protected)
        raise InvoiceExtractError(
            "Re-triage would discard user-edited documents or regions. "
            f"Refusing unless forced — would lose: {names}. Pass force=True to override."
        )


def _restitch_and_persist(
    conn: sqlite3.Connection, *, upload_id: int, run_id: int, outcomes: list[PageOutcome], actor: str,
) -> tuple[list[_Doc], list[dict[str, Any]]]:
    _detect_duplicates(outcomes)
    docs, unassigned = _stitch(outcomes)
    _deactivate_active_documents(conn, upload_id)
    _persist_documents(conn, upload_id=upload_id, run_id=run_id, docs=docs, unassigned=unassigned, actor=actor)
    return docs, unassigned


# ---------------------------------------------------------------------------
# Background execution — one run at a time per process
# ---------------------------------------------------------------------------
#
# A triage pass is long (many page calls). It must NOT run inside a Reflex
# handler thread: a websocket closing or a page reload must never stop it, and
# the handler must return at once. So ``run_triage`` only persists a QUEUED run
# row and hands the work to ONE process-wide worker thread. Further runs queue
# behind it (their row stays "queued"). Progress is written per page, so a UI
# can poll ``invoice_triage_runs``/``invoice_triage_pages``.
# ---------------------------------------------------------------------------

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="triage-worker")


def _run_chain(run_id: int, *, actor: str, db_path=None) -> None:
    """Hand a FINISHED run to the pipeline's chain, if one is registered.

    A no-op unless someone called ``pipeline.start_v2_pipeline``. Called on
    every exit of ``_execute_run``, so it may never raise: a chain failure must
    not turn a completed triage run into a failed one — the extraction simply
    does not start, and ``resume_extraction`` can still pick it up.
    """
    try:
        from src.invoice_extract import pipeline

        pipeline.run_chain(run_id, actor=actor, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: the pipeline chain for run {run_id} failed: {exc}", file=sys.stderr)


def _execute_run(run_id: int, *, actor: str, db_path=None, pages: Optional[list[int]] = None) -> None:
    """The worker body. Never raises — a failure is recorded on the run row."""
    conn = _connect(db_path)
    try:
        cur = conn.execute("SELECT * FROM invoice_triage_runs WHERE run_id = ?", (run_id,))
        run = _dict_row(cur, cur.fetchone())
        if run is None:
            return
        upload_id = int(run["upload_id"])
        total = int(run.get("pages_total") or 0)
        upload, file_bytes = _load_upload(conn, upload_id)
        cfg = get_config(db_path=db_path)
        _set_run_status(conn, run_id, RUN_RUNNING)
    finally:
        conn.close()

    try:
        page_numbers = pages if pages is not None else list(range(1, total + 1))
        outcomes = _run_pages(
            file_bytes=file_bytes, source_format=upload["source_format"], total=total,
            cfg=cfg, db_path=db_path, run_id=run_id, actor=actor, upload_id=upload_id,
            pages=page_numbers,
        )
    except Exception as exc:  # noqa: BLE001 — record, never crash the worker
        conn = _connect(db_path)
        try:
            _finalize_run(conn, run_id, status=RUN_FAILED,
                          error=f"{type(exc).__name__}: {exc}", finished_at=_now())
        finally:
            conn.close()
        _run_chain(run_id, actor=actor, db_path=db_path)
        return

    conn = _connect(db_path)
    try:
        # Merge the fresh outcomes over any cached per-page replies (a resume
        # only re-runs some pages), detect duplicates over the WHOLE set, then
        # persist and re-stitch.
        by_page = {o.page: o for o in _load_page_outcomes(conn, run_id)}
        for outcome in outcomes:
            by_page[outcome.page] = outcome
        all_outcomes = [by_page[p] for p in sorted(by_page)]
        _detect_duplicates(all_outcomes)

        pages_ok = pages_failed = 0
        for outcome in all_outcomes:
            _persist_page(conn, run_id, outcome)
            if outcome.status == "ok":
                pages_ok += 1
            else:
                pages_failed += 1
            _update_run_progress(conn, run_id, pages_ok, pages_failed)

        docs, unassigned = _stitch(all_outcomes)
        if pages_ok > 0:
            _deactivate_active_documents(conn, upload_id)
            _persist_documents(
                conn, upload_id=upload_id, run_id=run_id, docs=docs, unassigned=unassigned, actor=actor,
            )

        run_row_cur = conn.execute("SELECT * FROM invoice_triage_runs WHERE run_id = ?", (run_id,))
        run_row = _dict_row(run_row_cur, run_row_cur.fetchone()) or {}
        in_tokens = _sum_optional(o.input_tokens for o in outcomes)
        out_tokens = _sum_optional(o.output_tokens for o in outcomes)
        total_in = (run_row.get("input_tokens") or 0) + (in_tokens or 0)
        total_out = (run_row.get("output_tokens") or 0) + (out_tokens or 0)
        provider = run_row.get("provider") or next((o.provider for o in outcomes if o.provider), None)
        model = run_row.get("model") or next((o.model for o in outcomes if o.model), None)
        status = RUN_FAILED if pages_ok == 0 else (RUN_PARTIAL if pages_failed else RUN_COMPLETE)
        _finalize_run(
            conn, run_id, status=status, provider=provider, model=model,
            input_tokens=total_in, output_tokens=total_out,
            cost_usd=_estimate_cost(provider, model, total_in, total_out, db_path),
            error=(next((o.error for o in outcomes if o.error), None) if status == RUN_FAILED else None),
            finished_at=_now(), pages_ok=pages_ok, pages_failed=pages_failed,
            fallback_pages=_count_fallback_pages(conn, run_id),
        )
    finally:
        conn.close()

    # THE CHAIN: triage is done, so the documents it found can be read. Runs on
    # THIS worker, so closing the browser cannot stop the pair. A no-op when no
    # chain was registered for this run.
    _run_chain(run_id, actor=actor, db_path=db_path)


def run_triage(
    upload_id: int,
    *,
    actor: str = "system:triage",
    db_path=None,
    include_disabled: bool = False,
    force_supersede: bool = False,
) -> dict[str, Any]:
    """Queue a full triage pass and return AT ONCE with the run id.

    The pass runs on a background worker thread (module-level, independent of
    any websocket), so closing the browser cannot stop it. Poll ``get_triage``
    / the ``invoice_triage_runs`` row for progress. If another run is already
    executing, this run stays ``queued`` until the worker is free.

    ``include_disabled`` lets the CLI / scoring harness run while the feature is
    off by default; nothing in the normal upload path sets it.
    """
    cfg = get_config(db_path=db_path)
    if not cfg.enabled and not include_disabled:
        raise InvoiceExtractError(
            "Invoice triage is disabled. Enable invoice_extract.triage.enabled, or run the "
            "CLI (python -m src.invoice_extract.triage run <upload_id>) which passes "
            "include_disabled=True."
        )

    conn = _connect(db_path)
    try:
        upload, file_bytes = _load_upload(conn, upload_id)
        pages_total, _ = _probe_pages(upload, file_bytes, cfg)
        _maybe_supersede(conn, upload_id, force=force_supersede)
        run_id = _create_run(conn, upload_id, cfg, pages_total, status=RUN_QUEUED)
    finally:
        conn.close()

    _EXECUTOR.submit(_execute_run, run_id, actor=actor, db_path=db_path)
    return {"run_id": run_id, "upload_id": upload_id, "status": RUN_QUEUED, "pages_total": pages_total}


def get_run(run_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        cur = conn.execute("SELECT * FROM invoice_triage_runs WHERE run_id = ?", (run_id,))
        return _dict_row(cur, cur.fetchone())
    finally:
        conn.close()


def wait_for_run(
    run_id: int, *, db_path=None, timeout: Optional[float] = None, poll: float = 0.05,
) -> dict[str, Any]:
    """Block until a run reaches a terminal state (or ``timeout`` elapses)."""
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        run = get_run(run_id, db_path=db_path)
        if run is None:
            raise InvoiceExtractError(f"Triage run {run_id} not found.")
        if run["status"] in RUN_TERMINAL:
            return run
        if deadline is not None and time.monotonic() >= deadline:
            return run
        time.sleep(poll)


def init_triage(db_path=None) -> int:
    """Boot step: any run left 'running' by a previous process is 'interrupted'.

    Called from ``bootstrap`` on every boot. Returns how many were marked.
    """
    conn = _connect(db_path)
    try:
        try:
            cur = conn.execute(
                "UPDATE invoice_triage_runs SET status = ?, finished_at = COALESCE(finished_at, ?) "
                "WHERE status = ?",
                (RUN_INTERRUPTED, _now(), RUN_RUNNING),
            )
            conn.commit()
            return int(cur.rowcount or 0)
        except sqlite3.OperationalError:
            # Boot must not fail because a schema is mid-upgrade.
            return 0
    finally:
        conn.close()


def _resume_pages(conn: sqlite3.Connection, run_id: int, total: int) -> list[int]:
    """Pages that are NOT 'ok' — failed, or never finished before a crash."""
    done = {
        int(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT page, status FROM invoice_triage_pages WHERE run_id = ?", (run_id,)
        ).fetchall()
    }
    return [p for p in range(1, total + 1) if done.get(p) != "ok"]


def resume_triage(
    run_id: int, *, actor: str = "system:triage", db_path=None, include_disabled: bool = True,
) -> dict[str, Any]:
    """Re-queue an interrupted/partial run, re-running only its unfinished pages."""
    cfg = get_config(db_path=db_path)
    if not cfg.enabled and not include_disabled:
        raise InvoiceExtractError("Invoice triage is disabled.")

    conn = _connect(db_path)
    try:
        cur = conn.execute("SELECT * FROM invoice_triage_runs WHERE run_id = ?", (run_id,))
        run = _dict_row(cur, cur.fetchone())
        if run is None:
            raise InvoiceExtractError(f"Triage run {run_id} not found.")
        if run["status"] == RUN_RUNNING:
            raise InvoiceExtractError(f"Triage run {run_id} is already running.")
        total = int(run.get("pages_total") or 0)
        to_resume = _resume_pages(conn, run_id, total)
        if not to_resume:
            return {"run_id": run_id, "resume_pages": 0, "status": run["status"]}
        _set_run_status(conn, run_id, RUN_QUEUED)
    finally:
        conn.close()

    _EXECUTOR.submit(_execute_run, run_id, actor=actor, db_path=db_path, pages=to_resume)
    return {"run_id": run_id, "status": RUN_QUEUED, "resume_pages": len(to_resume)}


def retry_failed_pages(
    run_id: int, *, actor: str = "system:triage", db_path=None, include_disabled: bool = True,
) -> dict[str, Any]:
    """Synchronous convenience: resume a run's unfinished pages and wait.

    Kept for callers that want the finished result in hand (CLI, tests); the
    resumption itself still runs on the background worker.
    """
    queued = resume_triage(run_id, actor=actor, db_path=db_path, include_disabled=include_disabled)
    run = wait_for_run(run_id, db_path=db_path)
    upload_id = int(run["upload_id"]) if run else 0
    result = get_triage(upload_id, db_path=db_path) if upload_id else {"run": None, "documents": []}
    return {
        "run_id": run_id,
        "retried": int(queued.get("resume_pages", 0)),
        "status": (run or {}).get("status"),
        "triage": result,
    }


def _estimate_cost(
    provider: Optional[str], model: Optional[str],
    in_tokens: Optional[int], out_tokens: Optional[int], db_path,
) -> Optional[float]:
    try:
        from src.ai_models import service as ai_models

        return ai_models.estimate_cost_usd(provider, model, in_tokens, out_tokens, db_path=db_path)
    except Exception:  # noqa: BLE001
        return None


def _sum_optional(values) -> Optional[int]:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _load_page_outcomes(conn: sqlite3.Connection, run_id: int) -> list[PageOutcome]:
    rows = conn.execute(
        "SELECT page, status, rotation_deg, is_blank, duplicate_of_page, content_hash, "
        "text_layer_chars, attempts, error, result_json, model, fallback_used "
        "FROM invoice_triage_pages WHERE run_id = ? ORDER BY page",
        (run_id,),
    ).fetchall()
    out: list[PageOutcome] = []
    for r in rows:
        outcome = PageOutcome(
            page=int(r[0]), status=str(r[1]), rotation_deg=int(r[2] or 0),
            is_blank=bool(r[3]), duplicate_of_page=r[4], content_hash=str(r[5] or ""),
            text_layer_chars=int(r[6] or 0), attempts=int(r[7] or 0), error=r[8],
            model=r[10], fallback_used=bool(r[11]),
        )
        if r[9]:
            try:
                outcome.result = json.loads(r[9])
            except (TypeError, ValueError):
                outcome.result = None
        out.append(outcome)
    return out


def get_triage(upload_id: int, *, db_path=None) -> dict[str, Any]:
    """The ACTIVE triage result for an upload: run, documents, pages, unassigned."""
    conn = _connect(db_path)
    try:
        run_cur = conn.execute(
            "SELECT * FROM invoice_triage_runs WHERE upload_id = ? ORDER BY run_id DESC LIMIT 1",
            (upload_id,),
        )
        run = _dict_row(run_cur, run_cur.fetchone())
        if run is None:
            return {"run": None, "documents": [], "pages": [], "unassigned": []}
        run_id = int(run["run_id"])

        pages = [
            {
                "page": int(r[1]), "status": r[2], "rotation_deg": int(r[3] or 0),
                "is_blank": bool(r[4]), "duplicate_of_page": r[5], "content_hash": r[6],
                "text_layer_chars": int(r[7] or 0), "attempts": int(r[8] or 0), "error": r[9],
                "model": r[10], "fallback_used": bool(r[11]),
            }
            for r in conn.execute(
                "SELECT run_id, page, status, rotation_deg, is_blank, duplicate_of_page, "
                "content_hash, text_layer_chars, attempts, error, model, fallback_used "
                "FROM invoice_triage_pages WHERE run_id = ? ORDER BY page",
                (run_id,),
            ).fetchall()
        ]

        documents: list[dict[str, Any]] = []
        covered: set[int] = set()
        docs_cur = conn.execute(
            "SELECT * FROM invoice_documents WHERE upload_id = ? AND run_id = ? AND is_active = 1 "
            "ORDER BY ordinal",
            (upload_id, run_id),
        )
        for drow in docs_cur.fetchall():
            d = _dict_row(docs_cur, drow)
            assert d is not None
            regions = [
                {
                    "region_id": int(r[0]), "page": int(r[1]), "x": r[2], "y": r[3],
                    "w": r[4], "h": r[5], "order_in_doc": int(r[6]), "source": r[7],
                }
                for r in conn.execute(
                    "SELECT region_id, page, x, y, w, h, order_in_doc, source FROM document_regions "
                    "WHERE document_id = ? AND is_active = 1 ORDER BY order_in_doc",
                    (int(d["document_id"]),),
                ).fetchall()
            ]
            covered.update(r["page"] for r in regions)
            documents.append({
                "document_id": int(d["document_id"]),
                "ordinal": int(d["ordinal"]),
                "category": d["category"],
                "category_source": d["category_source"],
                "label": d["label"],
                "excluded": bool(d["excluded"]),
                "excluded_reason": d["excluded_reason"],
                "flags": json.loads(d["flags_json"] or "[]"),
                "regions": regions,
            })

        unassigned = []
        for p in pages:
            if p["page"] not in covered:
                reason = "page_failed" if p["status"] != "ok" else ("blank" if p["is_blank"] else "no_document_detected")
                unassigned.append({"page": p["page"], "reason": reason})

        return {"run": run, "documents": documents, "pages": pages, "unassigned": unassigned}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI + live scoring
# ---------------------------------------------------------------------------


def _cmd_run(upload_id: int, *, db_path=None) -> int:
    queued = run_triage(upload_id, actor="cli:triage", db_path=db_path, include_disabled=True)
    run = wait_for_run(int(queued["run_id"]), db_path=db_path) or queued
    triage_result = get_triage(upload_id, db_path=db_path)
    print(json.dumps({
        "run_id": queued["run_id"], "upload_id": upload_id, "status": run.get("status"),
        "pages_total": run.get("pages_total"), "pages_ok": run.get("pages_ok"),
        "pages_failed": run.get("pages_failed"), "fallback_pages": run.get("fallback_pages"),
        "documents": len(triage_result["documents"]),
        "unassigned": len(triage_result["unassigned"]),
        "provider": run.get("provider"), "model": run.get("model"),
        "input_tokens": run.get("input_tokens"), "output_tokens": run.get("output_tokens"),
        "cost_usd": run.get("cost_usd"),
    }, indent=2))
    return 0


def _annotate_pngs(upload_id: int, out_dir: Path, *, db_path=None) -> list[Path]:
    """Draw each document's boxes + label onto its page PNG (one file per page)."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    conn = _connect(db_path)
    try:
        upload, file_bytes = _load_upload(conn, upload_id)
    finally:
        conn.close()
    triage = get_triage(upload_id, db_path=db_path)
    cfg = get_config(db_path=db_path)

    written: list[Path] = []
    for p in triage["pages"]:
        rendered = _render_page(file_bytes, upload["source_format"], p["page"], cfg)
        img = Image.open(BytesIO(rendered.png)).convert("RGB")
        if p["rotation_deg"]:
            img = Image.open(BytesIO(_rotate_png(rendered.png, p["rotation_deg"]))).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        for doc in triage["documents"]:
            for region in doc["regions"]:
                if region["page"] != p["page"]:
                    continue
                x0, y0 = region["x"] * w, region["y"] * h
                x1, y1 = x0 + region["w"] * w, y0 + region["h"] * h
                colour = (200, 30, 30) if doc["excluded"] else (20, 120, 220)
                draw.rectangle([x0, y0, x1, y1], outline=colour, width=3)
                label = f"{doc['ordinal']}. {doc['category']}"
                if doc["label"]:
                    label += f" — {doc['label']}"
                draw.text((x0 + 4, max(0, y0 - 12)), label[:80], fill=colour)
        path = out_dir / f"page_{p['page']:03d}.png"
        img.save(path)
        written.append(path)
    return written


@dataclass
class _Expected:
    data: dict[str, Any]
    path: Path
    verified: bool = False


def _load_expected(path: Path) -> _Expected:
    if not path.exists():
        return _Expected(data={}, path=path, verified=False)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return _Expected(data={}, path=path, verified=False)
    return _Expected(data=data, path=path, verified=bool(data.get("verified")))


def _iou(a: list[float], b: list[float]) -> float:
    """IoU of two boxes given as [x0, y0, x1, y1]."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _doc_pages(doc: dict[str, Any]) -> list[int]:
    return sorted({r["page"] for r in doc["regions"]})


def _doc_category_ok(doc: dict[str, Any], expected: dict[str, Any]) -> bool:
    acceptable = expected.get("acceptable")
    if acceptable:
        return doc["category"] in acceptable
    return doc["category"] == expected.get("category")


# ---------------------------------------------------------------------------
# Model-comparison scoring
# ---------------------------------------------------------------------------
#
# ``score`` NEVER opens the database it is given for writing: it copies it
# (source opened ``mode=ro``, SQLite backup API) into a temp folder PER
# CANDIDATE and does all of its work there — migrations, the invoice_triage
# assignment change, the upload, and the run. The app's database, screens and
# touchpoint defaults are untouched.
# ---------------------------------------------------------------------------

SCORE_REPORT_NAME = "triage_score_report.md"
COST_CAP_DEFAULT = 1.00
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _parse_leg(spec: str) -> tuple[str, str]:
    """Parse a ``provider:model_id`` leg.

    Split on the FIRST colon, so an OpenRouter variant suffix (``:free``,
    ``:batch``) stays part of the model id. The model id is passed through
    VERBATIM — a DeepSeek/Sarvam id is never translated or looked up in the
    OpenRouter catalogue.
    """
    raw = (spec or "").strip()
    if ":" not in raw:
        raise InvoiceExtractError(
            "Model legs are written 'provider:model_id' — for example "
            f"openrouter:qwen/qwen3.7-flash or deepseek:deepseek-flash. Got {spec!r}."
        )
    provider, model = raw.split(":", 1)
    provider, model = provider.strip().lower(), model.strip()
    if not provider or not model:
        raise InvoiceExtractError(f"A leg needs both a provider and a model id; got {spec!r}.")
    return provider, model


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "candidate").lower()).strip("-") or "candidate"


def _copy_db(source_db: Path, dest_db: Path) -> None:
    """Copy the source database with SQLite's backup API.

    The SOURCE is opened read-only (``mode=ro``), so the database handed to the
    command is never opened for writing and its bytes cannot change.
    """
    src = sqlite3.connect(f"file:{Path(source_db)}?mode=ro", uri=True)
    dst = sqlite3.connect(dest_db)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


@dataclass
class _Copy:
    root: Path
    db: Path
    data: Path

    def cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)


def _make_copy(source_db: Path, *, prefix: str, db_name: str = "poc.db") -> _Copy:
    """A fresh temp copy of ``source_db`` with migrations applied to the COPY."""
    from src import bootstrap

    root = Path(tempfile.mkdtemp(prefix=prefix))
    dest = root / db_name
    _copy_db(source_db, dest)
    bootstrap.run_migrations(db_path=dest, data_root=root / "data")
    return _Copy(root=root, db=dest, data=root / "data")


def _current_triage_legs(copy: _Copy) -> tuple[tuple[str, str], tuple[str, str]]:
    """The invoice_triage legs already in the COPY (its seeded default when the
    source database had none) — used to fill in a leg the caller did not give."""
    row = ai_models.get_assignment("invoice_triage", db_path=str(copy.db)) or {}
    return (
        (row.get("primary_provider") or "openrouter", row.get("primary_model") or ""),
        (row.get("fallback_provider") or "openrouter", row.get("fallback_model") or ""),
    )


def _catalogue_available(copy: _Copy, provider_key: str) -> bool:
    try:
        return bool(ai_models.models_for_provider(provider_key, db_path=str(copy.db)))
    except Exception:  # noqa: BLE001 — treat an unreadable catalogue as "no catalogue"
        return False


def _apply_triage_assignment(
    copy: _Copy, *, primary: tuple[str, str], fallback: tuple[str, str],
    force_model: bool, actor: str = "triage:score",
) -> dict[str, Any]:
    """Set the invoice_triage legs **in the COPY ONLY**.

    Refuses (before any write) when a leg is provably not image-capable — the
    SAME ``ai_models.vision_check`` the AI Models screen uses — unless
    ``force_model``. When a NON-OpenRouter provider has no catalogue in the copy
    (its model list is empty), the catalogue check is SKIPPED for that leg and
    the caller relies on the live image test instead; the id is never looked up
    anywhere else.
    """
    notes: list[str] = []
    refusals: list[str] = []
    checked: list[str] = []
    skipped: list[str] = []

    for leg_name, (provider_key, model) in (("primary", primary), ("fallback", fallback)):
        if provider_key != "openrouter" and not _catalogue_available(copy, provider_key):
            notes.append(
                f"{leg_name} ({provider_key}): no catalogue in this copy — catalogue image "
                "check SKIPPED, relying on the live image test"
            )
            skipped.append(leg_name)
            continue
        allowed, reason = ai_models.vision_check(provider_key, model, db_path=str(copy.db))
        checked.append(leg_name)
        if not allowed:
            refusals.append(f"{leg_name}: {reason}")

    if refusals and not force_model:
        raise InvoiceExtractError(
            "Refusing to start — the image-capability check failed ("
            + "; ".join(refusals)
            + "). Pass --force-model to run it anyway, or choose an image-capable model."
        )
    if refusals and force_model:
        notes.append("--force-model overrode the image-capability check: " + "; ".join(refusals))

    # The internal guard must be told when we have deliberately decided to
    # proceed (forced, or a leg whose catalogue could not be consulted).
    ai_models.set_assignment_legs(
        "invoice_triage",
        primary_provider=primary[0], primary_model=primary[1],
        fallback_provider=fallback[0], fallback_model=fallback[1],
        actor=actor, allow_non_vision_override=bool(force_model or skipped),
        db_path=str(copy.db),
    )
    return {
        "primary": primary, "fallback": fallback,
        "forced": bool(refusals and force_model),
        "notes": notes, "checked": checked, "skipped": skipped,
    }


def _live_image_test(copy: _Copy, legs: list[str]) -> dict[str, Any]:
    """The app's own per-leg image test, used in place of a catalogue check when
    a provider has no catalogue in the copy."""
    out: dict[str, Any] = {}
    for leg in legs:
        try:
            out[leg] = ai_models.test_leg(
                "invoice_triage", leg, actor="triage:score", db_path=str(copy.db),
            )
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            out[leg] = {"ok": False, "kind": "image", "error": f"{type(exc).__name__}: {exc}"}
    return out


def _key_preflight(copy: _Copy, providers: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Report per provider ONLY whether a key is available — the app's own
    status check, which never decrypts, prints or copies a key."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider_key in providers:
        if provider_key in seen:
            continue
        seen.add(provider_key)
        try:
            configured, source = ai_models.provider_key_configured(
                provider_key, db_path=str(copy.db)
            )
        except Exception:  # noqa: BLE001
            configured, source = False, None
        rows.append({"provider": provider_key, "available": bool(configured), "source": source})
    missing = [r["provider"] for r in rows if not r["available"]]
    return rows, missing


def _normalise_candidates(payload: list[Any]) -> list[dict[str, Any]]:
    """``[{name, primary, fallback}]`` → legs as ``(provider, model)`` tuples."""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise InvoiceExtractError(
                f"Candidate {index} must be an object with name/primary/fallback."
            )
        name = str(item.get("name") or f"candidate-{index}")
        prim = item.get("primary")
        fb = item.get("fallback")
        out.append({
            "name": name,
            "primary": _parse_leg(prim) if isinstance(prim, str) else prim,
            "fallback": _parse_leg(fb) if isinstance(fb, str) else fb,
        })
    return out


def _candidates_from_args(
    *, primary: Optional[list[str]], fallback: Optional[list[str]],
    candidates_file: Optional[str], candidates: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build the candidate list from the CLI/JSON inputs.

    Repeatable ``--primary`` values become one candidate each, paired with the
    matching ``--fallback`` when present — so a single command can compare
    several models.
    """
    if candidates_file and (primary or fallback or candidates):
        raise InvoiceExtractError("Use either --candidates or --primary/--fallback, not both.")
    if candidates_file:
        try:
            payload = json.loads(Path(candidates_file).read_text())
        except (OSError, ValueError) as exc:
            raise InvoiceExtractError(f"Could not read the candidates file: {exc}") from exc
        if isinstance(payload, dict):
            payload = payload.get("candidates")
        if not isinstance(payload, list) or not payload:
            raise InvoiceExtractError(
                "The candidates file must be a non-empty JSON list of "
                "{name, primary, fallback} objects."
            )
        return _normalise_candidates(payload)
    if candidates:
        return _normalise_candidates(candidates)

    primaries = [_parse_leg(p) for p in (primary or [])]
    fallbacks = [_parse_leg(f) for f in (fallback or [])]
    if not primaries:
        if fallbacks:
            raise InvoiceExtractError("--fallback needs a matching --primary.")
        return [{"name": "default", "primary": None, "fallback": None}]
    if len(primaries) == 1:
        return [{"name": "cli", "primary": primaries[0],
                 "fallback": fallbacks[0] if fallbacks else None}]
    if len(fallbacks) > len(primaries):
        raise InvoiceExtractError("More --fallback values than --primary values.")
    return [
        {"name": f"cli-{i + 1}", "primary": p,
         "fallback": fallbacks[i] if i < len(fallbacks) else None}
        for i, p in enumerate(primaries)
    ]


def score(
    pdf_path: str,
    *,
    out_dir: Optional[Path] = None,
    expected_path: Optional[Path] = None,
    client_id: Optional[int] = None,
    keep: bool = False,
    db_name: str = "poc.db",
    db_path: Optional[str] = None,
    primary: Optional[list[str]] = None,
    fallback: Optional[list[str]] = None,
    candidates_file: Optional[str] = None,
    candidates: Optional[list[dict[str, Any]]] = None,
    repeat: int = 1,
    max_cost_usd: float = COST_CAP_DEFAULT,
    force_model: bool = False,
    report_name: str = SCORE_REPORT_NAME,
) -> dict[str, Any]:
    """Score one or more models against the fixture — on COPIES of the database.

    ``--db`` names the database to COPY (default: the app database). Each
    candidate gets its OWN fresh copy, so the invoice_triage assignment is only
    ever changed in a temp copy. Provider keys are resolved only through the
    app's own resolver; nothing here prints, logs, copies or writes a key.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise InvoiceExtractError(f"Fixture PDF not found: {pdf_path}")
    out_dir = Path(out_dir) if out_dir else DEFAULT_REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = _load_expected(Path(expected_path) if expected_path else DEFAULT_EXPECTED)

    source_db = Path(db_path) if db_path else Path(recon_db.DB_PATH)
    if not source_db.exists():
        raise InvoiceExtractError(
            f"No database at {source_db}. Pass --db <path> or run `python main.py` once."
        )
    repeats = max(1, int(repeat))
    cap = float(max_cost_usd) if max_cost_usd is not None else None
    wanted = _candidates_from_args(
        primary=primary, fallback=fallback, candidates_file=candidates_file, candidates=candidates,
    )

    pages_total = _pdf_page_count(pdf_path.read_bytes())

    # ---- pre-flight, in a throwaway COPY: resolve legs, check capability + keys
    pre = _make_copy(source_db, prefix="triage_preflight_", db_name=db_name)
    try:
        default_primary, default_fallback = _current_triage_legs(pre)
        plan: list[dict[str, Any]] = []
        for cand in wanted:
            eff_primary = cand["primary"] or default_primary
            eff_fallback = cand["fallback"] or default_fallback
            notes: list[str] = []
            if cand["primary"] is None:
                notes.append(f"primary not given — using the copy's current primary "
                             f"{eff_primary[0]}:{eff_primary[1]}")
            if cand["fallback"] is None:
                notes.append(f"fallback not given — using the copy's current fallback "
                             f"{eff_fallback[0]}:{eff_fallback[1]}")
            applied = _apply_triage_assignment(
                pre, primary=eff_primary, fallback=eff_fallback, force_model=force_model,
            )
            applied["notes"] = notes + applied["notes"]
            plan.append({
                "name": cand["name"], "primary": eff_primary, "fallback": eff_fallback,
                "applied": applied, "live_image_test": {}, "runs": [],
                "cost_cap_reached": False, "error": None, "totals": {},
            })

        # Keys FIRST: report availability only, and stop before anything runs
        # (or is even tested) when a provider a candidate needs has no key.
        providers = sorted({p for c in plan for p in (c["primary"][0], c["fallback"][0])})
        key_rows, missing_keys = _key_preflight(pre, providers)
        print("Provider keys — resolved through the app's own resolver (vault, then environment):")
        for row in key_rows:
            print(f"  key available: {'yes' if row['available'] else 'no '}  [{row['provider']}]")
        if missing_keys:
            raise InvoiceExtractError(
                "Missing API key for: " + ", ".join(missing_keys)
                + ". Add one on the AI Models screen (or set the provider's environment "
                "variable) — refusing to start so a half-run comparison is not produced."
            )

        # Only now (a key is known to exist) test the legs whose provider has no
        # catalogue in the copy: the app's own live image test stands in for it.
        for cand in plan:
            if not cand["applied"]["skipped"] or force_model:
                continue
            live = _live_image_test(pre, cand["applied"]["skipped"])
            cand["live_image_test"] = live
            failed = [leg for leg, res in live.items() if not res.get("ok")]
            if failed:
                detail = "; ".join(
                    f"{leg}: {live[leg].get('error') or 'the test image was not read'}"
                    for leg in failed
                )
                raise InvoiceExtractError(
                    "Refusing to start — no catalogue to check against and the live image "
                    f"test failed ({detail}). Pass --force-model to run it anyway."
                )

        print("Resolved assignment for invoice_triage (in each candidate's own copy):")
        for cand in plan:
            print(f"  {cand['name']:22} primary={cand['primary'][0]}:{cand['primary'][1]}"
                  f"   fallback={cand['fallback'][0]}:{cand['fallback'][1]}")
    finally:
        if not keep:
            pre.cleanup()

    # ---- run each candidate, one after another, each in its own copy
    for cand in plan:
        slug = _slug(cand["name"])
        cand_dir = out_dir / slug
        cand_dir.mkdir(parents=True, exist_ok=True)
        spent = 0.0
        for index in range(1, repeats + 1):
            if cap is not None and spent > cap:
                cand["cost_cap_reached"] = True
                break
            try:
                result = _run_candidate_once(
                    source_db=source_db, pdf_path=pdf_path, plan=cand, out_dir=cand_dir,
                    slug=slug, expected=expected, client_id=client_id, db_name=db_name,
                    keep=keep, force_model=force_model, index=index,
                )
            except InvoiceExtractError as exc:
                cand["error"] = str(exc)
                break
            cand["runs"].append(result)
            spent += float(result["cost_usd"] or 0.0)
            if cap is not None and spent > cap:
                cand["cost_cap_reached"] = True
                break
        cand["totals"] = {
            "cost_usd": round(spent, 6) if any(r["cost_usd"] is not None for r in cand["runs"]) else None,
            "runs": len(cand["runs"]),
            "elapsed_ms": sum(int(r["elapsed_ms"]) for r in cand["runs"]),
        }

    report_path = out_dir / report_name
    report_path.write_text(_render_comparison_report({
        "pdf": str(pdf_path), "pages_total": pages_total, "source_db": str(source_db),
        "expected": expected, "repeats": repeats, "cap": cap, "force_model": force_model,
        "key_rows": key_rows, "missing_keys": missing_keys, "out_dir": str(out_dir),
        "candidates": plan,
    }))

    failed = sum(
        1 for c in plan for r in c["runs"] if r["analysis"].get("failed")
    )
    summary = {
        "report_path": str(report_path),
        "pdf": str(pdf_path),
        "pages_total": pages_total,
        "source_db": str(source_db),
        "repeats": repeats,
        "max_cost_usd": cap,
        "force_model": force_model,
        "key_rows": key_rows,
        "candidates": [
            {
                "name": c["name"], "primary": ":".join(c["primary"]),
                "fallback": ":".join(c["fallback"]),
                "runs": len(c["runs"]), "cost_cap_reached": c["cost_cap_reached"],
                "error": c["error"],
                "checks_failed": sum(r["analysis"].get("failed", 0) for r in c["runs"]),
            }
            for c in plan
        ],
        "failed": failed,
    }
    return summary


def _run_candidate_once(
    *, source_db: Path, pdf_path: Path, plan: dict[str, Any], out_dir: Path, slug: str,
    expected: _Expected, client_id: Optional[int], db_name: str, keep: bool,
    force_model: bool, index: int,
) -> dict[str, Any]:
    """One scored run of one candidate, in its OWN fresh copy of the database."""
    copy = _make_copy(source_db, prefix=f"triage_{slug}_{index}_", db_name=db_name)
    try:
        _apply_triage_assignment(
            copy, primary=plan["primary"], fallback=plan["fallback"], force_model=force_model,
        )
        from src.clients import service as clients

        conn = _connect(str(copy.db))
        try:
            cid = client_id
            if cid is None:
                row = conn.execute(
                    "SELECT client_id FROM end_clients ORDER BY client_id LIMIT 1"
                ).fetchone()
                cid = int(row[0]) if row else None
        finally:
            conn.close()
        if cid is None:
            try:
                cid = clients.create_client(
                    legal_name="Triage Score Client", actor="triage:score", db_path=str(copy.db),
                )
            except Exception as exc:  # noqa: BLE001 — reported as a clear stop, not a raw traceback
                raise InvoiceExtractError(
                    "The database copy has no client to attribute the run to, and a "
                    f"placeholder one could not be created ({exc}). Pass --client-id <id>."
                ) from exc

        upload_id = _score_upload(copy.db, pdf_path, cid)
        started = time.monotonic()
        queued = run_triage(upload_id, actor="triage:score", db_path=str(copy.db), include_disabled=True)
        run = wait_for_run(int(queued["run_id"]), db_path=str(copy.db)) or queued
        elapsed_ms = int((time.monotonic() - started) * 1000)

        triage = get_triage(upload_id, db_path=str(copy.db))
        analysis = _compare(triage, run, expected)
        analysis["disagreements"] = _disagreements(triage, expected)
        pngs = _annotate_pngs(upload_id, out_dir, db_path=str(copy.db))
        return {
            "index": index,
            "status": run.get("status"),
            "cost_usd": run.get("cost_usd"),
            "input_tokens": run.get("input_tokens"),
            "output_tokens": run.get("output_tokens"),
            "elapsed_ms": elapsed_ms,
            "pages_total": run.get("pages_total"),
            "pages_failed": run.get("pages_failed"),
            "fallback_pages": run.get("fallback_pages"),
            "documents": len(triage["documents"]),
            "analysis": analysis,
            "pngs": [str(p) for p in pngs],
            "db_copy": str(copy.db),
            "upload_id": upload_id,
            "client_id": cid,
        }
    finally:
        if not keep:
            copy.cleanup()


def _disagreements(triage: dict[str, Any], expected: _Expected) -> dict[str, Any]:
    """Where the run differs from the expected bundle (ordinal-matched)."""
    docs = sorted(triage["documents"], key=lambda d: d["ordinal"])
    exp_docs = expected.data.get("documents", [])
    by_ordinal = {int(e.get("ordinal", 0)): e for e in exp_docs}
    found_ordinals = {d["ordinal"] for d in docs}

    page_mismatch, category_mismatch = [], []
    for d in docs:
        exp = by_ordinal.get(d["ordinal"])
        if exp is None:
            continue
        got_pages = _doc_pages(d)
        want_pages = sorted(exp.get("pages", []))
        if got_pages != want_pages:
            page_mismatch.append({
                "ordinal": d["ordinal"], "label": d.get("label") or "",
                "found": got_pages, "expected": want_pages,
            })
        if not _doc_category_ok(d, exp):
            category_mismatch.append({
                "ordinal": d["ordinal"], "label": d.get("label") or "",
                "found": d["category"], "expected": exp.get("category") or exp.get("acceptable"),
            })
    return {
        "page_mismatch": page_mismatch,
        "category_mismatch": category_mismatch,
        "extra_documents": [
            {"ordinal": d["ordinal"], "label": d.get("label") or "", "category": d["category"],
             "pages": _doc_pages(d)}
            for d in docs if d["ordinal"] not in by_ordinal
        ],
        "missed_documents": [
            {"ordinal": int(e.get("ordinal", 0)), "label": e.get("label") or "",
             "category": e.get("category") or e.get("acceptable"), "pages": e.get("pages", [])}
            for e in exp_docs if int(e.get("ordinal", 0)) not in found_ordinals
        ],
        "unassigned": triage.get("unassigned", []),
    }


def _score_upload(tmp_db: Path, pdf_path: Path, client_id: int) -> int:
    from src.invoice_extract import extractor

    conn = _connect(str(tmp_db))
    try:
        data = pdf_path.read_bytes()
        source_format = extractor.source_format_for(pdf_path.name)
        upload_id = idb.create_upload(
            conn, client_id=client_id, filename=pdf_path.name, file_ext=pdf_path.suffix.lstrip("."),
            source_format=source_format, extraction_path="structured", batch_id=None,
            status="extracted", duplicate_warning=False, actor="triage:score", file_bytes=data,
        )
    finally:
        conn.close()
    return upload_id


def _compare(triage: dict[str, Any], result: dict[str, Any], expected: _Expected) -> dict[str, Any]:
    exp = expected.data
    docs = sorted(triage["documents"], key=lambda d: d["ordinal"])
    exp_docs = exp.get("documents", [])

    covered = {r["page"] for d in docs for r in d["regions"]}
    pages_total = len(triage["pages"])
    unassigned = triage["unassigned"]
    every_page_covered = (covered | {u["page"] for u in unassigned}) == set(range(1, pages_total + 1))

    checks: list[dict[str, Any]] = []

    checks.append({
        "name": "every page accounted for",
        "pass": bool(every_page_covered),
        "detail": f"{len(covered)} covered + {len(unassigned)} unassigned of {pages_total}",
    })
    checks.append({
        "name": "document count",
        "pass": (len(docs) == exp.get("documents_total")) if exp.get("documents_total") else None,
        "detail": f"{len(docs)} found, expected {exp.get('documents_total')}",
    })

    # Page-to-document assignment + categories, matched by ordinal.
    assign_ok = True
    assign_detail: list[str] = []
    cat_ok = True
    cat_detail: list[str] = []
    for e in exp_docs:
        ord_i = int(e.get("ordinal", 0))
        got = next((d for d in docs if d["ordinal"] == ord_i), None)
        if got is None:
            assign_ok = False
            cat_ok = False
            assign_detail.append(f"#{ord_i} missing")
            continue
        got_pages = _doc_pages(got)
        if got_pages != sorted(e.get("pages", [])):
            assign_ok = False
            assign_detail.append(f"#{ord_i} pages {got_pages} != {e.get('pages')}")
        if not _doc_category_ok(got, e):
            cat_ok = False
            cat_detail.append(f"#{ord_i} {got['category']} != {e.get('category') or e.get('acceptable')}")
    checks.append({
        "name": "page-to-document assignment",
        "pass": assign_ok,
        "detail": "; ".join(assign_detail) or "all documents on the expected pages",
    })
    checks.append({
        "name": "categories",
        "pass": cat_ok,
        "detail": "; ".join(cat_detail) or "all categories as expected",
    })

    # The three page-1 boxes: IoU >= 0.5 each.
    page1 = [d for d in docs if 1 in _doc_pages(d)]
    exp_boxes = exp.get("page1_boxes", [])
    ious: list[dict[str, Any]] = []
    for e in exp_boxes:
        target = [x for x in e["box"]]  # [x0,y0,x1,y1]
        best = 0.0
        best_label = None
        for d in page1:
            for r in d["regions"]:
                if r["page"] != 1:
                    continue
                cand = [r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]]
                val = _iou(cand, target)
                if val > best:
                    best, best_label = val, d["label"]
        ious.append({"label": e.get("label"), "iou": round(best, 3), "matched": best_label})
    checks.append({
        "name": "page-1 region boxes (IoU >= 0.5)",
        "pass": all(i["iou"] >= 0.5 for i in ious) if ious else None,
        "detail": "; ".join(f"{i['label']}: {i['iou']}" for i in ious) or "no expected boxes",
        "ious": ious,
    })

    # Duplicate pages.
    dup_expected = {int(k): int(v) for k, v in (exp.get("duplicate_pages") or {}).items()}
    dup_got = {p["page"]: p["duplicate_of_page"] for p in triage["pages"] if p["duplicate_of_page"]}
    dup_ok = all(dup_got.get(k) == v for k, v in dup_expected.items()) if dup_expected else None
    checks.append({
        "name": "duplicate page detection",
        "pass": dup_ok,
        "detail": f"expected {dup_expected or '—'}, got {dup_got or '—'}",
    })

    # Rotation of the expected rotated pages.
    rot_expected = exp.get("rotated_pages") or []
    rot_got = {p["page"]: p["rotation_deg"] for p in triage["pages"]}
    rot_ok = all(rot_got.get(p, 0) != 0 for p in rot_expected) if rot_expected else None
    checks.append({
        "name": "rotated pages detected",
        "pass": rot_ok,
        "detail": "; ".join(f"p{p}: {rot_got.get(p, 0)}°" for p in rot_expected) or "no expected rotations",
    })

    passed = sum(1 for c in checks if c["pass"] is True)
    failed = sum(1 for c in checks if c["pass"] is False)
    unknown = sum(1 for c in checks if c["pass"] is None)
    return {
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "unknown": unknown,
        "documents": len(docs),
        "grand_total": {"input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens"),
                        "cost_usd": result.get("cost_usd"), "status": result.get("status")},
    }


def _check_marks(analysis: dict[str, Any]) -> str:
    return ", ".join(
        f"{c['name']}=" + ("pass" if c["pass"] is True else ("FAIL" if c["pass"] is False else "n/a"))
        for c in analysis["checks"]
    )


def _money(value: Any) -> str:
    """A money cell that is honest about a copy whose catalogue has no price."""
    if isinstance(value, (int, float)):
        return f"{float(value):.6f} USD"
    return "unknown (no price for this model in the copy's catalogue)"


def _render_comparison_report(payload: dict[str, Any]) -> str:
    """ONE Markdown report for every candidate. No images inside — annotated
    page PNGs are referenced by path (they live in the git-ignored report dir)."""
    expected = payload["expected"]
    cands = payload["candidates"]
    exp_total = expected.data.get("documents_total")
    lines: list[str] = []
    lines.append("# Invoice triage — model comparison report")
    lines.append("")
    lines.append(f"- Fixture: `{payload['pdf']}` ({payload['pages_total']} pages)")
    lines.append(f"- Source database: `{payload['source_db']}` — **copied**; the source is opened read-only and is never written to")
    lines.append(f"- Expected bundle: `{expected.path}` — "
                 + ("**provisional, not yet verified**" if not expected.verified else "verified"))
    lines.append(f"- Candidates: **{len(cands)}** · repeats per candidate: **{payload['repeats']}**"
                 f" · cost cap per candidate: **{'$' + format(payload['cap'], '.2f') if payload['cap'] is not None else 'none'}**")
    lines.append(f"- `--force-model`: **{'used' if payload['force_model'] else 'not used'}**")
    lines.append("")
    lines.append("**Provider keys** (via the app's own resolver: vault first, then environment)."
                 " Only availability is reported — no key is printed, logged or copied.")
    lines.append("")
    lines.append("| provider | key available |")
    lines.append("|---|---|")
    for row in payload["key_rows"]:
        lines.append(f"| {row['provider']} | {'yes' if row['available'] else 'no'} |")
    lines.append("")
    lines.append("## Resolved assignment (invoice_triage, set in each candidate's own copy)")
    lines.append("")
    lines.append("| candidate | primary | fallback | notes |")
    lines.append("|---|---|---|---|")
    for cand in cands:
        notes = "; ".join(cand["applied"].get("notes") or []) or "—"
        lines.append(
            f"| {cand['name']} | `{cand['primary'][0]}:{cand['primary'][1]}` "
            f"| `{cand['fallback'][0]}:{cand['fallback'][1]}` | {notes} |"
        )
    lines.append("")
    lines.append("## Comparison")
    lines.append("")
    lines.append("| candidate | models (primary → fallback) | cost (USD) | time (ms) | pages failed "
                 "| pages served by fallback | documents found"
                 + (f" (expected {exp_total})" if exp_total else "") + " | checks |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for cand in cands:
        models = f"`{cand['primary'][1]}` → `{cand['fallback'][1]}`"
        if cand["error"]:
            lines.append(f"| {cand['name']} | {models} | — | — | — | — | — | **error: {cand['error']}** |")
            continue
        runs = cand["runs"]
        if not runs:
            lines.append(f"| {cand['name']} | {models} | — | — | — | — | — | not run |")
            continue
        cost = cand["totals"].get("cost_usd")
        cost_txt = f"{cost:.6f}" if isinstance(cost, (int, float)) else "unknown"
        if cand["cost_cap_reached"]:
            cost_txt += " **(cost cap reached)**"
        totals = [f"{len(runs)} run(s)"]
        for key in ("pages_failed", "fallback_pages", "documents"):
            values = [r[key] for r in runs]
            totals.append("—" if all(v is None for v in values) else "/".join(str(v) for v in values))
        marks = "; ".join(_check_marks(r["analysis"]) for r in runs)
        lines.append(
            f"| {cand['name']} | {models} | {cost_txt} | {cand['totals'].get('elapsed_ms')} "
            f"| {totals[1]} | {totals[2]} | {totals[3]} | {marks} |"
        )
    lines.append("")
    lines.append("Cells with `a/b` are per-repeat when more than one run was made.")
    lines.append("")

    for cand in cands:
        lines.append(f"## Candidate: {cand['name']}")
        lines.append("")
        lines.append(f"- Resolved primary: `{cand['primary'][0]}:{cand['primary'][1]}`")
        lines.append(f"- Resolved fallback: `{cand['fallback'][0]}:{cand['fallback'][1]}`")
        if cand["applied"].get("forced"):
            lines.append("- **`--force-model` was used** — the image-capability check refused this "
                         "assignment and it was overridden explicitly.")
        for note in cand["applied"].get("notes") or []:
            lines.append(f"- Note: {note}")
        if cand.get("live_image_test"):
            for leg, res in cand["live_image_test"].items():
                state = "passed" if res.get("ok") else "FAILED"
                extra = res.get("error") or res.get("sample") or ""
                lines.append(f"- Live image test ({leg} leg): **{state}**"
                             + (f" — {extra}" if extra else ""))
        if cand["error"]:
            lines.append(f"- **Stopped:** {cand['error']}")
        if cand["cost_cap_reached"]:
            lines.append(f"- **Cost cap reached** (cap "
                         f"${format(payload['cap'], '.2f')}; spent "
                         f"${cand['totals'].get('cost_usd')}) — this candidate was stopped.")
        lines.append("")
        if not cand["runs"]:
            lines.append("_No run was produced for this candidate._")
            lines.append("")
            continue
        for run in cand["runs"]:
            analysis = run["analysis"]
            lines.append(f"### {cand['name']} — repeat {run['index']}")
            lines.append("")
            lines.append(f"- Status: `{run['status']}` · documents found: **{run['documents']}**"
                         + (f" (expected {exp_total})" if exp_total else ""))
            lines.append(f"- Cost: {_money(run['cost_usd'])} · tokens: in {run['input_tokens']}, "
                         f"out {run['output_tokens']} · time: {run['elapsed_ms']} ms")
            lines.append(f"- Pages: {run['pages_total']} total · {run['pages_failed']} failed · "
                         f"{run['fallback_pages']} served by the fallback leg")
            lines.append(f"- Checks: passed {analysis['passed']} / failed {analysis['failed']} "
                         f"/ n/a {analysis['unknown']}")
            lines.append(f"- Worked in the copy `{run['db_copy']}`")
            lines.append("")
            lines.append("| check | result | detail |")
            lines.append("|---|---|---|")
            for check in analysis["checks"]:
                mark = "PASS" if check["pass"] is True else ("FAIL" if check["pass"] is False else "n/a")
                lines.append(f"| {check['name']} | {mark} | {check['detail']} |")
            lines.append("")
            for check in analysis["checks"]:
                if check.get("ious"):
                    lines.append("Page-1 region boxes (IoU, pass at ≥ 0.5):")
                    lines.append("")
                    lines.append("| expected box | IoU | matched label |")
                    lines.append("|---|---|---|")
                    for iou in check["ious"]:
                        lines.append(f"| {iou['label']} | {iou['iou']} | {iou['matched'] or '—'} |")
                    lines.append("")

            disag = analysis.get("disagreements") or {}
            lines.append("**Disagreements with the expected bundle**")
            lines.append("")
            if not any(disag.get(k) for k in ("page_mismatch", "category_mismatch")):
                lines.append("None — every expected document matched on pages and category.")
            for item in disag.get("page_mismatch", []):
                pages = set(item["found"]) | set(item["expected"])
                refs = ", ".join(_png_ref(run, p) for p in sorted(pages))
                lines.append(f"- #{item['ordinal']} `{item['label']}` pages found {item['found']} "
                             f"vs expected {item['expected']} — annotated page(s): {refs}")
            for item in disag.get("category_mismatch", []):
                refs = ", ".join(_png_ref(run, p) for p in _expected_pages(disag, item["ordinal"]))
                lines.append(f"- #{item['ordinal']} `{item['label']}` category found "
                             f"`{item['found']}` vs expected {item['expected']} — annotated page(s): {refs}")
            lines.append("")
            unassigned = disag.get("unassigned") or []
            if unassigned:
                lines.append("Pages with no document (each with its reason): "
                             + ", ".join(f"p{u['page']} ({u['reason']})" for u in unassigned))
                lines.append("")
            lines.append("**Documents the run found that the expected bundle does not list**")
            lines.append("")
            extra = disag.get("extra_documents") or []
            if not extra:
                lines.append("None.")
            for item in extra:
                refs = ", ".join(_png_ref(run, p) for p in item["pages"])
                lines.append(f"- #{item['ordinal']} `{item['label']}` `{item['category']}` "
                             f"pages {item['pages']} — {refs}")
            lines.append("")
            lines.append("**Expected documents the run did not find**")
            lines.append("")
            missed = disag.get("missed_documents") or []
            if not missed:
                lines.append("None.")
            for item in missed:
                lines.append(f"- #{item['ordinal']} `{item['label']}` {item['category']} "
                             f"pages {item['pages']}")
            lines.append("")
            lines.append("**Annotated pages** (PNG files, referenced by path — no images are embedded here)")
            lines.append("")
            if run["pngs"]:
                lines.append(f"{len(run['pngs'])} file(s) in `{Path(run['pngs'][0]).parent}`:")
                lines.append("")
                for path in run["pngs"]:
                    lines.append(f"- `{path}`")
            else:
                lines.append("None written.")
            lines.append("")

    lines.append("## What could not be checked")
    lines.append("")
    unable: list[str] = []
    if payload["cap"] is not None:
        unable.append(
            "The cost cap is enforced per candidate between runs: a single run cannot be "
            "interrupted mid-flight (the triage worker has no cancel path), so a run that "
            "overshoots the cap finishes and the candidate is then stopped."
        )
    for cand in cands:
        if cand["error"]:
            unable.append(f"{cand['name']}: stopped early — {cand['error']}")
        if cand["cost_cap_reached"]:
            unable.append(f"{cand['name']}: remaining repeats skipped (cost cap reached).")
        if cand.get("live_image_test") and any(
            not r.get("ok") for r in cand["live_image_test"].values()
        ):
            unable.append(f"{cand['name']}: a live image test failed.")
    if not expected.verified:
        unable.append("The expected bundle is provisional (`\"verified\": false`), so every "
                      "expectation compared against is unconfirmed.")
    if not unable:
        unable.append("Nothing — every requested check produced a result.")
    for item in unable:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _png_ref(run: dict[str, Any], page: int) -> str:
    """The annotated PNG path for a page, so a disagreement points at its image."""
    for path in run.get("pngs") or []:
        if path.endswith(f"page_{page:03d}.png"):
            return f"`{path}`"
    return f"`(no annotated page for p{page})`"


def _expected_pages(disagreements: dict[str, Any], ordinal: int) -> list[int]:
    for item in (disagreements.get("missed_documents") or []):
        if item.get("ordinal") == ordinal:
            return list(item.get("pages") or [])
    for item in (disagreements.get("extra_documents") or []):
        if item.get("ordinal") == ordinal:
            return list(item.get("pages") or [])
    for item in (disagreements.get("page_mismatch") or []):
        if item.get("ordinal") == ordinal:
            return sorted(set(item.get("found") or []) | set(item.get("expected") or []))
    return []


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.invoice_extract.triage")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run triage on an existing upload")
    run_p.add_argument("upload_id", type=int)
    run_p.add_argument("--db", default=None, help="database path (default: the app DB)")
    run_p.add_argument("--allow-real-db", action="store_true", help="allow writing the repository's own db/poc.db")

    score_p = sub.add_parser("score", help="score one or more models against a PDF (uses DB COPIES)")
    score_p.add_argument("pdf")
    score_p.add_argument("--out", default=None)
    score_p.add_argument("--expected", default=None)
    score_p.add_argument("--client-id", type=int, default=None)
    score_p.add_argument("--keep", action="store_true", help="keep the temporary DB/data roots")
    score_p.add_argument(
        "--db", default=None,
        help="database to COPY for the run (default: the app database). The source is opened "
             "read-only and is never modified.",
    )
    score_p.add_argument(
        "--primary", action="append", default=None, metavar="PROVIDER:MODEL",
        help="invoice_triage primary leg, set in the COPY only (repeatable — each value becomes "
             "its own candidate)",
    )
    score_p.add_argument(
        "--fallback", action="append", default=None, metavar="PROVIDER:MODEL",
        help="invoice_triage fallback leg, set in the COPY only (repeatable, paired with --primary)",
    )
    score_p.add_argument(
        "--candidates", default=None, metavar="FILE.json",
        help="JSON list of {name, primary, fallback}; each runs in its own copy",
    )
    score_p.add_argument("--repeat", type=int, default=1, help="runs per candidate (default 1)")
    score_p.add_argument(
        "--max-cost-usd", type=float, default=COST_CAP_DEFAULT, dest="max_cost_usd",
        help=f"per-candidate cost cap in USD (default {COST_CAP_DEFAULT:.2f}); the candidate is "
             "stopped and marked 'cost cap reached' when exceeded",
    )
    score_p.add_argument(
        "--force-model", action="store_true",
        help="run even when a leg is not image-capable (recorded in the report)",
    )

    args = parser.parse_args(argv)
    from src import db as _db
    _db.require_real_db_allowed(
        getattr(args, "db", None), allow_real_db=getattr(args, "allow_real_db", False),
    )
    try:
        if args.cmd == "run":
            return _cmd_run(args.upload_id, db_path=args.db)
        if args.cmd == "score":
            result = score(
                args.pdf,
                out_dir=Path(args.out) if args.out else None,
                expected_path=Path(args.expected) if args.expected else None,
                client_id=args.client_id, keep=args.keep, db_path=args.db,
                primary=args.primary, fallback=args.fallback,
                candidates_file=args.candidates, repeat=args.repeat,
                max_cost_usd=args.max_cost_usd, force_model=args.force_model,
            )
            print(json.dumps(result, indent=2, default=str))
            print(f"\nReport: {result['report_path']}")
            return 0 if result["failed"] == 0 else 1
    except InvoiceExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
