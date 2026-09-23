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
| `main.py` | CLI | `python main.py` | One-time DB/config smoke test |
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

The SQLite database (`db/poc.db`) and auth/session tables are created and seeded automatically on first launch — the very first admin is `admin / ChangeMe123` (change it immediately).

---

## Running the app

### Frontend (Reflex)

```bash
source venv/bin/activate
reflex run
```

- Frontend: **http://localhost:3000**
- Backend (websocket API): **http://localhost:8000**

Reflex serves both; you only need to expose port **3000** (and **8000** if the browser will reach the backend on a different host than the frontend — see deployment notes).

### One-time smoke test

```bash
source venv/bin/activate
python main.py
```

Initializes the DB, loads `config/`, and prints confirmation.

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

### Step 4 — Run (dev-style)

```bash
reflex run
```

Because `backend_host` defaults to `0.0.0.0` and `cors_allowed_origins` is set to `"*"` (see below), the app is reachable to remote testers immediately.

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
- **Session life**: browser cookie `setu_session`, default timeout **8 hours** (configurable live in Settings). On expiry the user is returned to `/login` with an explicit **"signed out due to inactivity"** message — never a silent redirect.
- **Failed logins**: never silently lock out. Every failure is logged as a security event; repeated failures (5 in 15 min) log an *escalation* event.
- **First-run admin**: `admin / ChangeMe123` — change this password immediately after first sign-in.
- Every protected route is gated server-side; an unauthenticated visitor (or user without the required permission) is redirected rather than shown a partial UI.

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
├── main.py                # one-time DB/config smoke test
├── verify.py              # verification harness (CLI)
├── rxconfig.py            # Reflex config (ports, CORS, plugins)
├── requirements.txt
├── config/                # YAML configuration (matching rules, AI, mappings)
├── data/                  # uploaded source files: data/{client}/{period}/{source_type}/
├── db/                    # SQLite database (gitignored)
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

*Built for firm-internal reconciliation workflows. The default `admin / ChangeMe123` credential exists only for first-run onboarding and must be rotated before any shared or production use.*