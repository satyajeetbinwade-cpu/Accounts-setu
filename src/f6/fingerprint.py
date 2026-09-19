"""F6 §4 — Fingerprint specification.

Computed from STRUCTURE ONLY — never from data values, which vary every
period. Must be stable across periods/clients for the same layout, and
must change when the layout changes.

Four structural signals, per §4:
  1. Sheet signature   — sorted, normalized set of sheet names.
  2. Header locus       — detected header row index (or pair) per sheet,
                           single- vs two-row.
  3. Column signature   — ordered, normalized, FLATTENED header labels
                           (parent::child for merged headers — this is
                           what disambiguates IMS B2B-CN's two identical
                           Tax Amount blocks).
  4. Shape hints         — title-row count, trailing-total-row presence,
                           merged-range pattern of the top rows.

The stored fingerprint is a hash of (1)-(4) PLUS the retained plaintext of
(1) and (3), so a near-miss can be diffed against the closest registered
version. Matching is exact-hash first; on miss, similarity against active
versions of the same slot is computed and surfaced to a human on Path B —
never auto-selected.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd


def _normalize_label(label: Any) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation noise.

    Pandas represents a genuinely blank cell as float NaN, which becomes
    the literal string "nan" once cast — treat that (and "none") as blank,
    never as a real label, or a merged-header child cell with no sub-label
    of its own would flatten to "parent :: nan" instead of just "parent".
    """
    if label is None:
        return ""
    try:
        if pd.isna(label):
            return ""
    except (TypeError, ValueError):
        pass
    s = re.sub(r"\s+", " ", str(label)).strip().lower()
    if s in ("nan", "none"):
        return ""
    return s


@dataclass
class SheetStructure:
    """What we detect about ONE sheet before fingerprinting it."""

    name: str
    header_row_index: Optional[int]       # single-row header, 0-based
    header_row_pair: Optional[tuple[int, int]]  # two-row merged header, 0-based (parent, child)
    is_two_row_header: bool
    flattened_columns: list[str]          # "parent::child" or bare label
    title_row_count: int
    has_trailing_total_row: bool
    data_row_count: int


@dataclass
class FileStructure:
    """The full structural digest of a workbook/CSV, before mapping."""

    sheets: dict[str, SheetStructure] = field(default_factory=dict)


@dataclass
class Fingerprint:
    """A computed fingerprint: the hash plus retained plaintext for diffing."""

    hash: str
    sheet_signature: list[str]
    column_signature: dict[str, list[str]]   # sheet_name -> flattened columns
    header_locus: dict[str, str]             # sheet_name -> "single:N" | "pair:N,M"
    shape_hints: dict[str, Any]

    def to_plaintext(self) -> dict[str, Any]:
        return {
            "sheet_signature": self.sheet_signature,
            "column_signature": self.column_signature,
            "header_locus": self.header_locus,
            "shape_hints": self.shape_hints,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_plaintext(), sort_keys=True)


def flatten_two_row_header(parent_row: list[Any], child_row: list[Any]) -> list[str]:
    """Flatten a two-row merged header into parent::child labels.

    A parent cell that spans multiple child columns must be forward-filled
    (pandas reads a merged cell as non-null only in its first column, NaN
    thereafter). This is THE disambiguation mechanism for IMS B2B-CN's two
    identical (Integrated/Central/State-UT/Cess) blocks under different
    parents — keying on the child label alone would silently bind the
    wrong block (F6 §5.2's named regression risk).
    """
    flattened: list[str] = []
    last_parent = ""
    for idx, child in enumerate(child_row):
        parent = parent_row[idx] if idx < len(parent_row) else None
        if parent is not None and str(parent).strip() and str(parent).strip().lower() != "nan":
            last_parent = _normalize_label(parent)
        child_label = _normalize_label(child)
        if last_parent and child_label and child_label != last_parent:
            flattened.append(f"{last_parent} :: {child_label}")
        elif child_label:
            flattened.append(child_label)
        elif last_parent:
            flattened.append(last_parent)
        else:
            flattened.append(f"col_{idx}")
    return flattened


def compute_sheet_structure(
    name: str,
    raw: pd.DataFrame,
    *,
    header_row_index: Optional[int] = None,
    header_row_pair: Optional[tuple[int, int]] = None,
) -> SheetStructure:
    """Build a SheetStructure from a headerless raw read (header=None) of a
    sheet, given the CALLER's already-determined header location (per-format
    parsing rules — §5 requires header detection to be per-format, not a
    global assumption, so this function does not itself guess)."""
    is_two_row = header_row_pair is not None
    if is_two_row:
        parent_idx, child_idx = header_row_pair
        parent_row = list(raw.iloc[parent_idx]) if parent_idx < len(raw) else []
        child_row = list(raw.iloc[child_idx]) if child_idx < len(raw) else []
        flattened = flatten_two_row_header(parent_row, child_row)
        title_rows = parent_idx
        data_start = child_idx + 1
    else:
        idx = header_row_index if header_row_index is not None else 0
        header_row = list(raw.iloc[idx]) if idx < len(raw) else []
        flattened = [_normalize_label(c) or f"col_{i}" for i, c in enumerate(header_row)]
        title_rows = idx
        data_start = idx + 1

    data_rows = raw.iloc[data_start:]
    has_total_row = False
    if len(data_rows) > 0:
        last_row = data_rows.iloc[-1]
        joined = " ".join(_normalize_label(v) for v in last_row if v is not None)
        has_total_row = "total" in joined

    return SheetStructure(
        name=name,
        header_row_index=header_row_index,
        header_row_pair=header_row_pair,
        is_two_row_header=is_two_row,
        flattened_columns=flattened,
        title_row_count=title_rows,
        has_trailing_total_row=has_total_row,
        data_row_count=max(0, len(data_rows) - (1 if has_total_row else 0)),
    )


def compute_fingerprint(file_structure: FileStructure) -> Fingerprint:
    """Hash of (1) sheet signature, (2) header locus, (3) column signature,
    (4) shape hints — plus retained plaintext of (1) and (3) for diffing."""
    sheet_signature = sorted(_normalize_label(n) for n in file_structure.sheets.keys())

    column_signature: dict[str, list[str]] = {}
    header_locus: dict[str, str] = {}
    shape_hints: dict[str, Any] = {}

    for name, s in sorted(file_structure.sheets.items()):
        norm_name = _normalize_label(name)
        column_signature[norm_name] = s.flattened_columns
        if s.is_two_row_header and s.header_row_pair:
            header_locus[norm_name] = f"pair:{s.header_row_pair[0]},{s.header_row_pair[1]}"
        else:
            header_locus[norm_name] = f"single:{s.header_row_index}"
        shape_hints[norm_name] = {
            "title_row_count": s.title_row_count,
            "has_trailing_total_row": s.has_trailing_total_row,
        }

    payload = json.dumps(
        {
            "sheet_signature": sheet_signature,
            "column_signature": column_signature,
            "header_locus": header_locus,
            "shape_hints": shape_hints,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    return Fingerprint(
        hash=digest,
        sheet_signature=sheet_signature,
        column_signature=column_signature,
        header_locus=header_locus,
        shape_hints=shape_hints,
    )


def _column_similarity(a: list[str], b: list[str]) -> float:
    """Jaccard similarity over flattened column labels (order-agnostic —
    a genuinely new column inserted mid-sheet shouldn't tank the score to
    zero, since the near-miss diff is meant to say 'this looks like X with
    N new columns', not just 'no match')."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def similarity(fp: Fingerprint, other_plaintext: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Similarity of `fp` against a REGISTERED version's retained plaintext.

    Returns (score 0-1, diff) where diff names sheets added/removed and,
    per shared sheet, columns added/removed — advisory to a human only,
    NEVER used to auto-select a version (§4's explicit constraint).
    """
    other_sheets = set(other_plaintext.get("sheet_signature", []))
    this_sheets = set(fp.sheet_signature)
    sheets_added = sorted(this_sheets - other_sheets)
    sheets_removed = sorted(other_sheets - this_sheets)
    shared = sorted(this_sheets & other_sheets)

    other_columns = other_plaintext.get("column_signature", {})
    per_sheet_diff: dict[str, Any] = {}
    sheet_scores: list[float] = []
    for sheet in shared:
        this_cols = fp.column_signature.get(sheet, [])
        that_cols = other_columns.get(sheet, [])
        score = _column_similarity(this_cols, that_cols)
        sheet_scores.append(score)
        added = sorted(set(this_cols) - set(that_cols))
        removed = sorted(set(that_cols) - set(this_cols))
        if added or removed:
            per_sheet_diff[sheet] = {"columns_added": added, "columns_removed": removed}

    if not shared:
        overall = 0.0
    else:
        # Penalize sheet-set mismatches, not just column mismatches.
        sheet_set_score = len(shared) / len(this_sheets | other_sheets) if (this_sheets | other_sheets) else 1.0
        overall = 0.5 * sheet_set_score + 0.5 * (sum(sheet_scores) / len(sheet_scores))

    diff = {
        "sheets_added": sheets_added,
        "sheets_removed": sheets_removed,
        "sheet_column_diffs": per_sheet_diff,
    }
    return overall, diff


SIMILARITY_SURFACE_THRESHOLD = 0.55  # below this, don't bother naming a "closest" version at all
