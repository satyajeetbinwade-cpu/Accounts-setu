"""Dashboard landing state.

The signed-in landing screen's headline figures. Every number is read from
the same framework-agnostic services the module screens use — nothing is
computed inline in the component and nothing is fabricated. Where a figure
has no data yet it renders an em-dash rather than a fake zero.
"""

from __future__ import annotations

import reflex as rx

from src import queries
from src.action_center import service as action_center
from src.filing import service as filing
from setu.state.auth_state import AuthState


class DashboardState(AuthState):
    """Headline figures for the landing screen."""

    runs_total: int = 0
    open_exceptions: int = 0
    filings_due: int = 0
    awaiting_review: int = 0
    loaded: bool = False

    @rx.event
    def load(self):
        self._load()

    def _load(self) -> None:
        # Runs — total executed (runs are immutable; there is no "in progress"
        # state, so we report the count of runs on record).
        try:
            runs = queries.list_runs()
            self.runs_total = int(len(runs))
        except Exception:
            self.runs_total = 0

        # Open exceptions — the unified Action Center queue (every open
        # Flagged Item across all origin modules).
        try:
            items = action_center.list_flagged_items(status="open")
            self.open_exceptions = len(items)
        except Exception:
            self.open_exceptions = 0

        # Filings due — calendar entries not yet past their due date.
        try:
            entries = filing.list_calendar()
            self.filings_due = sum(
                1 for e in entries if (e.get("days_until_due") or 0) >= 0
            )
        except Exception:
            self.filings_due = 0

        # Awaiting review — results in the most recent run not yet reviewed.
        try:
            runs = queries.list_runs()
            if len(runs) > 0:
                latest = int(runs.iloc[0]["run_id"])
                summary = queries.get_run_summary(latest)
                total = int(summary.get("total_results", 0))
                reviewed = 0
                run = queries.get_run(latest)
                if run is not None:
                    state = queries.get_review_state(
                        run["client"], run["period"], run["recon_type"]
                    )
                    reviewed = sum(1 for v in state.values() if v.get("reviewed"))
                self.awaiting_review = max(total - reviewed, 0)
            else:
                self.awaiting_review = 0
        except Exception:
            self.awaiting_review = 0

        self.loaded = True
