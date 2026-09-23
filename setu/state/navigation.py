"""Pure navigation state — which nav area is expanded.

Kept separate from ``AuthState`` so a component can read/write the nav without
depending on the auth state's inherited vars.
"""

from __future__ import annotations

import reflex as rx

from setu.routes import AREAS


class NavState(rx.State):
    # The currently expanded sidebar area. "Reconcile" is the default landing
    # area.
    active_area: str = "Reconcile"

    @rx.event
    def set_area(self, area: str):
        self.active_area = area

    @rx.event
    def toggle_area(self, area: str):
        self.active_area = area if self.active_area != area else ""

    @rx.var
    def expanded_area(self) -> str:
        """The area to render open — the user's explicit choice via the
        header click. This is a plain accordion: clicking a header opens that
        area (and, via ``toggle_area``, closes it on a second click).

        We intentionally do NOT auto-override this with the current route's
        area. Doing so made it impossible to open a *different* area than the
        one the current screen lives in (e.g. from the dashboard you could
        never expand "Setup" to reach Config, because the dashboard's
        "Reconcile" area kept winning). A screen is still always reachable —
        its page renders regardless of area state, and the user opens its
        parent area with one click.
        """
        return self.active_area