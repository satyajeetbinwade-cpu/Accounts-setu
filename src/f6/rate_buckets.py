"""F6 §5.4 — the deterministic RATE-BUCKET detector.

A real Purchase Register does not carry one CGST column; it carries a CGST
column *per tax rate* (2.5%, 9%, …), and the same for SGST and IGST. Mapping
`cgst` therefore requires SUMMING N columns, which the one-column-to-one-field
model cannot express (RC1/D01) — and summing them naively double-counts the
taxable base, because CGST and SGST restate the same value (RC2/D02).

This module is the deterministic engine that resolves both problems. It runs
BEFORE the AI (the AI is only shown the resulting matrix as a structural hint,
per §5 Part 1.7) and it never hard-codes a slab: the rates come from the
headers themselves, so a new slab is a new bucket, not a code change.

Public surface:
  * `detect_rate_buckets(headers)` -> `RateBucketMatrix`
  * `RateBucketMatrix.aggregate_rules()` -> canonical-field rules for the parser
  * `RateBucketMatrix.mirror_pairs()` -> CGST/SGST pairs for the mirror check
  * `RateBucketMatrix.rate_bucket_inputs()` -> per-bucket inputs for the
    implied-rate check

Header shapes recognised (case-insensitive, whitespace-normalised):

    IGST Txbl. Amt @ 5%      IGST Tax Amt @ 5%
    CGST Txbl. Amt @ 2.5%    CGST Tax Amt @ 2.5%
    SGST Txbl. Amt @ 2.5%    SGST Tax Amt @ 2.5%
    UTGST Tax Amt @ 9%       CESS Txbl. Amt @ 12%

i.e. `<HEAD> <Txbl|Taxable|Tax> [Amt|Amount] @ <rate>%`, where HEAD is one of
IGST / CGST / SGST / UTGST / CESS. A non-bucketed `cgst`/`sgst`/… column (no
rate) is NOT claimed by this detector — it stays a plain direct mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Tax heads, in the order the canonical fields are emitted.
HEADS = ("IGST", "CGST", "SGST", "UTGST", "CESS")

# Canonical field each head's TAX columns aggregate into. UTGST folds into
# SGST (a union-territory supply substitutes UTGST for SGST; the two are
# never both charged on one invoice, so summing into the same field is
# correct and keeps the canonical schema unchanged — §3 rule 9).
HEAD_CANONICAL: dict[str, str] = {
    "IGST": "igst",
    "CGST": "cgst",
    "SGST": "sgst",
    "UTGST": "sgst",
    "CESS": "cess",
}

# CGST and SGST restate the SAME taxable base; UTGST is SGST's substitute.
# Only ONE side of a mirror pair may feed `taxable_value` (§5.4).
_MIRROR_OF: dict[str, str] = {"SGST": "CGST", "UTGST": "CGST"}

# `<HEAD> <kind> [amt|amount] @ <rate>%`
_BUCKET_RE = re.compile(
    r"^(?P<head>igst|cgst|sgst|utgst|cess)\s+"
    r"(?P<kind>txbl\.?|taxable|tax)\s*"
    r"(?:amt\.?|amount)?\s*"
    r"@\s*(?P<rate>\d+(?:\.\d+)?)\s*%$"
)


@dataclass
class RateBucket:
    """One `<head> <kind> @ <rate>` cell of the matrix."""

    head: str
    kind: str          # 'taxable' | 'tax'
    rate: float
    column: str        # the flattened source column label


@dataclass
class RateBucketMatrix:
    """The head × rate × {taxable, tax} matrix detected from a file's headers."""

    buckets: list[RateBucket] = field(default_factory=list)
    # source column -> head it belongs to, for the disposition report.
    ignored_columns: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return bool(self.buckets)

    def heads(self) -> list[str]:
        return [h for h in HEADS if any(b.head == h for b in self.buckets)]

    def rates_for(self, head: str) -> list[float]:
        return sorted({b.rate for b in self.buckets if b.head == head})

    def tax_columns(self, head: str) -> list[str]:
        return [b.column for b in self.buckets if b.head == head and b.kind == "tax"]

    def taxable_columns(self, head: str) -> list[str]:
        return [b.column for b in self.buckets if b.head == head and b.kind == "taxable"]

    # -- canonical rules ---------------------------------------------------

    def aggregate_rules(self) -> list[dict[str, Any]]:
        """The canonical-field rules the parser must apply.

        * each head's TAX columns -> that head's canonical field (a plain sum);
        * `taxable_value` -> the UNION of the TAXABLE columns of the
          non-mirrored heads only (IGST + CGST, never + SGST/UTGST), which is
          exactly §5.4's de-duplication;
        * `total_tax` -> derived, never mapped (§5 Part 2.2).
        """
        rules: list[dict[str, Any]] = []
        emitted: set[str] = set()

        for head in self.heads():
            if head in _MIRROR_OF:
                continue  # mirrored head — its tax is summed into the primary
            cols = self.tax_columns(head)
            if cols:
                canonical = HEAD_CANONICAL[head]
                if canonical not in emitted:
                    rules.append({
                        "canonical_field": canonical,
                        "kind": "aggregate",
                        "source_columns": cols,
                        "transform": None,
                    })
                    emitted.add(canonical)

        # Mirror heads contribute their TAX to the SAME canonical field as
        # their primary (SGST already has its own field; UTGST folds to sgst).
        for head, primary in _MIRROR_OF.items():
            cols = self.tax_columns(head)
            canonical = HEAD_CANONICAL[head]
            if cols and canonical not in emitted:
                rules.append({
                    "canonical_field": canonical,
                    "kind": "aggregate",
                    "source_columns": cols,
                    "transform": None,
                })
                emitted.add(canonical)

        # taxable_value — primary heads only (mirrors excluded).
        taxable_cols: list[str] = []
        excluded: list[str] = []
        for head in self.heads():
            cols = self.taxable_columns(head)
            if head in _MIRROR_OF:
                excluded.extend(cols)
            else:
                taxable_cols.extend(cols)
        if taxable_cols:
            rules.append({
                "canonical_field": "taxable_value",
                "kind": "aggregate",
                "source_columns": taxable_cols,
                "transform": "dedupe_taxable_base_cgst_sgst_restate",
                "excluded_mirrors": excluded,
            })
        return rules

    def mirror_pairs(self) -> list[tuple[str, str]]:
        """(primary canonical field, mirror canonical field) TAXABLE pairs.

        Only pairs that actually exist on BOTH sides are returned — a file
        with no SGST buckets has nothing to mirror, and the check must skip
        rather than flag every row.
        """
        pairs: list[tuple[str, str]] = []
        primary = "CGST"
        for head in ("SGST", "UTGST"):
            if not self.taxable_columns(head):
                continue
            primary_cols = self.taxable_columns(primary)
            if not primary_cols:
                continue
            # Compare column-by-column within the same rate bucket.
            for b in self.buckets:
                if b.head == head and b.kind == "taxable":
                    mate = next(
                        (x for x in self.buckets
                         if x.head == primary and x.kind == "taxable" and x.rate == b.rate),
                        None,
                    )
                    if mate is not None:
                        pairs.append((mate.column, b.column))
        return pairs

    def rate_bucket_inputs(self) -> list[dict[str, Any]]:
        """Per-bucket inputs for the implied-rate check: the TAX and TAXABLE
        columns of the SAME head and SAME rate, with that rate."""
        out: list[dict[str, Any]] = []
        for b in self.buckets:
            if b.kind != "tax":
                continue
            mate = next(
                (x for x in self.buckets
                 if x.head == b.head and x.kind == "taxable" and x.rate == b.rate),
                None,
            )
            if mate is None:
                continue
            out.append({"rate": b.rate, "taxable": mate.column, "tax": b.column})
        return out

    def to_report(self) -> list[dict[str, Any]]:
        """The head × rate matrix as rows, for the UI's Tax-columns card."""
        rows: list[dict[str, Any]] = []
        for head in self.heads():
            for rate in self.rates_for(head):
                taxable = next(
                    (b.column for b in self.buckets
                     if b.head == head and b.kind == "taxable" and b.rate == rate), None)
                tax = next(
                    (b.column for b in self.buckets
                     if b.head == head and b.kind == "tax" and b.rate == rate), None)
                rows.append({
                    "head": head,
                    "rate": rate,
                    "taxable_column": taxable,
                    "tax_column": tax,
                    "canonical_field": HEAD_CANONICAL[head],
                    "mirror_of": _MIRROR_OF.get(head),
                })
        return rows


def detect_rate_buckets(headers: list[str]) -> RateBucketMatrix:
    """Build the rate-bucket matrix from a file's flattened header labels.

    Deterministic and slab-agnostic: 5/18/2.5/9 are read from the headers,
    never assumed. Returns an empty matrix (`.detected is False`) when the
    file has no rate-bucketed columns at all — a plain one-column-per-head
    export must not be forced through this path.
    """
    matrix = RateBucketMatrix()
    for raw in headers:
        label = re.sub(r"\s+", " ", str(raw)).strip()
        if not label:
            continue
        m = _BUCKET_RE.match(label.lower())
        if not m:
            continue
        head = m.group("head").upper()
        kind_raw = m.group("kind").rstrip(".")
        kind = "taxable" if kind_raw in ("txbl", "taxable") else "tax"
        matrix.buckets.append(
            RateBucket(head=head, kind=kind, rate=float(m.group("rate")), column=label)
        )
    return matrix


def taxable_columns_not_in_rules(headers: list[str], matrix: RateBucketMatrix) -> list[str]:
    """Rate-bucket taxable columns the aggregation deliberately EXCLUDES
    (the mirrors) — reported as `excluded_mirrors` so the reviewer sees the
    de-duplication rather than an unexplained omission."""
    claimed = {b.column for b in matrix.buckets}
    return [h for h in headers if h in claimed and any(
        b.column == h and b.head in _MIRROR_OF for b in matrix.buckets
    )]
