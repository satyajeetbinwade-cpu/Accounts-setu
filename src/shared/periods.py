"""Single source of truth for the engine's period format.

The reconciliation engine's folder contract is exactly ``YYYY-MM``
(``data/<client_id>/<period>/<source_type>/``). Every layer that parses,
formats or validates a period must go through this module — the upload
form, the discovery/period picker, the run engine and the migration all
agree here, so a malformed period can never reach a path.

The ``"-"`` sentinel (``PERIOD_UNFILED``) is preserved for files that were
filed without a period: it is a legitimate *stored* value that the UI labels
and that no reconciliation can select. ``is_valid_period`` is False for it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

# The sentinel folder used when a file was filed without a period.
PERIOD_UNFILED = "-"

# Human label for the sentinel (a bare "-" reads as broken, not "unfiled").
UNFILED_LABEL = "Unfiled (not reconcilable)"

# A real, reconcilable period. Deliberately strict: YYYY-MM only.
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# The floor of the Year dropdown (shared by discovery.available_years).
EARLIEST_YEAR = 1980


def is_valid_period(period: Optional[str]) -> bool:
    """Whether a string is a real, reconcilable ``YYYY-MM`` period."""
    return bool(period) and bool(_PERIOD_RE.match(str(period).strip()))


def is_unfiled(period: Optional[str]) -> bool:
    """Whether a period is the unfiled sentinel (or absent)."""
    return not period or str(period).strip() in ("", PERIOD_UNFILED)


def period_label(period: Optional[str]) -> str:
    """Human label for a period value — the sentinel reads as unfiled."""
    return UNFILED_LABEL if is_unfiled(period) else str(period)


def parse_period(period: Optional[str]) -> Optional[tuple[int, int]]:
    """Split a valid period into ``(year, month)``, else ``None``."""
    if not is_valid_period(period):
        return None
    text = str(period).strip()
    year, month = text.split("-")
    return int(year), int(month)


def format_period(year: int, month: int) -> Optional[str]:
    """Format ``(year, month)`` as ``YYYY-MM``, or ``None`` if out of range.

    The 2000..2100 window is the only range GST-era documents realistically
    use; anything outside it is a parsing error, not a real period.
    """
    try:
        y = int(year)
        m = int(month)
    except (TypeError, ValueError):
        return None
    if not (2000 <= y <= 2100 and 1 <= m <= 12):
        return None
    return f"{y:04d}-{m:02d}"


def normalize_period(period: Optional[str]) -> Optional[str]:
    """Best-effort canonicalisation of an *existing* stored period.

    Accepts the strict form, and the common near-misses a hand-typed value
    produces: ``YYYY-M`` (single-digit month), surrounding whitespace, and
    ``/`` or ``.`` separators. Returns ``None`` when the value is not a
    period at all (including the unfiled sentinel, which is handled by
    ``is_unfiled`` and must be preserved, not "fixed").
    """
    if period is None:
        return None
    text = str(period).strip()
    if text in ("", PERIOD_UNFILED):
        return None
    m = re.match(r"^(\d{4})[-/.](\d{1,2})$", text)
    if not m:
        return None
    return format_period(int(m.group(1)), int(m.group(2)))


def require_valid_period(period: Optional[str]) -> str:
    """Return the period, or raise ``ValueError`` with a clear message.

    Used at service boundaries — most importantly inside ``source_data_path``
    — so a malformed period can never be turned into a filesystem path.
    """
    if not is_valid_period(period):
        raise ValueError(
            f"Invalid period {period!r}; expected 'YYYY-MM' "
            f"(e.g. '2026-08'), or the unfiled sentinel {PERIOD_UNFILED!r} "
            "only where the caller explicitly allows it."
        )
    return str(period).strip()


def current_period(now: Optional[datetime] = None) -> str:
    """The current ``YYYY-MM``."""
    n = now or datetime.now()
    return format_period(n.year, n.month)  # type: ignore[return-value]


def available_years(current_year: Optional[int] = None) -> list[int]:
    """Years for the Year dropdown: current down to ``EARLIEST_YEAR``.

    Independent of what data exists so a period can be chosen before any
    folder or run exists for it. Never offers a future year.
    """
    now_year = int(current_year) if current_year else datetime.now().year
    floor = min(EARLIEST_YEAR, now_year)
    return list(range(now_year, floor - 1, -1))
