"""The read-only view of one document that every Phase-3 rule works on.

Phase 3 has ONE rule about its inputs: it reads them, and it writes nothing
back. ``DocContext`` is that read-only view — a frozen snapshot of one
document's stored fields, its derived values, the client it belongs to and the
C1 configuration in force. Every check, applicability decision and direction
judgement is a PURE FUNCTION of one of these, which is what makes the tests
possible without a database and what makes ``run_rules`` idempotent.

WHICH READ'S VALUE IS USED
--------------------------
Phase 2 stores TWO verbatim values per field (read A and read B) and never picks
a winner. Phase 3 has to use one of them to do arithmetic, so:

* read A is used when it produced a value,
* otherwise read B's value is used,
* and ``value_source`` records which one, so every check states its source in
  ``inputs_json`` rather than hiding the choice.

Phase 3 does NOT treat a disagreement as an error and does not resolve it —
``reads_agree`` already records that, and Phase 4 / the reviewer owns it. A
check that cannot be computed from the value it has says ``not_applicable``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from src.invoice_extract import fields_v2 as fv2
from src.invoice_extract import normalisers as nz

#: Field statuses that mean "there is no value here to reason about".
_NO_VALUE_STATUSES = frozenset({fv2.STATUS_NOT_PRINTED})

#: The tax heads the canonical field set carries, in the order a document
#: prints them. ``sgst_*`` is the SECOND intra-state head: for a Union Territory
#: without a legislature the invoice prints UTGST there instead, and the Phase-2
#: field set has no separate UTGST field — so the tax-type check compares this
#: head while stating that UTGST is what applies. (See the report/PR note.)
TAX_HEADS: tuple[str, ...] = ("igst", "cgst", "sgst", "utgst")


@dataclass(frozen=True)
class RuleConfig:
    """The C1 configuration one document's rules run against."""

    total_tolerance: Decimal = Decimal("0.05")
    tax_tolerance: Decimal = Decimal("1.00")
    service_sac_prefix: str = "99"
    legal_form_suffixes: tuple[str, ...] = ()
    individual_stopwords: frozenset[str] = frozenset()
    #: Valid rate slabs for THIS document's invoice date (empty = cannot say).
    slabs: tuple[float, ...] = ()
    #: code -> state row, for every state code the C1 table knows.
    state_codes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def known_state_codes(self) -> set[str]:
        return set(self.state_codes)


@dataclass(frozen=True)
class DocContext:
    """One document, frozen for the rule pass."""

    document_id: int
    extraction_id: Optional[int]
    category: str
    upload_id: Optional[int] = None
    client_id: Optional[int] = None
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    derived: dict[str, str] = field(default_factory=dict)
    annotations: tuple[dict[str, Any], ...] = ()
    client: Optional[dict[str, Any]] = None
    aliases: tuple[dict[str, Any], ...] = ()
    config: RuleConfig = field(default_factory=RuleConfig)
    #: The state row the printed place of supply resolved to, resolved by the
    #: caller (which has the database). None means "the table does not know this
    #: value", which the state checks report as a failure rather than a skip.
    place_state: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ fields
    def row(self, key: str) -> Optional[dict[str, Any]]:
        return self.fields.get(key)

    def status(self, key: str) -> Optional[str]:
        row = self.fields.get(key)
        return None if row is None else str(row.get("status") or "")

    def value_source(self, key: str) -> Optional[str]:
        row = self.fields.get(key)
        if row is None:
            return None
        if row.get("value_a") not in (None, ""):
            return "a"
        if row.get("value_b") not in (None, ""):
            return "b"
        return None

    def value(self, key: str) -> Optional[str]:
        """The verbatim value the rules reason about (read A, else read B)."""
        row = self.fields.get(key)
        if row is None:
            return None
        for side in ("value_a", "value_b"):
            raw = row.get(side)
            if raw is not None and str(raw).strip() != "":
                return str(raw).strip()
        return None

    def has_value(self, key: str) -> bool:
        """Is there a value to reason about? A not_printed status means no, even
        if a stray value was stored beside it."""
        if self.status(key) in _NO_VALUE_STATUSES:
            return False
        return self.value(key) is not None

    def number(self, key: str) -> Optional[Decimal]:
        """The field's value as a Decimal, or None when it is not a number."""
        return decimal_of(self.value(key))

    def kind(self, key: str) -> str:
        return fv2.kind_of(key) or fv2.KIND_TEXT

    def line_keys(self) -> list[str]:
        """Every line-level key the extraction carries, in line order."""
        out: list[str] = []
        for key in self.fields:
            parsed = fv2.parse_line_key(key)
            if parsed is not None:
                out.append(key)
        return sorted(out, key=lambda k: (fv2.parse_line_key(k) or (0, ""))[0])

    def line_numbers(self) -> list[int]:
        seen: list[int] = []
        for key in self.line_keys():
            parsed = fv2.parse_line_key(key)
            if parsed and parsed[0] not in seen:
                seen.append(parsed[0])
        return sorted(seen)

    def tax_amounts(self) -> dict[str, Decimal]:
        """Every printed tax head amount, by head name ('igst', 'cgst', ...)."""
        out: dict[str, Decimal] = {}
        for head in TAX_HEADS:
            key = f"{head}_amount"
            if head == "utgst":
                # The canonical field set has no UTGST column; a UT invoice
                # prints UTGST in the second intra-state position.
                continue
            value = self.number(key)
            if value is not None and self.has_value(key):
                out[head] = value
        return out

    def tax_total(self) -> Decimal:
        return sum(self.tax_amounts().values(), Decimal("0"))

    def taxable(self) -> tuple[Optional[Decimal], str]:
        """(taxable value, how it was obtained).

        Uses the printed taxable value when there is one; otherwise sums the
        LINE AMOUNTS, and only when every line has one. Never invents a figure
        from a partial set of lines.
        """
        if self.has_value("taxable_value"):
            value = self.number("taxable_value")
            if value is not None:
                return value, "printed"
        numbers = self.line_numbers()
        if not numbers:
            return None, "no taxable value is printed and there are no lines"
        amounts: list[Decimal] = []
        for number in numbers:
            key = fv2.line_key(number, "amount")
            if not self.has_value(key):
                return None, f"{key} is not printed, so the lines cannot be totalled"
            value = self.number(key)
            if value is None:
                return None, f"{key} is not a number"
            amounts.append(value)
        return sum(amounts, Decimal("0")), "summed from the line amounts"

    def monetary_keys(self) -> list[str]:
        return [key for key in self.fields if fv2.is_monetary(key)]

    def printed_rate(self) -> tuple[Optional[str], Optional[Decimal]]:
        """The printed tax rate and its head, when exactly one head prints one."""
        found: list[tuple[str, Decimal]] = []
        for head in ("igst", "cgst", "sgst"):
            key = f"{head}_rate"
            if self.has_value(key):
                value = self.number(key)
                if value is not None:
                    found.append((key, value))
        if len(found) == 1:
            return found[0]
        if found:
            # Two printed rates (e.g. CGST 9% + SGST 9%) — report the first and
            # let the check compare head by head instead.
            return found[0]
        return None, None


def decimal_of(raw: Any) -> Optional[Decimal]:
    """A printed value as a Decimal, via the Phase-2 number normaliser.

    Returns None when the value is absent or is not a number — never a
    fabricated zero. Zero is a legitimate value (a nil-rated slab, a round-off
    of 0.00) and is returned as ``Decimal('0')``.
    """
    if raw is None:
        return None
    text = nz.normalise_number(raw)
    if text is None:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def money(value: Optional[Decimal]) -> str:
    """A rupee figure for a detail line, or an honest dash."""
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.01')):,}"


#: A resolver the caller supplies so the checks themselves stay pure: the C1
#: state-code lookup needs the database, so it is injected rather than imported.
StateResolver = Callable[[Optional[str]], Optional[dict[str, Any]]]
