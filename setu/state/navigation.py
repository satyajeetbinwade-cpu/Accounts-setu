"""Pure navigation state — which nav area is expanded.

Kept separate from ``AuthState`` so a component can read/write the nav without
depending on the auth state's inherited vars.
"""

from __future__ import annotations

import reflex as rx


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