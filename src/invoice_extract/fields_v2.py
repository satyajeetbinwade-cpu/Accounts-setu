"""The Phase-2 canonical field set: invoice level, plus one group per line.

WHAT THIS IS
------------
The list of fields the Phase-2 extraction pass asks for and stores. It is
deliberately narrow and PRINTED-ONLY:

* The model TRANSCRIBES what is printed. It never computes, rounds or
  reformats, and it never infers a value that is not on the page.
* A field the document does not print gets status ``not_printed`` with a NULL
  value — never a guess.
* A rate the invoice does not print stays ``not_printed``. Phase 3 derives
  rates; this phase must not.

``kind`` drives the comparison in ``normalisers.py``:

* ``number``     — money and quantities. Parsed to ``Decimal``; never
                   "near-matched" (a slightly different number is a different
                   number).
* ``date``       — parsed day-first (Indian convention) to ISO; never
                   "near-matched".
* ``identifier`` — GSTIN, PAN, HSN/SAC. Upper-cased, spaces removed, EXACT
                   match only: a single wrong character is a wrong identifier.
* ``text``       — names, addresses, narration. Casefolded with whitespace and
                   punctuation collapsed; the only kind that may match "near".

LINE FIELDS
-----------
``line.N.description`` for line ``N``. ``line_key(n, field)`` and
``parse_line_key(key)`` are the ONLY place that shape is built or read, so the
prompt, the persistence layer and the scorer cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --- kinds -----------------------------------------------------------------
KIND_TEXT = "text"
KIND_NUMBER = "number"
KIND_DATE = "date"
KIND_IDENTIFIER = "identifier"

# --- field statuses (mirrors doc_extract_schema) ---------------------------
STATUS_READ = "read"
STATUS_NOT_PRINTED = "not_printed"
STATUS_UNREADABLE = "unreadable"
STATUS_OUT_OF_REGION = "out_of_region"
STATUS_NEEDS_PRINTED = "needs_printed"

ALL_STATUSES = (
    STATUS_READ,
    STATUS_NOT_PRINTED,
    STATUS_UNREADABLE,
    STATUS_OUT_OF_REGION,
    STATUS_NEEDS_PRINTED,
)


@dataclass(frozen=True)
class FieldSpec:
    key: str
    kind: str
    label: str
    #: True for the fields the spec calls out as REQUIRED to come from the
    #: printed block (money). Used by the handwriting guard.
    monetary: bool = False


#: Invoice-level fields, in reading order. ``buyers_order_no`` and
#: ``reference_no_date`` are two SEPARATE optional fields (a buyer's order
#: number is not a document reference, and conflating them loses data).
INVOICE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("invoice_number", KIND_TEXT, "Invoice number"),
    FieldSpec("invoice_date", KIND_DATE, "Invoice date"),
    FieldSpec("issuer_name", KIND_TEXT, "Issuer name"),
    FieldSpec("issuer_gstin", KIND_IDENTIFIER, "Issuer GSTIN"),
    FieldSpec("issuer_pan", KIND_IDENTIFIER, "Issuer PAN"),
    FieldSpec("recipient_name", KIND_TEXT, "Recipient name"),
    FieldSpec("recipient_gstin", KIND_IDENTIFIER, "Recipient GSTIN"),
    FieldSpec("recipient_pan", KIND_IDENTIFIER, "Recipient PAN"),
    FieldSpec("place_of_supply", KIND_TEXT, "Place of supply"),
    FieldSpec("reference_no_date", KIND_TEXT, "Reference no. and date (as printed)"),
    FieldSpec("buyers_order_no", KIND_TEXT, "Buyer's order no."),
    FieldSpec("taxable_value", KIND_NUMBER, "Taxable value", monetary=True),
    FieldSpec("cgst_rate", KIND_NUMBER, "CGST rate (as printed)"),
    FieldSpec("cgst_amount", KIND_NUMBER, "CGST amount", monetary=True),
    FieldSpec("sgst_rate", KIND_NUMBER, "SGST rate (as printed)"),
    FieldSpec("sgst_amount", KIND_NUMBER, "SGST amount", monetary=True),
    FieldSpec("igst_rate", KIND_NUMBER, "IGST rate (as printed)"),
    FieldSpec("igst_amount", KIND_NUMBER, "IGST amount", monetary=True),
    FieldSpec("round_off", KIND_NUMBER, "Round off", monetary=True),
    FieldSpec("total_tax", KIND_NUMBER, "Total tax", monetary=True),
    FieldSpec("invoice_total", KIND_NUMBER, "Invoice total", monetary=True),
    FieldSpec("total_in_words", KIND_TEXT, "Total in words"),
)

#: Per-line fields. ``rate`` is the unit rate; ``amount`` the line taxable
#: amount. Both printed-only.
LINE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("description", KIND_TEXT, "Line description"),
    FieldSpec("hsn_sac", KIND_IDENTIFIER, "Line HSN/SAC"),
    FieldSpec("quantity", KIND_NUMBER, "Line quantity"),
    FieldSpec("rate", KIND_NUMBER, "Line rate", monetary=True),
    FieldSpec("amount", KIND_NUMBER, "Line amount", monetary=True),
)

#: Every invoice-level key, in order.
INVOICE_KEYS: tuple[str, ...] = tuple(f.key for f in INVOICE_FIELDS)

#: Every line-field name, in order.
LINE_FIELD_NAMES: tuple[str, ...] = tuple(f.key for f in LINE_FIELDS)

#: A hard cap on line numbers we will accept from a reply. A model that
#: hallucinates ``line.900.description`` must not be able to make the run
#: unbounded; a real Indian invoice has far fewer than this.
MAX_LINES = 60

_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in INVOICE_FIELDS}


def line_key(n: int, field: str) -> str:
    """``line_key(3, "amount")`` → ``"line.3.amount"``."""
    return f"line.{int(n)}.{field}"


def parse_line_key(key: str) -> Optional[tuple[int, str]]:
    """``"line.3.amount"`` → ``(3, "amount")``; None for any other key.

    Tolerant about the separator between "line" and the number so a reply using
    ``line3.amount`` still lands on the right field, but strict about the field
    name being one of :data:`LINE_FIELD_NAMES`.
    """
    if not key or not key.startswith("line"):
        return None
    rest = key[4:].lstrip("._-")
    head, _, tail = rest.partition(".")
    if not head.isdigit() or tail not in LINE_FIELD_NAMES:
        return None
    number = int(head)
    if number < 1 or number > MAX_LINES:
        return None
    return number, tail


def field_spec(key: str) -> Optional[FieldSpec]:
    """The spec for an invoice-level or line-level key, else None."""
    if key in _BY_KEY:
        return _BY_KEY[key]
    parsed = parse_line_key(key)
    if parsed is None:
        return None
    _, name = parsed
    return next((f for f in LINE_FIELDS if f.key == name), None)


def kind_of(key: str) -> Optional[str]:
    spec = field_spec(key)
    return spec.kind if spec else None


def is_monetary(key: str) -> bool:
    spec = field_spec(key)
    return bool(spec and spec.monetary)


def canonical_keys() -> tuple[str, ...]:
    """Every invoice-level key (line keys are unbounded, so they are not here)."""
    return INVOICE_KEYS


def prompt_field_list() -> str:
    """The field list as the prompt shows it (line fields are templated)."""
    lines = [f"  - {f.key} ({f.kind}): {f.label}" for f in INVOICE_FIELDS]
    lines.append(
        "  - line.N.description / line.N.hsn_sac / line.N.quantity / "
        "line.N.rate / line.N.amount — one group per printed line, N starting at 1"
    )
    return "\n".join(lines)
