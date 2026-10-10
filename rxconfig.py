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
    # "localhost"), so the socket.io handshake must accept *any* origin --
    # otherwise the event endpoint returns HTTP 403 "not an accepted origin"
    # and login / navigation never work off the dev machine. "*" is the
    # allow-any-origin setting; keep it.
    #
    # KNOWN, MEASURED LIMITATION -- two things must be true for cross-origin
    # realtime, and one of them is not in our hands (verified with a minimal
    # reproduction of Reflex's stack, see the PR for the probe):
    #
    #   reflex/app.py:825 wraps the whole ASGI app in Starlette's
    #   CORSMiddleware, and reflex/app.py:567-573 also hands its origins to
    #   engineio, which writes its own ACAO header (engineio/base_server.py).
    #   On /_event the response therefore carries the
    #   access-control-allow-origin header TWICE, and a browser rejects a
    #   duplicated ACAO. NO value of cors_allowed_origins avoids this -- "*",
    #   ("*",) and an explicit origin list all duplicate; only
    #   transport != "websocket" (which makes Reflex pass [] to engineio) gives
    #   a single header, at the cost of downgrading realtime to HTTP polling.
    #   We keep the websocket transport: production is unaffected because Caddy
    #   fronts the frontend and backend on ONE origin, so CORS never applies.
    #
    # Consequence: the frontend must reach the backend on the same origin.
    # - Production: yes (single Caddy port 8080).
    # - Local `reflex run` on two ports: the client asks for a same-origin
    #   ws://localhost:<frontend_port>/_event and the dev server must proxy the
    #   upgrade to the backend port. Do NOT "fix" a Connection Error here by
    #   setting REFLEX_API_URL to http://localhost:8000 -- that turns the
    #   handshake cross-origin and hits the duplicate ACAO above.
    # - If your machine resolves `localhost` to ::1 only, the backend must
    #   listen on both families: `reflex run --backend-host ::`.
    cors_allowed_origins="*",
    # Radix Themes is used by the Foundation components; declare it explicitly
    # (implicit enablement is deprecated in 0.9).
    plugins=[rx.plugins.RadixThemesPlugin()],
    # Sitemap adds nothing to an internal tool and prints a startup warning
    # if it isn't explicitly declared.
    disable_plugins=[rx.plugins.SitemapPlugin],
    telemetry_enabled=False,
)
