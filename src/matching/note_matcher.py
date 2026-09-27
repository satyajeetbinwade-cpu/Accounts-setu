"""GST NOTE-to-NOTE matching — the books note register vs the portal's notes.

REUSE-FIRST: this is NOT a separate engine. It imports and reuses
`gst_matcher`'s tolerance, scoring, banding, ambiguity-resolution,
pair-comparison and result-construction helpers directly (the same approach
`other_matcher.py` takes for 2C), so confidence semantics and the result shape
are identical to the invoice pass.

Structural separation is the whole point: notes are a SEPARATE document class.
This module takes note frames and returns note results — it never touches the
invoice pool, so a note can never move an invoice count, Period ITC or
Eligible ITC. It writes to its own table (see the note persistence slice) and
its own report section.

MATCH KEY, in the order the passes run:

  1. GSTIN + normalised note reference            (baseline `exact`)
  2. GSTIN + reference variant — one normalised reference CONTAINS the other
     (a prefix or an interior token the normaliser cannot reconcile)
                                                 (baseline `reference_variant`)
  3. GSTIN + date window + value tolerance        (baseline `gstin_date_value`)

then the SAME three passes against the ADJACENT period's portal file at the
cross-period penalty, because a note's own date lags the booking by a period —
a March note lives in the March GSTR-2B, not in the April run's file.

Sign convention (Q9): both sides are compared on ABSOLUTE values. A credit
note is ITC-negative on both sides, so comparing absolutes keeps a
double-negative from fabricating an `Amount Difference`.

Kind agreement (Q6): a CN never silently matches a DN. A reference that
matches with differing kinds is still PAIRED, so the disagreement is REPORTED
as `Document Type Mismatch` rather than left as two orphans. Set
`require_kind_agreement: false` to ignore the kind entirely instead (a CN may
then match a DN when the reference and values agree).
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

# Reuse the shared engine's helpers directly — do not re-implement.
from src.matching.gst_matcher import (
    _compare_pair,
    _date_diff_days,
    _date_within_window,
    _fmt,
    _gstin_structurally_invalid,
    _make_result,
    _proximity,
    _rank,
    _resolve_ambiguity,
    _row_json,
    _score,
    _validate_gstin_structure,
    _within_tolerance,
)
from src.matching.note_reference import (
    CREDIT_NOTE,
    DEBIT_NOTE,
    normalise_note_reference,
)

# Note-level classification buckets — the SAME four the invoice pass uses, as
# their own note-level set.
NOTE_CLASSIFICATIONS = ("Matched", "Amount Difference", "Not in Books", "Not in Portal")

# Private frame columns excluded from the record JSON.
_NOTE_EXCLUDE = ["_nid", "_doc_type"]

# Portal-side document-type values that mean a note, mapped onto the two
# canonical kinds. `document_type_map` in matching_rules.yaml already
# normalises most of these; this is the belt-and-braces set for a re-parsed
# CDNR sheet read directly.
_PORTAL_KIND_ALIASES: dict[str, str] = {
    "credit note": CREDIT_NOTE, "credit notes": CREDIT_NOTE,
    "cdn": CREDIT_NOTE, "cr note": CREDIT_NOTE, "c note": CREDIT_NOTE,
    "debit note": DEBIT_NOTE, "debit notes": DEBIT_NOTE,
    "dn": DEBIT_NOTE, "dr note": DEBIT_NOTE, "d note": DEBIT_NOTE,
}

_REFERENCE_FIELDS = ("note_reference", "invoice_number", "reference", "note_number")
_KIND_FIELDS = ("note_kind", "document_type", "_doc_type")
_DATE_FIELDS = ("note_date", "invoice_date", "date")
_PARTY_FIELDS = ("party_name", "supplier_name", "particulars", "buyer")
_VALUE_FIELDS = ("invoice_value", "note_value", "amount")
_TAXABLE_FIELDS = ("taxable_value", "taxable")
_TAX_FIELDS = ("igst", "cgst", "sgst", "cess")


# ---------------------------------------------------------------------------
# Frame preparation
# ---------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return None if text.lower() in ("", "nan", "none", "nat", "null") else value


def _first(row: Any, fields: tuple[str, ...]) -> Any:
    for field in fields:
        value = _clean(row.get(field))
        if value is not None:
            return value
    return None


def _as_float(value: Any) -> Optional[float]:
    value = _clean(value)
    if value is None:
        return None
    try:
        return abs(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _kind_of(row: Any) -> str:
    """The note's kind, from an explicit column or from the sign convention."""
    raw = _clean(_first(row, _KIND_FIELDS))
    if raw is not None:
        text = str(raw).strip().lower()
        if text in _PORTAL_KIND_ALIASES:
            return _PORTAL_KIND_ALIASES[text]
        if "credit" in text:
            return CREDIT_NOTE
        if "debit" in text:
            return DEBIT_NOTE
    # No usable type column: the sign convention is the same rule the invoice
    # engine's `_map_document_type` uses.
    for field in _VALUE_FIELDS + _TAXABLE_FIELDS:
        value = _clean(row.get(field))
        if value is None:
            continue
        try:
            if float(str(value).replace(",", "")) < 0:
                return CREDIT_NOTE
        except (TypeError, ValueError):
            continue
    return CREDIT_NOTE


def _reference_of(row: Any, config: dict) -> str:
    raw = _clean(_first(row, _REFERENCE_FIELDS))
    if raw is None:
        return ""
    spec = ((config.get("notes") or {}).get("normalise")) or {}
    return normalise_note_reference(raw, spec)


def as_note_frame(frame: Optional[pd.DataFrame], config: dict) -> pd.DataFrame:
    """Canonicalise ANY note frame into the shape the passes compare.

    Accepts either the books note register's output (which already carries
    `note_reference` / `note_kind` / `note_date`) or a portal note frame (which
    carries `invoice_number` / `document_type` / `invoice_date`), so the two
    sides are made comparable in ONE place.

    Money is stored as its ABSOLUTE value: both sides of a credit note are
    ITC-negative, so comparing absolutes is what keeps a double-negative from
    fabricating an `Amount Difference`.
    """
    columns = ["_nid", "gstin", "note_reference", "note_reference_raw", "note_kind", "note_date",
               "party_name", "taxable_value", "invoice_value", "total_tax",
               "igst", "cgst", "sgst", "cess", "source_type", "source_file"]
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=columns)

    records: list[dict[str, Any]] = []
    for index, row in frame.reset_index(drop=True).iterrows():
        tax = {field: _as_float(row.get(field)) for field in _TAX_FIELDS}
        total = sum(value for value in tax.values() if value is not None) if any(
            value is not None for value in tax.values()
        ) else None
        records.append({
            "_nid": index,
            "gstin": str(_clean(row.get("gstin")) or "").strip().upper(),
            "note_reference": _reference_of(row, config),
            # The verbatim cited string, kept as evidence. The portal side has
            # no separate raw column, so its note number IS the raw value.
            "note_reference_raw": _clean(_first(row, ("note_reference_raw",) + _REFERENCE_FIELDS)),
            "note_kind": _kind_of(row),
            "note_date": _clean(_first(row, _DATE_FIELDS)),
            "party_name": str(_clean(_first(row, _PARTY_FIELDS)) or ""),
            "taxable_value": _as_float(_first(row, _TAXABLE_FIELDS)),
            "invoice_value": _as_float(_first(row, _VALUE_FIELDS)),
            "total_tax": total,
            "igst": tax["igst"], "cgst": tax["cgst"],
            "sgst": tax["sgst"], "cess": tax["cess"],
            "source_type": _clean(row.get("source_type")),
            "source_file": _clean(row.get("source_file")),
        })

    prepared = pd.DataFrame(records)
    # `_doc_type` is what `_compare_pair` reads to raise a Document Type
    # Mismatch, so the note kind must reach it.
    prepared["_doc_type"] = prepared["note_kind"]
    return prepared


def _note_config(config: dict) -> dict:
    """The `gst` block with the note overrides merged in.

    The reused helpers read `amount_tolerance`, `rounding_tolerance`,
    `date_tolerance_days`, `confidence_thresholds`, `confidence_baselines` and
    `cross_period_penalty` from the top level, so the note overrides are lifted
    onto those keys rather than living in a parallel namespace the helpers
    would never read.
    """
    gst = dict(config or {})
    match = ((gst.get("notes") or {}).get("match")) or {}
    baselines = dict(gst.get("confidence_baselines") or {})
    note_baselines = dict(match.get("confidence_baselines") or {})
    baselines.update(note_baselines)

    gst["amount_tolerance"] = match.get("amount_tolerance") or gst.get("amount_tolerance") or {
        "absolute": 10.0, "percent": 1.0,
    }
    gst["rounding_tolerance"] = match.get("rounding_tolerance", gst.get("rounding_tolerance", 2.0))
    gst["date_tolerance_days"] = match.get("date_window_days", gst.get("date_tolerance_days", 5))
    gst["confidence_baselines"] = baselines
    if "cross_period_penalty" in note_baselines:
        gst["cross_period_penalty"] = note_baselines["cross_period_penalty"]
    gst["_notes_match"] = match
    return gst


# ---------------------------------------------------------------------------
# Passes
# ---------------------------------------------------------------------------


def _invalid_gstin(row: pd.Series) -> bool:
    return _gstin_structurally_invalid(str(row.get("gstin") or ""))


def _value_proximity(books: pd.Series, portal: pd.Series, config: dict) -> float:
    """Closeness of the two NOTE VALUES — used for ranking and the ambiguity
    margin only, never reported as confidence."""
    left = books.get("invoice_value")
    right = portal.get("invoice_value")
    if left is None or right is None:
        return 1.0
    tolerance = float(config["amount_tolerance"]["absolute"]) or 1.0
    return _proximity(abs(float(left) - float(right)), tolerance)


def _same_reference(books: pd.Series, portal: pd.Series) -> bool:
    left, right = books.get("note_reference"), portal.get("note_reference")
    return bool(left) and bool(right) and left == right


def _variant_reference(books: pd.Series, portal: pd.Series) -> Optional[str]:
    """How two references relate when they are not exactly equal, else None.

    Containment only: a shorter normalised reference inside a longer one is a
    prefix or an interior token the shared normaliser could not reconcile.
    Anything looser than that is not a reference match at all, and falls to the
    date+value pass where the evidence is stated as such.
    """
    left, right = books.get("note_reference"), portal.get("note_reference")
    if not left or not right or left == right:
        return None
    if left in right:
        return f"'{left}' is contained in '{right}'"
    if right in left:
        return f"'{right}' is contained in '{left}'"
    return None


def _key_gstin(row: pd.Series) -> str:
    return str(row.get("gstin") or "").strip().upper()


def _gstin_pair_ok(books: pd.Series, portal: pd.Series) -> tuple[bool, bool]:
    """``(is_usable_pair, a_gstin_is_missing)``.

    * both present -> must be EQUAL, or the pair is a different supplier.
    * exactly one present -> admissible, but with the evidence STRENGTHENED
      (the caller requires the amount to agree) and the score carrying a
      `missing_gstin_penalty`. Refusing it made a note permanently unmatchable
      whenever one export omitted the GSTIN — reporting a false "Not in Portal"
      AND a false "Not in Books" for what is one document. The present side is
      still a real supplier anchor.
    * NEITHER present -> NOT admissible. With no supplier on either side, a
      short reference ("50", "CD-12") does not identify a document, and this
      engine's stated preference is to flag a genuine pair as an exception
      rather than pass a mismatch as clean.
    """
    left, right = _key_gstin(books), _key_gstin(portal)
    if left and right:
        return left == right, False
    if left or right:
        return True, True
    return False, False


def _date_guard_ok(books: pd.Series, portal: pd.Series, config: dict) -> bool:
    """Whether two dates are close enough to be the SAME document.

    A reference is not proof on its own: the shared normaliser strips a
    trailing FY/period token, so a supplier who reuses a base number
    ("SRI-312/25-26" then "SRI-312/26-27") produces ONE key for two different
    notes — and nothing else in the pass would notice. This guard rejects a
    match across MORE THAN HALF A YEAR, i.e. a different document, without
    policing a genuinely late booking. A missing date cannot disqualify a
    reference match.
    """
    days = float((config.get("_notes_match") or {}).get("reference_date_guard_days", 180))
    difference = _date_diff_days(books.get("note_date"), portal.get("note_date"))
    return difference is None or difference <= days


def _pair_result(
    books: pd.Series, portal: pd.Series, config: dict, *,
    pass_name: str, baseline: float, forced_low: bool = False,
    score_override: Optional[float] = None, extra_reason: str = "",
    cross_period: bool = False, require_kind: bool = True,
) -> dict[str, Any]:
    """One paired note comparison, shaped like an invoice result.

    Classification and difference_type come from the SHARED `_compare_pair`, so
    a note is described with exactly the same vocabulary as an invoice.

    When `require_kind` is False the kind is taken OUT of the comparison (both
    sides are presented as the same document type), so a CN may match a DN.
    When it is True the kinds are left as they are, and a disagreement surfaces
    as `Document Type Mismatch` from `_compare_pair` — reported, never silent.
    """
    if not require_kind:
        books = books.copy()
        portal = portal.copy()
        books["_doc_type"] = "Note"
        portal["_doc_type"] = "Note"

    classification, difference_type, detail = _compare_pair(books, portal, config)

    if score_override is not None:
        score = max(0.0, min(100.0, round(float(score_override), 2)))
    else:
        score = _score(
            baseline, config,
            cross_period=cross_period, invalid_gstin=_invalid_gstin(books),
        )

    gstin = _key_gstin(books)
    gstin_valid, gstin_note = _validate_gstin_structure(gstin)
    reference = books.get("note_reference") or portal.get("note_reference") or "(no reference)"
    kind = books.get("note_kind") or portal.get("note_kind")

    parts = [f"{pass_name}: GSTIN {gstin}, note {reference} ({kind}); {detail}"]
    if extra_reason:
        parts.append(extra_reason)
    if cross_period:
        parts.append(" Matched against the ADJACENT period's portal file — a note is often issued a period before it is booked, so this is a timing difference, not a value dispute.")
    if not gstin_valid:
        parts.append(f" Note: {gstin_note}.")

    result = _make_result(
        classification, score, config,
        books_record=_row_json(books, exclude=_NOTE_EXCLUDE),
        portal_record=_row_json(portal, exclude=_NOTE_EXCLUDE),
        match_reason="".join(parts),
        difference_type=difference_type,
    )
    if forced_low:
        # `_make_result` bands from the score alone; an ambiguous candidate must
        # read as Low however high its baseline — the invoice engine applies the
        # same override, and a bare 90 would read as a confident match.
        result["confidence_band"] = "Low"
    return _with_note_fields(result, books, portal, config)


def _with_note_fields(
    result: dict[str, Any], books: Optional[pd.Series], portal: Optional[pd.Series], config: dict,
) -> dict[str, Any]:
    """Attach the note-level figures the note report needs.

    Kept SEPARATE from the invoice result shape: `note_tax` / `note_value` are
    the note-level ITC-impact inputs and must never be folded into the invoice
    Period/Eligible ITC figures (which read `match_results`, not this set).
    """
    kind = None
    reference = None
    reference_raw = None
    date = None
    tax = None
    value = None
    sides: list[str] = []

    if books is not None:
        sides.append("books")
        kind = books.get("note_kind") or kind
        reference = books.get("note_reference") or reference
        reference_raw = books.get("note_reference_raw") or reference_raw
        date = books.get("note_date") or date
        tax = books.get("total_tax") if books.get("total_tax") is not None else tax
        value = books.get("invoice_value") if books.get("invoice_value") is not None else value
    if portal is not None:
        sides.append("portal")
        kind = kind or portal.get("note_kind")
        reference = reference or portal.get("note_reference")
        reference_raw = reference_raw or portal.get("note_reference_raw")
        date = date or portal.get("note_date")
        tax = tax if tax is not None else portal.get("total_tax")
        value = value if value is not None else portal.get("invoice_value")

    result["note_reference"] = reference
    result["note_reference_display"] = reference or "(no reference extracted)"
    result["note_reference_raw"] = reference_raw
    result["note_kind"] = kind
    result["note_date"] = date
    result["note_tax"] = round(float(tax), 2) if tax is not None else 0.0
    result["note_value"] = round(float(value), 2) if value is not None else 0.0
    result["note_side"] = "both" if len(sides) == 2 else (sides[0] if sides else "")
    return result


def _match_against_pool(
    books: pd.DataFrame, pool: pd.DataFrame, config: dict,
    books_used: set[int], pool_used: set[int], results: list[dict[str, Any]],
    near_misses: dict[int, list[str]], *, cross_period: bool,
) -> None:
    """Passes 1-3 of one books-vs-pool pairing.

    `near_misses` collects, per books row, the references that DID match but
    were rejected by the same-document date guard. They are reported on the
    residual row rather than discarded — "no portal note matched" would be a
    misleading reason when one matched and was rejected for being a different
    year's document.
    """
    baselines = config["confidence_baselines"]
    require_kind = bool((config.get("_notes_match") or {}).get("require_kind_agreement", True))
    missing_penalty = float(baselines.get("missing_gstin_penalty", -8))
    label = ("Cross-period " if cross_period else "").strip()

    def candidates_for(
        book: pd.Series, mode: str,
    ) -> tuple[list[tuple[float, float, pd.Series, str]], list[str]]:
        found: list[tuple[float, float, pd.Series, str]] = []
        rejected: list[str] = []
        for _, portal in pool.iterrows():
            if portal["_nid"] in pool_used:
                continue
            usable, gstin_missing = _gstin_pair_ok(book, portal)
            if not usable:
                continue

            if mode == "exact":
                if not _same_reference(book, portal):
                    continue
                fragment = ""
            elif mode == "variant":
                relation = _variant_reference(book, portal)
                if relation is None:
                    continue
                fragment = f" Reference variant: {relation}."
            else:  # "fallback" — GSTIN + date window + value tolerance
                if gstin_missing:
                    # With no GSTIN at all there is no identity to fall back ON,
                    # so the date+value tier requires a real supplier key.
                    continue
                if not _date_within_window(book.get("note_date"), portal.get("note_date"), config):
                    continue
                if book.get("invoice_value") is None or portal.get("invoice_value") is None:
                    continue
                if not _within_tolerance(book["invoice_value"], portal["invoice_value"], config):
                    continue
                fragment = " No reference agreed, so this pair rests on GSTIN + date window + value."

            if mode in ("exact", "variant") and not _date_guard_ok(book, portal, config):
                rejected.append(
                    f"Note {book.get('note_reference')} also appears on a portal note dated "
                    f"{portal.get('note_date')} against this row's {book.get('note_date')} — "
                    "more than half a year apart, so it is treated as a DIFFERENT document "
                    "(a supplier can reuse a note number in a later year)."
                )
                continue

            if gstin_missing:
                # Strengthen, do not weaken: the reference alone cannot key the
                # pair when a GSTIN is absent, so the amount must also agree.
                if book.get("invoice_value") is None or portal.get("invoice_value") is None:
                    continue
                if not _within_tolerance(book["invoice_value"], portal["invoice_value"], config):
                    rejected.append(
                        f"Note {book.get('note_reference')} appears on a portal note, but one "
                        "side has no GSTIN and the amounts do not agree, so it was not accepted "
                        "as the same document."
                    )
                    continue
                fragment += (
                    " One side has no GSTIN, so this pair rests on the note reference plus the "
                    "amount and date agreeing."
                )

            if require_kind and book.get("note_kind") != portal.get("note_kind"):
                # PAIRED, so the disagreement is reported as a Document Type
                # Mismatch by `_compare_pair` instead of leaving two orphans.
                fragment += (
                    f" Kind disagreement: books says {book.get('note_kind')}, "
                    f"portal says {portal.get('note_kind')}."
                )

            baseline_key = {"exact": "exact", "variant": "reference_variant",
                            "fallback": "gstin_date_value"}[mode]
            baseline = float(baselines.get(baseline_key, 0.0))
            invalid = _invalid_gstin(book)
            identity = _score(baseline, config, cross_period=cross_period, invalid_gstin=invalid)
            if gstin_missing:
                identity = max(0.0, min(100.0, round(identity + missing_penalty, 2)))
            found.append((
                _rank(baseline, _value_proximity(book, portal, config), config,
                      cross_period=cross_period, invalid_gstin=invalid),
                identity,
                portal,
                fragment,
            ))
        return found, rejected

    for mode, pass_name in (
        ("exact", f"{label} Reference match".strip()),
        ("variant", f"{label} Reference variant".strip()),
        ("fallback", f"{label} GSTIN + date + value".strip()),
    ):
        baseline_key = {"exact": "exact", "variant": "reference_variant",
                        "fallback": "gstin_date_value"}[mode]
        pass_baseline = float(baselines.get(baseline_key, 0.0))
        for _, book in books.iterrows():
            if book["_nid"] in books_used:
                continue
            candidates, rejected = candidates_for(book, mode)
            if rejected:
                bucket = near_misses.setdefault(book["_nid"], [])
                bucket.extend(note for note in rejected if note not in bucket)
            if not candidates:
                continue
            chosen, final_score, ambiguity_note, forced_low = _resolve_ambiguity(candidates, config)
            fragment = next(f for _r, _i, portal, f in candidates if portal["_nid"] == chosen["_nid"])
            results.append(_pair_result(
                book, chosen, config, pass_name=pass_name,
                baseline=pass_baseline, forced_low=forced_low, score_override=final_score,
                extra_reason=fragment + ambiguity_note, cross_period=cross_period,
                require_kind=require_kind,
            ))
            books_used.add(book["_nid"])
            pool_used.add(chosen["_nid"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_notes(
    books_notes: Optional[pd.DataFrame],
    portal_notes: Optional[pd.DataFrame],
    config: dict,
    *,
    adjacent_portal_notes: Optional[pd.DataFrame] = None,
) -> list[dict[str, Any]]:
    """Reconcile the books note register against the portal's notes.

    `config` is the `gst` block of matching_rules.yaml (the note overrides are
    read from its `notes` sub-block).

    Returns one result dict per books and per portal note — `Matched`,
    `Amount Difference`, `Not in Portal` (books only) or `Not in Books`
    (portal only) — each carrying its own note-level figures. This function
    NEVER returns an invoice-classified row and never mutates the invoice pool.

    An unmatched note from the ADJACENT period's file is deliberately NOT
    emitted as this run's exception: it belongs to that period and is reported
    by that period's run. The adjacent file is a source of candidates only —
    exactly how the invoice engine treats `adjacent_portal_df`.
    """
    cfg = _note_config(config)
    books = as_note_frame(books_notes, cfg)
    portal = as_note_frame(portal_notes, cfg)
    adjacent = as_note_frame(adjacent_portal_notes, cfg) if adjacent_portal_notes is not None else None

    results: list[dict[str, Any]] = []
    books_used: set[int] = set()
    portal_used: set[int] = set()
    near_misses: dict[int, list[str]] = {}

    _match_against_pool(
        books, portal, cfg, books_used, portal_used, results, near_misses,
        cross_period=False,
    )

    want_adjacent = bool((cfg.get("_notes_match") or {}).get("adjacent_period", True))
    if adjacent is not None and len(adjacent) and want_adjacent:
        adjacent_used: set[int] = set()
        _match_against_pool(
            books, adjacent, cfg, books_used, adjacent_used, results, near_misses,
            cross_period=True,
        )
    has_adjacent = adjacent is not None and len(adjacent) > 0
    want_adjacent = bool((cfg.get("_notes_match") or {}).get("adjacent_period", True))

    for _, book in books.iterrows():
        if book["_nid"] in books_used:
            continue
        reason = (
            f"Not in Portal: GSTIN {_key_gstin(book)}, note "
            f"{book.get('note_reference') or '(no reference extracted)'} "
            f"({book.get('note_kind')}), {_fmt(float(book.get('invoice_value') or 0))} — "
            "no portal note matched on reference, or on GSTIN + date + value."
        )
        if not has_adjacent:
            reason += (
                " No adjacent-period portal file was supplied to check for a note "
                "issued in the prior period."
            )
        elif not want_adjacent:
            reason += (
                " An adjacent-period portal file WAS supplied, but the "
                "adjacent-period check is disabled for notes, so it was not consulted."
            )
        else:
            reason += " Including the adjacent-period check for a note issued in the prior period."
        for near_miss in near_misses.get(book["_nid"], []):
            reason += f" {near_miss}"
        result = _make_result(
            "Not in Portal", 0.0, cfg,
            books_record=_row_json(book, exclude=_NOTE_EXCLUDE),
            portal_record=None, match_reason=reason,
        )
        results.append(_with_note_fields(result, book, None, cfg))

    for _, note in portal.iterrows():
        if note["_nid"] in portal_used:
            continue
        reason = (
            f"Not in Books: GSTIN {_key_gstin(note)}, note "
            f"{note.get('note_reference') or '(no note number)'} "
            f"({note.get('note_kind')}), {_fmt(float(note.get('invoice_value') or 0))} — "
            "no entry in the books note register matched it."
        )
        result = _make_result(
            "Not in Books", 0.0, cfg,
            books_record=None,
            portal_record=_row_json(note, exclude=_NOTE_EXCLUDE), match_reason=reason,
        )
        results.append(_with_note_fields(result, None, note, cfg))

    return results


def summarise_notes(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The note-level figures the report needs.

    `note_tax_gross` is every note's tax on either side; `note_tax_at_risk` is
    the tax on the notes that did NOT match. Deliberately separate from the
    invoice Period/Eligible ITC figures — a note adjusts ITC and must never be
    netted into them here.
    """
    counts: dict[str, int] = {name: 0 for name in NOTE_CLASSIFICATIONS}
    tax_by_class: dict[str, float] = {name: 0.0 for name in NOTE_CLASSIFICATIONS}
    for result in results:
        classification = result.get("classification") or ""
        if classification in counts:
            counts[classification] += 1
            tax_by_class[classification] += float(result.get("note_tax") or 0.0)

    gross = round(sum(float(r.get("note_tax") or 0.0) for r in results), 2)
    at_risk = round(sum(
        float(r.get("note_tax") or 0.0)
        for r in results if (r.get("classification") or "") != "Matched"
    ), 2)
    return {
        "total": len(results),
        "counts": counts,
        "tax_by_class": {k: round(v, 2) for k, v in tax_by_class.items()},
        "note_tax_gross": gross,
        "note_tax_at_risk": at_risk,
        "matched_count": counts["Matched"],
    }
