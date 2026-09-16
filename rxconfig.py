"""Reflex configuration for the Setu app (Phase 2 frontend).

The Reflex rewrite of the original Streamlit app. The Streamlit app keeps
running in parallel (`streamlit run app.py`) until each module's Reflex
rebuild is verified, per the Phase 2 migration plan.

The business logic is NOT duplicated here: every Reflex State class calls
the existing, framework-agnostic ``src/*/service.py`` public APIs. Only the
presentation layer changes.

Run with: reflex run
"""

import os
from pathlib import Path

import reflex as rx

_REPO_ROOT = Path(__file__).resolve().parent

# Reflex watches every top-level child of the app root for hot reload. This
# repo's service layer writes to db/poc.db on essentially every state touch
# (session heartbeat, last_seen_at), and the SQLite file is a top-level child
# of the app root — so each write would restart the backend, forever, which
# shows up in the browser as a flaky "Cannot connect to server" banner.
# Exclude the mutable data directories so the reloader only watches source.
os.environ.setdefault(
    "REFLEX_HOT_RELOAD_EXCLUDE_PATHS",
    ":".join(
        str(_REPO_ROOT / name) for name in ("db", "data", ".states", "reflex.lock")
    ),
)

config = rx.Config(
    app_name="setu",
    # The Streamlit PoC's flow is on 8501/8502; give the Reflex app its own
    # ports so the two run side by side during the module-by-module
    # migration.
    frontend_port=3000,
    backend_port=8000,
    # The default wildcard origin causes a duplicated Access-Control-Allow-
    # Origin header in this environment, which browsers reject for the
    # websocket handshake. Pin the dev frontend origin explicitly.
    cors_allowed_origins=["http://localhost:3000"],
    # Radix Themes is used by the Foundation components; declare it explicitly
    # (implicit enablement is deprecated in 0.9).
    plugins=[rx.plugins.RadixThemesPlugin()],
    # Sitemap adds nothing to an internal tool and prints a startup warning
    # if it isn't explicitly declared.
    disable_plugins=[rx.plugins.SitemapPlugin],
    telemetry_enabled=False,
)
