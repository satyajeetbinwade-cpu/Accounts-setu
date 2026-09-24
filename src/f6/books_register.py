"""F6 §5.4 — deterministic parse of a rate-bucketed BOOKS / Purchase Register.

WHY THIS EXISTS
---------------
A books-side Purchase Register splits each tax head across rate buckets
(`CGST Tax Amt @ 2.5%`, `CGST Tax Amt @ 9%`, …), so a canonical field is the
SUM of N columns, and the CGST/SGST taxable columns restate the same base.
The generic model-mapper cannot express either (RC1/D01, RC2/D02): it maps one
column to one field and zero-fills whatever it cannot map.

`f6_bridge.parse_portal_export()` already solves this class of problem for
GSTR-2B / IMS. This module is the SAME approach for the books slot:

  * the rate-bucket matrix is detected DETERMINISTICALLY from the headers
    (`rate_buckets.detect_rate_buckets`) — no slab is hard-coded, so a client
    with 12% / 28% buckets works with no code change;
  * a parse config is GENERATED from that matrix and handed to F6's existing
    generic parser (`src.f6.parser.parse_file`), so the arithmetic is done by
    the one parser, not a second implementation;
  * §8 validation runs on the parsed rows, including §5.4's two named
    protections (mirror assertion + implied-rate check);
  * file metadata above the header is captured (entity / report title /
    period) so a wrong-client or wrong-period upload can be surfaced.

It deliberately does NOT go through `f6.service.ingest_file()`: the upload slot
already tells us the file is books-side, so there is no fingerprint gate to
pass. This mirrors `f6_bridge`'s documented rationale exactly.

Returns `None` when the file is not a rate-bucketed books register, so the
caller falls back to the generic path rather than force-fitting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from src.f6.fingerprint import _normalize_label
from src.f6.rate_buckets import RateBucketMatrix, detect_rate_buckets

# Source types that are a books-side register.
_BOOKS_SOURCE_TYPES = {"tally", "tally_purchase_register"}

# Header aliases for the non-bucketed identity/value columns. Matched on the
# NORMALIZED flattened label, exact first then substring — a client's export
# may title the column "Name & Address of Dealer" or "Supplier Name".
_ALIASES: dict[str, list[str]] = {
    "invoice_number": ["bill no", "bill number", "invoice no", "invoice number", "voucher no", "voucher number"],
    "invoice_date": ["date", "bill date", "invoice date", "voucher date"],
    "party_name": ["name & address of dealer", "supplier name", "party name", "dealer name", "name of dealer"],
    "gstin": ["gstin", "gstin/uin", "supplier gstin", "gstin of supplier", "party gstin"],
    "invoice_value": ["bill amount", "invoice value", "invoice amount", "bill value", "total amount"],
    "rounding_adjustment": ["other amt.", "other amt", "round off", "rounding", "round off amount"],
}

# Columns that are a VALIDATION SIGNAL rather than a mapping (§5 Part 1.2).
# `Purc. Type` states local (L) vs inter-state (I) — a strong independent
# check on the GSTIN state code and which tax head is populated.
_SIGNAL_ALIASES: dict[str, str] = {
    "purchase_type": "purc. type",
}

# Columns that are safely IGNORABLE, with a reason for the disposition report.
_IGNORED_ALIASES: dict[str, str] = {
    "s.no.": "Row serial number — carries no reconciliation value.",
    "sno": "Row serial number — carries no reconciliation value.",
    "sr no": "Row serial number — carries no reconciliation value.",
}

_DATE_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$")
_PERIOD_RE = re.compile(r"from\s+(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\s+to\s+(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", re.I)

# §5 Part 2.2 — requiredness tiers for the Purchase Register slot. C1 owns
# these in production; these are the proposed defaults from the build prompt.
#   required    — blocks confirm
#   recommended — flagged, does not block
#   derived     — never mapped (computed from other fields)
#   optional    — may legitimately be "not in this file"
REQUIRED_FIELDS_TIER: dict[str, list[str]] = {
    "required": [
        "gstin", "invoice_number", "invoice_date",
        "taxable_value", "igst", "cgst", "sgst", "invoice_value",
    ],
    "recommended": ["party_name", "rounding_adjustment"],
    "derived": ["total_tax"],
    "optional": ["cess"],
}
BOOKS_REQUIRED_FIELDS: list[str] = REQUIRED_FIELDS_TIER["required"]


def field_tier(canonical_field: str) -> str:
    """Which tier a canonical field belongs to, for the Purchase Register."""
    for tier, fields in REQUIRED_FIELDS_TIER.items():
        if canonical_field in fields:
            return tier
    return "optional"


@dataclass
class FileMetadata:
    """Rows above the header — §5 Part 3.1."""

    entity_name: Optional[str] = None
    address: Optional[str] = None
    report_title: Optional[str] = None
    period_start: Optional[str] = None   # ISO yyyy-mm-dd
    period_end: Optional[str] = None
    filter_text: Optional[str] = None
    header_row: Optional[int] = None
    date_format: Optional[str] = None
    date_format_evidence: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_name": self.entity_name,
            "address": self.address,
            "report_title": self.report_title,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "filter_text": self.filter_text,
            "header_row": self.header_row,
            "date_format": self.date_format,
            "date_format_evidence": self.date_format_evidence,
        }


@dataclass
class BooksParseResult:
    """Everything the review screen and the hard gate need."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    # Rows keyed by FLATTENED SOURCE column label — the §5.4 mirror /
    # implied-rate checks are assertions about source columns, not canonical
    # fields, so they must run against the source view, not the mapped one.
    source_rows: list[dict[str, Any]] = field(default_factory=list)
    frame: Optional[pd.DataFrame] = None
    rules: list[dict[str, Any]] = field(default_factory=list)
    matrix: Optional[RateBucketMatrix] = None
    metadata: FileMetadata = field(default_factory=FileMetadata)
    warnings: list[str] = field(default_factory=list)
    row_count_read: int = 0
    row_count_parsed: int = 0
    exclusion_reasons: list[dict[str, Any]] = field(default_factory=list)
    control_totals: list[tuple[float, Optional[float], str]] = field(default_factory=list)
    column_dispositions: list[dict[str, Any]] = field(default_factory=list)

    def validation_rows(self) -> list[dict[str, Any]]:
        """Canonical rows PLUS the raw source columns, merged per row.

        §8's checks are of two kinds: canonical-field checks (required
        fields, GSTIN, tax arithmetic, duplicates) and SOURCE-COLUMN checks
        (mirror, implied rate). Merging lets one row set drive both, since
        the parser keeps rows in the same order as the source read.
        """
        merged: list[dict[str, Any]] = []
        for idx, row in enumerate(self.rows):
            combined = dict(row)
            if idx < len(self.source_rows):
                for k, v in self.source_rows[idx].items():
                    combined.setdefault(k, v)
            merged.append(combined)
        return merged

    def mirror_inputs(self) -> list[tuple[str, str]]:
        return self.matrix.mirror_pairs() if self.matrix else []

    def rate_bucket_inputs(self) -> list[dict[str, Any]]:
        return self.matrix.rate_bucket_inputs() if self.matrix else []

    def run_validation(self, *, period: Optional[str] = None, required_fields: Optional[list[str]] = None):
        """Run §8's guardrails over this parse.

        `period` should be the period the upload is FILED under (not the
        file's own stated period) so an out-of-period row is flagged — that
        is a real reconciliation finding, not an ingestion defect.
        """
        from src.f6 import validation as f6validation

        required = required_fields or BOOKS_REQUIRED_FIELDS
        return f6validation.run_all_checks(
            rows=self.validation_rows(),
            row_count_read=self.row_count_read,
            row_count_parsed=self.row_count_parsed,
            exclusion_reasons=self.exclusion_reasons,
            required_fields=required,
            control_totals=self.control_totals,
            date_field="invoice_date",
            period=period,
            mirror_pairs=self.mirror_inputs(),
            rate_buckets=self.rate_bucket_inputs(),
        )


# ---------------------------------------------------------------------------
# Structural read
# ---------------------------------------------------------------------------


def _read_headerless(path: str, *, is_excel: bool) -> pd.DataFrame:
    if is_excel:
        return pd.read_excel(path, header=None, dtype=str, keep_default_na=False)
    return pd.read_csv(path, header=None, dtype=str, keep_default_na=False)


def _find_header_row(raw: pd.DataFrame, matrix_headers: list[str]) -> int:
    """Locate the header row: the row whose cells best match the detected
    rate-bucket labels. Deterministic — no scoring heuristics beyond exact
    label overlap, so it cannot drift."""
    targets = {_normalize_label(h) for h in matrix_headers}
    best_idx, best_score = 0, 0
    for i in range(min(30, len(raw))):
        row_labels = {_normalize_label(v) for v in raw.iloc[i] if str(v).strip()}
        score = len(row_labels & targets)
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx


def _capture_metadata(raw: pd.DataFrame, header_row: int) -> FileMetadata:
    """Rows 0..header_row-1 are file metadata (§5 Part 3.1). The layout is
    positional for this family: entity, address, report title, period,
    filter — but each is read defensively (first non-blank cell) so a
    missing row shifts nothing."""
    meta = FileMetadata(header_row=header_row)

    def first_cell(i: int) -> Optional[str]:
        if i >= header_row:
            return None
        for v in raw.iloc[i]:
            s = str(v).strip()
            if s and s.lower() != "nan":
                return s
        return None

    rows_above = [first_cell(i) for i in range(header_row)]
    rows_above = [r for r in rows_above if r]

    if rows_above:
        meta.entity_name = rows_above[0]
    if len(rows_above) > 1:
        meta.address = rows_above[1]
    for r in rows_above:
        if "register" in r.lower() or "report" in r.lower():
            meta.report_title = r
            break
    for r in rows_above:
        m = _PERIOD_RE.search(r)
        if m:
            d1, m1, y1, d2, m2, y2 = m.groups()
            meta.period_start = f"{int(y1):04d}-{int(m1):02d}-{int(d1):02d}"
            meta.period_end = f"{int(y2):04d}-{int(m2):02d}-{int(d2):02d}"
            break
    # Filter text: a short row above the header that is neither the entity,
    # address, title nor period.
    for r in rows_above:
        if r in (meta.entity_name, meta.address, meta.report_title):
            continue
        if meta.period_start and _PERIOD_RE.search(r):
            continue
        if len(r) < 40 and not any(ch.isdigit() for ch in r[:3]):
            meta.filter_text = r
            break
    return meta


def _detect_date_format(raw: pd.DataFrame, header_row: int, date_col: Optional[int]) -> tuple[str, str]:
    """Detect DD-MM-YYYY vs MM-DD-YYYY from the DATA, with evidence.

    Evidence: a first component above 12 proves day-first; a second component
    above 12 proves month-first. Without such a row the period header is the
    tie-breaker. Defaults to DD-MM-YYYY (the Indian convention) and SAYS SO
    rather than pretending to be certain.
    """
    if date_col is None:
        return "%d-%m-%Y", "No date column detected — assumed DD-MM-YYYY."
    day_first_proof = month_first_proof = False
    checked = 0
    for i in range(header_row + 1, min(header_row + 200, len(raw))):
        val = str(raw.iat[i, date_col]).strip()
        m = _DATE_RE.match(val)
        if not m:
            continue
        checked += 1
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            day_first_proof = True
        if b > 12:
            month_first_proof = True
    if day_first_proof and not month_first_proof:
        return "%d-%m-%Y", f"A day value above 12 exists in the date column ({checked} value(s) inspected)."
    if month_first_proof and not day_first_proof:
        return "%m-%d-%Y", f"A month value above 12 exists in the date column ({checked} value(s) inspected)."
    return "%d-%m-%Y", (
        f"No unambiguous date ({checked} value(s) inspected) — DD-MM-YYYY assumed "
        "(Indian convention). The period header is the corroborating signal."
    )


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------


def _match_alias(normalized_label: str, aliases: list[str]) -> bool:
    if normalized_label in aliases:
        return True
    return any(a in normalized_label for a in aliases)


def _build_parse_config(
    headers: list[str], matrix: RateBucketMatrix, header_row: int, date_format: str,
    signals: dict[str, str], ignored: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Generate the parse_config F6's generic parser consumes, plus the
    column-disposition report (§5 Part 1.2 — every column ends in exactly
    one of used-in / validation-signal / ignored / needs-decision)."""
    norm = {_normalize_label(h): h for h in headers}
    bucket_cols = {b.column for b in matrix.buckets}

    rules: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    used_cols: set[str] = set()

    # 1. Identity / value columns — direct or derived.
    for field_name, aliases in _ALIASES.items():
        if field_name == "rounding_adjustment":
            continue  # handled below with a residual-aware rule
        hit = next((norm[n] for n in norm if n not in bucket_cols and _match_alias(n, aliases)), None)
        if hit is None:
            continue
        if field_name == "invoice_date":
            rules.append({
                "canonical_field": field_name, "kind": "derived",
                "source_columns": [hit], "transform": f"parse_date:{date_format}",
            })
        else:
            rules.append({"canonical_field": field_name, "kind": "direct", "source_columns": [hit]})
        used_cols.add(hit)

    # rounding_adjustment — a residual; absent means 0, present means mapped.
    r_hit = next((norm[n] for n in norm if n not in bucket_cols
                  and _match_alias(n, _ALIASES["rounding_adjustment"])), None)
    if r_hit is not None:
        rules.append({"canonical_field": "rounding_adjustment", "kind": "direct", "source_columns": [r_hit]})
        used_cols.add(r_hit)

    # 2. Rate-bucket aggregations — from the DETECTED matrix (Part 1.4).
    excluded_mirrors: set[str] = set()
    bucket_field: dict[str, str] = {}
    for rule in matrix.aggregate_rules():
        rules.append(rule)
        used_cols.update(rule.get("source_columns") or [])
        for col in rule.get("source_columns") or []:
            bucket_field[col] = rule["canonical_field"]
        # A mirror column is deliberately NOT summed, but it IS accounted
        # for — it must end in the "used-in" disposition with the reason
        # recorded, never as an orphan "needs decision" (§5 Part 1.2).
        for col in rule.get("excluded_mirrors") or []:
            excluded_mirrors.add(col)
            used_cols.add(col)

    # 3. Dispositions for every remaining column.
    for h in headers:
        n = _normalize_label(h)
        if not n:
            continue
        if h in excluded_mirrors:
            b = next((x for x in matrix.buckets if x.column == h), None)
            primary = "CGST" if (b and b.head in ("SGST", "UTGST")) else "the primary head"
            dispositions.append({
                "column": h,
                "disposition": "used-in",
                "detail": f"Mirror of {primary} — deliberately NOT added (it restates the same "
                          "taxable base, so summing it would double-count).",
                "canonical_field": "taxable_value",
            })
            continue
        if h in used_cols:
            if h in bucket_field:
                b = next(x for x in matrix.buckets if x.column == h)
                dispositions.append({
                    "column": h, "disposition": "used-in",
                    "detail": f"Rate bucket {b.head} @ {b.rate:g}% ({b.kind}) — summed into "
                              f"{bucket_field[h]}.",
                    "canonical_field": bucket_field[h],
                })
            else:
                dispositions.append({
                    "column": h, "disposition": "used-in",
                    "detail": "Mapped directly to a Setu field.", "canonical_field": None,
                })
            continue
        if n in signals:
            dispositions.append({
                "column": h, "disposition": "validation-signal",
                "detail": signals[n], "canonical_field": None,
            })
            continue
        if n in ignored:
            dispositions.append({
                "column": h, "disposition": "ignored", "detail": ignored[n], "canonical_field": None,
            })
            continue
        dispositions.append({
            "column": h, "disposition": "needs-decision",
            "detail": "Not recognised as an identity, rate-bucket or signal column.",
            "canonical_field": None,
        })

    control_cols = [b.column for b in matrix.buckets] + ([r_hit] if r_hit else [])
    invoice_col = next((c for c in used_cols if _normalize_label(c) in _ALIASES["invoice_value"]), None)
    if invoice_col:
        control_cols.insert(0, invoice_col)

    config = {
        "sheets": {
            "books register": {
                "role": "line_items",
                "header": {"type": "single", "row": header_row},
                "data_start_row": header_row + 1,
                "row_exclusion": {
                    "trailing_blank": True,
                    "total_row_marker": {"column": signals.get("purchase_type", "purc. type"), "contains": "total"},
                },
                "field_rules": rules,
                "control_total": {"source": "own_total_row", "columns": control_cols},
            }
        },
        "date_format": date_format,
    }
    return config, dispositions


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _read_source_rows(
    raw: pd.DataFrame, header_row: int, parsed,
) -> list[dict[str, Any]]:
    """The kept data rows keyed by FLATTENED SOURCE column label.

    Built from the same header/data slice the parser used, with the same
    blank/total-row exclusions applied, so the §5.4 source-column assertions
    (mirror, implied rate) see exactly the rows the parser kept.
    """
    headers = [str(v) for v in raw.iloc[header_row]]
    total_marker_col = None
    for idx, h in enumerate(headers):
        if _normalize_label(h) == _normalize_label("purc. type"):
            total_marker_col = idx
            break

    out: list[dict[str, Any]] = []
    for i in range(header_row + 1, len(raw)):
        values = [raw.iat[i, c] if c < len(raw.columns) else None for c in range(len(headers))]
        texts = [str(v).strip() for v in values]
        if all(t in ("", "nan", "None") for t in texts):
            continue
        if total_marker_col is not None and "total" in texts[total_marker_col].lower():
            continue
        out.append({headers[c]: values[c] for c in range(len(headers))})
    return out


def parse_books_register(
    path: str, source_type: str, filename: str,
) -> Optional[BooksParseResult]:
    """Deterministically parse a rate-bucketed books register.

    Returns None when the file is not this shape (no rate buckets detected),
    so the caller falls back to the generic path rather than force-fitting.
    """
    if source_type not in _BOOKS_SOURCE_TYPES:
        return None
    is_excel = path.lower().endswith((".xlsx", ".xls"))

    try:
        raw = _read_headerless(path, is_excel=is_excel)
    except Exception:  # noqa: BLE001
        return None
    if raw.empty:
        return None

    # Detect the matrix from whichever row looks most like a header: probe
    # the conventional title-block depth first, then scan, so a file with a
    # deeper or shallower title block still resolves.
    matrix = detect_rate_buckets([str(v) for v in raw.iloc[min(10, len(raw) - 1)]])
    if not matrix.detected:
        for i in range(min(30, len(raw))):
            candidate = detect_rate_buckets([str(v) for v in raw.iloc[i]])
            if candidate.detected:
                matrix = candidate
                break
    if not matrix.detected:
        return None

    header_row = _find_header_row(raw, [b.column for b in matrix.buckets])
    headers = [str(v) for v in raw.iloc[header_row]]
    matrix = detect_rate_buckets(headers)
    if not matrix.detected:
        return None

    date_col_idx = None
    for idx, h in enumerate(headers):
        if _match_alias(_normalize_label(h), _ALIASES["invoice_date"]):
            date_col_idx = idx
            break
    date_format, date_evidence = _detect_date_format(raw, header_row, date_col_idx)

    signals = {
        alias: "Validation signal: local (L) vs inter-state (I) — checked against "
               "the GSTIN state code and which tax head is populated."
        for alias in _SIGNAL_ALIASES.values()
    }
    config, dispositions = _build_parse_config(headers, matrix, header_row, date_format, signals, _IGNORED_ALIASES)

    from src.f6.parser import parse_file

    parsed = parse_file(path, config, is_excel=is_excel)
    rows = parsed.all_rows
    if not rows:
        return None

    source_rows = _read_source_rows(raw, header_row, parsed)

    meta = _capture_metadata(raw, header_row)
    meta.date_format = date_format
    meta.date_format_evidence = date_evidence

    warnings: list[str] = []
    for sr in parsed.sheet_results:
        for item in sr.exclusion_reasons:
            warnings.append(f"{item['count']} row(s) excluded — {item['reason'].replace('_', ' ')}.")

    # Control totals: parsed invoice_value vs the file's own Total row.
    parsed_sum = sum(float(r.get("invoice_value") or 0) for r in rows)
    stated = 0.0
    for sr in parsed.sheet_results:
        stated += sum(sr.control_total_candidates.values())
    control_totals: list[tuple[float, Optional[float], str]] = []
    if parsed.sheet_results and parsed.sheet_results[0].control_total_candidates:
        # Compare the bill-amount column specifically where present.
        stated_bill = None
        for sr in parsed.sheet_results:
            for col, val in sr.control_total_candidates.items():
                if _normalize_label(col) in _ALIASES["invoice_value"]:
                    stated_bill = val
        control_totals.append((parsed_sum, stated_bill if stated_bill is not None else stated, "file's own Total row"))

    result = BooksParseResult(
        rows=rows,
        source_rows=source_rows,
        rules=config["sheets"]["books register"]["field_rules"],
        matrix=matrix,
        metadata=meta,
        warnings=warnings,
        row_count_read=parsed.row_count_read,
        row_count_parsed=parsed.row_count_parsed,
        exclusion_reasons=parsed.exclusion_reasons,
        control_totals=control_totals,
        column_dispositions=dispositions,
    )
    result.frame = _to_frame(rows, source_type, filename, matrix)
    return result


def _to_frame(rows: list[dict[str, Any]], source_type: str, filename: str, matrix: RateBucketMatrix) -> pd.DataFrame:
    """Canonical frame. Money stays Decimal-quantised; a genuinely absent
    field stays NULL — never 0 (§3 rule 2)."""
    import json as _json
    from decimal import Decimal, InvalidOperation

    from src.ingestion import GST_CANONICAL_FIELDS, GST_DATE_FIELDS, GST_UPPER_FIELDS

    structural = {"source_type", "source_file", "original_row", "total_tax"}
    fields = [f for f in GST_CANONICAL_FIELDS if f not in structural]
    records: list[dict[str, Any]] = []
    for row in rows:
        rec: dict[str, Any] = {}
        for f in fields:
            rec[f] = row.get(f)
        rec["source_type"] = source_type
        rec["source_file"] = filename
        rec["original_row"] = _json.dumps({str(k): (None if v is None else str(v)) for k, v in row.items()})
        records.append(rec)

    df = pd.DataFrame(records)
    for f in GST_UPPER_FIELDS:
        if f in df.columns:
            df[f] = df[f].astype(str).str.upper().str.strip().replace({"NAN": "", "NONE": "", "NONE": ""})
    for f in GST_DATE_FIELDS:
        if f in df.columns and df[f].notna().any():
            from src.ingestion import _normalize_dates_flexible

            df[f] = _normalize_dates_flexible(df[f], field=f, context=f"{source_type}/{filename}")

    money = ("taxable_value", "igst", "cgst", "sgst", "cess", "rounding_adjustment", "invoice_value")

    def to_decimal(v: Any) -> Any:
        if v is None or str(v).strip() in ("", "nan", "None"):
            return None  # absent stays absent — NEVER 0
        try:
            return Decimal(str(v).strip()).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    for f in money:
        if f in df.columns:
            df[f] = df[f].map(to_decimal)

    # total_tax is DERIVED, never mapped (§5 Part 2.2). Absent when every
    # component is absent — not zero.
    def total_tax(row) -> Any:
        parts = [row.get(x) for x in ("igst", "cgst", "sgst", "cess")]
        present = [p for p in parts if p is not None]
        if not present:
            return None
        return sum(present, Decimal("0.00"))

    if all(c in df.columns for c in ("igst", "cgst", "sgst", "cess")):
        df["total_tax"] = df.apply(total_tax, axis=1)

    identity = [c for c in ("gstin", "invoice_number") if c in df.columns]
    if identity:
        keep = df[identity].apply(
            lambda r: any(str(v).strip() not in ("", "nan", "None") for v in r), axis=1
        )
        df = df.loc[keep]

    order = [f for f in fields if f in df.columns] + ["total_tax", "source_type", "source_file", "original_row"]
    return df[[c for c in order if c in df.columns]].reset_index(drop=True)
