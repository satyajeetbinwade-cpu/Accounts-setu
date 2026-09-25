"""Invoice-number key derivation — pure, config-driven, GSTIN-scoped.

WHY THIS EXISTS
---------------
A portal export and a books export rarely spell the same invoice number the
same way. The portal appends a financial-year suffix ("T11439/26-27",
"RL/2915/26-27", "JSIT-03499/26-27"), sometimes a supplier prefix
("PD/26-27/3386", "MF/26-27/1913"), and sometimes leading zeros; the books
export holds the bare number ("T11439", "2915", "3499", "1913").

The previous key derivation normalised the raw string but did NOT strip the
FY suffix, so the trailing-numeric key of "T11439/26-27" was "27" — the
financial year, not the invoice. Two different invoices from the same
supplier then collided on "27" and matched each other (D3).

This module is the single, pure implementation of the required rules:

  * strip FY tokens FIRST (patterns come from config, never from code);
  * normalised key = uppercase alphanumerics, separators removed, leading
    zeros stripped from each digit run;
  * variant key = the numeric core (the last digit run after FY stripping);
  * keys are scoped to a GSTIN — the same number from two suppliers is not
    a collision;
  * any key that collides within one GSTIN on either side is REPORTED, never
    silently used.

Every function here is pure: no I/O, no globals, no config file reads. The
FY patterns are passed in by the caller (from matching_rules.yaml), so a new
FY format is a config change, not a code change.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional

_SEPARATORS_RE = re.compile(r"[\s/\\\-_.]+")
_DIGIT_RUN_RE = re.compile(r"\d+")
_LEADING_ZEROS_RE = re.compile(r"(?<!\d)0+(\d)")


def strip_fy_tokens(raw: Any, fy_patterns: Iterable[str]) -> str:
    """Remove financial-year tokens from an invoice number.

    Applied BEFORE any key is derived, so the trailing-numeric key can never
    pick the "27" out of a "26-27" suffix. Patterns are regexes supplied by
    config; an invalid pattern is skipped rather than raising, so one bad
    config entry cannot break matching entirely.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    for pattern in fy_patterns or ():
        try:
            s = re.sub(pattern, "", s, flags=re.IGNORECASE)
        except re.error:
            continue
    return s


def normalize_invoice_key(raw: Any, fy_patterns: Iterable[str]) -> str:
    """The primary matching key: uppercase alphanumerics, separators removed,
    leading zeros stripped from each digit run.

    "T11439/26-27" -> "T11439"; "RL/2915/26-27" -> "RL2915";
    "JSIT-03499/26-27" -> "JSIT3499"; "2915" -> "2915".
    """
    s = strip_fy_tokens(raw, fy_patterns).upper()
    s = _SEPARATORS_RE.sub("", s)
    s = _LEADING_ZEROS_RE.sub(r"\1", s)
    return s


def variant_invoice_key(raw: Any, fy_patterns: Iterable[str]) -> Optional[str]:
    """The variant key: the numeric core of the invoice number — the LAST
    digit run after FY stripping, with leading zeros removed.

    "T11439" -> "11439"; "RL/2915" -> "2915"; "JSIT-03499" -> "3499";
    "PD/3386" -> "3386". Returns None when the number carries no digits.
    """
    s = strip_fy_tokens(raw, fy_patterns)
    runs = _DIGIT_RUN_RE.findall(s)
    if not runs:
        return None
    return runs[-1].lstrip("0") or "0"


def _gstin_of(record: dict[str, Any], gstin_field: str) -> str:
    return str(record.get(gstin_field) or "").strip().upper()


def find_key_collisions(
    records: Iterable[dict[str, Any]],
    key_fn: Callable[[Any], Optional[str]],
    *,
    gstin_field: str = "gstin",
    number_field: str = "invoice_number",
    side: str = "",
) -> list[dict[str, Any]]:
    """Report keys that collide within one GSTIN on one side.

    A collision means two DIFFERENT raw invoice numbers derive the same key
    for the same supplier — the key is then unsafe to match on, because it
    cannot tell the two invoices apart. The caller must surface these rather
    than silently using the key.

    Returns one entry per colliding key:
        {gstin, key, side, numbers: [raw, ...]}
    """
    buckets: dict[tuple[str, str], list[str]] = {}
    for rec in records:
        key = key_fn(rec.get(number_field))
        if not key:
            continue
        gstin = _gstin_of(rec, gstin_field)
        buckets.setdefault((gstin, key), []).append(str(rec.get(number_field) or ""))

    out: list[dict[str, Any]] = []
    for (gstin, key), numbers in buckets.items():
        distinct = sorted({n for n in numbers})
        if len(distinct) > 1:
            out.append({"gstin": gstin, "key": key, "side": side, "numbers": distinct})
    return out
