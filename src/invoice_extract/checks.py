"""Phase-3 checks: pure functions of the stored values.

THE RULE THIS MODULE OBEYS
--------------------------
**A check that cannot fail must not be produced.**

If an input is missing, or one input would be DERIVED from the other, the check
returns ``not_applicable`` with a one-line reason — never ``pass``. A green tick
that could not have gone red is worse than no tick: it tells a reviewer a number
was verified when nothing was verified at all. Every ``not_applicable`` here
therefore carries the reason it could not be decided, and the reason names the
missing or circular input.

Each check returns a :class:`CheckResult`. Nothing here writes anything: the
caller persists the results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract.doc_context import DocContext, money
from src.invoice_extract.gst_identifiers import gstin_valid
from src.invoice_extract.rules_schema import (
    CHECK_FAIL,
    CHECK_NOT_APPLICABLE,
    CHECK_PASS,
)

#: A derived rate is "on a valid slab" within this many percentage points.
SLAB_TOLERANCE = 0.05

#: How close tax must be to rate x taxable, as a fraction of the tax, in
#: ADDITION to the absolute gst.tax_tolerance_rupees. Rounding on the printed
#: page is not proportional, but a percentage guard stops a large invoice's
#: rupee tolerance from swamping a real difference.
TAX_RATE_RELATIVE_TOLERANCE = Decimal("0.001")


@dataclass(frozen=True)
class CheckResult:
    check_key: str
    status: str
    detail: str
    inputs: dict[str, Any] = field(default_factory=dict)
    fields_implicated: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.status == CHECK_FAIL

    def as_json(self) -> dict[str, Any]:
        return {
            "check_key": self.check_key, "status": self.status, "detail": self.detail,
            "inputs": self.inputs, "fields_implicated": list(self.fields_implicated),
        }


def _na(check_key: str, reason: str, **inputs: Any) -> CheckResult:
    return CheckResult(check_key, CHECK_NOT_APPLICABLE, reason, inputs)


# ---------------------------------------------------------------------------
# 1. totals_reconcile
# ---------------------------------------------------------------------------


def totals_reconcile(ctx: DocContext) -> CheckResult:
    """taxable + tax ± round_off = invoice_total, within the totals tolerance."""
    total = ctx.number("invoice_total") if ctx.has_value("invoice_total") else None
    if total is None:
        return _na("totals_reconcile", "the invoice total is not printed, so there is nothing to reconcile to")

    taxable, how = ctx.taxable()
    if taxable is None:
        return _na("totals_reconcile", how, total=money(total))

    tax_heads = ctx.tax_amounts()
    tax = sum(tax_heads.values(), Decimal("0"))

    round_off_printed = ctx.has_value("round_off")
    round_off = ctx.number("round_off") if round_off_printed else Decimal("0")
    if round_off is None:
        return _na("totals_reconcile", "the printed round-off is not a number", total=money(total))

    expected = taxable + tax + round_off
    difference = total - expected
    tolerance = ctx.config.total_tolerance
    inputs = {
        "taxable": str(taxable), "taxable_source": how,
        "tax_heads": {head: str(value) for head, value in tax_heads.items()},
        "tax": str(tax),
        "round_off": str(round_off), "round_off_printed": round_off_printed,
        "invoice_total": str(total), "expected_total": str(expected),
        "difference": str(difference), "tolerance": str(tolerance),
        "value_source": ctx.value_source("invoice_total"),
    }
    detail = (
        f"{money(taxable)} taxable + {money(tax)} tax "
        + (f"+ {money(round_off)} round off " if round_off_printed else "")
        + f"= {money(expected)} against a printed total of {money(total)}"
        + (f" — off by {money(difference)}" if difference != 0 else "")
    )
    if abs(difference) <= tolerance:
        return CheckResult("totals_reconcile", CHECK_PASS, detail, inputs)
    implicated = ["invoice_total", "taxable_value"]
    for head in tax_heads:
        implicated.append(f"{head}_amount")
    if round_off_printed:
        implicated.append("round_off")
    return CheckResult(
        "totals_reconcile", CHECK_FAIL,
        detail + f" (tolerance {money(tolerance)})", inputs, tuple(implicated),
    )


# ---------------------------------------------------------------------------
# 2. tax_equals_rate_times_taxable
# ---------------------------------------------------------------------------


def tax_equals_rate_times_taxable(ctx: DocContext) -> CheckResult:
    """Does each printed tax head equal its PRINTED rate x taxable?

    Only produced when a rate is actually printed. A rate the invoice does not
    print is not an input, and deriving one here would make the check circular
    with the total it is meant to verify.
    """
    rate_key, rate = ctx.printed_rate()
    if rate_key is None or rate is None:
        return _na(
            "tax_equals_rate_times_taxable",
            "no tax rate is printed, so tax cannot be checked against rate x taxable "
            "(the derived rate is reported separately)",
        )
    taxable, how = ctx.taxable()
    if taxable is None:
        return _na("tax_equals_rate_times_taxable", how)

    issues: list[str] = []
    inputs: dict[str, Any] = {
        "rate_key": rate_key, "rate_percent": str(rate), "taxable": str(taxable),
        "taxable_source": how, "tolerance": str(ctx.config.tax_tolerance),
        "heads": {},
    }
    implicated: list[str] = []
    checked_any = False
    for name in ("igst", "cgst", "sgst"):
        amount_key = f"{name}_amount"
        if not ctx.has_value(amount_key):
            continue
        amount = ctx.number(amount_key)
        if amount is None:
            continue
        # Each head states its OWN rate against the full taxable value: an
        # intra-state invoice prints CGST 9% AND SGST 9%, each of which is 9% of
        # the taxable value — not half of a combined 18%.
        expected = (taxable * rate / 100).quantize(Decimal("0.01"))
        tolerance = max(ctx.config.tax_tolerance, abs(expected) * TAX_RATE_RELATIVE_TOLERANCE)
        checked_any = True
        inputs["heads"][name] = {
            "printed": str(amount), "expected": str(expected),
            "difference": str(amount - expected), "tolerance": str(tolerance),
        }
        if abs(amount - expected) > tolerance:
            issues.append(
                f"{name.upper()} {money(amount)} vs {money(expected)} expected "
                f"at {rate}% of {money(taxable)}"
            )
            implicated.extend([amount_key, f"{name}_rate", "taxable_value"])

    if not checked_any:
        return _na(
            "tax_equals_rate_times_taxable",
            "a rate is printed but no tax head amount is, so there is nothing to compare",
        )
    if issues:
        return CheckResult(
            "tax_equals_rate_times_taxable", CHECK_FAIL,
            "; ".join(issues), inputs, tuple(dict.fromkeys(implicated)),
        )
    return CheckResult(
        "tax_equals_rate_times_taxable", CHECK_PASS,
        f"each printed tax head matches {rate}% of {money(taxable)}", inputs,
    )


# ---------------------------------------------------------------------------
# 3. derived_rate_on_valid_slab
# ---------------------------------------------------------------------------


def derived_rate_on_valid_slab(ctx: DocContext) -> CheckResult:
    """Was the DERIVED rate (tax / taxable) one of the slabs legal on this date?

    Only produced when the rate is NOT printed — when it is printed, check 2
    covers it and this check would be testing a number nobody derived.
    """
    rate_key, _printed = ctx.printed_rate()
    if rate_key is not None:
        return _na(
            "derived_rate_on_valid_slab",
            "the rate is printed, so no rate was derived (see tax_equals_rate_times_taxable)",
        )
    derived = ctx.derived.get("line_rate")
    if derived is None:
        return _na("derived_rate_on_valid_slab", "no rate was derived for this document")
    try:
        percent = Decimal(str(derived))
    except Exception:  # noqa: BLE001
        return _na("derived_rate_on_valid_slab", f"the derived rate {derived!r} is not a number")
    if not ctx.config.slabs:
        return _na(
            "derived_rate_on_valid_slab",
            "no valid slab period matches this invoice's date, so the derived rate cannot be tested",
            derived_rate=str(percent),
        )
    slabs = [Decimal(str(s)) for s in ctx.config.slabs]
    nearest = min(slabs, key=lambda slab: abs(slab - percent))
    difference = abs(nearest - percent)
    inputs = {
        "derived_rate": str(percent), "slabs": [str(s) for s in slabs],
        "nearest_slab": str(nearest), "difference": str(difference),
        "tolerance": str(SLAB_TOLERANCE),
    }
    if difference <= Decimal(str(SLAB_TOLERANCE)):
        return CheckResult(
            "derived_rate_on_valid_slab", CHECK_PASS,
            f"the derived rate {percent}% is within {SLAB_TOLERANCE} of the {nearest}% slab", inputs,
        )
    return CheckResult(
        "derived_rate_on_valid_slab", CHECK_FAIL,
        f"the derived rate {percent}% is {difference} away from the nearest valid slab "
        f"({nearest}%) for the invoice date",
        inputs, ("taxable_value", "igst_amount", "cgst_amount", "sgst_amount"),
    )


# ---------------------------------------------------------------------------
# 4. gstin_valid
# ---------------------------------------------------------------------------


def gstin_valid_check(ctx: DocContext) -> CheckResult:
    """Shape + PAN + state code + check digit, for every printed GSTIN."""
    known = ctx.config.known_state_codes or None
    problems: list[str] = []
    inputs: dict[str, Any] = {"known_state_codes": sorted(known) if known else None, "gstins": {}}
    implicated: list[str] = []
    checked = 0
    for key in ("issuer_gstin", "recipient_gstin"):
        if not ctx.has_value(key):
            continue
        value = ctx.value(key)
        if value is None:
            continue
        checked += 1
        result = gstin_valid(value, known_state_codes=known)
        inputs["gstins"][key] = {
            "printed": value, "ok": result.ok, "reason": result.reason,
            "state_code": result.state_code, "pan": result.pan,
        }
        if not result.ok:
            problems.append(f"{key} {value!r}: {result.reason}")
            implicated.append(key)
    if checked == 0:
        return _na("gstin_valid", "no GSTIN is printed on this document")
    if problems:
        return CheckResult("gstin_valid", CHECK_FAIL, "; ".join(problems), inputs, tuple(implicated))
    return CheckResult("gstin_valid", CHECK_PASS, f"{checked} GSTIN(s) are structurally valid", inputs)


# ---------------------------------------------------------------------------
# 5. state_code_matches_gstin
# ---------------------------------------------------------------------------


def state_code_matches_gstin(ctx: DocContext) -> CheckResult:
    """Do the printed place of supply and the GSTINs' state codes exist in the table?"""
    place = ctx.value("place_of_supply")
    if not ctx.has_value("place_of_supply"):
        return _na("state_code_matches_gstin", "the place of supply is not printed")

    problems: list[str] = []
    inputs: dict[str, Any] = {"place_of_supply": place}
    implicated: list[str] = []

    place_state = ctx.place_state
    inputs["place_of_supply_state"] = (
        {"code": place_state["code"], "name": place_state["name"]} if place_state else None
    )
    if place_state is None:
        problems.append(f"the place of supply {place!r} is not a state/UT in the code table")
        implicated.append("place_of_supply")

    for key in ("issuer_gstin", "recipient_gstin"):
        if not ctx.has_value(key):
            continue
        value = ctx.value(key)
        code = str(value or "")[:2]
        state = ctx.config.state_codes.get(code)
        inputs[f"{key}_state"] = (
            {"code": state["code"], "name": state["name"]} if state else code or None
        )
        if state is None:
            problems.append(f"{key} starts with state code {code!r}, which the code table does not know")
            implicated.append(key)

    if problems:
        return CheckResult(
            "state_code_matches_gstin", CHECK_FAIL, "; ".join(problems), inputs,
            tuple(dict.fromkeys(implicated)),
        )
    return CheckResult(
        "state_code_matches_gstin", CHECK_PASS,
        "the place of supply and every GSTIN state code resolve against the state-code table", inputs,
    )


# ---------------------------------------------------------------------------
# 6. expected_tax_type
# ---------------------------------------------------------------------------


def expected_tax_type(ctx: DocContext) -> CheckResult:
    """Do the tax heads printed match what the two states imply?

    Inter-state (issuer state ≠ place of supply) implies IGST. Intra-state
    implies CGST + the second state head, which for a Union Territory WITHOUT a
    legislature is UTGST rather than SGST.

    This is an EXPECTATION. Where a document cannot support one — an export or
    an SEZ, a place of supply outside the state series, a reverse-charge
    document — the answer is ``not_applicable`` ("cannot determine"), never a
    failure, because a rule that cannot see the reason is not evidence of a
    mistake.
    """
    place_text = ctx.value("place_of_supply")
    issuer_gstin = ctx.value("issuer_gstin")
    if not ctx.has_value("place_of_supply"):
        return _na("expected_tax_type", "the place of supply is not printed, so no expectation can be formed")
    if not ctx.has_value("issuer_gstin"):
        return _na("expected_tax_type", "the issuer GSTIN is not printed, so the issuer's state is unknown")

    issuer_state = ctx.config.state_codes.get(str(issuer_gstin or "")[:2])
    place_state = ctx.place_state
    inputs: dict[str, Any] = {
        "issuer_gstin": issuer_gstin,
        "issuer_state": issuer_state["code"] if issuer_state else None,
        "place_of_supply": place_text,
        "place_state": place_state["code"] if place_state else None,
    }
    if issuer_state is None:
        return _na(
            "expected_tax_type",
            f"the issuer GSTIN's state code {str(issuer_gstin or '')[:2]!r} is not in the state-code table",
            **inputs,
        )
    if place_state is None:
        return _na(
            "expected_tax_type",
            f"the place of supply {place_text!r} is not a state/UT in the code table, so intra/inter-state "
            "cannot be decided",
            **inputs,
        )
    if issuer_state["special"] or place_state["special"]:
        return _na(
            "expected_tax_type",
            f"{place_state['name']} is not a state in the GST state series (export / special territory) — "
            "cannot determine the expected tax type",
            **inputs,
        )
    if _mentions_reverse_charge(ctx):
        return _na(
            "expected_tax_type",
            "the document mentions reverse charge, which changes who pays the tax — cannot determine the "
            "expected tax type",
            **inputs,
        )

    intra = issuer_state["code"] == place_state["code"]
    utgst_applies = bool(place_state.get("utgst"))
    # The canonical field set has NO utgst_* column: a Union Territory invoice
    # prints UTGST in the second intra-state position, which Phase 2 stores as
    # `sgst_*`. So the COMPARISON is on the two intra-state COLUMNS, while the
    # wording says UTGST where that is the head actually levied. The alternative
    # — expecting a field that cannot exist — would fail every UT invoice.
    second_column = "sgst"
    second_head = "utgst" if utgst_applies else "sgst"
    expected_columns = ("cgst", second_column) if intra else ("igst",)
    printed = {
        head: ctx.has_value(f"{head}_amount")
        for head in ("igst", "cgst", "sgst")
    }
    inputs.update({
        "basis": f"issuer {issuer_state['name']} → place of supply {place_state['name']}",
        "expected_columns": list(expected_columns),
        "expected": [
            second_head if (head == second_column and intra) else head for head in expected_columns
        ],
        "printed": printed,
        "utgst_applies": utgst_applies,
    })

    wrong = [
        head for head in ("igst", "cgst", "sgst")
        if printed[head] and head not in expected_columns
    ]
    missing = [head for head in expected_columns if not printed.get(head)]

    def _label(head: str) -> str:
        return "UTGST" if (head == second_column and intra and utgst_applies) else head.upper()

    if wrong:
        return CheckResult(
            "expected_tax_type", CHECK_FAIL,
            f"this is an {'intra' if intra else 'inter'}-state supply "
            f"({issuer_state['name']} → {place_state['name']}), so "
            + (" + ".join(_label(h) for h in expected_columns))
            + " was expected, but "
            + ", ".join(f"{h.upper()} is printed" for h in wrong)
            + " instead",
            inputs, tuple(f"{head}_amount" for head in wrong),
        )
    if missing:
        return CheckResult(
            "expected_tax_type", CHECK_FAIL,
            f"this is an {'intra' if intra else 'inter'}-state supply "
            f"({issuer_state['name']} → {place_state['name']}), but "
            + ", ".join(_label(h) for h in missing)
            + " is not printed",
            inputs, tuple(f"{head}_amount" for head in missing),
        )
    return CheckResult(
        "expected_tax_type", CHECK_PASS,
        f"{'intra' if intra else 'inter'}-state supply ({issuer_state['name']} → {place_state['name']}) "
        f"matched by the printed " + " + ".join(_label(h) for h in expected_columns),
        inputs,
    )


def _mentions_reverse_charge(ctx: DocContext) -> bool:
    """Does anything on the page mention reverse charge?

    Read from the stored values only — a text scan of what was PRINTED. It is
    deliberately over-eager: a false positive downgrades the check to
    not_applicable, which is the safe direction.
    """
    for row in ctx.fields.values():
        for side in ("value_a", "value_b"):
            text = str(row.get(side) or "").lower()
            if "reverse charge" in text or "rcm" in text:
                return True
    return False


# ---------------------------------------------------------------------------
# 7. provenance_in_region
# ---------------------------------------------------------------------------


def provenance_in_region(ctx: DocContext) -> CheckResult:
    """Was the box of every READ value inside the document's own regions?

    Only fields that HOLD a value AND CARRY A BOX are judged. A field whose
    reads returned no value was never read, and a value the model never boxed
    has no box that can lie outside — neither is "boxed outside". Counting
    them made this check fail on EVERY document (20+ phantom failures per
    document on the live bundle, most of them for fields the invoice cannot
    even have).

    A value genuinely boxed outside its regions still fails, exactly as
    before: its box is compared against the regions with the same overlap
    rule.
    """
    with_values = [
        key for key, row in ctx.fields.items()
        if (row.get("value_a") or row.get("value_b"))
    ]
    boxed = [key for key in with_values if row_has_box(ctx.fields[key])]
    unboxed = [key for key in with_values if key not in boxed]
    if not with_values:
        return _na("provenance_in_region", "no value was read for this document")
    outside = [key for key in boxed if not row_in_region(ctx.fields[key])]
    inputs = {
        "valued_fields": len(with_values),
        "boxed_fields": len(boxed),
        "unboxed_fields": len(unboxed),
        "outside_regions": outside,
    }
    if outside:
        return CheckResult(
            "provenance_in_region", CHECK_FAIL,
            f"{len(outside)} value(s) were boxed outside the document's regions: "
            + ", ".join(outside),
            inputs, tuple(outside),
        )
    detail = f"all {len(boxed)} boxed read value(s) sit inside the document's regions"
    if unboxed:
        detail += f" ({len(unboxed)} read value(s) carry no box)"
    return CheckResult("provenance_in_region", CHECK_PASS, detail, inputs)


def row_in_region(row: dict[str, Any]) -> bool:
    return bool(row.get("in_region"))


def row_has_box(row: dict[str, Any]) -> bool:
    """Does the stored row carry a usable box?

    Only a row WITH a box can be "boxed outside" (or inside) a region —
    ``in_region`` is False for a boxless row too, so the box itself must be
    checked before ``in_region`` is interpreted.
    """
    return all(row.get(k) is not None for k in ("x", "y", "w", "h"))


# ---------------------------------------------------------------------------
# 8. handwriting_guard
# ---------------------------------------------------------------------------


def handwriting_guard(ctx: DocContext) -> CheckResult:
    """Is any monetary value handwritten, or shadowed by a handwriting annotation?

    Phase 2 already flags a value it read as handwritten (``needs_printed``) and
    flags one whose box overlaps an annotation carrying the same amount. This
    check turns that into a document-level verdict a reviewer sees.
    """
    monetary = ctx.monetary_keys()
    if not monetary:
        return _na("handwriting_guard", "no monetary value is stored for this document")
    flagged = [
        key for key in monetary
        if ctx.status(key) == fv2.STATUS_NEEDS_PRINTED or ctx.fields[key].get("is_handwritten")
    ]
    inputs = {
        "monetary_fields": len(monetary),
        "flagged": flagged,
        "annotations": len(ctx.annotations),
    }
    if flagged:
        return CheckResult(
            "handwriting_guard", CHECK_FAIL,
            f"Amounts on this document are handwritten: {len(flagged)} of the "
            f"{len(monetary)} amount(s) Setu read. Setu never copies handwriting into your "
            "records on its own, so a person must confirm each amount.",
            inputs, tuple(flagged),
        )
    return CheckResult(
        "handwriting_guard", CHECK_PASS,
        f"All {len(monetary)} amount(s) on this document are printed"
        + (f", and none is covered by one of the {len(ctx.annotations)} handwritten note(s) "
           "Setu found on the page" if ctx.annotations else ""),
        inputs,
    )


# ---------------------------------------------------------------------------
# The set, in the order a reviewer reads them
# ---------------------------------------------------------------------------

CHECKS: tuple[tuple[str, Any], ...] = (
    ("totals_reconcile", totals_reconcile),
    ("tax_equals_rate_times_taxable", tax_equals_rate_times_taxable),
    ("derived_rate_on_valid_slab", derived_rate_on_valid_slab),
    ("gstin_valid", gstin_valid_check),
    ("state_code_matches_gstin", state_code_matches_gstin),
    ("expected_tax_type", expected_tax_type),
    ("provenance_in_region", provenance_in_region),
    ("handwriting_guard", handwriting_guard),
)

#: Human labels for the review model / report.
#: The check names a REVIEWER reads (the checks strip and the review cards).
#: A positive name — what holding means — because the strip shows the state
#: (green tick / red cross) beside it; the failure has its own words in
#: ``REVIEW_FAIL_TITLES`` below.
CHECK_LABELS: dict[str, str] = {
    "totals_reconcile": "Taxable value + tax ± round off equals the invoice total",
    "tax_equals_rate_times_taxable": "Each printed tax amount equals the printed rate × the taxable value",
    "derived_rate_on_valid_slab": "The tax rate is one the law allows for the invoice date",
    "gstin_valid": "Every printed GST number is a well-formed GSTIN",
    "state_code_matches_gstin": "The place of supply matches the GST number's state code",
    "expected_tax_type": "The right kind of tax was charged (IGST, or CGST + SGST/UTGST)",
    "provenance_in_region": "Every value came from inside the document",
    "handwriting_guard": "Amounts are printed, not handwritten",
}

#: What the review card says when the check FAILS — the plain-words headline
#: the reviewer acts on. Keyed by the same check keys; a new check must be
#: added here too (``tests/test_review_plain_language.py`` enforces it).
REVIEW_FAIL_TITLES: dict[str, str] = {
    "totals_reconcile": "The totals do not add up",
    "tax_equals_rate_times_taxable": "A tax amount does not match its rate and taxable value",
    "derived_rate_on_valid_slab": "The tax rate is not one the law allows for this date",
    "gstin_valid": "A GST number does not look right",
    "state_code_matches_gstin": "The state codes do not match the GST number",
    "expected_tax_type": "The wrong kind of tax was charged",
    "provenance_in_region": "A value was read from outside the document",
    "handwriting_guard": "Amounts on this document are handwritten",
}


def run_all(ctx: DocContext) -> list[CheckResult]:
    """Every check, in order. One check raising must not lose the others."""
    out: list[CheckResult] = []
    for key, check in CHECKS:
        try:
            result = check(ctx)
        except Exception as exc:  # noqa: BLE001 — a broken check is reported, never silent
            result = CheckResult(
                key, CHECK_NOT_APPLICABLE,
                f"the check could not run: {type(exc).__name__}: {exc}",
                {"error": str(exc)},
            )
        if result.check_key != key:
            result = CheckResult(
                key, result.status, result.detail, result.inputs, result.fields_implicated,
            )
        out.append(result)
    return out
