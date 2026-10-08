# Phase 2 — What's New (Communications & GST/Tally Integration)

> **Status:** Merged and integrated into `setu-poc-full-upload` (the default/"main" branch).
> **Source:** branch `phase-2` (commit `3b53d12`, *"Phase 2: Communications & GST/Tally Integration modules"*), merged as a merge commit, then integrated into the app tree.
> **Test status:** 514 tests pass (`514 passed`), including 21 new Phase 2 tests.

---

## 1. Summary

Phase 2 adds two new feature pillars to the Accounts Setu platform:

| # | Module | Purpose |
|---|--------|---------|
| 1 | **Communications** | Send WhatsApp / email follow-ups to clients (missing invoices, missing entries, reminders, custom). |
| 2 | **GST/Tally Integration** | Fetch data from the GST Portal and Tally, and push reconciliation entries back — all behind an admin **preview → confirm** workflow. |

The branch shipped these as a self-contained `phase2/` **staging folder** (with copy-and-wire
instructions). As part of this merge they have been **moved into the live app tree**, wired into
the boot sequence, routing, sidebar navigation, and the Reflex state layer, and covered by tests.
The staging folder was then removed to avoid duplication.

---

## 2. New sidebar routes

| Route | Sidebar area | Page |
|-------|--------------|------|
| `/follow-ups` | **Reconcile** | Client Follow-ups |
| `/integrations` | **Setup** | GST Portal & Tally Integration |

Both routes are registered through the existing `AREAS` map in `setu/routes.py`, so they are
automatically guarded by the standard auth gate (`check_session` + `require_auth`).

---

## 3. Module 1 — Communications

### 3.1 What it does
- Select any client from the roster.
- Edit recipient email / phone / name **at send time** (without changing the client profile),
  optionally saving them as that client's follow-up defaults.
- Choose a channel: `email`, `whatsapp`, or `both`.
- Four built-in templates with `{{variable}}` substitution:
  `missing_invoice`, `missing_entries`, `followup_reminder`, `custom`.
- Live preview of the rendered subject/body before sending.
- Per-client communication history with status tracking (`pending → sent / failed / read`).
- Pluggable providers: Email (SMTP / SendGrid) and WhatsApp (Twilio / WATI / Interakt).

### 3.2 New files
```
src/communications/
├── __init__.py                Module docstring / usage
├── models.py                  Dataclasses: FollowUpRecipient, FollowUpRequest,
│                              CommunicationLogEntry, ProviderConfig, FollowUpTemplate
├── schema.py                  4 tables + indexes; init_communications_schema()
├── db.py                      Raw SQL CRUD (no business logic)
├── service.py                 Public API: init_communications, list_providers, save_provider,
│                              list_templates, send_followup, list_logs,
│                              get_client_followup_prefs, save_followup_pref
├── templates.py               BUILTIN_TEMPLATES + render_template / render_subject
└── providers/
    ├── __init__.py
    ├── email.py               send_email() — SMTP / SendGrid
    └── whatsapp.py            send_whatsapp() — Twilio / WATI / Interakt
```

### 3.3 New database tables
| Table | Purpose |
|-------|---------|
| `followup_providers` | Provider configuration (channel, type, JSON config, active flag). |
| `followup_templates` | Reusable message templates (subject/body/variables); seeded with 4 built-ins. |
| `communication_log` | Audit trail of every message sent (recipient, body, status, provider ref, error). |
| `client_followup_prefs` | Per-client saved follow-up contact details / preferred channel. |

### 3.4 New service API (`src/communications/service.py`)
```python
init_communications(db_path=None) -> None
list_providers(*, channel=None, db_path=None) -> list[dict]
save_provider(provider_id, *, channel, provider_type, label=None, config, db_path=None) -> int
list_templates(*, channel=None, db_path=None) -> list[dict]
send_followup(request: FollowUpRequest, *, db_path=None) -> int   # returns log_id
list_logs(*, client_id=None, limit=50, db_path=None) -> list[dict]
get_client_followup_prefs(client_id, *, db_path=None) -> list[dict]
save_followup_pref(client_id, *, contact_id=None, email=None, phone=None,
                   preferred_channel="email", db_path=None) -> int
```

### 3.5 New UI
- State: `setu/state/communication_state.py` → `CommunicationState` (subclasses `AuthState`).
- Page: `setu/views/communication_page.py` → `communication_page()`, route `/follow-ups`.

---

## 4. Module 2 — GST Portal & Tally Integration

### 4.1 What it does
- Store GST Portal credentials **per client per GSTIN** (now encrypted at rest via the C4 vault).
- Configure Tally connections (host, port, company) and **test connectivity** with a ping.
- **Fetch** from the GST Portal (returns, invoices, filing calendar) and from Tally
  (chart of accounts, ledgers, vouchers) and cache it for preview.
- **Push** reconciliation entries to Tally as journal vouchers, and file/push returns to the GST Portal.
- Every external operation goes through an **admin preview → confirm** workflow; pending operations
  are cancellable and fully audited in `sync_operations` (`confirmed_by` / timestamps).

### 4.2 New files
```
src/gst_tally_integration/
├── __init__.py                    Module docstring / __version__
├── models.py                      GstCredentials, TallyConnection, SyncOperation,
│                                  GstReturnData, TallyData
├── schema.py                      5 tables + indexes; init_gst_tally_schema()
├── db.py                          Raw SQL CRUD (no business logic)
├── service.py                     Public API (credentials, connections, fetch/push, confirm/cancel)
├── gst_portal/
│   ├── __init__.py
│   └── client.py                  GstPortalClient — GSTN API client (interface scaffold)
└── tally/
    ├── __init__.py
    └── client.py                  TallyClient — Tally HTTP XML API client
```

### 4.3 New database tables
| Table | Purpose |
|-------|---------|
| `gst_credentials` | GST Portal credentials per (client, GSTIN); password encrypted at rest. |
| `tally_connections` | Tally connection config per client (host, port, company). |
| `sync_operations` | Queue + audit trail of every fetch/push with status and confirmation. |
| `gst_returns` | Cached GST return data (upserted by client/gstin/type/period). |
| `tally_data_cache` | Cached Tally data (COA / ledgers / vouchers) for preview. |

### 4.4 New service API (`src/gst_tally_integration/service.py`)
```python
init_gst_tally(db_path=None) -> None
list_gst_credentials(client_id=None, *, db_path=None) -> list[dict]   # password never returned
save_gst_credential(client_id, gstin, username, password, *, db_path=None) -> int  # encrypts
list_tally_connections(client_id=None, *, db_path=None) -> list[dict]
save_tally_connection(conn_id, client_id, *, host="localhost", port=9000,
                      company_name=None, db_path=None) -> int
test_tally_connection(conn_id, *, db_path=None) -> (bool, str | None)
fetch_from_gst_portal(client_id, gstin, data_type, period, *, db_path=None) -> int   # op_id
fetch_from_tally(conn_id, client_id, data_type, *, period=None, db_path=None) -> int
push_reconciliation_to_tally(conn_id, client_id, period, entries, *, db_path=None) -> int
push_to_gst_portal(client_id, gstin, return_type, period, data, *, db_path=None) -> int
confirm_sync_operation(op_id, confirmed_by, *, db_path=None) -> None
cancel_sync_operation(op_id, *, db_path=None) -> None
list_sync_operations(*, client_id=None, status=None, limit=50, db_path=None) -> list[dict]
get_sync_operation(op_id, *, db_path=None) -> dict | None
```

### 4.5 New UI
- State: `setu/state/gst_tally_state.py` → `GstTallyState` (subclasses `AuthState`).
- Page: `setu/views/gst_tally_page.py` → `gst_tally_page()`, route `/integrations`.
  Four tabs: **GST Portal**, **Tally**, **Connections**, **Sync History**.

---

## 5. Wiring changes to existing files

| File | Change |
|------|--------|
| `src/bootstrap.py` | Added `init_communications` and `init_gst_tally` to `_BOOT_STEPS` so a fresh DB always gets the 9 new tables + seeded templates. |
| `setu/routes.py` | Added `Follow-ups` (`/follow-ups`, Reconcile) and `Integrations` (`/integrations`, Setup) screens; added a `P2` entry to `MODULE_INVENTORY`. |
| `setu/views/pages.py` | Added `_followups_route` / `_integrations_route` and registered them in `render_route` / `_make_page`. |
| `setu/state/__init__.py` | Exported `CommunicationState` and `GstTallyState`. |
| `phase2/` (removed) | Staging folder deleted after the files were copied to their final homes; reference docs preserved under `docs/phase2/`. |

---

## 6. New tests

| File | Coverage |
|------|----------|
| `tests/test_phase2_communications.py` | Schema idempotency + template seeding, `{{var}}` rendering, provider upsert persistence, send flow (stubbed provider) → `sent` + `provider_ref`, no-provider → `failed`, missing contact → error, WhatsApp routing, per-client prefs upsert. |
| `tests/test_phase2_gst_tally.py` | Schema idempotency, credentials encrypted-at-rest + hidden from API, "confirm only a previewing op" guard, cancel blocks confirm, missing connection/credentials errors, push preview totals, GST payload retention, confirm executes push through the **correct** connection with the **decrypted** password, Tally XML escaping. |

---

## 7. Dependencies

No new third-party packages were required:
- Email uses the standard library `smtplib` / `email`.
- WhatsApp uses `urllib.request` (works with Twilio/WATI/Interakt REST APIs without SDKs).
- GST/Tally clients use `urllib` + `xml.etree.ElementTree`.
- Credential encryption reuses the existing C4 vault (`cryptography`), already a dependency.

---

## 8. Security notes & known limitations

- **GST/SMTP/WhatsApp secrets:** GST passwords are now encrypted at rest with the vault key.
  SMTP/WhatsApp provider secrets still live as JSON plaintext in `followup_providers.config_json`
  and should be moved into the vault next.
- **GSTN client is an interface scaffold:** `GstPortalClient` targets placeholder endpoints and
  uses a placeholder auth flow; it is **not** a certified GSTN (Suvidha Provider) integration.
  Filing through it is not production-ready.
- **Tally has no authentication** by design (its HTTP XML API is unauthenticated); restrict it to
  localhost / a trusted network or VPN.
- **Preview → confirm** protects irreversible pushes, but fetch operations currently execute
  immediately when "fetched" and are only *reviewed* afterwards.
- **Period formats are inconsistent** across the module (GST `MMYYYY`, Tally `YYYYMMDD`,
  communications `2026-08`). There is no central normaliser; see the code review.

---

## 9. Reference documents

- `docs/phase2/README.md` — the original integration guide from the branch.
- `docs/phase2/context.md` — the original design/context spec (tables, API contracts, flows).
- `docs/phase2/CODE_REVIEW.md` — the code review and list of fixes made during integration.
- `Accounts_Setu_User_Guide.md` (repo root) — the end-user guide shipped with the branch.
