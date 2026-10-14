# Setu — Auth Hardening & Per-User Data Silos

**Session summary · 11 Oct 2026 · branch `phase-2` (local, not yet committed or pushed)**

Two tasks were requested and completed: (1) remove credentials from the login
screen, and (2) add multi-user access with per-user data silos. A follow-up
request fixed a dead end in the Reconcile flow that the silo work exposed.

---

## 1. What was asked, and what happened

| # | Request | Outcome |
|---|---|---|
| 1 | Read the code and briefly explain how login, session handling and client records work | Reported before any change was made — see [§2](#2-what-i-found-before-changing-anything) |
| 2 | **Task 1** — remove credentials from the login screen, keep authentication | Done: visible hint, hardcoded seed password and README references removed; bcrypt; empty fields with `autocomplete="off"`; generic failure message; lockout after 5 failures |
| 3 | **Task 2** — 4 accounts (1 Admin + 3 standard users) with siloed data | Done: `users.role`, `owner_user_id` on clients and their children, server-side enforcement on every query, migration + env-driven seed |
| 4 | Give me User1/2/3 credentials | Created with local-only bootstrap passwords (see [§7](#7-migration--seed)) |
| 5 | Keep the same passwords, no forced change right away | Cleared the flag for all four accounts; added `--no-force-change` so it is repeatable rather than a one-off command |
| 6 | "The context screen is a dead end — rather give option to create client and then select the client to go ahead" | Done: create-client call to action, the new client appears in the picker immediately, and a new client can now be given a period — see [§6](#6-follow-up-the-reconcile-dead-end) |
| 7 | Make the chat and changes summary md file | This document |

---

## 2. What I found before changing anything

**Login.** [`setu/views/login.py`](setu/views/login.py) renders a card → `AuthState.submit_login` →
`src.auth.service.login()` → PBKDF2-HMAC-SHA256 (200k iterations, 16-byte salt)
against `users.password_salt` / `password_hash` → creates a row in `sessions`
(`secrets.token_urlsafe(32)`) → token held in an `rx.Cookie("setu_session")`.

Problems found:

- The login page printed the bootstrap Admin credential in a "First run?" hint
  (a real username and password, in plain sight).
- [`src/auth/seed.py`](src/auth/seed.py) hardcoded that same password as
  `DEFAULT_ADMIN_PASSWORD`, and `README.md` repeated it three times.
- Failures logged a `security_events` row and escalated at 5 attempts in 15
  minutes, but there was **no lockout or throttle** — `login()`'s own docstring
  said "never a silent lockout".
- Hashing was PBKDF2, not bcrypt/argon2 as the task required.

**Session handling.** DB-backed server sessions with an 8-hour absolute
lifetime from a C3 setting. `current_user()` rejected revoked/expired/
deactivated tokens and touched `last_seen_at`, but **nothing enforced an idle
timeout**. The role was resolved live from `role_id` → `roles.name`. Every page
state re-resolves the user from its own `session_token` cookie copy — all states
subclass `AuthState`, which is the hook the silo work uses.

**Client records.** Two disjoint notions:

- the F2 master, `end_clients` (soft-delete only, `created_by` = a username
  *string*) — **the table was empty**;
- the filesystem, `data/<client>/<period>/<source_type>/`, plus `runs.client`.

`discovery.list_clients()` served the filesystem view to ~9 states;
`clients.service.list_clients()` served the DB view to ~10 more. There was **no
ownership column anywhere**, and the only visibility helper,
`auth.visible_clients()`, was dead code that would have shown an unassigned user
*everything*.

---

## 3. Assumptions

1. **"Client" means both** the `end_clients` row and the same-named `data/`
   folder. `end_clients` became the single ownership registry; the two
   folder-only clients ("Meridian Fabrics", "Test Client") were registered as
   Admin-owned rows. No folders moved, no data lost.
2. **`users.role`** (`admin` | `user`) was added as specified and is **derived
   from the existing F1 role** (Admin/Partner → `admin`), re-derived on every
   boot and every role change. The `role_id` + permission model is untouched, so
   screen access behaves exactly as before.
3. **Child records** (contacts, chart-of-accounts rows, historical snapshots,
   documents) carry `owner_user_id`, backfilled from the parent. Scoping is
   enforced at both parent and child level.
4. **bcrypt** replaces PBKDF2; legacy rows still verify and are transparently
   re-hashed on the next successful sign-in.
5. **Credentials come from the environment** — no defaults in source.
6. **The previously displayed bootstrap password is treated as compromised**:
   accounts still holding a legacy hash are flagged for a forced change.
7. Module queues are scoped **through the client** rather than by adding an
   owner column to every module's own tables.

---

## 4. Task 1 — credentials

| File | Change |
|---|---|
| [`setu/views/login.py`](setu/views/login.py) | Removed the "First run?" hint box and both placeholder hints. Fields start empty; password is `type="password"` with `autoComplete="off"` |
| [`src/auth/security.py`](src/auth/security.py) | bcrypt cost 12 with per-hash salt. `verify_password` understands bcrypt **and** legacy PBKDF2; `needs_rehash`, `is_bcrypt_hash`, `generate_password`; 8–72-byte policy (bcrypt ignores bytes past 72) |
| [`src/auth/service.py`](src/auth/service.py) | Real lockout: failure counter on the user row, **5 consecutive failures → 15-minute lock**; generic `GENERIC_LOGIN_FAILURE = "Invalid ID or password."` for unknown user, wrong password and inactive account alike; success re-hashes legacy rows and clears the counter. Added `change_own_password`, `set_must_change_password`; `reset_user_password` revokes sessions and forces a change |
| [`src/auth/db.py`](src/auth/db.py) | `access_role_for_role_id`, `must_change_password`, failure-counter/lock helpers, `revoke_sessions_for_user`; `update_user_role` re-derives the access role |
| [`src/auth/seed.py`](src/auth/seed.py) | `DEFAULT_ADMIN_PASSWORD` **deleted**. Reads `SETU_ADMIN_PASSWORD` / `SETU_ADMIN_USERNAME`, else generates a random password printed **once on the server console** |
| [`setu/state/auth_state.py`](setu/state/auth_state.py) | `must_change_password` projection, redirect from `require_auth` on every protected route, the change handler, `_viewer()`, and cookie hardening (`SameSite=Lax`, `Secure` by default, `SETU_COOKIE_SECURE=0` for local http) |
| [`setu/views/change_password_page.py`](setu/views/change_password_page.py) | **New** — the forced-change screen; keeps no password in state |
| [`setu/setu.py`](setu/setu.py) | Registers the `/change-password` route |
| [`requirements.txt`](requirements.txt) | Added `bcrypt` |
| [`README.md`](README.md) | Credential references replaced with the environment-variable flow |

---

## 5. Task 2 — multi-user data silos

### The enforcement layer

[`src/auth/access.py`](src/auth/access.py) — **new**. One place that answers
"who may see what":

- `viewer_role`, `is_admin`, `viewer_user_id`, `owner_filter`;
- `visible_client_names`, `owned_client_ids`, `all_client_record_names`;
- `require_client_row` / `require_client_name` (raise `AccessDenied`);
- `filter_rows_by_owner` for the queue-shaped reads that span clients;
- `SYSTEM_VIEWER` for trusted non-request paths (migrations, seeds, engine).

A **missing viewer is the least privileged role**, never unrestricted, so a
scoped read fails closed.

The key design decision: **`viewer` is a required keyword argument** on every
scoped read. A call site that forgets to scope raises `TypeError` rather than
silently returning everything — the leak cannot compile. A test asserts this.

### Schema

| File | Change |
|---|---|
| [`src/auth/schema.py`](src/auth/schema.py) | `users.role`, `must_change_password`, `failed_login_count`, `locked_until`; idempotent migration re-deriving the access role on every boot |
| [`src/clients/schema.py`](src/clients/schema.py) | `end_clients.owner_user_id` + owner on `contact_directory`, `chart_of_accounts`, `historical_snapshots`; backfill to the first Admin. Owner indexes are created *after* the ALTERs — `CREATE INDEX IF NOT EXISTS` on a legacy table fails on the missing column |
| [`src/documents/schema.py`](src/documents/schema.py) | `documents.owner_user_id` + backfill from the owning client |

### Scoped reads and writes

| File | Change |
|---|---|
| [`src/clients/service.py`](src/clients/service.py) | `list_clients(viewer=…)`, `get_client` (strict, raises `ClientAccessDenied`) / `find_client` (render paths, returns `None`), `create_client(owner_user_id=…)`, `list_client_owners`, guards on every child read and write |
| [`src/clients/db.py`](src/clients/db.py) | `owner_user_id` filter, `list_client_names_for_owner`, `list_client_ids_for_owner`, `list_all_client_names`; child inserts mirror the parent's owner |
| [`src/documents/service.py`](src/documents/service.py) | Viewer-scoped list/get/versions/recently-deleted; scoped `list_queue` and `queue_progress` counts; `DocumentAccessDenied` |
| [`src/documents/db.py`](src/documents/db.py) | Owner column on insert, resolved defensively so a documents-only database still works |
| [`src/shared/discovery.py`](src/shared/discovery.py) | `list_clients(viewer, db_path=…)` filters by ownership; unregistered folders are admin-only so nothing leaks by omission |

### Scoping propagated to the UI

`self._viewer()` was threaded through **~25 call sites across 16 state files**.
Notably:

- [`setu/state/dashboard_state.py`](setu/state/dashboard_state.py) — all four
  headline figures are scoped: runs by client name, exceptions and filings by
  client id, and "awaiting review" is the latest **visible** run. *(Caught
  leaking live: User1's dashboard reported 2 runs belonging to an Admin-owned
  client.)*
- [`setu/state/action_center_state.py`](setu/state/action_center_state.py) and
  [`setu/state/filing_state.py`](setu/state/filing_state.py) — firm-wide queues
  filtered **before** any count is derived from them.
- [`setu/views/client_page.py`](setu/views/client_page.py) — Admin-only
  **Created by** column and a **filter by user** dropdown.

The internal callers in [`src/queries.py`](src/queries.py),
[`src/action_center/`](src/action_center),
[`src/reconciliation/run_model.py`](src/reconciliation/run_model.py) and
[`src/ingestion_ai/service.py`](src/ingestion_ai/service.py) resolve a folder
name to a client id *after* the caller has been authorised, so they pass
`SYSTEM_VIEWER` explicitly.

---

## 6. Follow-up: the Reconcile dead end

**Reported:** as User1, the Reconcile context screen said *"No client data
folders exist on this server yet… or seed the demo data (Meridian Fabrics / Test
Client)"* — a dead end, and it named the **Admin's** clients to a user who can
never see them (a small information leak too).

**Three separate causes, all fixed:**

1. **A newly created client never appeared in the picker.**
   `discovery.list_clients()` listed only `data/` folders and past runs, but a
   client's folder is created only when a file lands — so a just-created client
   was invisible. Client **records** are now included, so a client is selectable
   from the moment it exists. For a standard user the list is still exactly
   their owned clients, so nothing widened.
2. **No way to choose a period for a brand-new client.** `upload_slot` requires
   a real `YYYY-MM` period, and periods came only from disk. Added an **Open
   period** affordance that validates `YYYY-MM` and creates
   `data/<client>/<period>/` — the folder the engine reads — so it persists. It
   appears only when there is no period to pick.
3. **Creating a client threw the user out of the flow.** The call to action
   carries `next=reconcile`, and the New Client screen honours it (Cancel too),
   returning to Reconcile with the client preselected. From the Clients roster
   the behaviour is unchanged.

Two staleness bugs the reload exposed were also fixed: with no clients the
screen always lands on **Stage 1** (where the call to action is) instead of a
leftover Stage 2, and a context naming a client that no longer exists is cleared.

### Verified live as User1

| Step | Result |
|---|---|
| Reconcile, no clients | Stage 1 + **+ Create a client** |
| Click it | `/clients/new?next=reconcile` |
| Save the client | `/reconcile?client=User1%20Co`, client already selected |
| Period | **YYYY-MM** input + **Open period** |
| Open 2026-08 | *"Period 2026-08 opened for User1 Co."* → context `User1 Co · 2026-08 · GST` |
| Continue to upload | **Stage 2 unlocked** with all four rows (Books, GSTR-2B, IMS, Credit/Debit Note Register) |

---

## 7. Migration & seed

```bash
cd /Users/satyajeetbinwade/accounts-setu-test

# 1. Assign every existing client to the Admin; register folder-only clients.
#    Idempotent. Safe to re-run.
./venv/bin/python scripts/migrate_ownership.py --dry-run   # plan, writes nothing
./venv/bin/python scripts/migrate_ownership.py

# 2. Create the standard users. Credentials come from the environment and are
#    never hardcoded and never printed.
export SETU_USER1_PASSWORD='...' \
       SETU_USER2_PASSWORD='...' \
       SETU_USER3_PASSWORD='...'
./venv/bin/python scripts/seed_users.py
```

Options: `--role NAME`, `--reset-passwords`, `--no-force-change`,
`--no-grant-manage`, `--db PATH`, or one `SETU_SEED_USERS="User1:pw1,User2:pw2"`.

**Accounts**

| User | Access role | Sees |
|---|---|---|
| `admin` | admin | all clients, with Created-by column and filter |
| `User1` | user | only the clients User1 owns |
| `User2` | user | only the clients User2 owns |
| `User3` | user | only the clients User3 owns |

The standard users receive `clients.profile.manage` as a **per-user override**,
so they can create the clients they own without widening a shared role.

> **Passwords are deliberately not recorded in this file.** The local bootstrap
> passwords were typed on a command line and pasted into a chat, and this
> document lives in the repository — writing them here would bake a known
> credential into source control, which is exactly what Task 1 removed. Rotate
> with `--reset-passwords`.

**Two operational notes**

1. Re-running `migrate_ownership.py` re-flags `admin`, because it marks every
   account still holding a legacy-scheme hash and `admin` has not signed in yet.
   Signing in once upgrades the hash to bcrypt and stops the re-flagging.
2. `--no-force-change` is a **local/dev convenience**. The forced-change feature
   itself is intact and tested; only these local accounts have the flag off.

---

## 8. Test checklist

| # | Requirement | How it is proven | Result |
|---|---|---|---|
| **a** | No credentials visible on the login screen or in page source | Live DOM inspection: no leaked default, no "First run", no example ID; both fields empty, `autoComplete="off"`, password `type="password"`. A test also **scans the whole repository** for the old literal — this is what caught the remaining `README.md` references | **Pass** |
| **b** | User1 cannot see User2's clients via UI **or** API | Live: User1's roster showed only their own; User2's roster did not contain User1's client. API: `list_clients` / `get_client` / `discovery.list_clients`, plus dashboard and queue **counts**, are all scoped. `filter_owner_id` is ignored for a standard user; a missing viewer yields zero rows | **Pass** |
| **c** | Admin sees all clients | Live as an Admin: roster listed every client with a **Created by** column and a filter offering exactly {All users, each owner}; selecting an owner narrowed the roster to that owner's clients | **Pass** |
| **d** | Standard user opening another user's client URL/ID is denied | Live: User2 at `/clients?client=<User1's client>` got an empty roster — no client name, no profile tabs. Denied ids answer **identically** to nonexistent ones, so nothing is revealed by the difference | **Pass** |

**Automated coverage**

- [`tests/test_auth_security.py`](tests/test_auth_security.py) — 17 tests:
  login screen hygiene, repository-wide credential scan, bcrypt storage, legacy
  verify + upgrade, generic failure message, lockout after five failures,
  counter reset, forced change, reset revoking sessions, deactivation, idle
  expiry, logout, access-role derivation and demotion.
- [`tests/test_client_silos.py`](tests/test_client_silos.py) — 35 tests: list and
  count scoping, no widening via the Admin filter, fail-closed viewers, strict
  and render-path lookups, mutation refusal, filesystem view scoping,
  unregistered folders, child-record and document scoping, queue counts, session
  ownership of new clients, and the required-`viewer` guard. It also carries a
  structural guard that walks the AST for every scoped call site by receiver and
  name, so the review-round fixes cannot regress silently.
- [`tests/test_clients_model.py`](tests/test_clients_model.py) — updated for the
  scoped signatures; its original assertions are unchanged.

**Suite: 493 passed, 1 skipped** (baseline was 441 + 1). All **26 pages
render**.

### 8.1 Code review round

Before pushing, the change was put through a review that looked specifically for
paths where a standard user could still reach another user's data. It confirmed
the migrations (idempotent across a pre-change database, a fresh one and a
documents-only one), the authentication logic and the Reflex wiring, and found
**four real gaps**, all now fixed and pinned by tests:

| Gap | Why it mattered | Fix |
|---|---|---|
| The Action Center's **Report tab** was unscoped | `list_report_periods` listed every client+period in the firm and `build_period_report` built any of them — a standard user could read another client's full report | Both take a `viewer`; `build_period_report` refuses a folder the viewer does not own; `ReportState` passes the session viewer |
| **Id-keyed document operations** had no ownership check | `upload_document`, `add_version`, `get_version_bytes`, `get_current_version`, `set_document_notes`, `used_in`, `discard_document`, `resolve_queue_entry`, `soft_delete_document`, `restore_document`, `reassign_document` accepted an id from the event and acted on it — so a standard user could read, reclassify, soft-delete or discard another user's document, or file one into another user's client | Every one takes a required `viewer` and gates on the document (or the target client) first |
| **Client-selection setters** trusted the event | `set_ctx_client` / `set_client` assigned any value, and the queries keyed on it were not owner-filtered; `add_period` built a **filesystem path** from it | The setters now reject anything outside the viewer's own client list, and `add_period` re-checks the client and asserts the resolved path stays inside `DATA_ROOT` |
| `provision_end_client_login` called `get_client` without the new required `viewer` | Latent `TypeError` (no callers today) | Uses `find_client(..., viewer=access.SYSTEM_VIEWER)` |

Two further corrections came out of the same round: `reassign_document_client`
now re-derives `owner_user_id` from the new parent client (otherwise the old
owner kept seeing a reassigned document and the new owner did not), and a
`list_clients()` call in the Action Center's internal folder-key helper was
given an explicit `SYSTEM_VIEWER`.

### 8.2 Second review round — the same class of hole, in the modules

A verification pass confirmed the four fixes above and then found the *same*
pattern — an id taken from a Reflex event argument and used to key an unscoped
service call — in four more modules, none of which had any ownership concept:

| Gap | Why it mattered | Fix |
|---|---|---|
| **Run selection by id** (`set_review_run`, `set_selected_run`) | Run ids are sequential, so a standard user could enumerate them and read — or export — any client's reconciliation results | The run's own `client` is checked against the viewer's clients before any result is loaded |
| **Module 2** (`open_exception`, `resolve`) | Any client's exception and its evidence could be read, and `resolve` could mutate it | `_owns_client()` gate on open, on detail load and again before the write |
| **Filing** (`set_source_client`, `open_filing`, `send_filing`, `record_outcome`) | Any client's source availability (which lists that client's connection credentials) could be read; filings could be opened, sent or acknowledged across users | Ids validated against the viewer's clients, re-checked before each mutation |
| **invoice_extract / ingestion_ai** | Neither module had *any* owner scoping: uploads, stored file bytes, batches, downloads, the review queue and its **counts** were firm-wide; `client_id` came straight from an event | Lists and id lookups scope by owned client ids (`_may_see_upload`, `_require_batch_access`, `_scoped_uploads`), the queue summary counts are filtered, and `open_upload` refuses an id it cannot read |
| `link_evidence`, `find_duplicate_filenames` | The last unguarded writer; and duplicate detection could reveal that a filename exists in another user's vault, or wrongly badge this user's own file | Both scoped; internal callers state `SYSTEM_VIEWER` explicitly |

Two internal helpers that had begun failing closed on a *legitimate* path now use
an explicitly named unscoped accessor (`_raw_upload`) instead of a viewer-less
call, so the intent is visible rather than implied.

**A structural test now guards the whole class:** it walks `setu/` and `src/`
and fails if any call on a scoped receiver (`clients.`, `documents.`,
`report_svc.`, `ie.`, `ia.`, `discovery.`) omits the viewer. Alongside it are
behavioural tests for each closed hole: refused mutations on another user's
document, version-bytes refusal, queue-resolution refusal, both ends of a
reassignment, report scoping (including the Admin's unrestricted view), upload
reads failing closed with no viewer, and duplicate detection scoped per user.

**Verified live as a standard user:** every module loads as `User1` with no
server error and no reference to the Admin's clients — Clients, Reconcile,
Documents, Smart Ingestion, Invoice Extraction, Reconciliation, Action Center
and Filing.

---

## 9. Known limitations

- The `setu_session` cookie cannot be `HttpOnly`: Reflex sets and reads cookies
  client-side. Mitigations are a 256-bit token, server-side revocation, and both
  an absolute and an idle expiry. A reverse-proxy-set `HttpOnly` cookie is the
  real fix.
- Runs and folders are joined to ownership **by name**, so two users owning
  identically-named clients would share those runs.
- Module queues are scoped through the client rather than by an owner column on
  each module's own tables.
- The test suite writes `data/Meridian Fabrics/2026-08/tally/pr.xlsx` into the
  real `data/` tree. Pre-existing, unrelated to this change; cleaned up
  manually.

---

## 10. Running several users at once

The backend has no single-user bottleneck: `sessions` is keyed by token, not by
user, and the viewer is re-derived from the token on every request. Three
simultaneous sessions were verified end to end — three logins at once,
interleaved requests staying on their own identity, three concurrent client
creates with no errors, and the silo holding throughout (one user's roster
stayed empty while another's held only their own client).

**One browser, three users.** A browser has one cookie jar per *host*, and
`setu_session` lives in it, so three tabs on the same host are one user: the
second tab opens already signed in, and signing in as somebody else in one tab
changes the identity of every tab the next time it reloads. To run three users
side by side in one browser, give each a different host — cookies are scoped per
host, not per port:

| URL | Purpose |
|---|---|
| <http://localhost:3100> | user A |
| <http://127.0.0.1:3100> | user B |
| <http://u1.localhost:3100> | user C (any `*.localhost` alias resolves to 127.0.0.1) |

Separate browser profiles, an incognito window or separate devices work equally
well. `http://[::1]:3100` serves the frontend but the backend listens on IPv4
only, so signing in there fails.

**SQLite.** [`src/db.py`](src/db.py) now puts the database in **WAL** journal
mode, so readers never wait on the single writer and a lingering reader can no
longer make a writer fail at commit. This is hardening, not a repair of
observed breakage: measured with three writers and three readers over 150
statements on this codebase, both journal modes completed with **zero** errors,
because the 5 s busy timeout already absorbed the serialisation. The gain is in
the tail — WAL kept the slowest read at ~0.5 ms where the rollback journal
reached 4–22 ms.

WAL is recorded in the file header, so it is a one-off per database, and the
pragma falls back silently where WAL is unavailable (read-only filesystem, no
shared memory, network mounts). Set `SETU_SQLITE_JOURNAL=delete` to opt out.
Verified: fresh databases are created in WAL, re-opening is idempotent,
`integrity_check` is `ok`, `foreign_keys` stays on, and a value outside SQLite's
own list of modes is ignored rather than interpolated into the pragma.

---

## 11. Status

- All auth/silo work is **pushed to `origin/phase-2`**: `dfafdf2` ("Harden auth
  and add per-user client data silos") — 54 paths, +4,435/−362 — followed by
  `b090ae2` ("Add SQLite WAL mode for multi-user parallelism").
- Local dev server runs at <http://localhost:3100> (backend 8100) with
  `SETU_COOKIE_SECURE=0` so cookies work over plain http. Local database reports
  `journal_mode = wal`.
- **Pushing the branch did not deploy it.** The live app is a build of a
  *different* branch: release v30, and its container has no `src/auth/access.py`
  and no `scripts/seed_users.py`, while carrying files that are not on `phase-2`
  (`scripts/go_live_local.sh`, `scripts/smoke_release.py`, `railway.toml`,
  `verify_*.py`). Its database keeps the legacy `role_id` schema with PBKDF2
  hashes.
- Consequence: **the three standard users exist only in the local database.**
  The live app authenticates `admin` alone.

### 11.1 Which login works where

| Environment | URL | Accounts that work |
| --- | --- | --- |
| Local | `http://localhost:3100` | `admin` plus `User1`, `User2`, `User3` (this machine's `SETU_USERn_PASSWORD` values) |
| Live | <https://setu-poc.fly.dev/login> | `admin` only |

Entering a standard-user ID on the live URL returns "Invalid ID or password" —
correct behaviour for an account that does not exist there, not a throttle and
not a wrong password. Deliberately no passwords appear in this document; if you
have lost them, re-run `scripts/seed_users.py --reset-passwords` and stay
signed in with the console that launched it.

### 11.2 To make the three users real on Fly

Requires deploying `phase-2` first, which **overwrites the current live build**,
then running the scripts against the deployed volume:

```bash
flyctl deploy -a setu-poc                       # replaces v30
flyctl ssh console -a setu-poc -C "python3 scripts/migrate_ownership.py"
flyctl ssh console -a setu-poc -C \
  "SETU_USER1_PASSWORD=… SETU_USER2_PASSWORD=… SETU_USER3_PASSWORD=… \
   python3 scripts/seed_users.py --no-force-change"
```

Do not run this while another branch is mid-deploy on the same app, and rotate
the live `admin` password: it is still set to the historical default that used
to be seeded from source, so it must be treated as public knowledge.

## 12. Do NOT deploy this branch onto the live volume as-is

Section 11.2 above is **withdrawn**. Deploying `phase-2` over `setu-poc`'s
existing database does not merely replace a build — it breaks reconciliation at
runtime, because the live volume is on a *different schema lineage*.

**The divergence.** `release-fly` (the branch the live app is built from) contains
a client-identity refactor that `phase-2` never received: clients became integer
keyed (`end_clients.client_id`), so `runs.client_id` + `client_name_at_run`
replaced the old text `runs.client`, and `users.end_client_id` replaced
`end_client_ref`. The two branches share only the ancestor `e2ea3f6`:

- `phase-2` = `e2ea3f6` + `dfafdf2` (silos) + `e76a520` (WAL)
- `release-fly` = `e2ea3f6` + ~60 commits, including the client-id migration
- `git merge-base --is-ancestor dfafdf2 origin/release-fly` → **false**

`phase-2` has no `RENAME` migration and does not know `client_name_at_run`, so
nothing bridges the two naming schemes.

**Measured, not inferred.** Building a database from the live schema
(`sqlite_master` DDL, no rows) and running this branch's own boot sequence
(`src.bootstrap.init_all`) against it: no module failed — but then

```
db.insert_run(...) → OperationalError: table runs has no column named client
```

Columns the live database lacks that this branch's code writes:

| Table | Missing on live |
| --- | --- |
| `users` | `end_client_ref` (live has `end_client_id`), and the four silo columns — those four *are* added by `_migrate_users()`, `end_client_ref` is not |
| `runs`, `review_state`, `result_field_overrides`, `per_client_team_assignments` | `client` (live has `client_id`) |
| `ingestion_results`, `ingestion_shape_cache` | `client_ref` |

By contrast `owner_user_id` is fine: `src/clients/schema.py` and
`src/documents/schema.py` add it to `end_clients`, `documents`,
`chart_of_accounts`, `contact_directory` and `historical_snapshots` on boot.

Live data would also be invisible to this branch: 10 clients and 20 runs keyed by
integer `client_id`, which our text-keyed reads cannot reach.

**The safe route.** Port the silo work onto the `release-fly` lineage — take its
client-id model as the base and re-apply `src/auth/access.py`, the auth changes,
the ownership migrations and the seed script on top — and deploy that. The
`end_clients.owner_user_id` design carries over unchanged; only the client key
type in the scoped reads has to follow `client_id`.
