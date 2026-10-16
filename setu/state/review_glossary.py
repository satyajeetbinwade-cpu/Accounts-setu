"""Plain language for everything the review screen shows a person.

Two problems this solves. The screen said "Line 2 · hsn_sac" and "required on a
tax invoice — a missing code needs a person to clear it as not printed": true,
and useless to the accountant reading it. And a new field could be added to the
extraction without anyone deciding what it MEANS to a reviewer.

* ``FIELD_GLOSSARY`` is keyed by the field keys the extraction can produce. The
  keys come from the registry (``fields_v2.INVOICE_KEYS`` and
  ``fields_v2.LINE_FIELD_NAMES``), never from a hand-kept list, and a test walks
  the registry so a new key cannot appear without an entry here.
* ``REASON_TEXT`` turns every code that can put a card on the screen into a
  sentence that says what Setu found and what to do about it.

Nothing here decides anything: it is words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.invoice_extract import fields_v2 as fv2


@dataclass(frozen=True)
class FieldGloss:
    """One field, in words a non-expert accountant can act on."""

    key: str
    label: str
    meaning: str
    where: str = ""


def _g(key: str, label: str, meaning: str, where: str = "") -> tuple[str, FieldGloss]:
    return key, FieldGloss(key=key, label=label, meaning=meaning, where=where)


#: The invoice-level keys the extraction can produce (fields_v2.INVOICE_KEYS).
_INVOICE: dict[str, FieldGloss] = dict([
    _g("invoice_number", "Invoice number",
       "The supplier's own number for this bill. Setu files the bill under it, and the "
       "GST portal expects it in the return.",
       "Near the top of the bill, beside the title or the date."),
    _g("invoice_date", "Invoice date",
       "The date the supplier issued this bill. The tax period a bill falls in is decided "
       "by this date, so it must be right.",
       "Top of the bill, beside the invoice number."),
    _g("issuer_name", "Supplier name",
       "The name of the business that ISSUED the bill — the shop or firm you bought from.",
       "Top of the bill, usually with its address and GST number."),
    _g("issuer_gstin", "Supplier GSTIN",
       "The 15-character GST registration number of the business that issued the bill. It "
       "must be exact: the GST portal matches it character by character.",
       "Printed with the supplier's name or address; often small print at the bottom."),
    _g("issuer_pan", "Supplier PAN",
       "The supplier's 10-character income-tax PAN. It is printed on some bills beside the "
       "GST number.",
       "With the supplier's name, address or bank details."),
    _g("recipient_name", "Buyer name",
       "The name the bill is addressed to. Check it is your client — a bill in someone "
       "else's name cannot be claimed.",
       "In the 'Billed to' / 'Buyer' block, on the right or below the supplier's details."),
    _g("recipient_gstin", "Buyer GSTIN",
       "Your client's GST number as printed on the bill. A tax invoice should carry it; a "
       "bill to an individual often does not.",
       "In the 'Billed to' block. If the field is blank on the paper, say so rather than "
       "typing your own."),
    _g("recipient_pan", "Buyer PAN",
       "The buyer's PAN, printed on some bills instead of (or beside) a GST number.",
       "In the 'Billed to' block."),
    _g("place_of_supply", "Place of supply",
       "The state the supply is treated as happening in. It decides whether the bill carries "
       "IGST or CGST+SGST.",
       "In the invoice details block, often beside the buyer's state or in the tax summary."),
    _g("reference_no_date", "Reference no. and date",
       "The supplier's own reference (often an order or work-order number) and its date, "
       "printed as 'Ref: ASAP-V/25-26/197 dt. 25-Mar-26'. It is read as printed, number and "
       "date together.",
       "Below the invoice number, in the invoice details block."),
    _g("buyers_order_no", "Buyer's order number",
       "Your client's own purchase-order number, when the supplier quotes it on the bill.",
       "In the invoice details block, next to 'PO No.' or 'Order No.'."),
    _g("taxable_value", "Taxable value",
       "The value of the goods or services BEFORE GST is added. Every tax figure on the bill "
       "is a percentage of this number.",
       "In the tax summary block ('Taxable Value' / 'Total before tax')."),
    _g("cgst_rate", "CGST rate",
       "The Central GST percentage applied to the taxable value, printed as a rate (9% is "
       "half of 18%).",
       "In the tax summary block, beside the CGST amount."),
    _g("cgst_amount", "CGST amount",
       "The Central GST charged on this bill, in rupees. Together with SGST it makes up the "
       "GST on a bill within one state.",
       "In the tax summary block under 'CGST' or 'Central Tax'."),
    _g("sgst_rate", "SGST rate",
       "The State GST percentage applied to the taxable value — the same rate as CGST on a "
       "bill within one state.",
       "In the tax summary block, beside the SGST amount."),
    _g("sgst_amount", "SGST amount",
       "The State GST charged on this bill, in rupees. It is normally the same figure as the "
       "CGST amount.",
       "In the tax summary block under 'SGST' / 'State Tax'."),
    _g("igst_rate", "IGST rate",
       "The Integrated GST percentage applied when the supplier and the buyer are in "
       "different states.",
       "In the tax summary block, beside the IGST amount."),
    _g("igst_amount", "IGST amount",
       "The Integrated GST charged on this bill, in rupees — used instead of CGST+SGST when "
       "the supply crosses a state line.",
       "In the tax summary block under 'IGST' / 'Integrated Tax'."),
    _g("round_off", "Round off",
       "The small adjustment (usually under a rupee) between the exact total and the rounded "
       "amount payable. It can be negative, printed as (-)0.46.",
       "The last line of the tax summary, above the invoice total."),
    _g("total_tax", "Total tax",
       "All the GST on this bill added together (IGST, or CGST + SGST).",
       "In the tax summary block, sometimes printed and sometimes only worked out by adding "
       "the tax heads."),
    _g("invoice_total", "Invoice total",
       "The amount payable on the bill: taxable value plus tax, plus or minus any round off.",
       "The largest figure on the bill, often in the bottom-right, sometimes repeated in "
       "words."),
    _g("total_in_words", "Total in words",
       "The amount payable written out in words. It is a useful cross-check on the printed "
       "figure.",
       "Just below the invoice total, or at the bottom of the bill."),
])

#: The per-line keys (fields_v2.LINE_FIELD_NAMES), shown as "Line 2 — Rate". The
#: label is built by :func:`label_for`, so the line number is never baked in here.
_LINE: dict[str, FieldGloss] = dict([
    _g("line.description", "Description",
       "What was actually bought or sold on that line, in the supplier's own words (for "
       "example 'Cheque Return Charges').",
       "The widest column of the item table."),
    _g("line.hsn_sac", "HSN/SAC code",
       "HSN/SAC code — the classification code printed beside an item or service (HSN for "
       "goods, SAC for services, e.g. 997112); it decides the GST rate and is reported in "
       "GST returns.",
       "The item table, in the column headed HSN, HSN/SAC or SAC; a service bill sometimes "
       "prints it once in the tax summary instead."),
    _g("line.quantity", "Quantity",
       "How many units that line covers (numbers of pieces, kilos, litres, months).",
       "The item table, in the Qty column."),
    _g("line.rate", "Rate",
       "The price of ONE unit on that line — not the line total.",
       "The item table, in the Rate column."),
    _g("line.amount", "Amount",
       "What that line costs in total (quantity multiplied by rate, before GST).",
       "The right-hand money column of the item table."),
])

#: Keys the task asked to describe that the extraction does not currently read
#: as their own fields. They are here so the screen can explain them if a future
#: registry adds them, and so the gap is written down rather than guessed at.
_EXTRA: dict[str, FieldGloss] = dict([
    _g("line.unit", "Unit",
       "The unit a quantity is counted in (nos, kg, months). Setu does not read this today: "
       "the quantity is stored without its unit.",
       "The item table, beside the Qty column."),
    _g("line.discount", "Discount",
       "A reduction given on that line, before GST. Setu does not read a line discount "
       "today; a discounted amount appears in the line amount as printed.",
       "The item table, in a Discount column when the bill prints one."),
    _g("cess_amount", "Cess",
       "An extra levy on some goods (for example large cars), charged on top of GST. Setu "
       "does not read a cess head today; if a bill prints one it will be picked up as "
       "another tax amount.",
       "The tax summary block, under 'Cess'."),
])

FIELD_GLOSSARY: dict[str, FieldGloss] = {**_INVOICE, **_LINE, **_EXTRA}


def missing_glossary_keys() -> list[str]:
    """Registry keys the extraction can produce and the glossary does NOT cover.

    The list is read from the field registry every time (``fields_v2``), never
    from a hand-kept list here, so a new key added to the extraction turns up in
    this list — and ``tests/test_review_plain_language.py`` fails on a non-empty
    answer rather than letting the screen show a raw key to a reviewer.
    """
    wanted: list[str] = [str(key) for key in fv2.INVOICE_KEYS]
    wanted += [fv2.line_key(1, str(name)) for name in fv2.LINE_FIELD_NAMES]
    return [key for key in wanted if gloss_for(key) is None]


# ---------------------------------------------------------------------------
# Looking a field up
# ---------------------------------------------------------------------------


def _split(field_key: str) -> tuple[Optional[int], str]:
    """('line.2.hsn_sac') -> (2, 'line.hsn_sac'); invoice keys -> (None, key)."""
    parsed = fv2.parse_line_key(field_key)
    if parsed is None:
        return None, field_key
    number, name = parsed
    return int(number), f"line.{name}"


def gloss_for(field_key: str) -> Optional[FieldGloss]:
    """The glossary entry for a field key, line keys included."""
    if not field_key:
        return None
    _number, base = _split(field_key)
    return FIELD_GLOSSARY.get(base) or FIELD_GLOSSARY.get(field_key)


def label_for(field_key: str) -> str:
    """The friendly label: 'Line 2 — HSN/SAC code', 'Invoice total', ….

    Falls back to the raw key so an unknown field is still visible (the guard
    test is what keeps that from happening quietly).
    """
    entry = gloss_for(field_key)
    if entry is None:
        return field_key
    number, _base = _split(field_key)
    if number is not None:
        return f"Line {number} — {entry.label}"
    return entry.label


def meaning_for(field_key: str) -> str:
    entry = gloss_for(field_key)
    return entry.meaning if entry else ""


def where_for(field_key: str) -> str:
    entry = gloss_for(field_key)
    return entry.where if entry else ""


def hint_for(field_key: str) -> str:
    """The one-line hint under a card title: meaning, then where to look."""
    meaning = meaning_for(field_key)
    where = where_for(field_key)
    if meaning and where:
        return f"{meaning} Where: {where}"
    return meaning or where


# ---------------------------------------------------------------------------
# Reasons, in plain words
# ---------------------------------------------------------------------------

#: Every code that can put a card on the review screen (the needs-you kinds),
#: plus the codes a decision is recorded under. Each says what Setu found and
#: what the person should do.
REASON_TEXT: dict[str, str] = {
    # ---- the needs-you kinds (see rules.py: needs_you)
    "missing_required": (
        "Setu did not find this on the document. If it is printed, type it; if the document "
        "really does not carry it, choose \u201cNot printed on the document\u201d."
    ),
    "check_failed": (
        "Setu checked the figures on this document and this one did not work out. Look at "
        "the numbers it compared and either correct the value or record why the document is "
        "like that."
    ),
    "below_threshold": (
        "Setu read this, but not confidently enough to file it on its own. Compare it with "
        "the document and confirm it, or type what is really printed."
    ),
    "direction_unsettled": (
        "Setu cannot tell from the paper whether this bill was paid by you or issued by you. "
        "Choose purchase or sale, and the rest of the file follows."
    ),
    # ---- the decision codes a person's answer is recorded under
    "not_printed": (
        "You are saying the document itself does not carry this. Setu records the answer, "
        "and the gap stops holding the document back."
    ),
    "not_on_document": (
        "You are saying the figure Setu read is not on the document at all."
    ),
    "not_applicable_here": (
        "You are saying this kind of document never needs this field."
    ),
    "correct": "You checked this against the document and it is right as read.",
    "ocr_error": "The reader slipped: the document prints something else.",
    "wrong_reader": "The wrong reader was used for this value.",
    "other": "None of the standard reasons fits — say why in your own words.",
}


def reason_text(code: str, *, field_label: str = "") -> str:
    """The plain sentence for a code, with the field named when we have one.

    For ``missing_required`` the sentence names the field, because "did not find
    this" is only useful when the reader knows what "this" is.
    """
    text = REASON_TEXT.get(code, "")
    if code == "missing_required" and field_label:
        return (
            f"Setu could not find the {field_label} on this document. If it is printed, type "
            "it; if the document really does not carry it, choose \u201cNot printed on the "
            "document\u201d."
        )
    if code == "below_threshold" and field_label:
        return (
            f"Setu read the {field_label}, but not confidently enough to file it on its own. "
            "Compare it with the document and confirm it, or type what is really printed."
        )
    return text


#: What the three decision buttons mean, shown as a tooltip on each.
BUTTON_HELP: dict[str, str] = {
    "confirm": "I checked this against the document; it is right",
    "not_printed": "The document itself does not carry this",
    "not_applicable": "This kind of document never needs it",
}

#: The button labels, so the words live in one place.
BUTTON_LABEL_CONFIRM = "Confirm value"
BUTTON_LABEL_NOT_PRINTED = "Not printed on the document"
BUTTON_LABEL_NOT_APPLICABLE = "Not applicable here"
