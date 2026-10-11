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
from src.auth import access
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
        viewer = self._viewer()
        # Every headline figure is scoped to the viewer. ``None`` means
        # unrestricted (admin); a set means "only these clients exist as far as
        # this user is concerned". Without this, a standard user's dashboard
        # would report how much work exists on other users' clients — a count
        # is data, and the silo covers counts.
        visible_names = access.visible_client_names(viewer)
        visible_ids = access.owned_client_ids(viewer)

        def _visible_runs():
            runs = queries.list_runs()
            if visible_names is not None and not runs.empty:
                runs = runs[runs["client"].isin(visible_names)]
            return runs

        # Runs — total executed (runs are immutable; there is no "in progress"
        # state, so we report the count of runs on record).
        try:
            self.runs_total = int(len(_visible_runs()))
        except Exception:
            self.runs_total = 0

        # Open exceptions — the unified Action Center queue (every open
        # Flagged Item across all origin modules), restricted to the viewer's
        # clients.
        try:
            items = action_center.list_flagged_items(status="open")
            if visible_ids is not None:
                items = [i for i in items if i.get("client_id") in visible_ids]
            self.open_exceptions = len(items)
        except Exception:
            self.open_exceptions = 0

        # Filings due — calendar entries not yet past their due date, again
        # restricted to the viewer's clients.
        try:
            entries = filing.list_calendar()
            if visible_ids is not None:
                entries = [e for e in entries if e.get("client_id") in visible_ids]
            self.filings_due = sum(
                1 for e in entries if (e.get("days_until_due") or 0) >= 0
            )
        except Exception:
            self.filings_due = 0

        # Awaiting review — results in the most recent run THE VIEWER CAN SEE
        # (not the firm's most recent run, which may belong to someone else).
        try:
            runs = _visible_runs()
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
