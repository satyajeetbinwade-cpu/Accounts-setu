"""A tax-ledger row printed under the items is not a line item.

Tally-style bills print the tax ledger and the round-off UNDER the item table, in
the same columns and the same typeface::

    IGST Payable Account            180.00
    Output CGST - 9%              1,275.84
    Less : Short and Excess         (-)0.46

Read naively each of those becomes ``line.N`` — and because a tax invoice needs
an HSN/SAC on every stored line (see ``applicability``), a false line blocks the
reviewer from finishing the document. The extraction prompt now says not to read
them (``extract._SYSTEM_PROMPT`` rule 6); this module is the second line of
defence: a DETERMINISTIC post-read rule applied to EACH read's own output, before
the two reads are compared and before anything is stored.

A row is dropped only when BOTH hold:

* its description reads like a tax-ledger or round-off row (:func:`is_ledger_row`);
* its amount EQUALS one of the tax heads — or the round-off — printed on the same
  document (:func:`matches_a_printed_head`).

Either test alone is not enough, and the controls are deliberate: an item called
"Freight IGST charges" whose amount matches no printed head is a real charge; a
real "Professional Fee" that happens to equal the tax is a real fee (its
description is not a ledger one); a row called "Output" whose amount matches
nothing stays; a handwritten or empty row is never touched.

Nothing here decides a value. It removes rows and renumbers what is left, so the
surviving lines are 1..K with no gaps — the caller keeps each kept row's own
box/annotation with it because the rows are re-keyed, never rebuilt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.invoice_extract import fields_v2 as fv2

#: The heads a printed tax-ledger row can duplicate, in the order they are
#: reported. ``utgst_amount`` and ``cess_amount`` are listed even though the
#: registry does not read them today: when a read does not carry a head there is
#: simply nothing to match, and the day the registry adds it this rule works.
TAX_HEADS: tuple[str, ...] = (
    "igst_amount",
    "cgst_amount",
    "sgst_amount",
    "utgst_amount",
    "cess_amount",
    "round_off",
)

#: "equals, to within a paisa". A printed tax of 1,275.84 and a row of 1275.84
#: are the same amount; 1,275.84 and 1,275.85 are not.
AMOUNT_TOLERANCE = Decimal("0.01")

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

#: Words that, on their own, make a description a tax-ledger row: a tax head, a
#: ledger account name, or the word "tax".
_LEDGER_WORDS = frozenset({"igst", "cgst", "sgst", "utgst", "cess", "output", "payable", "tax"})

#: Round-off, however it is printed ("Round Off", "Rounded", "Rounding Off").
_ROUND_WORDS = frozenset({"round", "roundoff", "rounding", "rounded"})

#: "Short and excess" / "Short & Excess" — both words must be present.
_SHORT_EXCESS_WORDS = frozenset({"short", "excess"})

#: "Less" is only a ledger word TOGETHER with short/excess/round ("Less : Short
#: and Excess"), never alone: plenty of real item descriptions start with "Less".
_LESS_WORD = "less"

_AMOUNT_TOKEN = re.compile(r"\d[\d,]*\.?\d*")
_BRACKET_MINUS = re.compile(r"\(\s*-\s*\)")


def _words(description: object) -> set[str]:
    return {word for word in _WORD_SPLIT.split(str(description or "").lower()) if word}


def is_ledger_row(description: object) -> bool:
    """Does this description read like a tax-ledger or round-off row?

    Case-insensitive and tolerant of punctuation and spacing: "Output CGST - 9%",
    "IGST  Payable   Account", "Less : Short & Excess" and "ROUND OFF" all match.
    """
    words = _words(description)
    if not words:
        return False
    if words & _LEDGER_WORDS:
        return True
    if words & _ROUND_WORDS:
        return True
    if _SHORT_EXCESS_WORDS <= words:
        return True
    return _LESS_WORD in words and bool(words & (_SHORT_EXCESS_WORDS | _ROUND_WORDS))


def _is_negative(text: str) -> bool:
    """'(-)0.46', '(0.46)' and '-0.46' are negative; a bare figure is not."""
    if _BRACKET_MINUS.search(text):
        return True
    stripped = text.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        return True
    return re.search(r"-\s*\d|\d\s*-$", text) is not None


def parse_amount(raw: object) -> Optional[Decimal]:
    """A printed amount as a Decimal, Indian grouping and negatives included.

    ``1,275.84`` → 1275.84 · ``12,34,567.89`` → 1234567.89 · ``(-)0.46`` and
    ``(0.46)`` and ``-0.46`` → -0.46 · ``₹ 180.00`` → 180.00. None when the text
    carries no number at all ("-", "Nil", "").
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    match = _AMOUNT_TOKEN.search(text)
    if not match:
        return None
    try:
        value = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:  # pragma: no cover — the regex only matches digits
        return None
    return -value if _is_negative(text) else value


#: The words a description uses for each head. CGST and SGST are very often the
#: SAME number, so a row that names its head must be matched to THAT head —
#: otherwise "Output SGST-9%" would be reported as the CGST row.
_HEAD_WORDS: dict[str, tuple[str, ...]] = {
    "igst_amount": ("igst",),
    "cgst_amount": ("cgst",),
    "sgst_amount": ("sgst",),
    "utgst_amount": ("utgst",),
    "cess_amount": ("cess",),
    "round_off": ("round", "roundoff", "rounding", "rounded", "short", "excess"),
}


def _named_head(description: object) -> Optional[str]:
    """The head this description names, if it names one."""
    words = _words(description)
    for head, vocabulary in _HEAD_WORDS.items():
        if words & set(vocabulary):
            return head
    return None


def matches_a_printed_head(
    amount: object, heads: dict[str, Optional[str]], description: object = None,
) -> Optional[tuple[str, str]]:
    """The ``(head, printed text)`` this amount duplicates, or None.

    Round-off is compared sign-insensitively — bills print it as ``0.46`` or
    ``(-)0.46`` and mean the same thing — while a tax head is compared as
    printed, because the ledger row carries the same sign as the summary block.
    When more than one head matches (CGST and SGST are routinely equal), the head
    the description NAMES wins, so the log row says which tax it actually was.
    """
    value = parse_amount(amount)
    if value is None:
        return None
    matching: list[tuple[str, str]] = []
    for head in TAX_HEADS:
        printed = heads.get(head)
        head_value = parse_amount(printed)
        if head_value is None:
            continue
        if head == "round_off":
            if abs(abs(value) - abs(head_value)) <= AMOUNT_TOLERANCE:
                matching.append((head, str(printed)))
        elif abs(value - head_value) <= AMOUNT_TOLERANCE:
            matching.append((head, str(printed)))
    if not matching:
        return None
    named = _named_head(description)
    for head, printed in matching:
        if head == named:
            return head, printed
    return matching[0]


@dataclass(frozen=True)
class DroppedLine:
    """One row that was NOT a line item, kept for the change log."""

    line_no: int            # the number it had in THIS read
    description: str
    amount: str
    matched_head: str       # the head whose printed value it repeated
    head_value: str         # that head, exactly as printed


@dataclass
class LineFilterResult:
    """What is left of one read's lines, renumbered, plus what was dropped."""

    #: ``line.N.<field>`` → verbatim text, for the KEPT rows, numbered 1..K.
    values: dict[str, Optional[str]] = field(default_factory=dict)
    #: old key → new key, for every KEPT key (so a caller can re-key rows and
    #: their boxes/annotations in place rather than rebuilding them).
    renumber: dict[str, str] = field(default_factory=dict)
    #: The rows that were removed, in the order they were read.
    dropped: list[DroppedLine] = field(default_factory=list)


def filter_lines(
    values: dict[str, Optional[str]], heads: dict[str, Optional[str]],
) -> LineFilterResult:
    """Drop tax-ledger/round-off rows from ONE read and renumber the rest 1..K.

    ``values`` are that read's own ``line.*`` keys — nothing else is looked at,
    so a caller can pass its whole field dict and only the lines are affected.
    """
    by_line: dict[int, dict[str, Optional[str]]] = {}
    for key, text in values.items():
        parsed = fv2.parse_line_key(key)
        if parsed is None:
            continue
        number, name = parsed
        by_line.setdefault(int(number), {})[name] = text

    result = LineFilterResult()
    kept: list[int] = []
    for number in sorted(by_line):
        row = by_line[number]
        description = str(row.get("description") or "")
        amount = str(row.get("amount") or "")
        if is_ledger_row(description):
            hit = matches_a_printed_head(amount, heads, description)
            if hit is not None:
                result.dropped.append(DroppedLine(
                    line_no=number, description=description, amount=amount,
                    matched_head=hit[0], head_value=hit[1],
                ))
                continue
        kept.append(number)

    for new_number, old_number in enumerate(kept, start=1):
        for name, text in by_line[old_number].items():
            new_key = fv2.line_key(new_number, name)
            result.renumber[fv2.line_key(old_number, name)] = new_key
            result.values[new_key] = text
    return result


__all__ = [
    "AMOUNT_TOLERANCE",
    "DroppedLine",
    "LineFilterResult",
    "TAX_HEADS",
    "filter_lines",
    "is_ledger_row",
    "matches_a_printed_head",
    "parse_amount",
]
