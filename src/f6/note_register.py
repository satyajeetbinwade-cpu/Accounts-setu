"""F6 — deterministic parse of a books-side Credit/Debit NOTE register.

WHY THIS EXISTS
---------------
A note is a SEPARATE document class from an invoice. Until now notes were
detected and excluded; this module gives them a real ingestible shape so they
can be reconciled by their own pass (see `src/matching/note_reference.py` for
the identity rules and `gst.notes` in matching_rules.yaml for the config).

It is the SAME approach `books_register.py` takes for the Purchase Register —
deterministic, zero model calls, falling through to the generic
fingerprint-and-learn path when the file is not this shape:

  * the sheet is selected by name (a real workbook carries the Purchase
    Register and the Note Register as two sheets of ONE file, so parsing the
    wrong one is a live risk — the Purchase Register sheet is declared
    `out_of_scope` so it can never be force-fit);
  * the rate-bucket matrix comes from `rate_buckets.detect_rate_buckets`
    (extended for this layout's header shape — see `_BUCKET_RE_LOOSE`);
  * a parse config is GENERATED from that matrix and handed to F6's one
    generic parser, so the arithmetic lives in one place;
  * §8 validation runs, with the two protections re-derived for a TAX-ONLY
    layout (see `run_validation`).

WHAT IS DIFFERENT FROM THE PURCHASE REGISTER
--------------------------------------------
1. The rate buckets carry **TAX only** — the header has no `Txbl.`/`Tax`
   token at all (`Unclaimed CGST @ 2.5%`). The taxable base is a separate set
   of ITEM-CATEGORY columns, detected STRUCTURALLY as the columns between the
   invoice-value column and the first rate bucket, never by client-specific
   names. The arithmetic proves the split: on the reference file
   ``2.5% x 2654.64 = 66.37`` and ``6% x 2185.38 = 131.12`` both equal the
   bucket values, so the buckets are tax and the item columns are the base.
2. `taxable_value` is therefore a plain SUM of the item-category columns —
   there is no CGST/SGST taxable mirror to de-duplicate (§5.4's
   `mirror_pairs()` correctly returns nothing here). The mirror assertion that
   DOES apply is on the TAX columns (`tax_mirror_pairs()`).
3. §5.4's implied-rate check is NOT derivable (no Tax/Txbl pair per rate), so
   it is deliberately skipped and replaced by the EXPLANATION check: every
   rate-bucket tax must be explained by some combination of item-category
   taxable values at that rate. Running the implied-rate check here would
   compare a bucket against nothing and check nothing.
4. The note's date is `Voucher Ref. Date`, NOT the booking `Date` — the note
   was issued a period before it was booked. Validation is therefore run with
   `period=None`: passing the run's period would flag EVERY row as
   out-of-period, which is the expected shape, not a defect.
5. `igst` is optional — an intra-state register carries no IGST column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from src.f6.books_register import FileMetadata
from src.f6.fingerprint import _normalize_label
from src.f6.rate_buckets import RateBucketMatrix, detect_rate_buckets

# The source type (upload slot) this parser serves.
NOTE_SOURCE_TYPES = {"credit_notes"}

# Sheet names, normalised. A real workbook has BOTH sheets; the note parser
# must take the second and leave the first alone.
NOTE_SHEET_KEY = "debit note register"
OUT_OF_SCOPE_SHEETS = ("purchase register",)

# Note-identity columns carried on the canonical frame, so the matcher never
# has to re-derive them.
NOTE_FIELDS = (
    "note_reference",
    "note_reference_raw",
    "note_kind",
    "note_reference_signal",
    "voucher_type_raw",
    "note_citation_count",
    # The note's OWN date, mirrored from `invoice_date` (which carries it on the
    # canonical frame) so the note required-field list can name it honestly.
    "note_date",
)

# §5 Part 2.2 tiers for the NOTE slot. `igst` is OPTIONAL: an intra-state
# register has no IGST column, and requiring it would flag every row of a
# perfectly good file. `taxable_value` and `invoice_value` are required even
# so, because the explanation and arithmetic checks depend on them.
NOTE_REQUIRED_FIELDS = ["gstin", "note_reference", "note_date", "taxable_value", "invoice_value"]
NOTE_OPTIONAL_FIELDS = ["igst", "cess", "party_name"]

# Structural alias sets. These must agree with `gst.notes.columns` in
# matching_rules.yaml — a test asserts the overlap, so the documented contract
# and the implementation cannot drift.
_ALIASES: dict[str, list[str]] = {
    # Order matters: `_match_alias` also accepts a SUBSTRING, so the more
    # specific field must be claimed first ("Voucher Ref. Date" is the note
    # date; the bare "Date" is the booking date).
    "gstin": ["gstin/uin", "gstin", "gstin of supplier", "supplier gstin", "party gstin"],
    "party_name": ["particulars", "buyer", "supplier name", "party name", "name & address of dealer"],
    "invoice_date": ["voucher ref. date", "note date", "credit note date"],
    "narration": ["narration", "remarks", "description"],
    "voucher_type": ["voucher type"],
    "voucher_no": ["voucher no.", "voucher no", "voucher number"],
    "voucher_ref": ["voucher ref. no.", "voucher ref no", "note no", "note number", "credit note no"],
    "booking_date": ["date", "voucher date"],
    "invoice_value": ["gross total", "gross amount", "total"],
    "rounding_adjustment": ["short & excess", "short and excess", "round off", "other amt."],
}

# Columns read for the note IDENTITY and for audit evidence, but never mapped
# to a canonical field. `voucher_no` matters specifically because it is the
# CLIENT's own booking number ("8/2025-26") — mapping it would turn a booking
# reference into the join key, which fails SILENTLY (every note unmatched).
_EVIDENCE_COLUMNS = ("voucher_ref", "narration", "voucher_type", "voucher_no", "booking_date")

_DATE_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
# Item-category subsets are enumerated for the explanation check. 10 columns
# (1024 subsets per bucket per row) is far beyond any real register; the cap
# only exists so a pathological header row cannot stall a run.
_MAX_SUBSET_COLUMNS = 10


def _norm_header(value: Any) -> str:
    """The SAME normalisation `detect_rate_buckets` applies to a header.

    Keying the source rows by this (rather than the verbatim cell) keeps the
    source-column checks and the matrix columns comparing identical strings.
    """
    return re.sub(r"\s+", " ", str(value)).strip()


def _match_alias(normalized_label: str, aliases: list[str]) -> bool:
    if normalized_label in aliases:
        return True
    return any(a in normalized_label for a in aliases)


def notes_config() -> dict[str, Any]:
    """The live `gst.notes` block, or {} (module defaults) if unavailable."""
    try:
        from src.config_loader import load_config

        return (load_config().get("gst") or {}).get("notes") or {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class NoteParseResult:
    """Everything the review screen, the hard gate and the note pass need."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    # Rows keyed by FLATTENED SOURCE label — the mirror / explanation checks
    # are assertions about SOURCE columns, so they run on the source view.
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
    item_category_columns: list[str] = field(default_factory=list)
    # Rows where a cited note could not be given a usable reference.
    identity_warnings: list[str] = field(default_factory=list)

    def validation_rows(self) -> list[dict[str, Any]]:
        """Canonical rows PLUS the raw source columns, merged per row, so the
        source-column checks see exactly the rows the parser kept."""
        merged: list[dict[str, Any]] = []
        for idx, row in enumerate(self.rows):
            combined = dict(row)
            if idx < len(self.source_rows):
                for key, value in self.source_rows[idx].items():
                    combined.setdefault(key, value)
            merged.append(combined)
        return merged

    def run_validation(self, *, required_fields: Optional[list[str]] = None):
        """§8's guardrails, re-derived for a TAX-ONLY note register.

        Deliberately different from the Purchase Register's call:
          * `period=None` — the note's own date is a period BEHIND the booking
            period, so an out-of-period flag on every row would be noise;
          * NO implied-rate check — the layout has no Tax/Txbl pair to check
            against, so it is replaced by the explanation check;
          * mirror is on the TAX columns (`tax_mirror_pairs()`), not the
            taxable ones.
        """
        from src.f6 import validation as f6validation

        required = required_fields or NOTE_REQUIRED_FIELDS
        outcome = f6validation.run_all_checks(
            rows=self.validation_rows(),
            row_count_read=self.row_count_read,
            row_count_parsed=self.row_count_parsed,
            exclusion_reasons=self.exclusion_reasons,
            required_fields=required,
            control_totals=self.control_totals,
            date_field="invoice_date",
            period=None,
            mirror_pairs=self.matrix.tax_mirror_pairs() if self.matrix else [],
            rate_buckets=None,
            # A note's identity is its GSTIN + REFERENCE, not its invoice
            # number (which does not exist here). The default invoice key would
            # call two different notes on the same date a duplicate.
            duplicate_key_fields=("gstin", "note_reference", "note_date"),
        )
        outcome.results.append(
            _check_bucket_explained(
                self.validation_rows(),
                self.item_category_columns,
                self.matrix.explanation_inputs() if self.matrix else [],
            )
        )
        for result in outcome.results:
            if result.result == "row_flag":
                for idx in result.affected_rows:
                    outcome.row_flags.setdefault(idx, []).append(result.check)
        return outcome


# ---------------------------------------------------------------------------
# The note-register replacement for the implied-rate check
# ---------------------------------------------------------------------------


def _check_bucket_explained(
    rows: list[dict[str, Any]],
    item_columns: list[str],
    buckets: list[dict[str, Any]],
    *,
    tolerance: float = 1.0,
):
    """Every rate-bucket TAX must be explained by the item-category base.

    §5.4's `check_implied_rate` needs a Tax AND a Txbl column at the same
    rate; this layout has only the Tax half, so that check would silently
    skip every bucket. This is the substitute: for each rate bucket, the
    recorded tax must equal `rate x (sum of SOME non-empty subset of the
    item-category columns)` within ₹1.

    A SUBSET, not a single column: two categories can share one rate, in
    which case the bucket covers their sum. The proof it provides is the
    same as the implied-rate check — that the rate printed in the header is
    real and the base is composed of the item columns — plus it catches a
    bucket filed under the wrong head.

    Escalates to a run-level hard stop above a 20% failure rate, matching
    every other systematic-error check in §8.
    """
    from src.f6.validation import (
        CheckResult,
        _TAX_ARITHMETIC_ESCALATION_RATE,
    )

    if not item_columns or not buckets:
        return CheckResult(
            "rate_bucket_explained", "pass",
            "No item-category columns or rate buckets to check — check skipped.", [],
        )
    if len(item_columns) > _MAX_SUBSET_COLUMNS:
        return CheckResult(
            "rate_bucket_explained", "pass",
            f"{len(item_columns)} item-category columns exceeds the subset cap "
            f"({_MAX_SUBSET_COLUMNS}) — check skipped rather than guessed.", [],
        )

    values: list[list[float]] = []
    for row in rows:
        row_values: list[float] = []
        for column in item_columns:
            try:
                row_values.append(float(row.get(column) or 0))
            except (TypeError, ValueError):
                row_values.append(0.0)
        values.append(row_values)

    failed: list[int] = []
    for idx, row_values in enumerate(values):
        row = rows[idx]
        for bucket in buckets:
            try:
                tax = float(row.get(bucket["tax_column"]) or 0)
            except (TypeError, ValueError):
                continue
            if tax == 0:
                continue  # absent bucket — nothing to explain
            expected = float(bucket["rate"]) / 100.0
            if not _any_subset_explains(row_values, expected, tax, tolerance):
                failed.append(idx)
                break

    checkable = [i for i, row in enumerate(rows) if row.get("invoice_value") not in (None, "")]
    if not failed:
        return CheckResult(
            "rate_bucket_explained", "pass",
            f"Every rate-bucket tax is explained by the item-category base at its own rate "
            f"({len(buckets)} bucket(s), {len(item_columns)} item column(s) checked).", [],
        )
    rate = len(failed) / len(checkable) if checkable else 0.0
    if rate > _TAX_ARITHMETIC_ESCALATION_RATE:
        return CheckResult(
            "rate_bucket_explained", "hard_stop",
            f"{len(failed)}/{len(checkable)} rows ({rate:.0%}) have a rate-bucket tax that no "
            f"combination of the item-category columns explains at that rate — exceeds the "
            f"{_TAX_ARITHMETIC_ESCALATION_RATE:.0%} escalation threshold. Typically a bucket filed "
            "under the wrong tax head, or a taxable column mistaken for a tax column. Run-level hard stop.",
            failed,
        )
    return CheckResult(
        "rate_bucket_explained", "row_flag",
        f"{len(failed)}/{len(checkable)} rows ({rate:.0%}) have an unexplained rate-bucket tax — "
        "flagged, run continues.",
        failed,
    )


def _any_subset_explains(values: list[float], expected_rate: float, tax: float, tolerance: float) -> bool:
    """Whether any non-empty subset of `values` satisfies |tax − rate x Σ| ≤ tol."""
    n = len(values)
    for mask in range(1, 1 << n):
        total = 0.0
        for bit in range(n):
            if mask & (1 << bit):
                total += values[bit]
        if total and abs(tax - expected_rate * total) <= tolerance:
            return True
    return False


# ---------------------------------------------------------------------------
# Structural read
# ---------------------------------------------------------------------------


def _sheet_names(path: Any) -> list[str]:
    try:
        return list(pd.ExcelFile(path).sheet_names)
    except Exception:  # noqa: BLE001
        return []


def _pick_note_sheet(path: Any, *, is_excel: bool) -> Optional[str]:
    """The sheet holding the note register, or None.

    By NAME first (the reference workbook names it "Debit Note Register"), so
    the Purchase Register sheet sitting beside it is never parsed as notes.
    """
    if not is_excel:
        return NOTE_SHEET_KEY
    for name in _sheet_names(path):
        if _normalize_label(name) == _normalize_label(NOTE_SHEET_KEY):
            return name
    for name in _sheet_names(path):
        normalised = _normalize_label(name)
        if "note" in normalised and "register" in normalised:
            return name
    return None


def _read_sheet(path: Any, sheet: str) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet, header=None, dtype=str, keep_default_na=False)


def _find_header_row(raw: pd.DataFrame) -> int:
    """The row carrying the column labels: the one that names BOTH a GSTIN-ish
    column and a narration column. Positional scoring only, so it cannot drift.
    """
    best_idx, best_score = 0, 0
    for i in range(min(40, len(raw))):
        labels = [_normalize_label(v) for v in raw.iloc[i]]
        score = 0
        if any("gstin" in x for x in labels):
            score += 2
        if any("narration" in x for x in labels):
            score += 2
        if any("gross total" in x or "gross amount" in x for x in labels):
            score += 1
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx


def _build_parse_config(
    headers: list[str], matrix: RateBucketMatrix, header_row: int, date_format: str,
    item_columns: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Generate the parse config, plus the column-disposition report and the
    resolved column names the caller needs for the note identity."""
    norm = {_normalize_label(h): h for h in headers}
    bucket_cols = {b.column for b in matrix.buckets}
    claimed: set[str] = set()
    resolved: dict[str, str] = {}

    rules: list[dict[str, Any]] = []
    for field_name, aliases in _ALIASES.items():
        hit = next(
            (norm[n] for n in norm
             if n and norm[n] not in claimed and norm[n] not in bucket_cols
             and _match_alias(n, aliases)),
            None,
        )
        if hit is None:
            continue
        claimed.add(hit)
        resolved[field_name] = hit
        if field_name in _EVIDENCE_COLUMNS:
            continue  # read for the note identity / audit, never mapped to a field
        if field_name == "invoice_date":
            rules.append({
                "canonical_field": field_name, "kind": "derived",
                "source_columns": [hit], "transform": f"parse_date:{date_format}",
            })
        else:
            rules.append({"canonical_field": field_name, "kind": "direct", "source_columns": [hit]})

    rules.extend(matrix.aggregate_rules())

    if item_columns:
        rules.append({
            "canonical_field": "taxable_value",
            "kind": "aggregate",
            "source_columns": list(item_columns),
            # A plain sum: the item-category columns are the taxable base
            # itself, and there is no CGST/SGST mirror restating it.
            "transform": None,
        })

    dispositions: list[dict[str, Any]] = []
    # Built from the RULES, so a rate bucket resolves to the canonical field it
    # is summed into (`cgst`) rather than to a description of itself.
    field_of: dict[str, str] = {}
    for rule in rules:
        for column in rule.get("source_columns") or []:
            field_of[column] = rule["canonical_field"]

    for header in headers:
        normalised = _normalize_label(header)
        if not normalised:
            continue
        if header in field_of:
            detail = "Mapped directly to a Setu field."
            if header in item_columns:
                detail = ("Item-category taxable column — summed into taxable_value. Detected "
                          "structurally as the columns between the invoice-value column and the "
                          "first rate bucket, never by name.")
            elif header in bucket_cols:
                bucket = next(b for b in matrix.buckets if b.column == header)
                prefix = f" (prefixed {bucket.prefix!r})" if bucket.prefix else ""
                detail = (f"Rate bucket {bucket.head} @ {bucket.rate:g}% ({bucket.kind}){prefix} — "
                          f"summed into {bucket.head.lower()}.")
            dispositions.append({
                "column": header, "disposition": "used-in", "detail": detail,
                "canonical_field": field_of.get(header),
            })
            continue
        if header in resolved.values():
            dispositions.append({
                "column": header, "disposition": "validation-signal",
                "detail": "Read for the note identity / audit only — never mapped to a field.",
                "canonical_field": None,
            })
            continue
        dispositions.append({
            "column": header, "disposition": "ignored",
            "detail": "Not an identity, item-category, rate-bucket or signal column.",
            "canonical_field": None,
        })

    control_columns = [b.column for b in matrix.buckets] + list(item_columns)
    if "invoice_value" in resolved:
        control_columns.insert(0, resolved["invoice_value"])

    config = {
        "sheets": {
            NOTE_SHEET_KEY: {
                "role": "line_items",
                "header": {"type": "single", "row": header_row},
                "data_start_row": header_row + 1,
                "row_exclusion": {
                    "trailing_blank": True,
                    # A total row is labelled in the FIRST column (the date
                    # column carries no text), so the marker keys off it.
                    "total_row_marker": {"column": headers[0], "contains": "total"},
                },
                "field_rules": rules,
                "control_total": {"source": "own_total_row", "columns": control_columns},
            },
            # Declared so the Purchase Register sheet sitting beside the note
            # sheet is RECOGNISED and reported, never structurally force-fit.
            **{name: {"role": "out_of_scope"} for name in OUT_OF_SCOPE_SHEETS},
        },
        "date_format": date_format,
    }
    return config, dispositions, resolved


def _detect_date_format(raw: pd.DataFrame, header_row: int, column: Optional[str]) -> tuple[str, str]:
    """DD-MM-YYYY vs MM-DD-YYYY vs ISO from the DATA, with the evidence stated.

    ISO is detected explicitly rather than left to the parser's day-first
    fallback, so the declared format is the TRUE one — a fallback that silently
    rescues every date would hide a genuinely wrong declaration.
    """
    if column is None:
        return "%d-%m-%Y", "No note-date column detected — assumed DD-MM-YYYY."
    try:
        index = [str(v) for v in raw.iloc[header_row]].index(column)
    except ValueError:
        return "%d-%m-%Y", "Note-date column not locatable — assumed DD-MM-YYYY."
    iso = day_first = month_first = 0
    for i in range(header_row + 1, min(header_row + 200, len(raw))):
        value = str(raw.iat[i, index]).strip()
        if _ISO_DATE_RE.match(value):
            iso += 1
            continue
        match = _DATE_RE.match(value)
        if not match:
            continue
        if int(match.group(1)) > 12:
            day_first += 1
        if int(match.group(2)) > 12:
            month_first += 1
    if iso and not day_first and not month_first:
        return "%Y-%m-%d", f"{iso} ISO (YYYY-MM-DD) date(s) found."
    if day_first and not month_first:
        return "%d-%m-%Y", f"A day value above 12 exists ({day_first + month_first} value(s) inspected)."
    if month_first and not day_first:
        return "%m-%d-%Y", f"A month value above 12 exists ({day_first + month_first} value(s) inspected)."
    return "%d-%m-%Y", (
        f"No unambiguous date ({iso + day_first + month_first} value(s) inspected) — "
        "DD-MM-YYYY assumed (Indian convention)."
    )


def _read_source_rows(raw: pd.DataFrame, header_row: int) -> tuple[list[dict[str, Any]], int]:
    """The kept data rows keyed by NORMALISED source label, plus how many rows
    were skipped as blank or as the file's own total row."""
    headers = [_norm_header(v) for v in raw.iloc[header_row]]
    out: list[dict[str, Any]] = []
    skipped = 0
    for i in range(header_row + 1, len(raw)):
        values = [raw.iat[i, c] if c < len(raw.columns) else None for c in range(len(headers))]
        texts = [str(v).strip() for v in values]
        if all(t in ("", "nan", "None") for t in texts):
            skipped += 1
            continue
        if headers and "total" in texts[0].lower():
            skipped += 1
            continue
        out.append({headers[c]: values[c] for c in range(len(headers))})
    return out, skipped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_note_register(
    path: Any, source_type: str, filename: str, *, config: Optional[dict[str, Any]] = None,
) -> Optional[NoteParseResult]:
    """Deterministically parse a books-side Credit/Debit Note Register.

    Returns None when the file is not this shape (no note sheet, no rate
    buckets, no parsable rows) so the caller falls through to the generic
    model path rather than force-fitting.
    """
    if source_type not in NOTE_SOURCE_TYPES:
        return None
    is_excel = str(path).lower().endswith((".xlsx", ".xls"))

    sheet = _pick_note_sheet(path, is_excel=is_excel)
    if sheet is None:
        return None

    try:
        if is_excel:
            raw = _read_sheet(path, sheet)
        else:
            raw = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
    except Exception:  # noqa: BLE001
        return None
    if raw.empty:
        return None

    header_row = _find_header_row(raw)
    headers = [_norm_header(v) for v in raw.iloc[header_row]]
    matrix = detect_rate_buckets(headers)
    if not matrix.detected:
        return None

    notes_cfg = config if config is not None else notes_config()
    value_block = (notes_cfg or {}).get("value_block") or {}

    # Resolve the columns the item-category rule needs, using the SAME
    # normalisation the matrix used.
    norm = {_normalize_label(h): h for h in headers if h}
    bucket_cols = {b.column for b in matrix.buckets}

    def first_alias(aliases: list[str]) -> Optional[str]:
        return next(
            (norm[n] for n in norm
             if norm[n] not in bucket_cols and _match_alias(n, aliases)),
            None,
        )

    invoice_value_col = first_alias(value_block.get("invoice_value_column_aliases") or _ALIASES["invoice_value"])
    item_columns = _item_category_columns(headers, invoice_value_col, bucket_cols)
    if not item_columns:
        # A note register with no taxable base at all is not this shape.
        return None

    date_format, date_evidence = _detect_date_format(raw, header_row, first_alias(_ALIASES["invoice_date"]))
    parse_config, dispositions, resolved = _build_parse_config(
        headers, matrix, header_row, date_format, item_columns
    )

    from src.f6.parser import parse_file

    try:
        parsed = parse_file(path if is_excel else path, parse_config, is_excel=is_excel)
    except Exception:  # noqa: BLE001
        return None
    if is_excel:
        # parse_file takes bytes-or-path; a sheet-specific read is not needed
        # here because the config keys the sheet by NAME.
        pass
    rows = parsed.all_rows
    if not rows:
        return None

    source_rows, skipped = _read_source_rows(raw, header_row)

    warnings: list[str] = []
    for sheet_result in parsed.sheet_results:
        for item in sheet_result.exclusion_reasons:
            warnings.append(f"{item['count']} row(s) excluded — {item['reason'].replace('_', ' ')}.")

    from src.matching.note_reference import classify_note

    identity_warnings: list[str] = []
    for idx, row in enumerate(rows):
        source = source_rows[idx] if idx < len(source_rows) else {}
        identity = classify_note(
            reference_column=source.get(resolved.get("voucher_ref", "")),
            narration=source.get(resolved.get("narration", "")),
            voucher_type=source.get(resolved.get("voucher_type", "")),
            invoice_value=row.get("invoice_value"),
            taxable_value=row.get("taxable_value"),
            config=notes_cfg,
        )
        row["note_reference"] = identity.reference
        row["note_reference_raw"] = identity.reference_raw
        row["note_kind"] = identity.kind
        row["note_reference_signal"] = identity.signal
        row["voucher_type_raw"] = identity.voucher_type_raw
        row["note_citation_count"] = identity.citation_count
        # The note date is the REGISTER's Voucher Ref. Date (mapped above to
        # invoice_date) — never the booking Date, which is a period later.
        row["note_date"] = row.get("invoice_date")
        for warning in identity.warnings:
            text = f"Row {idx + 1}: {warning}"
            warnings.append(text)
            identity_warnings.append(text)

    meta = FileMetadata(header_row=header_row)
    meta.date_format = date_format
    meta.date_format_evidence = date_evidence
    if headers:
        meta.entity_name = _first_metadata_cell(raw, header_row)

    result = NoteParseResult(
        rows=rows,
        source_rows=source_rows,
        rules=[r for r in parse_config["sheets"][NOTE_SHEET_KEY]["field_rules"]],
        matrix=matrix,
        metadata=meta,
        warnings=warnings,
        row_count_read=parsed.row_count_read + skipped,
        row_count_parsed=parsed.row_count_parsed,
        exclusion_reasons=list(parsed.exclusion_reasons),
        column_dispositions=dispositions,
        item_category_columns=item_columns,
        identity_warnings=identity_warnings,
    )

    stated = None
    for sheet_result in parsed.sheet_results:
        for column, value in sheet_result.control_total_candidates.items():
            if invoice_value_col and _normalize_label(column) == _normalize_label(invoice_value_col):
                stated = value
    if stated is not None:
        parsed_sum = sum(float(r.get("invoice_value") or 0) for r in rows)
        result.control_totals.append((parsed_sum, stated, "file's own Total row"))

    from src.f6.books_register import _to_frame

    result.frame = _to_frame(
        rows, source_type, filename, matrix,
        extra_fields=list(NOTE_FIELDS),
        identity_fields=("gstin", "note_reference"),
    )
    return result


def _first_metadata_cell(raw: pd.DataFrame, header_row: int) -> Optional[str]:
    for i in range(header_row):
        for value in raw.iloc[i]:
            text = str(value).strip()
            if text and text.lower() != "nan":
                return text
    return None


def _item_category_columns(
    headers: list[str], invoice_value_col: Optional[str], bucket_cols: set[str],
) -> list[str]:
    """The taxable-base columns: everything strictly BETWEEN the invoice-value
    column and the FIRST rate-bucket column.

    Structural on purpose — the reference file's categories are garment names
    ("Pur- Lehanga" / "Pur - Sarees", with inconsistent spacing), so matching
    them by name would break the next client.
    """
    if invoice_value_col is None or not bucket_cols:
        return []
    if invoice_value_col not in headers:
        return []
    bucket_positions = [headers.index(c) for c in bucket_cols if c in headers]
    if not bucket_positions:
        return []
    start = headers.index(invoice_value_col)
    end = min(bucket_positions)
    out: list[str] = []
    for header in headers[start + 1:end]:
        if not _normalize_label(header) or header in bucket_cols:
            continue
        out.append(header)
    return out
