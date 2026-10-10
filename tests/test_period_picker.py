"""Period picker — value format and Year/Month rules.

The period picker exposes ONE value in the existing ``"YYYY-MM"`` format so
folder paths, run records and review state keep working unchanged. These
tests pin that contract: zero-padded months, empty until BOTH parts are
chosen, no future month in the current year, and a descending year list that
offers every year down to a single fixed floor (independent of what data
happens to exist).
"""

from __future__ import annotations

from datetime import datetime

from src.shared import discovery
from setu.state.context_selectors import (
    MONTH_NAMES,
    MonthOption,
    PeriodPickerMixin,
    month_is_valid,
    month_options,
    normalize_after_year_change,
    period_from_parts,
    split_period,
)

# --- period_from_parts: the "YYYY-MM" contract -----------------------------


def test_month_is_zero_padded():
    assert period_from_parts("2026", "8") == "2026-08"


def test_already_padded_month_is_unchanged():
    assert period_from_parts("2026", "08") == "2026-08"


def test_december_is_two_digits():
    assert period_from_parts("2026", "12") == "2026-12"


def test_empty_until_both_parts_chosen():
    assert period_from_parts("", "") == ""
    assert period_from_parts("2026", "") == ""
    assert period_from_parts("", "08") == ""


def test_year_must_be_four_digits():
    assert period_from_parts("26", "08") == ""
    assert period_from_parts("abcd", "08") == ""


def test_month_out_of_range_is_rejected():
    assert period_from_parts("2026", "0") == ""
    assert period_from_parts("2026", "13") == ""


# --- split_period: the reverse direction ------------------------------------


def test_split_round_trips_a_real_period():
    assert split_period("2026-08") == ("2026", "08")


def test_split_returns_blanks_for_non_periods():
    for value in ("", "-", "2026", "2026-8", "garbage"):
        assert split_period(value) == ("", "")


# --- month_is_valid / month_options: no future periods ----------------------


def test_future_month_of_current_year_is_invalid():
    now = datetime(2026, 8, 10)
    assert month_is_valid("2026", "08", now=now) is True
    assert month_is_valid("2026", "09", now=now) is False


def test_past_year_months_are_all_valid():
    now = datetime(2026, 8, 10)
    assert month_is_valid("2025", "12", now=now) is True


def test_month_options_lists_twelve_calendar_months():
    opts = month_options("2026", now=datetime(2026, 8, 10))
    assert len(opts) == 12
    assert [o.label for o in opts] == MONTH_NAMES
    assert [o.month for o in opts] == [f"{i:02d}" for i in range(1, 13)]


def test_month_options_disable_future_months_only_in_current_year():
    opts = month_options("2026", now=datetime(2026, 8, 10))
    disabled = {o.month for o in opts if o.disabled}
    assert disabled == {"09", "10", "11", "12"}

    past = month_options("2025", now=datetime(2026, 8, 10))
    assert all(not o.disabled for o in past)


# --- changing the Year clears an invalid Month ------------------------------


def test_month_is_kept_when_still_valid_after_year_change():
    now = datetime(2026, 8, 10)
    assert normalize_after_year_change("2025", "12", now=now) == "12"
    assert normalize_after_year_change("2026", "08", now=now) == "08"


def test_month_cleared_when_it_became_a_future_month():
    now = datetime(2026, 8, 10)
    # December is fine in 2025 but a future month once the year is 2026.
    assert normalize_after_year_change("2026", "12", now=now) == ""


def test_month_stays_empty_when_none_chosen():
    assert normalize_after_year_change("2026", "", now=datetime(2026, 8, 10)) == ""


# --- the mixin projects the two parts onto ctx_period -----------------------


class _FakeState:
    """Stand-in for a state that mixes in PeriodPickerMixin — the same
    unbound-method technique the existing Reconcile tests use."""

    def __init__(self, year="", month="", period=""):
        self.ctx_year = year
        self.ctx_month = month
        self.ctx_period = period
        self.changes: list[str] = []

    def _on_period_changed(self, period: str) -> None:
        self.changes.append(period)


def test_sync_sets_the_single_value_and_notifies():
    state = _FakeState(year="2026", month="8")
    PeriodPickerMixin._sync_ctx_period(state)
    assert state.ctx_period == "2026-08"
    assert state.changes == ["2026-08"]


def test_sync_clears_the_value_when_a_part_is_missing():
    state = _FakeState(year="2026", month="8", period="2026-08")
    state.ctx_month = ""
    PeriodPickerMixin._sync_ctx_period(state)
    assert state.ctx_period == ""
    assert state.changes == [""]


def test_sync_is_silent_when_the_value_did_not_change():
    state = _FakeState(year="2026", month="08", period="2026-08")
    PeriodPickerMixin._sync_ctx_period(state)
    assert state.changes == []


def test_a_half_chosen_period_never_advances_the_value():
    """Both parts are compulsory: a year on its own (and a month on its own)
    must leave ``ctx_period`` empty, so nothing downstream can proceed."""
    year_only = _FakeState(year="2026", month="")
    PeriodPickerMixin._sync_ctx_period(year_only)
    assert year_only.ctx_period == ""

    month_only = _FakeState(year="", month="08")
    PeriodPickerMixin._sync_ctx_period(month_only)
    assert month_only.ctx_period == ""


# --- available_years: all years, descending, current-first ------------------


def test_available_years_starts_at_the_current_year():
    years = discovery.available_years(current_year=2026)
    assert years[0] == 2026


def test_available_years_is_descending():
    years = discovery.available_years(current_year=2026)
    assert years == sorted(years, reverse=True)


def test_available_years_offers_every_year_down_to_the_floor():
    years = discovery.available_years(current_year=2026)
    assert years == list(range(2026, discovery.EARLIEST_YEAR - 1, -1))


def test_available_years_goes_back_before_2000():
    # The floor is deliberately below 2000 so older periods can be chosen.
    assert discovery.EARLIEST_YEAR < 2000
    assert 1999 in discovery.available_years(current_year=2026)


def test_available_years_is_independent_of_existing_data():
    # No monkeypatching of any evidence scan: the list is the same whether or
    # not the workspace happens to hold data for a given year.
    assert discovery.available_years(current_year=2026) == discovery.available_years(
        current_year=2026
    )


def test_available_years_never_offers_a_future_year():
    assert max(discovery.available_years(current_year=2026)) == 2026
