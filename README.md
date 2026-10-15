# Setu — Reconciliation Platform

**Setu** is a GST / TDS / "Other" source reconciliation platform for accounting firms. It ingests a client's books and portal downloads (GSTR-2B, IMS, Form 26AS, TDS challans, bank statements, etc.), runs them through a fingerprint-based matching engine, classifies every record (Matched / Amount Difference / Not in Books / Not in Portal, with sub-classifications like *Short Deduction* and *Rounding*), carries review status across re-runs, and produces a polished, auditable report.

The codebase is organized around a **framework-agnostic service layer** (`src/`) that the frontend calls — the business logic is never duplicated in the UI.

---

## Table of contents

- [What's in this repo](#whats-in-this-repo)
- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Local development setup](#local-development-setup)
- [Running the app](#running-the-app)
- [Deploying to a remote server](#deploying-to-a-remote-server)
- [Login & security](#login--security)
  - [Multi-user access and data silos](#multi-user-access-and-data-silos)
- [Configuration files](#configuration-files)
- [Directory layout](#directory-layout)
- [Verification harness (CLI)](#verification-harness-cli)
- [Troubleshooting](#troubleshooting)

---

## What's in this repo

There is **one frontend** — the Reflex app — which shares the engine with the CLI tools:

| Entry point | Framework | Run with | Purpose |
|-------------|-----------|----------|---------|
| `setu/setu.py` | Reflex | `reflex run` | **The production frontend** (full module suite, sidebar navigation) |
| `main.py` | CLI | `python main.py --migrate` | Boot the database: schema, seeds, migrations (`--check` is the read-only report) |
| `verify.py` | CLI | `python verify.py ...` | Baseline/verification harness |

The **service layer** under `src/` is shared by the frontend and the CLI tools. Every module follows the same shape:

```
src/
  <module>/
    service.py   # public API every UI imports
    db.py        # data access
    schema.py    # SQLite DDL
    seed.py      # first-run seed data (additive-only, never resets admin edits)
```

---

## How it works

1. **Ingestion** — source files live under `data/{client}/{period}/{source_type}/`. The `tally` directory is the *books side*; everything else (`gstr2b`, `ims`, `form26as`, `tds`, `bank`, …) is the *portal side*. Smart Ingestion (`F3-AI`) can map raw columns automatically.

2. **Matching engine** (`src/runner.py` + `src/matching/`) — a run is immutable: every execution inserts a new row into `runs` with a `config_snapshot` (the YAML as it was at run time) and its own set of `match_results`. Rules (amount/rounding/date tolerance, fuzzy party-name threshold, cross-period penalty, etc.) come from `config/matching_rules.yaml`.

3. **Review** — review status lives in a separate `review_state` table keyed by a stable fingerprint, so it **survives re-runs**. If a later run changes the verdict for the same fingerprint, the row is flagged *STALE* rather than silently treated as reviewed.

4. **Verification** — an optional baseline/verification harness (`verify.py`) compares engine output against a firm-provided "golden" workbook and reports disagreements for adjudication.

5. **AI layer** (`src/ai_analysis.py`, `src/ingestion_ai/`) — on-demand, advisory-only narrative produced by an LLM (via OpenRouter). It never writes back to results and never invents a classification the engine didn't produce.

---

## Prerequisites

- **Python 3.12** (the project sits in a `venv/` built on 3.12).
- **Node.js 20+** (Reflex compiles the frontend with Bun/Node; 22+ recommended).
- **Bun** is bundled by Reflex and installed automatically on first run.

---

## Local development setup

```bash
# 1. Clone
git clone https://github.com/satyajeetbinwade-cpu/Accounts-setu.git
cd Accounts-setu

# 2. Create and activate a virtualenv
python3.12 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Set the AI provider key — only needed for AI features
export OPENROUTER_API_KEY="sk-or-..."
```

The SQLite database (`db/poc.db`) and auth/session tables are created and seeded automatically on first launch. The first Admin's password is **never hardcoded**: set `SETU_ADMIN_PASSWORD` before the first boot, or the app generates one and prints it once to the server console (see [First-run credentials](#first-run-credentials)). Either way the account must replace it at first sign-in.

---

## Running the app

### First: create/upgrade the database (once per checkout, and after a pull)

```bash
source venv/bin/activate
python main.py --migrate     # the only way to migrate from the command line
python main.py --check       # read-only: what is applied, what is pending
```

**This is not optional and not just a smoke test any more.** `main.py --migrate`
is the only entry point allowed to create or migrate the schema — it creates
every module's tables, seeds first-run defaults, applies the client-id migration
(backing up `db/poc.db` to `db/backups/` first), and moves `data/<name>/`
folders to `data/<client_id>/`.

**A bare `python main.py` no longer migrates.** It prints a refusal naming the
two flags above and exits 2 **without touching anything**, unless
`SETU_ALLOW_BOOT_MIGRATE=1` is set — which is exactly what the Dockerfile CMD
sets, so a container boot still migrates and still fails closed. The reason is a
real incident: `main.py` used to run the whole boot sequence before argparse saw
`argv`, so `python main.py --help` migrated a live database and moved the real
client folders.

The UI is deliberately **read-only** at import: `setu/setu.py` only *checks*
the schema. If you start the frontend against a database that has not been
initialised, it refuses to boot with

```
SchemaMigrationPending: run: python main.py --migrate
```

That is the intended fail-closed behaviour — it means the app can never rewrite
your database or move your client folders as a side effect of starting up.

**Fly / Docker:** the CMD runs `python main.py` with `SETU_ALLOW_BOOT_MIGRATE=1`
set, so the container is the one place a start may migrate, and a migration
failure still stops the chain before Reflex or Caddy start (the container exits
non-zero and Fly keeps the previous release serving).

The migration itself has two more guards, both new: it refuses to run when the
data root is not the one that belongs to the database being migrated (the
`data/` beside a `db/` folder, or a sibling of a scratch database) and never
from a temp or sandbox folder when the app database is the target; and it
requires a **readable database backup to exist before any folder is moved**, so
there is always something to roll back to.

### Frontend (Reflex)

```bash
source venv/bin/activate
reflex run
```

- Frontend: **http://localhost:3000**
- Backend (websocket API): **http://localhost:8000**

Reflex serves both; you only need to expose port **3000** (and **8000** if the browser will reach the backend on a different host than the frontend — see deployment notes).

Two local quirks worth knowing:

- If your machine resolves `localhost` to `::1` only, the backend binds IPv4
  only and the browser gets `ERR_CONNECTION_REFUSED` for the websocket. Start
  it as `reflex run --backend-host ::` to bind both families.
- The realtime socket must be **same-origin**: the browser asks for
  `ws://localhost:<frontend_port>/_event` and the dev server proxies it to the
  backend port. Do **not** set `REFLEX_API_URL=http://localhost:8000` to "fix" a
  Connection Error — that makes the handshake cross-origin, where Reflex emits a
  duplicate `access-control-allow-origin` header that browsers reject (see the
  comment on `cors_allowed_origins` in `rxconfig.py`).

---

## Deploying to a remote server

Reflex reads many settings from `REFLEX_`-prefixed environment variables, so the same codebase runs in dev and prod with no source changes.

### Step 1 — Prepare the server

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git curl
# Node 22 (Reflex's frontend compiler needs a recent Node)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

### Step 2 — Get the code + deps

```bash
git clone https://github.com/satyajeetbinwade-cpu/Accounts-setu.git
cd Accounts-setu
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 3 — Configure the runtime (critical for remote access)

Set the **API URL** to the externally reachable host so remote browsers connect to the *server's* backend (not their own `localhost`):

```bash
# Replace with your server's public hostname or IP
export REFLEX_API_URL="http://YOUR-SERVER-HOST:8000"
export REFLEX_DEPLOY_URL="http://YOUR-SERVER-HOST:3000"
```

> **Why this matters:** the compiled frontend's `env.json` records the backend endpoints. With the default `localhost` value, a tester opening the page from another machine tries to reach *their own* `localhost:8000`, the websocket handshake fails, and the app shows **"Cannot connect to server"** and no login works.

> **Caveat (measured):** setting `REFLEX_API_URL` to a host on a *different
> origin* than the frontend does not actually work in this Reflex version. The
> websocket handshake becomes cross-origin, and Reflex emits the
> `access-control-allow-origin` header twice on `/_event` (its own Starlette
> `CORSMiddleware` plus engineio's), which browsers reject — no value of
> `cors_allowed_origins` avoids it. Serve the frontend and backend on **one
> origin** (the Caddy setup in the Dockerfile does exactly that: both ports
> behind one public port). See the `cors_allowed_origins` comment in
> `rxconfig.py`.

### Step 4 — Run (dev-style)

```bash
reflex run
```

Because `backend_host` defaults to `0.0.0.0`, the backend port is reachable from other machines.

### Production hardening

For a real deployment, run the backend and a static frontend separately behind a reverse proxy (Nginx/Caddy) with TLS:

```bash
# Build the frontend bundle
reflex export --frontend-only

# Serve the static bundle (e.g. with `reflex run --env prod` or any static server)
# and run the backend with a production server:
reflex run --env prod
```

A reverse proxy should forward:
- `http://your-host:3000` → the static frontend
- `http://your-host:8000` (or a sub-path such as `/api`) → the backend, **including WebSocket upgrade (`/ _event`)**.

> If everything is served from one domain (e.g. `https://setu.example.com` serving both the frontend and proxying the backend), leave `REFLEX_API_URL` unset — the client then targets the same host it loaded from.

---

## Login & security

- The auth layer (`src/auth/`) provides roles (Admin, Partner, Manager, Senior Accountant, Article-Trainee, End-Client), an expandable permission set (`<module>.<action>`), per-user permission overrides, and a security-event log.
- **Session life**: browser cookie `setu_session`, default timeout **8 hours** (configurable live in Settings) **plus a 60-minute inactivity window** (`SETU_SESSION_IDLE_MINUTES`), both enforced server-side on every request. On expiry the user is returned to `/login` with an explicit **"signed out due to inactivity"** message — never a silent redirect.
- **Failed logins**: every failure is logged as a security event and counted on the user row; the account **locks for 15 minutes after 5 consecutive failures**. The message is always the generic *"Invalid ID or password."*, so it never reveals whether the ID or the password was wrong, or whether the account exists.
- **First-run admin**: no credential is baked into the source. Set `SETU_ADMIN_PASSWORD` (and optionally `SETU_ADMIN_USERNAME`) before the first boot; otherwise a random password is generated and printed **once, on the server console**. The account is flagged `must_change_password`, so the bootstrap credential cannot stay in use.
- **Ownership**: every client carries `owner_user_id`; a standard user's queries are filtered to their own clients, and a direct lookup of someone else's id is refused.
- Every protected route is gated server-side; an unauthenticated visitor (or user without the required permission) is redirected rather than shown a partial UI.

### Multi-user access and data silos

Two orthogonal things decide what a user sees:

| | F1 role (`roles` table) | Access role (`users.role`) |
|---|---|---|
| **Decides** | which *screens* and *actions* are permitted | which *clients* are visible |
| **Values** | Admin, Partner, Manager, Senior Accountant, Article-Trainee, End-Client | `admin` (firm-wide) or `user` (own clients only) |

`users.role` is derived from the F1 role — Admin/Partner → `admin`, everything else → `user` — and re-derived on every boot and on every role change, so a promotion or demotion takes effect on the user's next request.

A standard user (`role = 'user'`):

- sees, searches and counts **only the clients they own** (`end_clients.owner_user_id`);
- is refused a direct lookup of anyone else's record — by id, by name, or through a child record (contact, COA row, snapshot, document) — answering exactly like a nonexistent id so nothing is revealed by the difference;
- gets their dashboards, review queues, filing calendar and exports scoped too, because a count is data;
- cannot widen any of it from the UI: the viewer is a **required argument** on every scoped read, so a call site that forgets to scope raises `TypeError` rather than returning everything.

An `admin` sees every client, and the Client Roster gains a **Created by** column plus a filter by owner.

#### Setting up the accounts

```bash
# 1. Assign every existing client to the Admin (idempotent; safe to re-run)
venv/bin/python scripts/migrate_ownership.py --dry-run   # see the plan first
venv/bin/python scripts/migrate_ownership.py

# 2. Create the standard users — credentials come from the environment,
#    are never hardcoded and are never printed
export SETU_USER1_PASSWORD='...'
export SETU_USER2_PASSWORD='...'
export SETU_USER3_PASSWORD='...'
venv/bin/python scripts/seed_users.py
```

`seed_users.py` accepts `--role NAME`, `--reset-passwords`, `--db PATH`, and a single
`SETU_SEED_USERS="User1:pw1,User2:pw2"` variable instead of the numbered pairs. Each new
account is created with `must_change_password` set, so the issued password is good for
exactly one sign-in: the user is routed to `/change-password` and cannot open any other
screen until they replace it. It also grants those users `clients.profile.manage` as a
**per-user override**, so they can create the clients they own without widening a shared role.

**Known limits** (deliberate, for this PoC):

- The `setu_session` cookie is set and read by the browser (Reflex manages cookies client-side), so it cannot be `HttpOnly`. The token is 256-bit, stored server-side in `sessions` where it can be revoked, and expires on both an absolute and an idle timer.
- Clients reachable only as a folder (`data/<name>/`) or purely inside `runs` are matched to ownership by **name**, so two users owning identically-named clients would share those runs. Run `migrate_ownership.py` to register folder clients as owned rows.
- Module queues are scoped through the client (`filter_rows_by_owner`), not by an owner column on every module's own tables.

---

## Configuration files

All under `config/`:

| File | Purpose |
|------|---------|
| `matching_rules.yaml` | GST/TDS/Other matching thresholds, tolerances, and confidence bands |
| `source_mappings.yaml` | How source-file columns map to the engine's canonical fields |
| `baseline_mapping.yaml` | Mapping for the verification baseline workbooks |
| `verification_criteria.yaml` | Disagreement/verification criteria metrics |
| `ai_config.yaml` | OpenRouter endpoint, model routing, and the `OPENROUTER_API_KEY` env-var name |

The matching engine snapshots `matching_rules.yaml` on every run, so later config edits compare cleanly against any historical run (see **Config** in the app).

---

## Directory layout

```
.
├── main.py                # boot: --migrate / --check (a bare start refuses without SETU_ALLOW_BOOT_MIGRATE=1)
├── verify.py              # verification harness (CLI)
├── rxconfig.py            # Reflex config (ports, CORS, plugins)
├── requirements.txt
├── config/                # YAML configuration (matching rules, AI, mappings)
├── data/                  # uploaded source files: data/{client}/{period}/{source_type}/
├── db/                    # SQLite database (gitignored)
├── scripts/               # operational scripts (migrate ownership, seed users)
│   ├── migrate_ownership.py  #   assign existing clients to Admin + register folder-only clients
│   └── seed_users.py         #   create the standard users from env-var credentials
├── src/                   # shared service layer (framework-agnostic)
│   ├── runner.py          #   execution / matching orchestration
│   ├── queries.py         #   read API (runs, results, review state)
│   ├── export.py          #   Excel workbook generation
│   ├── shared/            #   framework-agnostic helpers (tokens, discovery, formatting)
│   ├── matching/          #   the matching/classification engine
│   ├── verification/      #   baseline comparison harness
│   ├── auth/              #   F1 — login, sessions, roles, permissions
│   ├── clients/           #   F2 — client master data
│   ├── settings/          #   C3 — firm profile, notifications, session timeout
│   ├── rules/             #   C1 — rules/taxonomy/regulatory config
│   ├── vault/             #   C4 — encrypted credentials & DPDP workflow
│   ├── documents/         #   F3 — document repository
│   ├── ingestion_ai/      #   F3-AI — smart ingestion + LLM
│   ├── invoice_extract/   #   F3-B — invoice extraction & digitalization
│   ├── f4/ f5/ f6/        #   F4–F6 modules
│   ├── filing/            #   C2 — portal connect & filing
│   ├── module2/           #   Module 2 — reconciliation engine
│   ├── action_center/     #   Module 8 — action center
│   └── ai_models/         #   AI model registry
└── setu/                  # Reflex frontend
    ├── setu.py            #   app entry + route registration
    ├── routes.py          #   nav map (areas → screens → permissions)
    ├── state/             #   per-module Reflex State classes
    ├── views/             #   per-screen components
    └── foundation/        #   design tokens + shared components
```

---

## Verification harness (CLI)

```bash
# Single period
python verify.py --client X --period YYYY-MM --recon-type GST \
    --baseline path/to/baseline.xlsx --output path/to/report.xlsx

# With an adjudicated disagreements file
python verify.py --client X --period YYYY-MM --recon-type GST \
    --baseline path/to/baseline.xlsx --output report.xlsx \
    --adjudicated path/to/adjudicated.xlsx

# Multi-period
python verify.py --multi --run-ids 1,2,3 \
    --baselines b1.xlsx,b2.xlsx,b3.xlsx --output multi.xlsx

# Export disagreements for firm review
python verify.py --export-disagreements \
    --client X --period YYYY-MM --recon-type GST \
    --baseline path/to/baseline.xlsx --output path/to/disagreements.xlsx
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| **"Cannot connect to server"** / login button does nothing on a remote machine | Frontend's `env.json` still points at `localhost:8000` | Set `REFLEX_API_URL`/`REFLEX_DEPLOY_URL` to the server's external host (or serve everything from one domain) and restart. |
| **WebSocket handshake 403 "not an accepted origin"** | `cors_allowed_origins` was pinned to `localhost:3000` | This is fixed — `rxconfig.py` now uses `cors_allowed_origins="*"`. No action needed; if you pin it for prod, include the exact external origin. |
| **Port already in use on 3000/8000** | Another Reflex instance | Override with `REFLEX_FRONTEND_PORT`/`REFLEX_BACKEND_PORT`. |
| **AI features report "No API key available"** | `OPENROUTER_API_KEY` not set | `export OPENROUTER_API_KEY="sk-or-..."` (and restart the app). |
| **Backend restarts constantly / databases churn** | Reflex hot-reload watching `db/`, `data/` | Already excluded in `rxconfig.py` via `REFLEX_HOT_RELOAD_EXCLUDE_PATHS`. |
| **Login shows "signed out due to inactivity"** | The session cookie expired | This is expected behaviour — sign in again. The message is intentional, not an error. |

---

*Built for firm-internal reconciliation workflows. No credential ships with the source: the first Admin is created from `SETU_ADMIN_PASSWORD` (or a one-time generated password printed to the server console) and is required to replace it before the app will let it in.*