"""GSTIN / PAN structural validation and the GST check-digit algorithm.

A GSTIN is 15 characters:

    positions 1-2   state code (digits)
    positions 3-12  the PAN of the registered person
    position  13    the entity number for that PAN in that state (1-9, A-Z)
    position  14    'Z' by default
    position  15    the CHECK DIGIT, computed over the first 14 characters

The check digit is worth having: it catches a transcription error that shape
checks alone cannot, which is exactly the class of mistake a two-read invoice
pipeline produces. The algorithm below is the one the GSTIN specification
defines — a weighted sum over the first 14 characters with the weights and the
base-36 alphabet as given, then the check character is the value that makes the
whole sum a multiple of 36.

``gstin_valid`` is deliberately strict about the ALPHABET (digits and
upper-case letters only) and about the state code being one the C1 table knows,
because a GSTIN with a state code we do not recognise is a real-world "is this
even a GSTIN" question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

#: The characters the GSTIN alphabet uses, in the order the algorithm indexes
#: them (0-9 then A-Z).
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: PAN shape: five letters, four digits, one letter.
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

#: A GSTIN, shape only — the check digit is verified separately.
GSTIN_SHAPE_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


@dataclass(frozen=True)
class GstinCheck:
    """The outcome of validating one GSTIN string."""

    ok: bool
    reason: str
    normalised: str = ""
    state_code: Optional[str] = None
    pan: Optional[str] = None


def normalise_gstin(raw: Optional[str]) -> str:
    """Upper-case and strip whitespace — the ONLY normalisation applied."""
    return "".join(str(raw or "").split()).upper()


def gstin_check_digit(first14: str) -> Optional[str]:
    """The check character for the first 14 characters, or None if invalid.

    The published algorithm: for each of the 14 characters, take its index in
    the base-36 alphabet, multiply by a weight that ALTERNATES 1, 2, 1, 2…, and
    sum ``value * weight`` after folding any product of 36 or more back by 36
    (integer-divide the product by 36 and add the remainder). The check
    character is then ``(36 - (sum % 36)) % 36`` — the alphabet entry that makes
    the total a multiple of 36.
    """
    text = normalise_gstin(first14)
    if len(text) != 14:
        return None
    total = 0
    for index, char in enumerate(text):
        value = _ALPHABET.find(char)
        if value < 0:
            return None
        weight = 1 if index % 2 == 0 else 2
        product = value * weight
        total += (product // 36) + (product % 36)
    return _ALPHABET[(36 - (total % 36)) % 36]


def gstin_valid(raw: Optional[str], *, known_state_codes: Optional[set[str]] = None) -> GstinCheck:
    """Validate one GSTIN: length, shape, PAN, state code and check digit.

    ``known_state_codes`` is the set of state codes the C1 table knows. When it
    is None the state-code step is skipped (the caller has no table to hand) —
    the step is never silently downgraded to a pass, it is simply not claimed.
    """
    text = normalise_gstin(raw)
    if not text:
        return GstinCheck(False, "no GSTIN is printed")
    if len(text) != 15:
        return GstinCheck(False, f"a GSTIN is 15 characters; this one has {len(text)}")
    if not GSTIN_SHAPE_RE.match(text):
        return GstinCheck(
            False,
            "the shape is wrong — a GSTIN is 2 state digits, a 10-character PAN, "
            "1 entity character, 'Z' and a check character",
        )
    state_code = text[:2]
    pan = text[2:12]
    if not PAN_RE.match(pan):
        return GstinCheck(False, f"the embedded PAN {pan!r} is not PAN-shaped", text, state_code, pan)
    if known_state_codes is not None and state_code not in known_state_codes:
        return GstinCheck(
            False, f"state code {state_code!r} is not a known GST state code", text, state_code, pan,
        )
    expected = gstin_check_digit(text[:14])
    if expected is None:
        return GstinCheck(False, "the first 14 characters are not valid GSTIN characters", text, state_code, pan)
    if text[14] != expected:
        return GstinCheck(
            False,
            f"the check character is {text[14]!r} but the first 14 characters give {expected!r} "
            "— a transcription error",
            text, state_code, pan,
        )
    return GstinCheck(True, "", text, state_code, pan)


def pan_valid(raw: Optional[str]) -> bool:
    """Is this a PAN-shaped string? (No check digit exists for a PAN.)"""
    return bool(PAN_RE.match(normalise_gstin(raw)))


def gstin_state_code(raw: Optional[str]) -> Optional[str]:
    """The 2-digit state code of a GSTIN, whether or not the whole thing is valid."""
    text = normalise_gstin(raw)
    if len(text) >= 2 and text[:2].isdigit():
        return text[:2]
    return None


def pan_from_gstin(raw: Optional[str]) -> Optional[str]:
    """The PAN embedded in a GSTIN (positions 3-12), or None."""
    text = normalise_gstin(raw)
    if len(text) != 15:
        return None
    pan = text[2:12]
    return pan if PAN_RE.match(pan) else None
