"""Invoice Extraction Phase 2 — read each document from its own region crops.

WHAT PHASE 2 DOES
-----------------
Phase 1 (``triage``) found the documents. Phase 2, for each eligible document:

1. cuts the document out of the page (its Phase-1 regions, padded, upright),
2. reads EVERY crop in ONE call — "Read A" — into the canonical field set,
3. takes a SECOND, independent read — "Read B" — through a DIFFERENT
   touchpoint, BLIND to read A, from a DIFFERENT provider,
4. records both verbatim values per field, how they compare, where on the page
   each value sits, and any handwritten note or stamp as an annotation.

WHAT IT DOES NOT DO
-------------------
* No confidence scores (Phase 4).
* No tax rules, no rate derivation, no direction detection, no screen changes.
* It never chooses a winner between the two reads — both values are stored.

THE LEGACY PATH IS UNTOUCHED
----------------------------
Everything here is behind ``invoice_extract.v2.enabled`` (default FALSE). The
legacy extraction path does not import this module, and nothing in the normal
upload flow calls it.

ONE EXTERNAL SEAM
-----------------
:func:`_call_extract_model` is the ONLY place this module touches a model. The
test suite stubs that one function, which keeps every test free of live calls.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src import db as recon_db
from src.invoice_extract import crops as crops_mod
from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import normalisers as nz
from src.invoice_extract.doc_extract_schema import (
    EXTRACTION_UNFINISHED,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
)
from src.invoice_extract.service import InvoiceExtractError, _connect

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- touchpoints -----------------------------------------------------------
TOUCHPOINT_READ_A = "invoice_extraction_visual"     # the same 'read the page' job as Phase 1
TOUCHPOINT_READ_B = "invoice_extraction_verify"     # the independent second read

# --- settings --------------------------------------------------------------
KEY_ENABLED = "invoice_extract.v2.enabled"
KEY_DPI = "invoice_extract.v2.dpi"
KEY_MAX_EDGE = "invoice_extract.v2.max_edge_px"
KEY_CONCURRENCY = "invoice_extract.v2.concurrency"
KEY_MAX_COST = "invoice_extract.v2.max_cost_usd"
KEY_PADDING = "invoice_extract.v2.padding_frac"
KEY_TIMEOUT = "invoice_extract.v2.timeout_seconds"
KEY_RETRIES = "invoice_extract.v2.retries"

# Same retry/backoff shape as the Phase-1 amendments: transient failures
# (429 / timeout / 5xx) retry on the SAME leg with exponential backoff before
# failing over to the fallback leg.
EXTRACT_RETRY_POLICY: dict[str, Any] = {
    "attempts": 3, "base_delay": 1.0, "max_delay": 10.0, "jitter": 0.5,
}

#: Only these categories are extracted. Everything else is skipped WITH A
#: REASON — an excluded document is not the same as a document that vanished.
TARGET_CATEGORIES: tuple[str, ...] = ("tax_invoice", "no_gst_expense")

DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "invoice_extract_v2"
SCORE_REPORT_NAME = "extract_v2_score_report.md"

_ANNOTATION_KINDS = ("tds", "payment", "cheque", "other", "unknown")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractConfig:
    enabled: bool = False
    dpi: int = 200
    max_edge_px: int = 2000
    concurrency: int = 4
    max_cost_usd: float = 2.00
    padding_frac: float = 0.03
    timeout_seconds: int = 120
    retries: int = 1

    def as_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "dpi": self.dpi, "max_edge_px": self.max_edge_px,
            "concurrency": self.concurrency, "max_cost_usd": self.max_cost_usd,
            "padding_frac": self.padding_frac, "timeout_seconds": self.timeout_seconds,
            "retries": self.retries,
        }


def _setting_str(key: str, default: Optional[str], db_path) -> Optional[str]:
    try:
        from src.settings import service as settings

        return settings.get_setting_str(key, default=default, db_path=db_path)
    except Exception:  # noqa: BLE001 — settings must never break extraction
        return default


def _setting_int(key: str, default: int, db_path) -> int:
    raw = _setting_str(key, None, db_path)
    if raw is None:
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _setting_float(key: str, default: float, db_path) -> float:
    raw = _setting_str(key, None, db_path)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _setting_bool(key: str, default: bool, db_path) -> bool:
    raw = _setting_str(key, None, db_path)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def get_config(*, db_path=None) -> ExtractConfig:
    """The Phase-2 settings, all read from C3 with the spec's defaults."""
    return ExtractConfig(
        enabled=_setting_bool(KEY_ENABLED, False, db_path),
        dpi=max(36, _setting_int(KEY_DPI, 200, db_path)),
        max_edge_px=max(200, _setting_int(KEY_MAX_EDGE, 2000, db_path)),
        concurrency=max(1, _setting_int(KEY_CONCURRENCY, 4, db_path)),
        max_cost_usd=max(0.0, _setting_float(KEY_MAX_COST, 2.00, db_path)),
        padding_frac=max(0.0, min(0.2, _setting_float(KEY_PADDING, 0.03, db_path))),
        timeout_seconds=max(10, _setting_int(KEY_TIMEOUT, 120, db_path)),
        retries=max(0, _setting_int(KEY_RETRIES, 1, db_path)),
    )


def require_enabled(db_path=None) -> None:
    """Refuse when the v2 pipeline is off — the ONE gate every v2 service uses.

    ``start_extraction`` keeps its own message (it can be forced past the flag,
    which is how the scoring harness runs); everything else that reads or writes
    v2 data calls this instead of re-reading the setting itself.
    """
    if not get_config(db_path=db_path).enabled:
        raise InvoiceExtractError(
            f"The v2 invoice extraction is OFF ({KEY_ENABLED} is false). The legacy "
            "extraction path is unaffected; turn the setting on to use it."
        )


# ---------------------------------------------------------------------------
# The prompt (PRINTED values only — the model never computes or infers)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a field-reading engine for an Indian accounting firm. You are "
    "shown the CROPPED IMAGE(S) of ONE document. Read ONLY what is PRINTED on "
    "the page. Respond with JSON only.\n"
    "Rules:\n"
    "1. TRANSCRIBE VERBATIM exactly as printed. Never calculate, never round, "
    "never reformat, never convert a rate or an amount, never add a currency "
    "symbol or a thousands separator that is not printed.\n"
    "2. If a field is NOT PRINTED on the document, return null for its value. "
    "NEVER guess, infer or compute a value. A rate that is not printed is "
    "null — do NOT derive it from the tax and the taxable value.\n"
    "3. Mark every value you return as printed (handwritten=false) or "
    "handwritten (handwritten=true). A value written by hand is NOT a field "
    "value: return it ONLY inside \"annotations\" and leave the field null.\n"
    "4. Handwritten notes, stamps, rubber-stamp texts, signatures and "
    "scribbles go in \"annotations\" — NEVER in a field.\n"
    "5. The invoice total, tax amounts and taxable value come from the "
    "PRINTED summary block, even when a handwritten amount sits beside it.\n"
    "6. Read the line items in printed order as line.1, line.2, ... Give each "
    "printed line its own description, HSN/SAC, quantity, rate and amount.\n"
    "7. For EVERY value, also give its box: x, y (top-left) and w, h (size) as "
    "FRACTIONS of the CROP you are looking at (0 to 1, top-left origin), and "
    "the 1-based index of the region the value came from (\"region\": 1 for "
    "Region 1).\n"
    "8. Never merge two different documents. You are shown ONE document.\n"
)

_USER_PROMPT_TEMPLATE = (
    "This document is shown as {n} crop(s):\n"
    "{regions}\n"
    "\n"
    "Fields to read (use these exact keys):\n"
    "{fields}\n"
    "\n"
    "Return exactly this JSON shape:\n"
    "{{\n"
    '  "fields": [\n'
    "    {{\n"
    '      "key": "invoice_number",\n'
    '      "value": "<verbatim as printed, or null when not printed>",\n'
    '      "handwritten": false,\n'
    '      "region": 1,\n'
    '      "box": {{"x": 0.10, "y": 0.05, "w": 0.30, "h": 0.04}}\n'
    "    }}\n"
    "  ],\n"
    '  "annotations": [\n'
    "    {{\n"
    '      "text": "<verbatim handwritten text>",\n'
    '      "kind_guess": "tds|payment|cheque|other|unknown",\n'
    '      "region": 1,\n'
    '      "box": {{"x": 0.10, "y": 0.70, "w": 0.20, "h": 0.05}}\n'
    "    }}\n"
    "  ]\n"
    "}}"
)


def _user_prompt(crops: list[crops_mod.Crop]) -> str:
    regions = "\n".join(
        f"  - {crop.label} ({crop.px_w}x{crop.px_h} px, upright)"
        for crop in crops
    )
    return _USER_PROMPT_TEMPLATE.format(
        n=len(crops), regions=regions, fields=fv2.prompt_field_list(),
    )


# ---------------------------------------------------------------------------
# THE single external seam (tests stub this)
# ---------------------------------------------------------------------------


def _call_extract_model(
    *,
    touchpoint: str,
    system_prompt: str,
    user_prompt: str,
    crops: list[crops_mod.Crop],
    upload_id: Optional[int],
    document_id: Optional[int],
    extraction_id: Optional[int],
    actor: str,
    timeout: int,
    db_path=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One read (A or B) through the gateway, with all crops in ONE call.

    ``images`` carries every crop in region order, so a document spread over
    two pages costs one call and the model can follow a value across the page
    break. The gateway applies the vision capability guard, the vision
    concurrency limit and the retry/backoff policy.

    This is the ONE place Phase 2 touches a model: stub this to test the whole
    pipeline without a network.
    """
    from src.ai_models import gateway

    images = [("image/png", crop.png) for crop in crops]
    return gateway.call_touchpoint_json(
        touchpoint,
        system_prompt,
        user_prompt,
        images=images,
        max_tokens=4000,
        temperature=0.0,
        actor=actor,
        timeout=timeout,
        reference_type="doc_extraction",
        reference_id=extraction_id,
        upload_id=upload_id,
        retry_policy=EXTRACT_RETRY_POLICY,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Read-A/B reply normalisation + validation
# ---------------------------------------------------------------------------


@dataclass
class ReadField:
    key: str
    value_a: Optional[str]
    raw: dict[str, Any]               # the model's own field object
    is_handwritten: bool
    region_index: Optional[int]
    crop_box: Optional[tuple[float, float, float, float]]


@dataclass
class ReadResult:
    fields: dict[str, ReadField]
    annotations: list[dict[str, Any]]


def _parse_read(reply: dict[str, Any]) -> ReadResult:
    """Turn one read's JSON reply into a keyed, typed result."""
    fields: dict[str, ReadField] = {}
    raw_fields = reply.get("fields")
    if isinstance(raw_fields, list):
        for item in raw_fields:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key or fv2.field_spec(key) is None:
                continue
            if key in fields:
                continue                      # first one wins; a duplicate key is a model bug
            raw_value = item.get("value")
            value = None if raw_value is None else str(raw_value).strip()
            if value == "":
                value = None
            try:
                region_index = int(item.get("region")) if item.get("region") is not None else None
            except (TypeError, ValueError):
                region_index = None
            fields[key] = ReadField(
                key=key, value_a=value, raw=item,
                is_handwritten=bool(item.get("handwritten")),
                region_index=region_index,
                crop_box=crops_mod.parse_crop_box(item.get("box")),
            )

    annotations: list[dict[str, Any]] = []
    raw_annotations = reply.get("annotations")
    if isinstance(raw_annotations, list):
        for item in raw_annotations:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text is None or not str(text).strip():
                continue
            kind = str(item.get("kind_guess") or "unknown").strip().lower()
            try:
                region_index = int(item.get("region")) if item.get("region") is not None else None
            except (TypeError, ValueError):
                region_index = None
            annotations.append({
                "text": str(text).strip(),
                "kind_guess": kind if kind in _ANNOTATION_KINDS else "unknown",
                "region_index": region_index,
                "crop_box": crops_mod.parse_crop_box(item.get("box")),
            })
    return ReadResult(fields=fields, annotations=annotations)


def _region_crop(crops: list[crops_mod.Crop], index: Optional[int]) -> Optional[crops_mod.Crop]:
    if index is None:
        return None
    for crop in crops:
        if crop.region_index == index:
            return crop
    return None


def _place(
    read: ReadField, crops: list[crops_mod.Crop], *, fallback: crops_mod.Crop,
) -> tuple[Optional[tuple[float, float, float, float]], crops_mod.Crop]:
    """Where a value sits: the model named a region (preferred) or we fall back
    to the first crop of the document. Returns (page box or None, crop used)."""
    crop = _region_crop(crops, read.region_index) or fallback
    if read.crop_box is None:
        return None, crop
    return crops_mod.crop_box_to_page(read.crop_box, crop), crop


def _handwriting_guard(
    key: str,
    value: Optional[str],
    page_box: Optional[tuple[float, float, float, float]],
    annotations: list[dict[str, Any]],
) -> Optional[str]:
    """The deterministic guard: a numeric value that EQUALS an annotation's
    amount, and whose box the annotation actually COVERS, needs the printed one.

    Returns the matching annotation text when the guard fires, else None. This
    catches the case the prompt alone cannot: a handwritten total scribbled over
    the printed block, where the model reports the printed number but the page
    is genuinely ambiguous.

    BOTH halves are required. Equal amounts alone are not enough — the same
    figure appears many times on a real invoice and a handwritten "6,825" in
    the margin says nothing about a printed 6,825.00 elsewhere. And the overlap
    must be SUBSTANTIAL: at least ``crops_mod.HANDWRITING_OVERLAP_MIN`` of the
    value's own box, so a figure written in the neighbouring column cannot flag
    a value it merely touches. ``rules.get_review_model`` re-derives the same
    match from the stored annotation to show the reviewer the figure.
    """
    if value is None or page_box is None:
        return None
    if nz.normalise_number(value) is None:
        return None
    target = nz.normalise_number(value)
    for ann in annotations:
        ann_value = nz.normalise_number(ann.get("text"))
        if ann_value is None or ann_value != target:
            continue
        ann_box = ann.get("page_box")
        if ann_box is None:
            continue
        if crops_mod.overlap_ratio_of(page_box, ann_box) >= crops_mod.HANDWRITING_OVERLAP_MIN:
            return str(ann.get("text"))
    return None


# ---------------------------------------------------------------------------
# Text-layer match
# ---------------------------------------------------------------------------


def _page_words(file_bytes: bytes, source_format: str, page: int) -> tuple[list[tuple[float, float, float, float, str]], float, float]:
    """(words, page_width, page_height) in RAW PDF points. Empty for an image."""
    if source_format != "pdf":
        return [], 0.0, 0.0
    import fitz

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        if page < 1 or page > doc.page_count:
            return [], 0.0, 0.0
        fitz_page = doc.load_page(page - 1)
        rect = fitz_page.rect
        words = [
            (float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4]))
            for w in fitz_page.get_text("words")
        ]
        return words, float(rect.width), float(rect.height)
    except Exception:  # noqa: BLE001 — the text layer is a bonus signal, never a blocker
        return [], 0.0, 0.0
    finally:
        doc.close()


def _text_layer_match(
    kind: str,
    value: Optional[str],
    page_box: Optional[tuple[float, float, float, float]],
    page: Optional[int],
    rotation_deg: int,
    words: list[tuple[float, float, float, float, str]],
    page_w: float,
    page_h: float,
) -> Optional[int]:
    """1/0 when the value appears in the PDF text layer inside the value's box,
    None when there is no text layer to search (the source is a scan/image)."""
    if not words or page_w <= 0 or page_h <= 0:
        return None
    if value is None or page_box is None or page is None:
        return 0 if words else None
    raw_box = crops_mod.unrotate_box(page_box, rotation_deg)
    text = crops_mod.text_in_box(words, raw_box, page_w=page_w, page_h=page_h)
    if not text:
        return 0
    needle = nz.normalise(kind, value)
    haystack = nz.normalise(kind, text)
    if needle is None or haystack is None:
        return 0
    # Numbers/identifiers must appear literally; text uses the same token test
    # the A-vs-B comparison uses, so the two agree on what "found" means.
    if kind == fv2.KIND_TEXT:
        return 1 if needle in haystack or nz.text_similarity(needle, haystack) >= nz.NEAR_THRESHOLD else 0
    return 1 if needle in haystack.replace(" ", "") else 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _insert_extraction(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    status: str,
    skipped_reason: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO invoice_doc_extractions (document_id, status, skipped_reason, "
        "started_at, is_active) VALUES (?, ?, ?, ?, 1)",
        (document_id, status, skipped_reason, _now() if status != STATUS_SKIPPED else None),
    )
    return int(cur.lastrowid)


def _update_extraction(conn: sqlite3.Connection, extraction_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE invoice_doc_extractions SET {keys} WHERE extraction_id = ?",
        (*fields.values(), extraction_id),
    )


def _insert_field(conn: sqlite3.Connection, *, extraction_id: int, document_id: int, row: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO invoice_doc_fields (extraction_id, document_id, field_key, status, "
        "value_a, value_b, reads_agree, text_layer_match, is_handwritten, page, x, y, w, h, in_region) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            extraction_id, document_id, row["field_key"], row["status"],
            row.get("value_a"), row.get("value_b"), row.get("reads_agree"),
            row.get("text_layer_match"), 1 if row.get("is_handwritten") else 0,
            row.get("page"), row.get("x"), row.get("y"), row.get("w"), row.get("h"),
            1 if row.get("in_region") else 0,
        ),
    )


def _insert_annotation(conn: sqlite3.Connection, *, extraction_id: int, document_id: int, ann: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO invoice_doc_annotations (extraction_id, document_id, page, x, y, w, h, "
        "text_verbatim, kind_guess, used_as_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            extraction_id, document_id, ann.get("page"), ann.get("x"), ann.get("y"),
            ann.get("w"), ann.get("h"), ann["text"], ann.get("kind_guess") or "unknown",
        ),
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


def configured_providers(*, db_path=None) -> list[str]:
    """Every provider that has a usable key, WITHOUT decrypting anything."""
    from src.ai_models import providers as prov
    from src.ai_models import service as ai_models

    out: list[str] = []
    for key in prov.PROVIDER_SPECS:
        try:
            configured, _ = ai_models.provider_key_configured(key, db_path=db_path)
        except Exception:  # noqa: BLE001
            configured = False
        if configured:
            out.append(key)
    return sorted(out)


def other_providers(serving: Optional[str], *, db_path=None) -> list[str]:
    """Providers other than the one that served read A."""
    return [p for p in configured_providers(db_path=db_path) if p != serving]


def require_independent_provider(serving: Optional[str], *, db_path=None) -> None:
    """Read B must come from a DIFFERENT vendor than read A.

    Raises with a clear message rather than quietly using the same vendor —
    two reads from one vendor are not an independent second opinion.
    """
    others = other_providers(serving, db_path=db_path)
    if others:
        return
    if serving is None:
        raise InvoiceExtractError(
            "Read B needs a second provider, and no provider has a usable key. "
            "Add a key for another provider on the AI Models screen."
        )
    raise InvoiceExtractError(
        f"Read B must come from a different provider than read A (which was served by "
        f"'{serving}'), but no other provider has a usable key. Add a key for another "
        f"provider on the AI Models screen — two reads from '{serving}' would not be an "
        f"independent second opinion."
    )


# ---------------------------------------------------------------------------
# The per-document pipeline
# ---------------------------------------------------------------------------


@dataclass
class DocOutcome:
    document_id: int
    status: str
    skipped_reason: Optional[str] = None
    error: Optional[str] = None
    fields: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)
    provider_a: Optional[str] = None
    model_a: Optional[str] = None
    provider_b: Optional[str] = None
    model_b: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    attempts: int = 0
    elapsed_ms: int = 0
    crops: int = 0


def _usage_of(meta: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    usage = meta.get("usage") or {}
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")
    return (
        int(in_tok) if isinstance(in_tok, (int, float)) else None,
        int(out_tok) if isinstance(out_tok, (int, float)) else None,
    )


def _cost_of(meta: dict[str, Any], *, db_path) -> Optional[float]:
    from src.ai_models import service as ai_models

    in_tok, out_tok = _usage_of(meta)
    try:
        return ai_models.estimate_cost_usd(
            meta.get("provider"), meta.get("model"), in_tok, out_tok, db_path=db_path,
        )
    except Exception:  # noqa: BLE001 — cost is reporting, never a blocker
        return None


def _sum_cost(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None and b is None:
        return None
    return round((a or 0.0) + (b or 0.0), 6)


def _sum_tokens(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None and b is None:
        return None
    return int(a or 0) + int(b or 0)


def extract_document(
    document: dict[str, Any],
    file_bytes: bytes,
    source_format: str,
    *,
    rotations: dict[int, int],
    text_layer_chars: int,
    cfg: ExtractConfig,
    actor: str,
    db_path=None,
    upload_id: Optional[int] = None,
    extraction_id: Optional[int] = None,
) -> DocOutcome:
    """Read ONE document: crops → read A → read B → compare. Never raises.

    Returns the outcome; the caller persists it. Any failure is reported as
    ``status='failed'`` with the reason — a document is never silently empty.
    """
    started = time.monotonic()
    outcome = DocOutcome(document_id=int(document["document_id"]), status=STATUS_FAILED)

    regions = list(document.get("regions") or [])
    if not regions:
        outcome.skipped_reason = "the document has no regions"
        outcome.error = "the document has no regions to crop"
        return outcome

    # ---- 1. crops (a document whose crop cannot be rendered FAILS)
    try:
        crop_list, _renders = crops_mod.render_region_crops(
            file_bytes, source_format, regions, rotations,
            dpi=cfg.dpi, max_edge_px=cfg.max_edge_px, padding_frac=cfg.padding_frac,
        )
    except crops_mod.CropError as exc:
        outcome.error = f"crop failed: {exc}"
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome
    outcome.crops = len(crop_list)

    # ---- 2. read A
    try:
        reply_a, meta_a = _call_extract_model(
            touchpoint=TOUCHPOINT_READ_A,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_user_prompt(crop_list),
            crops=crop_list,
            upload_id=upload_id,
            document_id=int(document["document_id"]),
            extraction_id=extraction_id,
            actor=actor,
            timeout=cfg.timeout_seconds,
            db_path=db_path,
        )
        outcome.attempts += 1
    except Exception as exc:  # noqa: BLE001 — a failed read is an outcome, not a crash
        outcome.error = f"read A failed: {type(exc).__name__}: {exc}"
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome

    read_a = _parse_read(reply_a if isinstance(reply_a, dict) else {})
    outcome.provider_a = meta_a.get("provider")
    outcome.model_a = meta_a.get("model")
    in_a, out_a = _usage_of(meta_a)
    cost_a = _cost_of(meta_a, db_path=db_path)

    # ---- 3. read B must be independent
    try:
        require_independent_provider(outcome.provider_a, db_path=db_path)
    except InvoiceExtractError as exc:
        outcome.error = str(exc)
        outcome.input_tokens, outcome.output_tokens = in_a, out_a
        outcome.cost_usd = cost_a
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome

    try:
        reply_b, meta_b = _call_extract_model(
            touchpoint=TOUCHPOINT_READ_B,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_user_prompt(crop_list),
            crops=crop_list,
            upload_id=upload_id,
            document_id=int(document["document_id"]),
            extraction_id=extraction_id,
            actor=actor,
            timeout=cfg.timeout_seconds,
            db_path=db_path,
        )
        outcome.attempts += 1
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"read B failed: {type(exc).__name__}: {exc}"
        outcome.input_tokens, outcome.output_tokens = in_a, out_a
        outcome.cost_usd = cost_a
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome

    read_b = _parse_read(reply_b if isinstance(reply_b, dict) else {})
    outcome.provider_b = meta_b.get("provider")
    outcome.model_b = meta_b.get("model")
    if outcome.provider_b and outcome.provider_a and outcome.provider_b == outcome.provider_a:
        outcome.error = (
            f"read B was served by '{outcome.provider_b}', the same provider as read A — "
            "the touchpoint "
            f"'{TOUCHPOINT_READ_B}' must be assigned a model from a different provider."
        )
        in_b, out_b = _usage_of(meta_b)
        cost_b = _cost_of(meta_b, db_path=db_path)
        outcome.input_tokens = _sum_tokens(in_a, in_b)
        outcome.output_tokens = _sum_tokens(out_a, out_b)
        outcome.cost_usd = _sum_cost(cost_a, cost_b)
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome

    in_b, out_b = _usage_of(meta_b)
    cost_b = _cost_of(meta_b, db_path=db_path)
    outcome.input_tokens = _sum_tokens(in_a, in_b)
    outcome.output_tokens = _sum_tokens(out_a, out_b)
    outcome.cost_usd = _sum_cost(cost_a, cost_b)

    # ---- 4. annotations (page coords + placement) BEFORE the guard needs them
    annotations: list[dict[str, Any]] = []
    for raw_ann in read_a.annotations:
        crop = _region_crop(crop_list, raw_ann.get("region_index")) or crop_list[0]
        box = (
            crops_mod.crop_box_to_page(raw_ann["crop_box"], crop)
            if raw_ann.get("crop_box") is not None else None
        )
        annotations.append({
            "text": raw_ann["text"],
            "kind_guess": raw_ann.get("kind_guess") or "unknown",
            "page": crop.region_page,
            "page_box": box,
            "x": box[0] if box else None, "y": box[1] if box else None,
            "w": box[2] if box else None, "h": box[3] if box else None,
        })

    # ---- 5. every canonical invoice key gets a row; line keys come from the reads
    keys = list(fv2.INVOICE_KEYS)
    for read in (read_a, read_b):
        for key in read.fields:
            if key not in keys:
                keys.append(key)

    field_rows: list[dict[str, Any]] = []
    for key in keys:
        spec = fv2.field_spec(key)
        if spec is None:
            continue
        a = read_a.fields.get(key)
        b = read_b.fields.get(key)

        page_box: Optional[tuple[float, float, float, float]] = None
        page: Optional[int] = None
        crop_used: Optional[crops_mod.Crop] = None
        if a is not None:
            page_box, crop_used = _place(a, crop_list, fallback=crop_list[0])
            page = crop_used.region_page
        elif b is not None:
            page_box, crop_used = _place(b, crop_list, fallback=crop_list[0])
            page = crop_used.region_page

        in_region = (
            crops_mod.in_any_region(page_box, regions) if page_box is not None else False
        )

        verdict = nz.compare(spec.kind, a.value_a if a else None, b.value_a if b else None)

        status = fv2.STATUS_NOT_PRINTED
        if page_box is not None and not in_region:
            # The value was read, but its box lands outside every region of this
            # document: keep the value, flag where it came from.
            status = fv2.STATUS_OUT_OF_REGION
        elif a is None and b is None:
            status = fv2.STATUS_NOT_PRINTED
        elif a is not None and a.is_handwritten:
            # A handwritten value is not a field value: require the printed one.
            status = fv2.STATUS_NEEDS_PRINTED
        else:
            status = fv2.STATUS_READ

        guard_hit = _handwriting_guard(
            key, (a.value_a if a else None) or (b.value_a if b else None), page_box, annotations,
        )
        if guard_hit is not None and status == fv2.STATUS_READ and spec.monetary:
            status = fv2.STATUS_NEEDS_PRINTED

        tlm = _text_layer_match(
            spec.kind,
            (a.value_a if a else None) or (b.value_a if b else None),
            page_box, page, rotations.get(page or 0, 0),
            *_page_words(file_bytes, source_format, page or 0),
        ) if text_layer_chars > 0 and page else None

        field_rows.append({
            "field_key": key,
            "status": status,
            "value_a": a.value_a if a else None,
            "value_b": b.value_a if b else None,
            "reads_agree": verdict.reads_agree,
            "text_layer_match": tlm,
            "is_handwritten": bool((a and a.is_handwritten) or (b and b.is_handwritten)),
            "page": page,
            "x": page_box[0] if page_box else None,
            "y": page_box[1] if page_box else None,
            "w": page_box[2] if page_box else None,
            "h": page_box[3] if page_box else None,
            "in_region": in_region,
            "guard_hit": guard_hit,
        })

    outcome.fields = field_rows
    outcome.annotations = annotations
    outcome.status = STATUS_COMPLETE if not outcome.error else STATUS_PARTIAL
    outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
    return outcome


# ---------------------------------------------------------------------------
# The job: start / resume / execute, on the Phase-1 persisted-worker pattern
# ---------------------------------------------------------------------------

#: ONE process-wide worker, exactly like ``triage._EXECUTOR``: the DB rows say
#: queued/running, so a restart can resume, and two uploads never interleave
#: their document-level pools.
_EXTRACT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract-worker")

#: upload_id → {"event": Event, "summary": dict|None}. Registered BEFORE the
#: job is submitted, so a caller that waits immediately after starting can
#: never observe "no job" and return early. The event lets a waiter stop as
#: soon as the job is done even when documents were deliberately LEFT QUEUED
#: (the cost cap does exactly that) — those rows wait for a RESUME, not for
#: this job.
_JOB_LOCK = threading.Lock()
_JOBS: dict[int, dict[str, Any]] = {}


def _register_job(upload_id: int) -> dict[str, Any]:
    state: dict[str, Any] = {"event": threading.Event(), "summary": None}
    with _JOB_LOCK:
        _JOBS[upload_id] = state
    return state


def _run_job(upload_id: int, state: dict[str, Any], *, actor: str, db_path=None) -> None:
    try:
        state["summary"] = _execute_extraction(upload_id, actor=actor, db_path=db_path)
    finally:
        state["event"].set()


def job_state(upload_id: int) -> Optional[dict[str, Any]]:
    """The in-process state of this upload's most recent job, if any."""
    with _JOB_LOCK:
        return _JOBS.get(upload_id)

class _Budget:
    """The per-run cost guard.

    Thread-safe because the document pool shares ONE budget for the run. A cap
    of 0 or less means "no cap". When a document's cost is UNKNOWN (the model
    catalogue has no price for it) the cap cannot be enforced for that
    document — that is recorded and reported rather than silently ignored.
    """

    def __init__(self, cap: float) -> None:
        self.cap = float(cap or 0.0)
        self.spent = 0.0
        self.exceeded = False
        self.unknown_costs = 0
        self._lock = threading.Lock()

    def add(self, cost: Optional[float]) -> None:
        with self._lock:
            if cost is None:
                self.unknown_costs += 1
                return
            self.spent = round(self.spent + float(cost), 6)
            self.check_locked()

    def check_locked(self) -> None:
        if self.cap > 0 and self.spent > self.cap:
            self.exceeded = True

    @property
    def reason(self) -> str:
        return (
            f"cost cap reached for this run (invoice_extract.v2.max_cost_usd = "
            f"{self.cap:.2f}, spent {self.spent:.6f}) — not started, left queued "
            f"so it can be resumed with a higher cap or a new run."
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "cap_usd": self.cap, "spent_usd": self.spent, "exceeded": self.exceeded,
            "unknown_cost_calls": self.unknown_costs,
        }


def _active_rows(
    conn: sqlite3.Connection, upload_id: int, *, unfinished_only: bool = True,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT e.extraction_id, e.document_id, e.status, e.attempts "
        "FROM invoice_doc_extractions e "
        "JOIN invoice_documents d ON d.document_id = e.document_id "
        "WHERE d.upload_id = ? AND e.is_active = 1"
    )
    params: list[Any] = [upload_id]
    if unfinished_only:
        sql += " AND e.status IN ('queued', 'running')"
    sql += " ORDER BY e.extraction_id"
    cur = conn.execute(sql, tuple(params))
    return [
        {"extraction_id": int(r[0]), "document_id": int(r[1]), "status": r[2], "attempts": int(r[3] or 0)}
        for r in cur.fetchall()
    ]


def _skip_reason_for(document: dict[str, Any]) -> Optional[str]:
    """Why a document is not extracted, or None when it IS extracted."""
    if document.get("excluded"):
        return (
            f"the document is excluded ({document.get('excluded_reason') or 'excluded in triage'})"
        )
    category = document.get("category")
    if category not in TARGET_CATEGORIES:
        return f"category '{category}' is not extracted (only tax_invoice and no_gst_expense are)"
    return None


def start_extraction(
    upload_id: int, *, actor: str, db_path=None, force: bool = False,
) -> dict[str, Any]:
    """Queue a Phase-2 extraction pass for an upload's eligible documents.

    Only ``tax_invoice`` and ``no_gst_expense`` documents are extracted. Every
    other document (and every excluded one) gets a row with status ``skipped``
    AND a reason, so a document never disappears silently.

    A re-extraction is allowed: it deactivates the previous rows (history is
    kept, nothing is deleted) and inserts new ones. It is refused while a
    previous pass for the same upload is still queued or running, so two passes
    can never interleave — pass ``force=True`` to supersede anyway.
    """
    cfg = get_config(db_path=db_path)
    if not cfg.enabled and not force:
        raise InvoiceExtractError(
            f"Phase-2 extraction is OFF ({KEY_ENABLED} is false). The legacy extraction path "
            "is unaffected; turn the setting on (or pass force=True) to run this pass."
        )

    from src.invoice_extract.service import get_upload
    from src.invoice_extract.triage import get_triage

    if get_upload(upload_id, db_path=db_path) is None:
        raise InvoiceExtractError(f"Unknown upload {upload_id}.")
    triage = get_triage(upload_id, db_path=db_path)
    documents = list(triage["documents"])
    if not documents:
        raise InvoiceExtractError(
            f"Upload {upload_id} has no triaged documents — run triage first."
        )

    conn = _connect(db_path)
    try:
        running = _active_rows(conn, upload_id)
        if running and not force:
            raise InvoiceExtractError(
                f"An extraction pass for upload {upload_id} is still "
                f"{running[0]['status']} ({len(running)} document(s)). Wait for it to finish, "
                "or pass force=True to supersede it."
            )
        doc_ids = [int(d["document_id"]) for d in documents]
        placeholders = ",".join("?" for _ in doc_ids)
        # Documents that ALREADY had an active pass: a re-extraction replaces
        # what a reviewer may have signed off, so their standing review is
        # cleared below (inside the log, after this transaction closes).
        superseded = [
            int(r[0]) for r in conn.execute(
                f"SELECT DISTINCT document_id FROM invoice_doc_extractions "
                f"WHERE is_active = 1 AND document_id IN ({placeholders})",
                tuple(doc_ids),
            ).fetchall()
        ]
        # Supersede: deactivate the previous rows (never delete — history kept).
        conn.execute(
            f"UPDATE invoice_doc_extractions SET is_active = 0 "
            f"WHERE is_active = 1 AND document_id IN ({placeholders})",
            tuple(doc_ids),
        )

        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for document in documents:
            document_id = int(document["document_id"])
            reason = _skip_reason_for(document)
            if reason is None:
                extraction_id = _insert_extraction(conn, document_id=document_id, status=STATUS_QUEUED)
                created.append({
                    "extraction_id": extraction_id, "document_id": document_id,
                    "ordinal": document["ordinal"], "category": document["category"],
                    "pages": sorted({int(r["page"]) for r in document.get("regions") or []}),
                })
            else:
                extraction_id = _insert_extraction(
                    conn, document_id=document_id, status=STATUS_SKIPPED, skipped_reason=reason,
                )
                skipped.append({
                    "extraction_id": extraction_id, "document_id": document_id,
                    "ordinal": document["ordinal"], "category": document["category"],
                    "reason": reason,
                })
        conn.commit()
    finally:
        conn.close()

    # A re-extraction un-signs any standing review: the earlier sign-off was
    # about a different set of values. Logged, never silent. Done AFTER the
    # transaction above so the two writers never hold the same file at once.
    if superseded:
        from src.invoice_extract import doc_actions

        doc_actions.clear_reviewed_for_upload(
            upload_id, actor=actor, triggered_by="re_extraction",
            reason="the document was extracted again", document_ids=superseded, db_path=db_path,
        )

    if created:
        state = _register_job(upload_id)
        _EXTRACT_EXECUTOR.submit(_run_job, upload_id, state, actor=actor, db_path=db_path)

    return {
        "upload_id": upload_id,
        "queued": len(created),
        "skipped": len(skipped),
        "documents": created,
        "skipped_documents": skipped,
        "max_cost_usd": cfg.max_cost_usd,
        "concurrency": cfg.concurrency,
    }


def resume_extraction(upload_id: int, *, actor: str, db_path=None) -> dict[str, Any]:
    """Resume ONLY the unfinished (queued / running) documents of an upload."""
    conn = _connect(db_path)
    try:
        rows = _active_rows(conn, upload_id)
    finally:
        conn.close()
    if not rows:
        return {"upload_id": upload_id, "resumed": 0, "status": "nothing_to_resume"}
    state = _register_job(upload_id)
    _EXTRACT_EXECUTOR.submit(_run_job, upload_id, state, actor=actor, db_path=db_path)
    return {"upload_id": upload_id, "resumed": len(rows)}


def init_extract(db_path=None) -> int:
    """Boot step: a row left ``running`` by a stopped process goes back to
    ``queued`` so ``resume_extraction`` can pick it up. Nothing is lost."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE invoice_doc_extractions SET status = 'queued', "
            "error = COALESCE(error || ' ', '') || "
            "'interrupted: the process stopped while this document was running' "
            "WHERE status = 'running'"
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _persist_outcome(
    conn: sqlite3.Connection, extraction_id: int, document_id: int, outcome: DocOutcome,
) -> None:
    """Write one document's outcome. All-or-nothing per document."""
    for row in outcome.fields:
        _insert_field(conn, extraction_id=extraction_id, document_id=document_id, row=row)
    for ann in outcome.annotations:
        _insert_annotation(conn, extraction_id=extraction_id, document_id=document_id, ann=ann)
    _update_extraction(
        conn, extraction_id,
        status=outcome.status,
        skipped_reason=outcome.skipped_reason,
        provider_a=outcome.provider_a, model_a=outcome.model_a,
        provider_b=outcome.provider_b, model_b=outcome.model_b,
        attempts=outcome.attempts,
        input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
        cost_usd=outcome.cost_usd, error=outcome.error, finished_at=_now(),
    )
    conn.commit()


def _execute_extraction(upload_id: int, *, actor: str, db_path=None) -> dict[str, Any]:
    """The worker body: read every unfinished document of one upload."""
    cfg = get_config(db_path=db_path)
    from src.invoice_extract.service import get_upload, get_upload_bytes
    from src.invoice_extract.triage import _page_total_and_text, get_triage

    start = time.monotonic()
    triage = get_triage(upload_id, db_path=db_path)
    docs_by_id = {int(d["document_id"]): d for d in triage["documents"]}
    rotations = {int(p["page"]): int(p["rotation_deg"] or 0) for p in triage["pages"]}

    upload = get_upload(upload_id, db_path=db_path) or {}
    source_format = upload.get("source_format") or "pdf"
    file_bytes = get_upload_bytes(upload_id, db_path=db_path) or b""
    if not file_bytes:
        # Nothing to read: fail every unfinished document loudly.
        conn = _connect(db_path)
        try:
            for row in _active_rows(conn, upload_id):
                _update_extraction(
                    conn, row["extraction_id"], status=STATUS_FAILED,
                    error="the upload has no stored file bytes", finished_at=_now(),
                )
            conn.commit()
        finally:
            conn.close()
        return {"upload_id": upload_id, "documents": 0, "failed": 1, "reason": "no file bytes"}

    try:
        text_layer_chars = int(_page_total_and_text(file_bytes, source_format)[1])
    except Exception:  # noqa: BLE001
        text_layer_chars = 0

    conn = _connect(db_path)
    try:
        rows = _active_rows(conn, upload_id)
        # Mark everything we are about to attempt as running, so a crash in the
        # middle is visible rather than leaving a document looking untouched.
        for row in rows:
            _update_extraction(conn, row["extraction_id"], status=STATUS_RUNNING)
            conn.execute(
                "UPDATE invoice_doc_extractions SET attempts = attempts + 1 WHERE extraction_id = ?",
                (row["extraction_id"],),
            )
        conn.commit()
    finally:
        conn.close()

    budget = _Budget(cfg.max_cost_usd)
    outcomes: list[tuple[int, int, DocOutcome, bool]] = []
    workers = max(1, min(int(cfg.concurrency), max(1, len(rows))))

    def _work(document: dict[str, Any], extraction_id: int) -> DocOutcome:
        """Read ONE document — unless the run has already spent its cap.

        The check happens HERE, immediately before the model is called, so a
        document whose task has not started is never billed once the cap is
        reached. That is what makes the cap a real stop rather than a report
        after the fact.
        """
        if budget.exceeded:
            return DocOutcome(
                document_id=int(document["document_id"]), status=STATUS_QUEUED,
                error=budget.reason,
            )
        return extract_document(
            document, file_bytes, source_format,
            rotations=rotations, text_layer_chars=text_layer_chars, cfg=cfg,
            actor=actor, db_path=db_path, upload_id=upload_id, extraction_id=extraction_id,
        )

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extract-doc")
    pending = list(rows)
    inflight: dict[Any, dict[str, Any]] = {}

    def _fill() -> None:
        """Start work while there is room AND budget left.

        Submitting every document at once would make the cost cap a report
        written after the money was spent. Instead the pool is kept full only
        while the run is inside its cap, so a document the cap stops is never
        sent to a model at all — it stays QUEUED with the reason recorded.
        """
        while pending and len(inflight) < workers and not budget.exceeded:
            row = pending.pop(0)
            document = docs_by_id.get(row["document_id"])
            if document is None:
                continue
            inflight[pool.submit(_work, document, row["extraction_id"])] = row

    try:
        _fill()
        while inflight:
            done, _ = futures_wait(list(inflight), return_when=FIRST_COMPLETED)
            for future in done:
                row = inflight.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 — a document never takes the run down
                    outcome = DocOutcome(
                        document_id=row["document_id"], status=STATUS_FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                outcomes.append((row["extraction_id"], row["document_id"], outcome, False))
                budget.add(outcome.cost_usd)
            _fill()
    finally:
        pool.shutdown(wait=True)

    # Anything the cap stopped before it started stays queued and resumable.
    for row in pending:
        outcomes.append((
            row["extraction_id"], row["document_id"],
            DocOutcome(document_id=row["document_id"], status=STATUS_QUEUED, error=budget.reason),
            True,
        ))

    conn = _connect(db_path)
    ruled: list[int] = []
    rules_failed: list[dict[str, Any]] = []
    try:
        for extraction_id, document_id, outcome, blocked in outcomes:
            if blocked or (outcome.status == STATUS_QUEUED and outcome.attempts == 0):
                # Not started because the cap was reached: stays QUEUED and
                # resumable, with the reason recorded rather than failing
                # silently.
                _update_extraction(
                    conn, extraction_id, status=STATUS_QUEUED, error=outcome.error,
                )
                continue
            _persist_outcome(conn, extraction_id, document_id, outcome)
        conn.commit()
    finally:
        conn.close()

    # Phase 3: the deterministic layer runs for every document that produced a
    # read, so the review model is ready without a second pass. A rules failure
    # is recorded and reported — it never hides an extraction that DID succeed,
    # and re-running the rules is always possible (`rules --rerun`).
    for extraction_id, document_id, outcome, blocked in outcomes:
        if blocked or outcome.attempts == 0:
            continue
        try:
            from src.invoice_extract import rules as rules_mod

            rules_mod.run_rules(extraction_id, actor=actor, db_path=db_path)
            ruled.append(extraction_id)
        except Exception as exc:  # noqa: BLE001 — reported, never silent
            rules_failed.append({
                "extraction_id": extraction_id, "document_id": document_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"warning: rules did not run for document {document_id}: {exc}", file=sys.stderr)

    summary = {
        "upload_id": upload_id,
        "documents": len(outcomes),
        "budget": budget.as_json(),
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "statuses": _status_counts(upload_id, db_path=db_path),
        "rules_run": len(ruled),
        "rules_failed": rules_failed,
    }
    return summary


def _status_counts(upload_id: int, *, db_path=None) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT e.status, COUNT(*) FROM invoice_doc_extractions e "
            "JOIN invoice_documents d ON d.document_id = e.document_id "
            "WHERE d.upload_id = ? AND e.is_active = 1 GROUP BY e.status",
            (upload_id,),
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}
    finally:
        conn.close()


def is_extracting(upload_id: int, *, db_path=None) -> bool:
    conn = _connect(db_path)
    try:
        return bool(_active_rows(conn, upload_id))
    finally:
        conn.close()


def extraction_status_counts(upload_id: int, *, db_path=None) -> dict[str, int]:
    """How many ACTIVE extractions of this upload are in each status.

    The public form of the counter the progress panel needs; a status with no
    rows is simply absent from the mapping.
    """
    return _status_counts(upload_id, db_path=db_path)


def wait_for_extraction(
    upload_id: int, *, db_path=None, timeout: float = 900.0, poll: float = 0.05,
) -> dict[str, int]:
    """Block until this upload has no unfinished document, or the job is done.

    Two ways to finish, and the difference matters: every document reaching a
    terminal state, OR the job itself returning while some documents were
    deliberately left QUEUED (the cost cap). Those queued rows are waiting for
    ``resume_extraction``, so a waiter that insisted on zero queued rows would
    hang forever.
    """
    deadline = time.monotonic() + max(1.0, float(timeout))
    while True:
        counts = _status_counts(upload_id, db_path=db_path)
        unfinished = sum(counts.get(s, 0) for s in EXTRACTION_UNFINISHED)
        if unfinished == 0:
            return counts
        state = job_state(upload_id)
        if state is not None and state["event"].is_set():
            return counts
        if time.monotonic() > deadline:
            return counts
        time.sleep(max(0.01, float(poll)))


# ---------------------------------------------------------------------------
# Reading the results back
# ---------------------------------------------------------------------------


def get_extraction(upload_id: int, *, db_path=None, active_only: bool = True) -> dict[str, Any]:
    """Every active extraction for an upload, with its fields and annotations."""
    conn = _connect(db_path)
    try:
        where = "d.upload_id = ?" + (" AND e.is_active = 1" if active_only else "")
        cur = conn.execute(
            f"SELECT e.*, d.ordinal, d.category, d.label FROM invoice_doc_extractions e "
            f"JOIN invoice_documents d ON d.document_id = e.document_id WHERE {where} "
            f"ORDER BY d.ordinal, e.extraction_id",
            (upload_id,),
        )
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

        out: list[dict[str, Any]] = []
        for row in rows:
            extraction_id = int(row["extraction_id"])
            document_id = int(row["document_id"])
            fcur = conn.execute(
                "SELECT * FROM invoice_doc_fields WHERE extraction_id = ? ORDER BY field_id",
                (extraction_id,),
            )
            fields = [dict(zip([c[0] for c in fcur.description], r)) for r in fcur.fetchall()]
            acur = conn.execute(
                "SELECT * FROM invoice_doc_annotations WHERE extraction_id = ? ORDER BY annotation_id",
                (extraction_id,),
            )
            annotations = [dict(zip([c[0] for c in acur.description], r)) for r in acur.fetchall()]
            regions = [
                {
                    "page": int(r[0]), "x": r[1], "y": r[2], "w": r[3], "h": r[4],
                    "order_in_doc": int(r[5]),
                }
                for r in conn.execute(
                    "SELECT page, x, y, w, h, order_in_doc FROM document_regions "
                    "WHERE document_id = ? AND is_active = 1 ORDER BY order_in_doc",
                    (document_id,),
                ).fetchall()
            ]
            out.append({
                **row,
                "extraction_id": extraction_id,
                "document_id": document_id,
                "fields": fields,
                "annotations": annotations,
                "regions": regions,
                "pages": sorted({r["page"] for r in regions}),
            })
        return {"upload_id": upload_id, "extractions": out}
    finally:
        conn.close()


def fields_by_key(extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(f["field_key"]): f for f in extraction.get("fields") or []}


def provider_preflight(*, db_path=None) -> tuple[list[dict[str, Any]], list[str]]:
    """What the two reads need, and whether the platform can supply it.

    Reports per provider ONLY whether a key is available — never a key, never a
    decrypted secret. Returns ``(rows, problems)``; each problem is a sentence
    naming what is missing, so a caller can stop BEFORE spending anything.
    """
    from src.ai_models import service as ai_models

    available = configured_providers(db_path=db_path)
    needed: list[str] = []
    # (read label, touchpoint, the model its PRIMARY leg would use)
    reads: list[tuple[str, str, Optional[str]]] = []
    for read, touchpoint in (("read A", TOUCHPOINT_READ_A), ("read B", TOUCHPOINT_READ_B)):
        legs = ai_models.effective_leg(touchpoint, db_path=db_path) or {}
        for leg in ("primary", "fallback"):
            provider = (legs.get(leg) or {}).get("provider")
            if provider and provider not in needed:
                needed.append(provider)
        reads.append((read, touchpoint, (legs.get("primary") or {}).get("model")))
    ordered = sorted(set(needed) | set(available))
    rows = [{"provider": key, "available": key in available} for key in ordered]

    problems: list[str] = []
    # A read with NO model assigned cannot run at all. Reported plainly, and
    # FIRST: a fresh database legitimately starts here, because the seed refuses
    # to guess a model — "no vision model assigned" is the honest report, and
    # quietly falling back to an expensive one is what this replaces.
    for read, touchpoint, model in reads:
        if not model:
            problems.append(
                f"{read} ({touchpoint}) — refused: no vision model assigned for this read. "
                "Choose an image-capable model for it on the AI Models screen."
            )
    missing = [row["provider"] for row in rows if not row["available"] and row["provider"] in needed]
    if missing:
        problems.append("No usable API key for: " + ", ".join(missing) + ".")
    if len(available) < 2:
        problems.append(
            "Read B must come from a different provider than read A (two reads from one "
            f"provider are not an independent second opinion), and only {len(available)} "
            f"provider(s) have a usable key ({', '.join(available) or 'none'})."
        )
    return rows, problems


def annotate_crops(
    upload_id: int, out_dir: Path, *, db_path=None, cfg: Optional[ExtractConfig] = None,
) -> list[Path]:
    """Draw each field's box (and every annotation) onto its own region crop.

    One PNG per region per document, written into the report folder (which is
    git-ignored). The Markdown report references these by PATH and embeds no
    images — a report that inlines a client's invoice is a data-leak risk.
    """
    from io import BytesIO

    from PIL import Image, ImageDraw

    from src.invoice_extract.service import get_upload, get_upload_bytes
    from src.invoice_extract.triage import get_triage

    cfg = cfg or get_config(db_path=db_path)
    upload = get_upload(upload_id, db_path=db_path) or {}
    source_format = upload.get("source_format") or "pdf"
    file_bytes = get_upload_bytes(upload_id, db_path=db_path) or b""
    if not file_bytes:
        return []
    rotations = {
        int(p["page"]): int(p["rotation_deg"] or 0)
        for p in get_triage(upload_id, db_path=db_path)["pages"]
    }

    written: list[Path] = []
    for extraction in get_extraction(upload_id, db_path=db_path)["extractions"]:
        regions = extraction.get("regions") or []
        if not regions:
            continue
        try:
            crop_list, _ = crops_mod.render_region_crops(
                file_bytes, source_format, regions, rotations,
                dpi=cfg.dpi, max_edge_px=cfg.max_edge_px, padding_frac=cfg.padding_frac,
            )
        except crops_mod.CropError:
            continue
        doc_dir = out_dir / "annotated" / f"doc_{extraction['document_id']}"
        doc_dir.mkdir(parents=True, exist_ok=True)
        for crop in crop_list:
            image = Image.open(BytesIO(crop.png)).convert("RGB")
            draw = ImageDraw.Draw(image)
            cx, cy, cw, ch = crop.page_box
            for field_row in extraction.get("fields") or []:
                box = _page_box_of(field_row)
                if box is None or not crops_mod.boxes_overlap(box, crop.page_box):
                    continue
                rect = _page_box_to_crop_px(box, crop, cx, cy, cw, ch)
                colour = (200, 0, 0) if field_row.get("status") in (
                    fv2.STATUS_OUT_OF_REGION, fv2.STATUS_NEEDS_PRINTED,
                ) else (0, 110, 200)
                draw.rectangle(rect, outline=colour, width=2)
                draw.text((rect[0], max(0, rect[1] - 11)), f"{field_row['field_key']}", fill=colour)
            for ann in extraction.get("annotations") or []:
                box = _page_box_of(ann)
                if box is None or ann.get("page") != crop.region_page:
                    continue
                rect = _page_box_to_crop_px(box, crop, cx, cy, cw, ch)
                draw.rectangle(rect, outline=(0, 140, 0), width=2)
                draw.text((rect[0], max(0, rect[1] - 11)), f"hand: {ann.get('text_verbatim')}",
                          fill=(0, 140, 0))
            path = doc_dir / f"region_{crop.region_index}_page_{crop.region_page}.png"
            image.save(path, format="PNG")
            written.append(path)
    return written


def _page_box_of(row: dict[str, Any]) -> Optional[tuple[float, float, float, float]]:
    x, y, w, h = row.get("x"), row.get("y"), row.get("w"), row.get("h")
    if None in (x, y, w, h):
        return None
    try:
        return (float(x), float(y), float(w), float(h))
    except (TypeError, ValueError):
        return None


def _page_box_to_crop_px(
    box: tuple[float, float, float, float], crop: crops_mod.Crop,
    cx: float, cy: float, cw: float, ch: float,
) -> tuple[int, int, int, int]:
    """A page-normalised box → pixels INSIDE that crop's own image."""
    if cw <= 0 or ch <= 0:
        return (0, 0, 0, 0)
    x0 = (box[0] - cx) / cw * crop.px_w
    y0 = (box[1] - cy) / ch * crop.px_h
    x1 = (box[0] + box[2] - cx) / cw * crop.px_w
    y1 = (box[1] + box[3] - cy) / ch * crop.px_h
    return (int(x0), int(y0), int(x1), int(y1))


def _call_log_for(upload_id: int, *, db_path=None) -> list[dict[str, Any]]:
    from src.ai_models import service as ai_models

    try:
        return ai_models.list_ai_call_log(upload_id=upload_id, limit=500, db_path=db_path)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Scoring: extraction vs the provisional expected field bundle
# ---------------------------------------------------------------------------

DEFAULT_EXPECTED_FIELDS = (
    REPO_ROOT / "tests" / "fixtures" / "invoice_extract" / "batch_apr2026_expected_fields.json"
)


@dataclass
class ExpectedFields:
    path: Path
    data: dict[str, Any]
    verified: bool


def load_expected_fields(path: Path) -> ExpectedFields:
    if not Path(path).exists():
        return ExpectedFields(path=Path(path), data={"documents": []}, verified=False)
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        data = {"documents": []}
    return ExpectedFields(path=Path(path), data=data, verified=bool(data.get("verified")))


def _match_expected(
    extractions: list[dict[str, Any]], expected: ExpectedFields,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(pairs, unmatched_expected, unmatched_extracted). Matched on the page the
    expected document is on, which is how the fixture is written."""
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used: set[int] = set()
    unmatched_expected: list[dict[str, Any]] = []
    for exp_doc in expected.data.get("documents", []):
        page = exp_doc.get("page")
        found = None
        for extraction in extractions:
            if extraction["extraction_id"] in used:
                continue
            if page in (extraction.get("pages") or []):
                found = extraction
                break
        if found is None:
            unmatched_expected.append(exp_doc)
        else:
            used.add(found["extraction_id"])
            pairs.append((exp_doc, found))
    unmatched_extracted = [e for e in extractions if e["extraction_id"] not in used]
    return pairs, unmatched_expected, unmatched_extracted


@dataclass
class FieldScore:
    key: str
    expected: Optional[str]
    value_a: Optional[str]
    value_b: Optional[str]
    reads_agree: Optional[str]
    status: str
    kind: str
    a_verdict: str          # match | wrong | missing | n/a (no expectation)
    b_verdict: str
    page: Optional[int]


def score_document(
    expected_doc: dict[str, Any], extraction: dict[str, Any],
) -> tuple[list[FieldScore], dict[str, int]]:
    """Per-field comparison of one document's two reads against the fixture."""
    rows = fields_by_key(extraction)
    expect_map: dict[str, Any] = dict(expected_doc.get("expect") or {})
    not_printed = set(expected_doc.get("not_printed") or [])

    keys = list(expect_map) + [k for k in not_printed if k not in expect_map]
    keys += [k for k in rows if k not in keys]

    scores: list[FieldScore] = []
    for key in keys:
        row = rows.get(key) or {}
        spec = fv2.field_spec(key)
        kind = spec.kind if spec else fv2.KIND_TEXT
        expected_value = expect_map.get(key)
        value_a = row.get("value_a")
        value_b = row.get("value_b")

        if key in not_printed and expected_value is None:
            # The fixture asserts this field is NOT printed; the only correct
            # outcome is that neither read invented one.
            a_verdict = "missing" if value_a is None else "wrong"
            b_verdict = "missing" if value_b is None else "wrong"
        elif expected_value is None and key not in expect_map:
            a_verdict = b_verdict = "n/a"
        else:
            a_verdict = (
                "missing" if value_a is None
                else ("match" if nz.same_value(kind, value_a, expected_value) else "wrong")
            )
            b_verdict = (
                "missing" if value_b is None
                else ("match" if nz.same_value(kind, value_b, expected_value) else "wrong")
            )

        scores.append(FieldScore(
            key=key, expected=expected_value, value_a=value_a, value_b=value_b,
            reads_agree=row.get("reads_agree"), status=str(row.get("status") or "not_printed"),
            kind=kind, a_verdict=a_verdict, b_verdict=b_verdict,
            page=row.get("page"),
        ))

    counts = {
        "fields": len(scores),
        "a_wrong": sum(1 for s in scores if s.a_verdict == "wrong"),
        "a_missing": sum(1 for s in scores if s.a_verdict == "missing"),
        "b_wrong": sum(1 for s in scores if s.b_verdict == "wrong"),
        "b_missing": sum(1 for s in scores if s.b_verdict == "missing"),
        # THE KEY METRIC: of read A's wrong values, how many did read B disagree on?
        "b_disagreed_on_a_wrong": sum(
            1 for s in scores if s.a_verdict == "wrong"
            and s.reads_agree in (nz.AGREE_DISAGREE, nz.AGREE_MISSING_B)
        ),
        # ...and how many CORRECT values were wrongly flagged as disagreeing?
        "a_correct_flagged_disagree": sum(
            1 for s in scores if s.a_verdict == "match" and s.reads_agree == nz.AGREE_DISAGREE
        ),
        "a_correct_confirmed": sum(
            1 for s in scores if s.a_verdict == "match"
            and s.reads_agree in (nz.AGREE_EXACT, nz.AGREE_NEAR)
        ),
        "out_of_region": sum(1 for s in scores if s.status == fv2.STATUS_OUT_OF_REGION),
        "needs_printed": sum(1 for s in scores if s.status == fv2.STATUS_NEEDS_PRINTED),
        "not_printed_a": sum(1 for s in scores if s.value_a is None),
    }
    return scores, counts


def _handwriting_violations(
    expected_doc: dict[str, Any], extraction: dict[str, Any],
) -> list[dict[str, Any]]:
    """Every place a fixture-listed HANDWRITTEN amount was used as a field value.

    The fixture names the handwritten amounts on the awkward pages; if one of
    them shows up as read A's or read B's value for a field, the prompt rule
    ("a handwritten value is not a field value") was broken and the reviewer
    must see it.
    """
    amounts = [str(a) for a in (expected_doc.get("handwritten_amounts") or [])]
    if not amounts:
        return []
    wanted = {nz.normalise_number(a) for a in amounts} - {None}
    out: list[dict[str, Any]] = []
    for field_row in extraction.get("fields") or []:
        for side in ("value_a", "value_b"):
            value = field_row.get(side)
            normalised = nz.normalise_number(value)
            if normalised is not None and normalised in wanted:
                out.append({
                    "field_key": field_row.get("field_key"),
                    "side": side,
                    "value": value,
                    "status": field_row.get("status"),
                })
    return out


def _score_all(result: dict[str, Any], expected: ExpectedFields) -> dict[str, Any]:
    extractions = [e for e in result.get("extractions", [])]
    pairs, unmatched_expected, unmatched_extracted = _match_expected(extractions, expected)

    documents: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    for exp_doc, extraction in pairs:
        scores, counts = score_document(exp_doc, extraction)
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
        documents.append({
            "expected": exp_doc, "extraction": extraction, "scores": scores, "counts": counts,
            "handwriting_violations": _handwriting_violations(exp_doc, extraction),
        })

    return {
        "documents": documents,
        "unmatched_expected": unmatched_expected,
        "unmatched_extracted": [
            {"document_id": e["document_id"], "ordinal": e.get("ordinal"),
             "category": e.get("category"), "pages": e.get("pages"), "status": e.get("status")}
            for e in unmatched_extracted
        ],
        "totals": totals,
    }


def _money(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.6f}"
    return "unknown"


def _render_score_report(payload: dict[str, Any]) -> str:
    """ONE Markdown report. No page images inside — annotated crops are written
    to the (git-ignored) report folder and referenced by path."""
    expected: ExpectedFields = payload["expected"]
    scoring = payload["scoring"]
    result: dict[str, Any] = payload["result"]
    totals = scoring["totals"]
    lines: list[str] = []

    lines.append("# Invoice Extraction Phase 2 — scoring report")
    lines.append("")
    lines.append(f"- Fixture: `{payload['pdf']}` ({payload['pages_total']} pages)")
    lines.append(
        f"- Source database: `{payload['source_db']}` — **copied**; the source is opened "
        "read-only and is never written to"
    )
    lines.append(f"- Working copy: `{payload['db_copy']}` · data root `{payload['data_root']}`")
    lines.append(
        f"- Expected fields: `{expected.path}` — "
        + ("**provisional (verified: false)**" if not expected.verified else "verified")
    )
    lines.append(
        f"- Read A touchpoint: `{TOUCHPOINT_READ_A}` · Read B touchpoint: `{TOUCHPOINT_READ_B}`"
    )
    lines.append(
        f"- Triage found {payload.get('triage_documents')} document(s); "
        f"{payload.get('triage_unassigned')} page(s) unassigned"
    )
    lines.append(f"- Run time: {payload['elapsed_ms']} ms · cost: ${_money(payload['cost_usd'])} "
                 f"· tokens in {payload['input_tokens']} / out {payload['output_tokens']}")
    lines.append(f"- Budget: {json.dumps(payload['budget'])}")
    key_rows = payload.get("key_rows") or []
    if key_rows:
        lines.append(
            "- Provider keys (availability only — no key is printed, logged or copied): "
            + ", ".join(f"{r['provider']}={'yes' if r['available'] else 'no'}" for r in key_rows)
        )
    lines.append("")

    # ---------------------------------------------------------------- metrics
    lines.append("## KEY METRIC — did the second read catch the first read's mistakes?")
    lines.append("")
    a_wrong = totals.get("a_wrong", 0)
    caught = totals.get("b_disagreed_on_a_wrong", 0)
    false_flags = totals.get("a_correct_flagged_disagree", 0)
    confirmed = totals.get("a_correct_confirmed", 0)
    lines.append("| measure | value |")
    lines.append("|---|---|")
    lines.append(f"| fields compared | {totals.get('fields', 0)} |")
    lines.append(f"| read A wrong values | {a_wrong} |")
    lines.append(f"| read A fields with no value | {totals.get('a_missing', 0)} |")
    lines.append(
        f"| **of read A's wrong values, read B disagreed on** | **{caught}** "
        f"({(100.0 * caught / a_wrong):.0f}% of {a_wrong}) |" if a_wrong else
        "| **of read A's wrong values, read B disagreed on** | **0** (read A had no wrong values) |"
    )
    lines.append(
        f"| read A CORRECT values wrongly flagged as disagree | **{false_flags}** |"
    )
    lines.append(f"| read A correct values read B confirmed | {confirmed} |")
    lines.append(f"| read B wrong values (independent of A) | {totals.get('b_wrong', 0)} |")
    lines.append(f"| read B fields with no value | {totals.get('b_missing', 0)} |")
    lines.append(f"| values flagged `out_of_region` | {totals.get('out_of_region', 0)} |")
    lines.append(f"| values flagged `needs_printed` (handwriting guard) | {totals.get('needs_printed', 0)} |")
    lines.append("")
    if a_wrong:
        prec = (100.0 * caught / a_wrong)
        lines.append(
            f"Read B contradicted **{prec:.0f}%** of read A's wrong values; it also "
            f"contradicted {false_flags} value(s) that were actually right."
        )
    lines.append("")

    # -------------------------------------------------------------- per doc
    for doc in scoring["documents"]:
        exp_doc, extraction = doc["expected"], doc["extraction"]
        counts = doc["counts"]
        lines.append(
            f"## {exp_doc.get('label') or ('document on page ' + str(exp_doc.get('page')))} "
            f"— pages {extraction.get('pages')}"
        )
        lines.append("")
        lines.append(
            f"- Extraction `{extraction['extraction_id']}` status "
            f"`{extraction.get('status')}` · category `{extraction.get('category')}`"
            + (f" · **skipped: {extraction.get('skipped_reason')}**" if extraction.get("skipped_reason") else "")
        )
        lines.append(
            f"- read A: `{extraction.get('provider_a')}:{extraction.get('model_a')}` · "
            f"read B: `{extraction.get('provider_b')}:{extraction.get('model_b')}`"
            + (f" · **error: {extraction.get('error')}**" if extraction.get("error") else "")
        )
        lines.append(
            f"- A wrong {counts['a_wrong']} · A missing {counts['a_missing']} · "
            f"B disagreed on A-wrong {counts['b_disagreed_on_a_wrong']} · "
            f"A-correct-false-flagged {counts['a_correct_flagged_disagree']} · "
            f"out_of_region {counts['out_of_region']} · needs_printed {counts['needs_printed']}"
        )
        lines.append("")
        lines.append("| field | expected | read A | vs exp | read B | vs exp | A vs B | status | page |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for score in doc["scores"]:
            expected_text = (
                "*(not printed)*" if score.key in set(exp_doc.get("not_printed") or [])
                else ("—" if score.expected is None else str(score.expected))
            )
            lines.append(
                f"| `{score.key}` | {expected_text} "
                f"| {score.value_a if score.value_a is not None else '*(none)*'} | {score.a_verdict} "
                f"| {score.value_b if score.value_b is not None else '*(none)*'} | {score.b_verdict} "
                f"| {score.reads_agree or '—'} | {score.status} "
                f"| {score.page if score.page else '—'} |"
            )
        lines.append("")
        annotations = extraction.get("annotations") or []
        violations = doc.get("handwriting_violations") or []
        lines.append(
            f"### Handwriting — {len(annotations)} annotation(s), "
            f"{len(violations)} used as a field value"
        )
        lines.append("")
        fixture_amounts = exp_doc.get("handwritten_amounts") or []
        if fixture_amounts:
            lines.append(
                "The fixture names these handwritten amounts, which must NEVER be used as a "
                f"value: {', '.join(str(a) for a in fixture_amounts)}."
            )
            lines.append("")
        if violations:
            lines.append("**VIOLATIONS — a handwritten amount became a field value:**")
            lines.append("")
            for item in violations:
                lines.append(
                    f"- `{item['field_key']}` read {item['side']} = {item['value']!r} "
                    f"(status {item['status']})"
                )
        else:
            lines.append("None — no handwritten amount was used as a field value.")
        lines.append("")
        lines.append("| page | text (verbatim) | kind guess | page box | used as a value |")
        lines.append("|---|---|---|---|---|")
        if annotations:
            for ann in annotations:
                box = (
                    f"{ann.get('x')}, {ann.get('y')}, {ann.get('w')}, {ann.get('h')}"
                    if ann.get("x") is not None else "—"
                )
                lines.append(
                    f"| {ann.get('page')} | {ann.get('text_verbatim')} | {ann.get('kind_guess')} "
                    f"| {box} | {'YES — BUG' if ann.get('used_as_value') else 'no'} |"
                )
        else:
            lines.append("| — | *(none found)* | — | — | — |")
        lines.append("")

    # -------------------------------------------------- handwriting + region
    lines.append("## Handwriting guard")
    lines.append("")
    lines.append(
        "The guard is deterministic: a monetary field whose value EQUALS an annotation's "
        "amount and whose box OVERLAPS that annotation is marked `needs_printed`, so the "
        "printed value is confirmed rather than a scribble trusted."
    )
    lines.append("")
    for page in (23, 24, 25):
        hits = [
            (doc, score) for doc in scoring["documents"] for score in doc["scores"]
            if score.page == page and score.status == fv2.STATUS_NEEDS_PRINTED
        ]
        if hits:
            lines.append(f"- page {page}: {len(hits)} field(s) flagged needs_printed — "
                         + ", ".join(f"`{s.key}` ({s.value_a or s.value_b})" for _, s in hits))
        else:
            lines.append(f"- page {page}: no field flagged needs_printed")
    lines.append("")

    lines.append("## Reads that landed outside the document's regions")
    lines.append("")
    outside = [
        (doc["extraction"], score) for doc in scoring["documents"] for score in doc["scores"]
        if score.status == fv2.STATUS_OUT_OF_REGION
    ]
    if not outside:
        lines.append("None.")
    for extraction, score in outside:
        lines.append(
            f"- doc {extraction['document_id']} page {score.page} `{score.key}` = "
            f"{(score.value_a or score.value_b)!r} — value kept, flagged"
        )
    lines.append("")

    lines.append("## Skipped and unmatched documents")
    lines.append("")
    skipped = [e for e in result.get("extractions", []) if e.get("status") == STATUS_SKIPPED]
    if skipped:
        for extraction in skipped:
            lines.append(
                f"- doc {extraction['document_id']} (ordinal {extraction.get('ordinal')}, "
                f"`{extraction.get('category')}`): {extraction.get('skipped_reason')}"
            )
    else:
        lines.append("- nothing was skipped")
    for exp_doc in scoring["unmatched_expected"]:
        lines.append(f"- EXPECTED but NOT EXTRACTED: {exp_doc.get('label')} on page {exp_doc.get('page')}")
    for item in scoring["unmatched_extracted"]:
        lines.append(
            f"- EXTRACTED but not in the fixture: doc {item['document_id']} "
            f"(ordinal {item['ordinal']}, `{item['category']}`, pages {item['pages']})"
        )
    lines.append("")

    lines.append("## Model calls (every attempt, including failures)")
    lines.append("")
    calls: list[dict[str, Any]] = payload.get("calls") or []
    if calls:
        lines.append("| touchpoint | provider | model | outcome | attempt | in | out |")
        lines.append("|---|---|---|---|---|---|---|")
        for call in calls:
            lines.append(
                f"| {call.get('touchpoint_key')} | {call.get('provider')} | {call.get('model')} "
                f"| {call.get('outcome')} | {call.get('attempt')} "
                f"| {call.get('input_tokens')} | {call.get('output_tokens')} |"
            )
    else:
        lines.append("No `ai_call_log` rows for this upload.")
    lines.append("")

    lines.append("## Annotated crops")
    lines.append("")
    annotated = payload.get("annotated") or []
    lines.append(
        "No images are embedded in this report — that would inline a client's invoice. "
        "The annotated crops below were written to the git-ignored report folder. "
        "Blue = a printed value, red = needs_printed / out_of_region, green = a handwritten "
        "annotation."
    )
    lines.append("")
    if annotated:
        for path in annotated:
            lines.append(f"- `{path}`")
    else:
        lines.append("None written.")
    lines.append("")

    lines.append("## What this run cannot tell you")
    lines.append("")
    notes: list[str] = []
    if not expected.verified:
        notes.append(
            "The expected field values are PROVISIONAL (`\"verified\": false`) — they were "
            "transcribed by hand and are unconfirmed, so every 'wrong' above may be a wrong "
            "expectation rather than a wrong read."
        )
    if payload["budget"].get("unknown_cost_calls"):
        notes.append(
            f"{payload['budget']['unknown_cost_calls']} read(s) had NO price in the copy's "
            "catalogue, so the cost cap could not be enforced for them."
        )
    if payload["budget"].get("exceeded"):
        notes.append("The cost cap was reached; some documents were left queued and not read.")
    counts = payload.get("status_counts") or {}
    if counts.get("queued") or counts.get("running"):
        notes.append(f"Unfinished documents: {counts}")
    if not notes:
        notes.append("Nothing — this phase makes no claim about accuracy beyond the two reads.")
    for note in notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def score(
    pdf_path: str,
    *,
    db_path: Optional[str] = None,
    out_dir: Optional[Path] = None,
    expected_path: Optional[Path] = None,
    keep: bool = False,
    client_id: Optional[int] = None,
) -> dict[str, Any]:
    """Run triage + Phase-2 extraction on a COPY and score it.

    Never touches the real ``db/`` or ``data/``: the source database is opened
    read-only, copied, and the copy is the only thing written to.
    """
    from src.invoice_extract import triage

    pdf = Path(pdf_path)
    if not pdf.exists():
        raise InvoiceExtractError(f"Fixture PDF not found: {pdf}")
    out_dir = Path(out_dir) if out_dir else DEFAULT_REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = load_expected_fields(Path(expected_path) if expected_path else DEFAULT_EXPECTED_FIELDS)

    source_db = Path(db_path) if db_path else Path(recon_db.DB_PATH)
    if not source_db.exists():
        raise InvoiceExtractError(f"No database at {source_db}. Pass --db <path>.")

    copy = triage._make_copy(source_db, prefix="extract_v2_score_")
    try:
        # Key pre-flight IN THE COPY: report availability only, never a key,
        # and stop before anything runs when a leg has no usable provider.
        key_rows, problems = provider_preflight(db_path=str(copy.db))
        print("Provider keys — through the app's own resolver (vault, then environment):")
        for row in key_rows:
            print(f"  key available: {'yes' if row['available'] else 'no '}  [{row['provider']}]")
        if problems:
            raise InvoiceExtractError(
                " ".join(problems)
                + " Add a key on the AI Models screen (or set the provider's environment "
                "variable) — refusing to start so a half-run score is not produced."
            )

        # Enable Phase 2 IN THE COPY ONLY.
        from src.settings import service as settings

        settings.set_setting_str(KEY_ENABLED, "true", actor="extract:score", db_path=str(copy.db))

        conn = _connect(str(copy.db))
        try:
            row = conn.execute("SELECT client_id FROM end_clients ORDER BY client_id LIMIT 1").fetchone()
            cid = client_id or (int(row[0]) if row else None)
        finally:
            conn.close()
        if cid is None:
            from src.clients import service as clients

            try:
                cid = clients.create_client(
                    legal_name="Extract V2 Score Client", actor="extract:score",
                    db_path=str(copy.db),
                )
            except Exception as exc:  # noqa: BLE001
                raise InvoiceExtractError(
                    "The database copy has no client to attribute the run to, and a "
                    f"placeholder one could not be created ({exc}). Pass --client-id <id>."
                ) from exc

        started = time.monotonic()
        upload_id = triage._score_upload(copy.db, pdf, int(cid))
        queued = triage.run_triage(
            upload_id, actor="extract:score", db_path=str(copy.db), include_disabled=True,
        )
        triage.wait_for_run(int(queued["run_id"]), db_path=str(copy.db))
        triage_result = triage.get_triage(upload_id, db_path=str(copy.db))

        summary = start_extraction(upload_id, actor="extract:score", db_path=str(copy.db), force=True)
        status_counts = wait_for_extraction(upload_id, db_path=str(copy.db))
        elapsed_ms = int((time.monotonic() - started) * 1000)

        result = get_extraction(upload_id, db_path=str(copy.db))
        calls = _call_log_for(upload_id, db_path=str(copy.db))
        scoring = _score_all(result, expected)

        cost_total: Optional[float] = None
        input_total = output_total = 0
        for extraction in result["extractions"]:
            if extraction.get("cost_usd") is not None:
                cost_total = round((cost_total or 0.0) + float(extraction["cost_usd"]), 6)
            input_total += int(extraction.get("input_tokens") or 0)
            output_total += int(extraction.get("output_tokens") or 0)

        budget_row = _Budget(get_config(db_path=str(copy.db)).max_cost_usd)
        budget_row.spent = cost_total or 0.0
        budget_row.check_locked()

        report_path = out_dir / SCORE_REPORT_NAME
        annotated = annotate_crops(upload_id, out_dir, db_path=str(copy.db))
        report_path.write_text(_render_score_report({
            "pdf": str(pdf), "pages_total": triage._pdf_page_count(pdf.read_bytes()),
            "source_db": str(source_db), "db_copy": str(copy.db), "data_root": str(copy.data),
            "expected": expected, "result": result, "scoring": scoring, "calls": calls,
            "elapsed_ms": elapsed_ms, "cost_usd": cost_total,
            "input_tokens": input_total, "output_tokens": output_total,
            "budget": budget_row.as_json(), "status_counts": status_counts,
            "queue_summary": summary,
            "triage_documents": len(triage_result["documents"]),
            "triage_unassigned": len(triage_result["unassigned"]),
            "key_rows": key_rows,
            "annotated": [str(p) for p in annotated],
        }))
        return {
            "report_path": str(report_path),
            "upload_id": upload_id,
            "documents": len(result["extractions"]),
            "totals": scoring["totals"],
            "status_counts": status_counts,
            "cost_usd": cost_total,
            "budget": budget_row.as_json(),
            "elapsed_ms": elapsed_ms,
        }
    finally:
        if not keep:
            copy.cleanup()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_argv(argv: Optional[list[str]] = None) -> list[str]:
    return list(sys.argv[1:] if argv is None else argv)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.invoice_extract.extract",
        description="Invoice Extraction Phase 2 — read each document from its region crops.",
    )
    sub = parser.add_subparsers(dest="cmd")

    score_p = sub.add_parser(
        "score", help="run triage + Phase-2 extraction on a DB COPY and score it",
    )
    score_p.add_argument("pdf")
    score_p.add_argument("--db", default=None, help="database to COPY (default: the app database)")
    score_p.add_argument("--out", default=None, help="report directory")
    score_p.add_argument("--expected", default=None, help="expected-fields JSON")
    score_p.add_argument("--client-id", type=int, default=None)
    score_p.add_argument("--keep", action="store_true", help="keep the temp copy")

    args = parser.parse_args(_cli_argv(argv))
    try:
        if args.cmd == "score":
            result = score(
                args.pdf, db_path=args.db,
                out_dir=Path(args.out) if args.out else None,
                expected_path=Path(args.expected) if args.expected else None,
                keep=args.keep, client_id=args.client_id,
            )
            print(json.dumps(result, indent=2, default=str))
            print(f"\nReport: {result['report_path']}")
            return 0
        parser.print_help()
        return 0
    except InvoiceExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
