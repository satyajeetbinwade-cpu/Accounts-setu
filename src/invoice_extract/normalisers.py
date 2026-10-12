"""Phase-2 value normalisers and the read-A / read-B comparison.

This module answers ONE question per field: do the two independent reads agree?
It never picks a winner — both verbatim values are stored, and ``reads_agree``
records how they compared.

NORMALISERS (the spec's rules, exactly)
--------------------------------------
* numbers     → ``Decimal`` after stripping commas, currency symbols and
                trailing zeros. ``₹1,000.00`` → ``1000``; ``1,000`` → ``1000``.
* dates       → ISO, parsing the DAY-FIRST Indian convention (``27-Apr-26``,
                ``13/04/2026``, ``6.4.26``). An unparseable date normalises to
                the collapsed text, so it still compares as an identifier-ish
                exact string rather than silently matching anything.
* identifiers → upper-cased with whitespace removed, EXACT match only
                (GSTIN / PAN / HSN-SAC: one wrong character is wrong).
* text        → casefolded, whitespace and punctuation collapsed.

VERDICTS
--------
* ``exact``     — equal after normalising.
* ``near``      — text token similarity >= 0.9. NEVER for numbers, dates or
                  identifiers: a near-match on a number is a wrong number.
* ``disagree``  — both reads produced a value and they do not match. Also used
                  when read A produced nothing and read B did (there is no
                  ``missing_a`` verdict in the schema, and calling it agreement
                  would be a lie).
* ``missing_b`` — read A found the value, read B returned none.
* ``null``      — neither read found it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.invoice_extract.fields_v2 import (
    KIND_DATE,
    KIND_IDENTIFIER,
    KIND_NUMBER,
)

AGREE_EXACT = "exact"
AGREE_NEAR = "near"
AGREE_DISAGREE = "disagree"
AGREE_MISSING_B = "missing_b"
AGREE_NULL = "null"

#: Text token similarity at or above this is "near".
NEAR_THRESHOLD = 0.9

_CURRENCY = "\u20b9\u0024\u20ac\u00a3\u00a5"          # ₹ $ € £ ¥
_NUM_RE = re.compile(r"[-+]?\d[\d,\s]*(?:\.\d+)?")
_NON_TEXT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Day-first formats, most specific first. Two-digit years are read as 20xx —
# an Indian invoice scan in this product is never from the 1900s.
_DATE_FORMATS: tuple[str, ...] = (
    "%d-%b-%y", "%d-%b-%Y", "%d %b %y", "%d %b %Y",
    "%d-%B-%y", "%d-%B-%Y", "%d %B %y", "%d %B %Y",
    "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y",
    "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d",
    "%d %m %Y", "%d %m %y",
)


def _clean(raw: object) -> Optional[str]:
    """A verbatim-ish string from a model value, or None when there is none."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.casefold() in ("null", "none", "n/a", "na", "-", "--", "not printed", "not_printed"):
        return None
    return text


# ---------------------------------------------------------------------------
# The four normalisers
# ---------------------------------------------------------------------------


def normalise_number(raw: object) -> Optional[str]:
    """Money/quantity → a canonical decimal string, or None if not a number.

    ``1,000.00`` → ``1000``; ``₹ 180.00`` → ``180``; ``0.32`` → ``0.32``
    (a trailing zero on a fraction is not stripped — that would change
    ``0.30`` into ``0.3``, which is the same number but a different string, so
    the comparison below works on the Decimal, not the string).
    """
    text = _clean(raw)
    if text is None:
        return None
    stripped = text.replace(",", "").strip()
    for symbol in _CURRENCY:
        stripped = stripped.replace(symbol, "")
    stripped = stripped.replace("Rs.", "").replace("rs.", "").replace("Rs", "").replace("rs", "").strip()
    match = _NUM_RE.search(stripped)
    if not match:
        return None
    token = match.group(0).replace(",", "").replace(" ", "")
    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    return _decimal_key(value)


def _decimal_key(value: Decimal) -> str:
    """A stable string for a Decimal: normal form, no exponent, no trailing
    fraction zeros. ``Decimal('1000.00')`` and ``Decimal('1e3')`` both give
    ``'1000'``."""
    text = format(value.normalize(), "f")   # 'f' never uses an exponent
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def numbers_equal(a: object, b: object) -> bool:
    """Do two values denote the SAME number? (no near-matching for money)"""
    na, nb = normalise_number(a), normalise_number(b)
    return na is not None and na == nb


def normalise_date(raw: object) -> Optional[str]:
    """A printed date → ISO ``YYYY-MM-DD``, day-first. None when unparseable."""
    text = _clean(raw)
    if text is None:
        return None
    candidate = text.strip().rstrip(".")
    # Drop a leading "dt." / "dated" and any time part.
    candidate = re.sub(r"^(dt\.?|dated|date)\s*[:\-]?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.split(r"[\sT]+(?=\d{1,2}:\d{2})", candidate)[0]
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(candidate.strip(), fmt).date()
        except ValueError:
            continue
        return parsed.isoformat()
    return None


def dates_equal(a: object, b: object) -> bool:
    na, nb = normalise_date(a), normalise_date(b)
    return na is not None and na == nb


def normalise_identifier(raw: object) -> Optional[str]:
    """GSTIN / PAN / HSN-SAC → upper-cased, whitespace removed."""
    text = _clean(raw)
    if text is None:
        return None
    return _WS_RE.sub("", text).upper()


def identifiers_equal(a: object, b: object) -> bool:
    na, nb = normalise_identifier(a), normalise_identifier(b)
    return na is not None and na == nb


def normalise_text(raw: object) -> Optional[str]:
    """Names / narration → casefolded, punctuation + whitespace collapsed."""
    text = _clean(raw)
    if text is None:
        return None
    text = text.casefold()
    text = _NON_TEXT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def text_similarity(a: object, b: object) -> float:
    """Similarity of two normalised non-numeric strings, 0.0-1.0.

    A character-level ratio (``difflib``), because the job of "near" is to
    tolerate a typo or an OCR slip: ``Suresh Kumar Jain`` vs ``Suresh Kumar
    Jam`` is the same name, while ``Uttar Pradesh`` vs ``Uttar Pradesh State``
    is an extra word — a content difference, not a slip — and stays a
    disagreement. Deliberately NOT a token-set metric: a single wrong character
    in one token of a three-token name would fall below any sane token
    threshold and be reported as a disagreement.

    Never used for numbers, dates or identifiers: there, a difference of one
    character or digit is a different value.
    """
    na, nb = normalise_text(a), normalise_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return round(difflib.SequenceMatcher(None, na, nb).ratio(), 6)


def normalise(kind: str, raw: object) -> Optional[str]:
    """The normalised form of ``raw`` for a field of ``kind``."""
    if kind == KIND_NUMBER:
        return normalise_number(raw)
    if kind == KIND_DATE:
        return normalise_date(raw)
    if kind == KIND_IDENTIFIER:
        return normalise_identifier(raw)
    return normalise_text(raw)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    reads_agree: str
    norm_a: Optional[str]
    norm_b: Optional[str]
    similarity: Optional[float] = None

    @property
    def agree(self) -> bool:
        return self.reads_agree in (AGREE_EXACT, AGREE_NEAR)


def compare(kind: str, value_a: object, value_b: object) -> Verdict:
    """Compare the two reads of one field. Never picks a winner."""
    a, b = _clean(value_a), _clean(value_b)
    if a is None and b is None:
        return Verdict(AGREE_NULL, None, None)
    if a is not None and b is None:
        return Verdict(AGREE_MISSING_B, normalise(kind, a), None)
    if a is None and b is not None:
        # No "missing_a" verdict exists in the schema; the honest reading is
        # that the two reads do not agree.
        return Verdict(AGREE_DISAGREE, None, normalise(kind, b))

    assert a is not None and b is not None
    na, nb = normalise(kind, a), normalise(kind, b)

    if kind == KIND_NUMBER:
        equal = na is not None and na == nb
        return Verdict(AGREE_EXACT if equal else AGREE_DISAGREE, na, nb)
    if kind == KIND_DATE:
        if dates_equal(a, b):
            return Verdict(AGREE_EXACT, na, nb)
        # Two unparseable dates still compare as exact text when identical.
        return Verdict(AGREE_EXACT if na == nb else AGREE_DISAGREE, na, nb)
    if kind == KIND_IDENTIFIER:
        equal = identifiers_equal(a, b)
        # An identifier that does not parse as one on either side is compared
        # as text, but still exactly.
        return Verdict(AGREE_EXACT if (equal or na == nb) else AGREE_DISAGREE, na, nb)

    similarity = text_similarity(a, b)
    if na == nb:
        return Verdict(AGREE_EXACT, na, nb, similarity)
    if similarity >= NEAR_THRESHOLD:
        return Verdict(AGREE_NEAR, na, nb, similarity)
    return Verdict(AGREE_DISAGREE, na, nb, similarity)


def same_value(kind: str, value: object, expected: object) -> bool:
    """Does a read MATCH an expected printed value, by the field's own rules?

    Uses the same normalisers, so the scorer can never be stricter or looser
    than the A-vs-B comparison. Numbers and identifiers are exact; text may be
    near (a name printed with different spacing is the same name).
    """
    verdict = compare(kind, value, expected)
    return verdict.reads_agree in (AGREE_EXACT, AGREE_NEAR)


def as_date(raw: object) -> Optional[date]:
    iso = normalise_date(raw)
    if iso is None:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None
