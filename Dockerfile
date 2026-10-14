# syntax=docker/dockerfile:1
FROM python:3.12-slim
 
WORKDIR /app
 
# Node/npm: Reflex compiles a React frontend and needs a JS toolchain.
# The libX*/libgbm/etc libs are Chromium's runtime deps, needed for PDF export via Playwright.
# Caddy: a single static binary we use below to combine Reflex's two ports (3000
# frontend, 8000 backend) behind one public port, since Fly proxies one
# internal_port per service by default.
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm build-essential curl unzip \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Caddy: installed from the upstream GitHub release tarball rather than APT.
# The Debian `caddy` package is served by Cloudsmith's APT repo, which now
# answers 402 Payment Required and fails the build.
ARG CADDY_VERSION=2.11.7
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) caddy_arch=amd64 ;; \
        arm64) caddy_arch=arm64 ;; \
        *) echo "unsupported architecture for caddy: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/caddy.tar.gz \
        "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${caddy_arch}.tar.gz"; \
    tar -xzf /tmp/caddy.tar.gz -C /usr/bin caddy; \
    rm /tmp/caddy.tar.gz; \
    caddy version
 
# Python deps first, on their own layer, so code-only edits don't force a
# slow reinstall of pandas/pymupdf/playwright/etc on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium
 
# App source. NOTE: data/ is deliberately NOT dockerignored — the image keeps
# a pristine copy of the demo source folders so it can seed an empty volume on
# first boot (see the CMD below). db/ IS excluded (poc.db must be created on
# the volume by main.py, never shipped in the image).
COPY . .

# Stash the bundled demo data OUTSIDE /app/data. At runtime /app/data becomes a
# symlink to the mounted volume, which shadows the image's copy — so the seed
# source has to live somewhere the symlink will not cover.
RUN cp -a /app/data /app/seed_data
 
# NOTE ON PRODUCTION MODE: we originally planned to run `reflex export
# --frontend-only --no-zip` here to pre-compile the frontend at build time,
# then serve it with `reflex run --env prod`. That path is blocked: Reflex's
# production build always prerenders every route to static HTML, and the
# /review route crashes that step with "document is not defined" -- a known
# bug in the vaul drawer library's SSR handling
# (https://github.com/emilkowalski/vaul/issues/188), not something fixable
# via rxconfig.py, an env var, or editing the generated react-router config
# (it's regenerated and overwritten on every `reflex export`/`reflex run`
# invocation, so hand-edits don't survive). Dev mode never prerenders, so we
# run that instead at container startup. Trade-offs: a larger unminified
# bundle, Reflex's file-watcher runs uselessly in the background, and the
# frontend compiles once at container startup rather than at image-build
# time, adding to cold-start time. Revisit once vaul ships the SSR fix
# referenced in that issue, or the /review page's Drawer usage is reworked
# to avoid it -- then this can move back to `reflex export` + `--env prod`.
COPY Caddyfile /etc/caddy/Caddyfile
 
EXPOSE 8080
 
# main.py creates poc.db / vault.key on first boot if they don't already
# exist on the mounted volume (idempotent -- safe on every restart too).
# Caddy runs in the foreground as the container's main process and fronts
# Reflex's frontend (3000) and backend (8000) on the one port Fly exposes.
#
# NOTE: this backgrounds `reflex run` with a bare `&`, which is fine for a
# PoC but means a frontend/backend crash won't stop the container. If this
# becomes a real production deployment, swap this for a proper process
# supervisor (e.g. s6-overlay or supervisord) so both processes are
# monitored and restarted on failure.
# Fly only supports one volume per machine, so db/ and data/ (which the app
# hardcodes as separate top-level paths -- see src/db.py, src/data_paths.py)
# both live as subdirectories of the single mounted volume at /app/persist,
# symlinked into place at every boot.
#
# SEEDING: the app's Reconcile client picker is driven by the F2 client
# master (end_clients), NOT by folders under data/ — so an unseeded volume
# leaves the flow unusable (empty Client dropdown). On first boot only, the
# bundled demo folders (stashed at /app/seed_data) are seeded by
# `python -m src.demo_seed`: it creates the demo clients (with their real
# PAN/GSTIN) and files each folder under data/<client_id>/... via the shared
# path builder — so no name-keyed folder is ever created. It is idempotent, so
# it can never clobber real uploads (existing clients are reused and existing
# files are skipped). `rm -rf /app/data` removes the image's real directory
# (or a stale symlink) so `ln -s` always yields a clean symlink.
#
# FAIL-LOUD: the CMD is one `&&` chain. `python main.py` runs the boot
# sequence, which includes the client-id migration; if it raises, the chain
# stops — Reflex and Caddy are NEVER started — and the container exits
# non-zero, so Fly keeps the previous release serving instead of bringing up a
# half-initialised app.
#
# SETU_ALLOW_BOOT_MIGRATE=1 is what lets this bare `python main.py` migrate.
# Without it main.py refuses (exit 2) and changes nothing — that is the guard
# against an accidental `python main.py --help` migrating a live database from
# a laptop. The application itself never migrates: `reflex run` only checks the
# schema version and aborts with "run: python main.py --migrate" if it is
# behind. So a deploy is still the ONLY path that migrates, and it still fails
# closed.
#
# REFLEX_HOT_RELOAD_EXCLUDE_PATHS is set explicitly (colon-separated, per
# Reflex's env var format) rather than relying on rxconfig.py's own
# setdefault for db/data/.states/reflex.lock: the dev-mode file watcher
# resolves the db/data symlinks to their real target under persist/, which
# isn't covered by those excluded names, so every SQLite write to poc.db
# was being treated as a source change and triggering a restart loop.
CMD ["/bin/sh", "-c", "mkdir -p /app/persist/db /app/persist/data && rm -rf /app/data && ln -s /app/persist/data /app/data && ln -sfn /app/persist/db /app/db && export REFLEX_HOT_RELOAD_EXCLUDE_PATHS=\"db:data:.states:reflex.lock:persist\" && export __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=\"setu-poc.fly.dev\" && export SETU_ALLOW_BOOT_MIGRATE=1 && python main.py && python -m src.demo_seed && (reflex run &) && exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"]
