"""Period derivation for Smart Document Ingestion.

WHY THIS EXISTS
---------------
The reconciliation engine reads source files from
``data/<client>/<period>/<source_type>/`` and the Run/Reconcile pickers list
the sub-directories of ``data/<client>/`` as the available periods. A file
filed under a period that is not a real ``YYYY-MM`` (historically the literal
``"-"`` written when the upload form's optional Period field was left blank)
is therefore INVISIBLE to every reconciliation — it exists on disk but no
period dropdown can ever select it.

This module derives a best-effort ``YYYY-MM`` period from what the file
itself says, so the upload form can pre-fill a suggestion instead of
silently writing an unusable folder. It is deliberately:

- **stdlib-only** (``re`` + ``datetime``) so it is import-safe from any layer
  and unit-testable without pandas/Reflex;
- **honest** — it returns ``None`` rather than guessing when there is no
  signal, so the caller can require a human to supply the period;
- **metadata-first** — a period the file states about itself (a books
  register's "01 Aug 2026 to 31 Aug 2026" header, or a portal export's
  return-period column) always beats a guess from the filename.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

# The sentinel folder used when a file was filed without a period. Kept as a
# named constant so the UI, the discovery layer and the repair action all
# agree on the exact string.
PERIOD_UNFILED = "-"

# Human label for the sentinel, shown in period pickers instead of a bare
# "-" so an unfiled file is visibly unusable rather than mysteriously absent.
UNFILED_LABEL = "Unfiled (not reconcilable)"

# A real, reconcilable period. Deliberately strict: the engine's folder
# contract is exactly YYYY-MM.
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "01 Aug 2026", "1-Aug-2026", "01/08/2026", "2026-08-01" — the shapes a
# books register's period header realistically uses.
_DATE_PATTERNS = (
    re.compile(r"(?P<d>\d{1,2})[-/\s](?P<mon>[A-Za-z]{3,9})[-/\s](?P<y>\d{4})"),
    re.compile(r"(?P<d>\d{1,2})[-/](?P<m>\d{1,2})[-/](?P<y>\d{4})"),
    re.compile(r"(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})"),
)

# "Aug'26", "Aug-25", "August 2026" — a portal export's return period.
_MONTH_YEAR_PATTERNS = (
    re.compile(r"(?P<mon>[A-Za-z]{3,9})[\s'\-/]*(?P<y>\d{2,4})"),
)

# Filename shapes: "2026-08", "202608", "082026", "Aug'25", "Aug-2025".
_FN_ISO = re.compile(r"(?<!\d)(?P<y>20\d{2})[-_](?P<m>0[1-9]|1[0-2])(?!\d)")
_FN_YYYYMM = re.compile(r"(?<!\d)(?P<y>20\d{2})(?P<m>0[1-9]|1[0-2])(?!\d)")
_FN_MMYYYY = re.compile(r"(?<!\d)(?P<m>0[1-9]|1[0-2])(?P<y>20\d{2})(?!\d)")
_FN_MON_YY = re.compile(r"(?<![A-Za-z])(?P<mon>[A-Za-z]{3,9})[\s'\-_/]*(?P<y>\d{2,4})(?!\d)")


def is_valid_period(period: Optional[str]) -> bool:
    """Whether a string is a real, reconcilable ``YYYY-MM`` period."""
    return bool(period) and bool(_PERIOD_RE.match(str(period).strip()))


def is_unfiled(period: Optional[str]) -> bool:
    """Whether a period is the unfiled sentinel (or absent)."""
    return not period or str(period).strip() in ("", PERIOD_UNFILED)


def period_label(period: Optional[str]) -> str:
    """Human label for a period value — the sentinel reads as unfiled."""
    return UNFILED_LABEL if is_unfiled(period) else str(period)


def _fmt(year: int, month: int) -> Optional[str]:
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    return f"{year:04d}-{month:02d}"


def _expand_year(raw: str) -> Optional[int]:
    """'26' -> 2026, '2026' -> 2026. Two-digit years are 2000-based, which
    is the only sensible reading for GST-era documents."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 100:
        return 2000 + n
    return n


def _month_from_name(name: str) -> Optional[int]:
    return _MONTHS.get(str(name).strip().lower()[:3])


def period_from_date_string(value: Any) -> Optional[str]:
    """Derive ``YYYY-MM`` from a single date-ish string.

    Handles ISO, DD-MM-YYYY, DD/MM/YYYY, DD-Mon-YYYY and Mon-YY forms. Used
    for a books register's ``period_start`` (already ISO) and for any raw
    date text.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Already ISO YYYY-MM-DD (or YYYY-MM).
    iso = re.match(r"^(?P<y>\d{4})-(?P<m>\d{2})(?:-\d{2})?$", text)
    if iso:
        return _fmt(int(iso.group("y")), int(iso.group("m")))

    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groupdict()
        year = _expand_year(groups.get("y") or "")
        if year is None:
            continue
        if groups.get("mon"):
            month = _month_from_name(groups["mon"])
        else:
            try:
                month = int(groups.get("m") or 0)
            except (TypeError, ValueError):
                month = 0
        if month:
            return _fmt(year, month)

    for pattern in _MONTH_YEAR_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        month = _month_from_name(m.group("mon"))
        year = _expand_year(m.group("y"))
        if month and year:
            return _fmt(year, month)
    return None


def period_from_metadata(metadata: Optional[dict[str, Any]]) -> Optional[str]:
    """Derive the period from the file's OWN stated metadata.

    Priority:
    1. ``period_start`` — a books register's header period (ISO date).
    2. ``period_end`` — used only when ``period_start`` is absent.
    3. ``gstr1_period`` — a portal export's return-period column, already
       normalised to ``YYYY-MM`` by the F6 ``parse_period`` transform.

    Returns ``None`` when the file states nothing usable.
    """
    if not metadata:
        return None
    for key in ("period_start", "period_end"):
        derived = period_from_date_string(metadata.get(key))
        if derived:
            return derived
    stated = metadata.get("gstr1_period")
    if stated:
        text = str(stated).strip()
        if is_valid_period(text):
            return text
        return period_from_date_string(text)
    return None


def period_from_filename(filename: Optional[str]) -> Optional[str]:
    """Derive ``YYYY-MM`` from a filename.

    Tries, in order: ISO (``2026-08``), ``YYYYMM`` (``202608``), ``MMYYYY``
    (``082026`` — the GSTN GSTR-2B download convention), then ``Mon-YY``
    (``Aug'25``). Returns ``None`` when the name carries no period signal.

    NOTE: a filename is a WEAKER signal than the file's own metadata — a
    download date (``_15092026``) is not the return period. Callers should
    treat a filename-derived period as a suggestion to confirm, never as
    authoritative.
    """
    if not filename:
        return None
    name = str(filename)

    m = _FN_ISO.search(name)
    if m:
        return _fmt(int(m.group("y")), int(m.group("m")))

    m = _FN_YYYYMM.search(name)
    if m:
        return _fmt(int(m.group("y")), int(m.group("m")))

    m = _FN_MMYYYY.search(name)
    if m:
        return _fmt(int(m.group("y")), int(m.group("m")))

    m = _FN_MON_YY.search(name)
    if m:
        month = _month_from_name(m.group("mon"))
        year = _expand_year(m.group("y"))
        if month and year:
            return _fmt(year, month)
    return None


def suggest_period(
    filename: Optional[str], stored_result: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], str]:
    """Best-effort period suggestion for an upload.

    Returns ``(period, source)`` where ``source`` is a short human phrase
    naming the evidence — ``"the file's stated period"``,
    ``"the file's return period"``, ``"the filename"`` or ``""`` when there
    is no signal at all. The caller shows the source so a suggestion is
    never mistaken for a confirmed fact.
    """
    metadata = (stored_result or {}).get("metadata") or {}
    derived = period_from_metadata(metadata)
    if derived:
        if metadata.get("period_start") or metadata.get("period_end"):
            return derived, "the file's stated period"
        return derived, "the file's return period"

    derived = period_from_filename(filename)
    if derived:
        return derived, "the filename"
    return None, ""


def today_period() -> str:
    """The current calendar month as ``YYYY-MM`` — the last-resort default
    offered when a file carries no period signal at all."""
    now = datetime.now()
    return f"{now.year:04d}-{now.month:02d}"