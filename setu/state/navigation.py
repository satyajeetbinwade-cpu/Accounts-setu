"""Pure navigation state — which nav area is expanded.

Kept separate from ``AuthState`` so a component can read/write the nav without
depending on the auth state's inherited vars.
"""

from __future__ import annotations

import reflex as rx

from setu.routes import AREAS


class NavState(rx.State):
    # The currently expanded sidebar area. "Reconcile" is the default landing
    # area, matching the Streamlit app's nav reorg.
    active_area: str = "Reconcile"

    @rx.event
    def set_area(self, area: str):
        self.active_area = area

    @rx.event
    def toggle_area(self, area: str):
        self.active_area = area if self.active_area != area else ""

    @rx.var
    def current_area(self) -> str:
        """The area that owns the current route, so the sidebar can expand it
        automatically — a screen is never hidden behind a collapsed area."""
        path = self.router.page.path
        for area, screens in AREAS.items():
            for screen in screens:
                if screen.route == path:
                    return area
        return ""

    @rx.var
    def expanded_area(self) -> str:
        """The area to render open: the user's explicit choice, unless the
        current route lives in a different area (then that one wins)."""
        current = self.current_area
        if current and current != self.active_area:
            return current
        return self.active_area