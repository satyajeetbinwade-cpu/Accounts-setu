"""Shared Client / Period / Recon-type context selection.

Every screen that opens a run or an operation picks the same three things: a
client, a period and a recon type. Two rules apply to all of them:

1. **No default is ever pre-selected.** The user must choose deliberately, so
   a screen can never silently operate on the wrong client or period.
2. **The period is chosen, not scanned.** It is picked as a Year + Month
   (``YYYY-MM``), NOT read back from the folders that happen to exist under
   ``data/<client>/<period>/``. A period with no data yet is a valid choice —
   the upload step creates its folder on demand.

``PeriodPickerMixin`` owns the Year/Month pair and projects it onto the single
``ctx_period`` string in ``YYYY-MM`` form that the rest of the app (folder
paths, run records, review state) already uses, so nothing downstream changes.
It is deliberately a Reflex *mixin* (``mixin=True``): it contributes its vars,
computed vars and event handlers to whichever state class mixes it in, without
being a state of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import reflex as rx

from src.shared import discovery

MONTH_NAMES: list[str] = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


@dataclass
class MonthOption:
    """One Month dropdown entry. ``disabled`` is True for a month later than
    the current month in the current year, so a future period can't be picked."""

    month: str
    label: str
    disabled: bool


def period_from_parts(year: str, month: str) -> str:
    """Compose the single ``YYYY-MM`` value from its two parts.

    Returns ``""`` until BOTH a valid 4-digit year and a 1–12 month are chosen,
    and zero-pads the month (``2026-08``, never ``2026-8``).
    """
    y = str(year or "").strip()
    m = str(month or "").strip()
    if len(y) != 4 or not y.isdigit():
        return ""
    if not m.isdigit():
        return ""
    month_int = int(m)
    if not 1 <= month_int <= 12:
        return ""
    return f"{y}-{month_int:02d}"


def split_period(period: str) -> tuple[str, str]:
    """Split a stored ``YYYY-MM`` back into ``(year, month)`` strings.

    Returns ``("", "")`` for anything that is not a real period (the unfiled
    sentinel, an empty value, a malformed string), so the picker simply shows
    both placeholders again.
    """
    text = str(period or "").strip()
    if len(text) == 7 and text[4] == "-" and text[:4].isdigit() and text[5:].isdigit():
        month_int = int(text[5:])
        if 1 <= month_int <= 12:
            return text[:4], f"{month_int:02d}"
    return "", ""


def month_is_valid(year: str, month: str, *, now: datetime | None = None) -> bool:
    """Whether ``month`` may be chosen for ``year``.

    A month later than the current month in the current year is a future
    period and is not valid.
    """
    if not month:
        return False
    try:
        month_int = int(month)
    except (TypeError, ValueError):
        return False
    if not 1 <= month_int <= 12:
        return False
    today = now or datetime.now()
    if year == str(today.year) and month_int > today.month:
        return False
    return True


def month_options(year: str, *, now: datetime | None = None) -> list[MonthOption]:
    """January–December in calendar order, for the given ``year``.

    In the current year, months after the current month are disabled so a
    future period can't be chosen. Shared by the mixin and by any state that
    needs a second independent Year/Month pair.
    """
    today = now or datetime.now()
    current_year = str(today.year)
    return [
        MonthOption(
            month=f"{i:02d}",
            label=MONTH_NAMES[i - 1],
            disabled=(year == current_year and i > today.month),
        )
        for i in range(1, 13)
    ]


def normalize_after_year_change(
    year: str, month: str, *, now: datetime | None = None
) -> str:
    """The month to keep after the Year changes.

    Returns the month when it is still valid for the new year, and ``""`` when
    it is not (a month that is now in the future because the new year is the
    current one). Pure, so the clearing rule is directly testable.
    """
    if month and month_is_valid(year, month, now=now):
        return month
    return ""


class PeriodPickerMixin(rx.State, mixin=True):
    """The Year + Month halves of the period context, plus the handlers that
    keep them in step with the single ``ctx_period`` value.

    The concrete screen state must itself declare ``ctx_period`` (it already
    does); this mixin only writes it. When the composed period changes it calls
    ``_on_period_changed(period)``, which subclasses override to reload the
    state that depends on the period.
    """

    ctx_year: str = ""
    ctx_month: str = ""

    @rx.var
    def year_options(self) -> list[str]:
        """Descending years, current year first, down to ``discovery.EARLIEST_YEAR``."""
        return [str(y) for y in discovery.available_years()]

    @rx.var
    def month_options(self) -> list[MonthOption]:
        """January–December in calendar order. In the current year, months
        after the current month are disabled."""
        return month_options(self.ctx_year)

    @rx.var
    def period_selected(self) -> bool:
        """True only once BOTH Year and Month are chosen."""
        return bool(self.ctx_year and self.ctx_month)

    @rx.event
    def set_ctx_year(self, value: str):
        """Choose a year. Clears the month if it is no longer valid (a future
        month of the current year)."""
        self.ctx_year = value
        self.ctx_month = normalize_after_year_change(value, self.ctx_month)
        self._sync_ctx_period()

    @rx.event
    def set_ctx_month(self, value: str):
        """Choose a month. Ignores a future month in the current year."""
        if value and not month_is_valid(self.ctx_year, value):
            return
        self.ctx_month = value
        self._sync_ctx_period()

    def reset_period(self) -> None:
        """Clear the period parts and the composed value (used when the client
        changes; the caller reloads dependent state afterwards)."""
        self.ctx_year = ""
        self.ctx_month = ""
        self.ctx_period = ""

    def _sync_ctx_period(self) -> None:
        period = period_from_parts(self.ctx_year, self.ctx_month)
        if period == self.ctx_period:
            return
        self.ctx_period = period
        self._on_period_changed(period)

    def _on_period_changed(self, period: str) -> None:
        """Hook for subclasses — reload whatever depends on the period."""
