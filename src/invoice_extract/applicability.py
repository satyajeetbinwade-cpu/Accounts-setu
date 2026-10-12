"""Phase-3 applicability: is a field required, optional or not applicable?

WHY THIS EXISTS
---------------
A missing field is not one thing. "IGST is missing" on an intra-state invoice is
CORRECT — the invoice should not carry it, and the reviewer must not be asked
about it. "The invoice number is missing" is a real gap. "The buyer's order
number is missing" is normal — most invoices do not print one. Conflating the
three is how a review screen fills up with questions nobody needs to answer, and
how a real gap gets lost among them.

So every canonical field a document could carry gets a LEVEL:

* ``required``          — an absent value is a real gap that needs a person.
* ``optional``          — an absent value is simply "not printed"; never asked.
* ``not_applicable``    — the document's own facts say this field cannot exist
  here (the tax heads the supply does not attract), with the reason.

and a REASON in every case, so the screen can explain itself.

WHAT THIS MODULE NEVER DOES
---------------------------
It never changes a stored value, and it never turns an optional field into a
required one. The one rule that relaxes anything (the service/SAC prefix) can
only move a field from required to optional — a rule that ADDS requirements from
a prefix guess would push work onto a reviewer on no evidence.

OVERRIDES
---------
A reviewer may override a level, with a reason. An override is a row with
``source = 'user'`` and is PRESERVED across re-runs; only ``source = 'rule'``
rows are recomputed. An override never changes a stored value either — it
changes what the review screen asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract.doc_context import DocContext
from src.invoice_extract.rules_schema import (
    LEVEL_NOT_APPLICABLE,
    LEVEL_OPTIONAL,
    LEVEL_REQUIRED,
)

#: Documents whose values the pipeline reads.
EXTRACTED_CATEGORIES = ("tax_invoice", "no_gst_expense")

#: The invoice-level fields a tax invoice must carry for the document to be
#: usable at all.
REQUIRED_TAX_INVOICE: tuple[str, ...] = (
    "invoice_number",
    "invoice_date",
    "issuer_name",
    "issuer_gstin",
    "place_of_supply",
    "invoice_total",
)

#: A no-GST expense document is the same document with the GST removed, so its
#: required set is the same minus the GST-only fields.
REQUIRED_NO_GST_EXPENSE: tuple[str, ...] = (
    "invoice_date",
    "issuer_name",
    "invoice_total",
)

#: Fields that are optional ALWAYS, whatever the document says: an invoice that
#: prints neither is complete.
ALWAYS_OPTIONAL: tuple[str, ...] = ("buyers_order_no", "reference_no_date")

#: The tax heads, split by what a document must actually carry. An AMOUNT is
#: required when the supply attracts it; a RATE is not, because an invoice that
#: prints the tax need not print the percentage separately — Phase 3 derives it
#: (see the line_rate derived value) rather than pushing a question onto the
#: reviewer for every invoice that omits it.
IGST_AMOUNT = ("igst_amount",)
IGST_RATE = ("igst_rate",)
STATE_AMOUNTS = ("cgst_amount", "sgst_amount")
STATE_RATES = ("cgst_rate", "sgst_rate")
IGST_HEADS = IGST_RATE + IGST_AMOUNT
STATE_HEADS = STATE_RATES + STATE_AMOUNTS

#: Reasons, used verbatim by the screen.
REASON_INTER_STATE = "inter-state supply — the document should not carry this head"
REASON_INTRA_STATE = "intra-state supply — the document should not carry IGST"
REASON_RATE_DERIVED = "optional — a rate the invoice does not print is derived from tax ÷ taxable"


@dataclass(frozen=True)
class Applicability:
    field_key: str
    level: str
    reason: str
    source: str = "rule"


def _tax_expectation(ctx: DocContext) -> tuple[str, str]:
    """('igst' | 'intra' | 'unknown', reason) for this document.

    Reuses the same facts as the expected_tax_type check but NEVER fails: an
    unknown supply makes the tax heads optional (no review pressure), which is
    the honest answer when the states cannot be compared.
    """
    issuer_gstin = ctx.value("issuer_gstin")
    if not ctx.has_value("issuer_gstin") or not ctx.has_value("place_of_supply"):
        return "unknown", "the issuer state or the place of supply is not printed, so the supply type is unknown"
    issuer_state = ctx.config.state_codes.get(str(issuer_gstin or "")[:2])
    place_state = ctx.place_state
    if issuer_state is None or place_state is None:
        return "unknown", "the issuer state or place of supply is not a known state code"
    if issuer_state["special"] or place_state["special"]:
        return "unknown", f"{place_state['name']} is not a state in the GST state series"
    if issuer_state["code"] == place_state["code"]:
        return "intra", f"intra-state supply ({issuer_state['name']} → {place_state['name']})"
    return "igst", f"inter-state supply ({issuer_state['name']} → {place_state['name']})"


def _line_keys_for(ctx: DocContext) -> list[tuple[int, dict[str, str]]]:
    """The line fields that apply to this document, one group per line.

    When the extraction carries NO line keys at all, the first line's fields are
    still offered — for a tax invoice that is how a reviewer sees "the line
    details are missing" rather than nothing at all.
    """
    numbers = ctx.line_numbers()
    if not numbers and ctx.category == "tax_invoice":
        numbers = [1]
    return [
        (number, {name: fv2.line_key(number, name) for name in fv2.LINE_FIELD_NAMES})
        for number in numbers
    ]


def compute(ctx: DocContext) -> list[Applicability]:
    """The rule-based applicability for one document."""
    out: list[Applicability] = []
    expectation, expectation_reason = _tax_expectation(ctx)

    # ---- invoice-level fields
    if ctx.category == "tax_invoice":
        for key in REQUIRED_TAX_INVOICE:
            out.append(Applicability(key, LEVEL_REQUIRED, "required on a tax invoice"))
    elif ctx.category == "no_gst_expense":
        for key in REQUIRED_NO_GST_EXPENSE:
            out.append(Applicability(key, LEVEL_REQUIRED, "required on an expense document"))
        # A no-GST document may genuinely not carry any number at all.
        out.append(Applicability(
            "invoice_number", LEVEL_OPTIONAL,
            "optional on an expense document — it does not block export if the paper carries no number",
        ))
    else:
        # Not an extracted category: nothing is required, and that is stated.
        out.append(Applicability(
            "invoice_total", LEVEL_NOT_APPLICABLE,
            f"'{ctx.category}' documents are not extracted, so no field is required",
        ))

    for key in ALWAYS_OPTIONAL:
        out.append(Applicability(
            key, LEVEL_OPTIONAL, "optional — an invoice that does not print one is complete",
        ))

    # ---- the tax heads, driven by the supply type
    if expectation == "igst":
        for key in IGST_AMOUNT:
            out.append(Applicability(key, LEVEL_REQUIRED, expectation_reason))
        for key in IGST_RATE:
            out.append(Applicability(key, LEVEL_OPTIONAL, REASON_RATE_DERIVED))
        for key in STATE_HEADS:
            out.append(Applicability(key, LEVEL_NOT_APPLICABLE, REASON_INTER_STATE))
    elif expectation == "intra":
        for key in STATE_AMOUNTS:
            out.append(Applicability(key, LEVEL_REQUIRED, expectation_reason))
        for key in STATE_RATES:
            out.append(Applicability(key, LEVEL_OPTIONAL, REASON_RATE_DERIVED))
        for key in IGST_HEADS:
            out.append(Applicability(key, LEVEL_NOT_APPLICABLE, REASON_INTRA_STATE))
    else:
        for key in IGST_HEADS + STATE_HEADS:
            out.append(Applicability(
                key, LEVEL_OPTIONAL, f"optional — {expectation_reason}",
            ))

    # Remaining invoice-level keys that are not otherwise mentioned are optional:
    # the field set is what COULD be printed, not what must be.
    mentioned = {row.field_key for row in out}
    for key in fv2.INVOICE_KEYS:
        if key in mentioned:
            continue
        out.append(Applicability(
            key, LEVEL_OPTIONAL, "optional — it never blocks export and is not always printed",
        ))

    # ---- line fields
    is_service = False
    for _number, keys in _line_keys_for(ctx):
        hsn = keys["hsn_sac"]
        value = ctx.value(hsn)
        if ctx.has_value(hsn) and value and value.strip().startswith(ctx.config.service_sac_prefix):
            is_service = True
    service_reason = (
        f"optional — a service line (SAC starts with '{ctx.config.service_sac_prefix}') rarely prints one"
    )
    for _number, keys in _line_keys_for(ctx):
        out.append(Applicability(
            keys["description"], LEVEL_OPTIONAL, "optional — a line description is not always printed",
        ))
        # hsn_sac IS required on a tax invoice: its absence is the case that
        # needs the reviewer (who can clear it with a "not printed" reason).
        out.append(Applicability(
            keys["hsn_sac"],
            LEVEL_REQUIRED if ctx.category == "tax_invoice" else LEVEL_OPTIONAL,
            "required on a tax invoice — a missing code needs a person to clear it as not printed",
        ))
        for name in ("quantity", "rate"):
            out.append(Applicability(
                keys[name], LEVEL_OPTIONAL,
                service_reason if is_service else "optional — it never blocks export",
            ))
        out.append(Applicability(
            keys["amount"], LEVEL_OPTIONAL, "optional — a line amount is used when printed",
        ))

    # De-duplicate, first mention wins (an explicit rule beats the catch-all).
    seen: dict[str, Applicability] = {}
    for row in out:
        seen.setdefault(row.field_key, row)
    return [seen[key] for key in sorted(seen)]


def missing_required(ctx: DocContext, rows: list[Applicability]) -> list[dict[str, Any]]:
    """The required fields this document does NOT carry — the reviewer's queue.

    A required field is missing when there is no value AND the extraction did not
    deliberately record it as ``not_printed`` with no value... except that
    ``not_printed`` IS the absence, so it counts. What does not count is a field
    the extraction never looked at, which for an invoice-level key means the same
    thing: no value.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.level != LEVEL_REQUIRED:
            continue
        if ctx.has_value(row.field_key):
            continue
        out.append({
            "field_key": row.field_key,
            "reason": row.reason,
            "status": ctx.status(row.field_key) or "not_extracted",
        })
    return out
