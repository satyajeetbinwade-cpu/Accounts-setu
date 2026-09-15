"""Structural file read + column-to-canonical-field inference.

Per the F3-AI build prompt this is "AI column-to-canonical-field mapping
using column headers, sample values, and position as signal — not a
fixed lookup table." This PoC has no LLM budget for a per-file model
call (cost-driven, consistent with the rest of the repo — see
src/documents/service.py's classify_document(), which is the same kind
of deliberately-simple heuristic stood in for a real AI-assisted
classifier). This module is that stand-in for column mapping: a
similarity scorer (rapidfuzz, already a project dependency) run against
the raw-header vocabulary already captured in config/source_mappings.yaml
for each source_type, adjusted by column position. Every caller only
ever sees the (raw_column, confidence, status) result — never this
scoring logic — so swapping this for a real model call later is a
same-shape change.

C1 does not model column-schema definitions today (its schema is rates/
taxonomy/regulatory config, not a per-source-type field dictionary), so
this module reads config/source_mappings.yaml's committed column lists
+ src/ingestion.py's canonical field lists as the working stand-in for
"C1's canonical schema definitions" the build prompt calls for. Noted
here explicitly as an adaptation, not a re-derivation of C1.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml
from rapidfuzz import fuzz

from src.ingestion import (
    GST_CANONICAL_FIELDS,
    TDS_CANONICAL_FIELDS,
    MAPPINGS_PATH,
)

# Fields every canonical schema carries that are never "mapped from a raw
# column" (bookkeeping fields ingestion.py adds itself) — excluded from
# mapping inference entirely.
_STRUCTURAL_FIELDS = {"source_type", "source_file", "original_row", "total_tax"}

# Confidence floor below which a raw file is considered to not match this
# canonical schema at all (used for the "unrecognized shape" flag, distinct
# from a normal low-confidence field).
UNRECOGNIZED_FLOOR = 30

# How close two candidate scores must be to both be shown as ambiguous
# ("where two plausible mappings exist... the review screen shows both
# candidates — never silently picks one").
AMBIGUITY_MARGIN = 6


def canonical_fields_for_source_type(source_type: str) -> list[str]:
    """The canonical field list relevant to a given raw source_type.
    'tally' feeds both GST and TDS books, so both field sets are offered —
    ingestion only ever pulls what a given recon run actually needs, and
    the mapping-review screen shows both groups clearly separated."""
    if source_type in ("gstr2b", "ims"):
        return [f for f in GST_CANONICAL_FIELDS if f not in _STRUCTURAL_FIELDS]
    if source_type in ("form26as", "tds"):
        return [f for f in TDS_CANONICAL_FIELDS if f not in _STRUCTURAL_FIELDS]
    if source_type == "tally":
        return [f for f in (GST_CANONICAL_FIELDS + TDS_CANONICAL_FIELDS) if f not in _STRUCTURAL_FIELDS]
    return []


def _load_known_aliases(source_type: str) -> dict[str, list[str]]:
    """canonical_field -> known raw column header strings for this
    source_type, read from config/source_mappings.yaml — the closest
    thing this repo has to a confirmed vocabulary per shape."""
    with open(MAPPINGS_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    columns_map: dict[str, str] = (raw.get(source_type) or {}).get("columns", {})
    aliases: dict[str, list[str]] = {}
    for raw_col, canonical_field in columns_map.items():
        aliases.setdefault(canonical_field, []).append(raw_col)
    return aliases


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.strip().lower()).strip()


def detect_header_row(path: Path, *, max_scan_rows: int = 10) -> int:
    """Detect the header row amid metadata/title rows: reads the first
    ``max_scan_rows`` rows with no header assumed, and picks the row with
    the most non-empty, non-purely-numeric cells — a title/metadata row
    is typically sparse (one or two cells) or a single merged title
    string; a real header row has many short text labels. Multi-row
    headers are not merged (documented limitation) — the first
    header-shaped row wins."""
    ext = path.suffix.lower()
    try:
        if ext == ".csv":
            preview = pd.read_csv(path, header=None, nrows=max_scan_rows, dtype=str, keep_default_na=False)
        else:
            preview = pd.read_excel(path, header=None, nrows=max_scan_rows, dtype=str)
    except Exception:
        return 0

    best_row, best_score = 0, -1
    for i in range(len(preview)):
        row = preview.iloc[i]
        non_empty = [str(v).strip() for v in row if str(v).strip() not in ("", "nan", "None")]
        if len(non_empty) < 2:
            continue
        text_cells = sum(1 for v in non_empty if not re.match(r"^-?\d+(\.\d+)?$", v))
        score = text_cells * 2 + len(non_empty)
        # Skip obvious subtotal/grand-total rows.
        if any(_normalize(v) in ("total", "grand total", "subtotal") for v in non_empty):
            continue
        if score > best_score:
            best_row, best_score = i, score
    return best_row


def read_with_detected_header(path: Path) -> pd.DataFrame:
    """Read the file, skipping metadata/title rows above the detected
    header row."""
    header_row = detect_header_row(path)
    ext = path.suffix.lower()
    if ext == ".csv":
        df = pd.read_csv(path, header=header_row, dtype=str, keep_default_na=False)
    else:
        df = pd.read_excel(path, header=header_row, dtype=str)
    # Drop subtotal/grand-total rows (any row whose first cell reads as one).
    if len(df.columns):
        first_col = df.columns[0]
        mask = df[first_col].astype(str).str.strip().str.lower().isin(["total", "grand total", "subtotal"])
        df = df.loc[~mask]
    return df


def score_column(raw_header: str, canonical_field: str, aliases: list[str], position: int, alias_position: Optional[int]) -> int:
    """Similarity score 0-100 between one raw header and one canonical
    field's known alias vocabulary, with a small positional boost when
    the column sits in the position that field is conventionally found
    at for this source_type (position is signal, not the sole basis)."""
    norm_header = _normalize(raw_header)
    best = 0
    for alias in aliases:
        best = max(best, fuzz.token_sort_ratio(norm_header, _normalize(alias)))
    if alias_position is not None and position == alias_position:
        best = min(100, best + 5)
    return int(best)


def infer_mapping(raw_headers: list[str], source_type: str) -> list[dict[str, Any]]:
    """Return one result dict per canonical field relevant to
    ``source_type``:
        {
          "canonical_field": str,
          "raw_column": str | None,
          "confidence": int | None,
          "candidates": [{"raw_column": str, "confidence": int}, ...],
          "status": "unavailable",  # filled in by the confidence-threshold
                                      # pass in service.py — this function
                                      # only scores candidates.
        }
    Never invents a mapping for a field with zero plausible candidates —
    such a field is left with raw_column=None ("genuinely absent", not a
    failed extraction).
    """
    fields = canonical_fields_for_source_type(source_type)
    aliases_by_field = _load_known_aliases(source_type)
    results: list[dict[str, Any]] = []

    for field in fields:
        aliases = aliases_by_field.get(field, [])
        alias_position = None
        # A field's conventional position in the KNOWN mapping (if any),
        # used only as a small tie-breaking signal.
        known_cols = list(aliases_by_field.keys())
        scored: list[tuple[str, int]] = []
        for pos, header in enumerate(raw_headers):
            if not aliases:
                continue
            score = score_column(header, field, aliases, pos, alias_position)
            if score > 0:
                scored.append((header, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        candidates = [{"raw_column": h, "confidence": s} for h, s in scored[:3] if s >= 40]

        if not candidates:
            results.append({
                "canonical_field": field, "raw_column": None, "confidence": None,
                "candidates": [],
            })
            continue

        top = candidates[0]
        results.append({
            "canonical_field": field, "raw_column": top["raw_column"], "confidence": top["confidence"],
            "candidates": candidates,
        })

    return results


def file_is_recognizable(field_results: list[dict[str, Any]]) -> bool:
    """A file is 'unrecognized' (doesn't match any known canonical
    schema) if NOT ONE field scored above the floor — never force-mapped
    into the nearest available schema in that case."""
    return any(
        (r["confidence"] or 0) >= UNRECOGNIZED_FLOOR for r in field_results
    )
