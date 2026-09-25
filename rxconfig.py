"""Reflex configuration for the Setu app.

The Reflex frontend is the app's only UI. The business logic is NOT
implemented here: every Reflex State class calls the existing,
framework-agnostic ``src/*/service.py`` public APIs. Only the presentation
layer lives under ``setu/``.

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
    frontend_port=int(os.environ.get('FRONTEND_PORT', 3000)),
    backend_port=8000,
    # Remote testers hit this app through the server's IP/hostname (not
    # "localhost"), so the browser's websocket handshake to the backend must
    # accept *any* origin — otherwise the socket.io event endpoint returns
    # HTTP 403 "not an accepted origin" and login / navigation never work for
    # anyone off the dev machine. A single "*" maps to engineio's
    # "allow any origin" branch (properly reflects the request origin with
    # credentials rather than duplicating the ACAO header), so this handles
    # both local and remote access in one setting.
    cors_allowed_origins="*",
    # Radix Themes is used by the Foundation components; declare it explicitly
    # (implicit enablement is deprecated in 0.9).
    plugins=[rx.plugins.RadixThemesPlugin()],
    # Sitemap adds nothing to an internal tool and prints a startup warning
    # if it isn't explicitly declared.
    disable_plugins=[rx.plugins.SitemapPlugin],
    telemetry_enabled=False,
)
