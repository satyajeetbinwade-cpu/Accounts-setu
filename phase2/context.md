# Phase 2 — Feature Implementation Context

> **Status:** Ready for Implementation
> **Applies to:** Accounts Setu Platform
> **Target Modules:** Communications (Follow-ups), GST/Tally Integration

---

## 1. Overview

Phase 2 adds two major feature pillars to the existing Accounts Setu GST reconciliation platform:

### 1.1 Feature 1: Client Follow-ups (WhatsApp + Email)

**Purpose:** Enable admins to send automated follow-up messages to clients requesting missing invoices, missing entry context, or custom messages. Contact information (email, phone) is editable at send-time without persisting to the client profile, but can optionally be saved as a default for future follow-ups.

**Key Capabilities:**
- Select any client from the roster
- Edit recipient email/phone/name at send time
- Choose from 4 message templates: `missing_invoice`, `missing_entries`, `followup_reminder`, `custom`
- Templates support `{{variable}}` substitution (period, recon_type, missing_count, missing_details, contact_name)
- Send via Email, WhatsApp, or both
- Live preview of rendered message before sending
- Communication history log per client with status tracking (pending → sent/failed/read)
- Provider abstraction: Email (SMTP/SendGrid), WhatsApp (Twilio/WATI/Interakt)
- Saved follow-up contact preferences per client

### 1.2 Feature 2: GST Portal & Tally Integration

**Purpose:** Enable bidirectional data exchange between the platform and external systems — GST Portal (GSTN APIs) and Tally (HTTP XML API). All operations follow a **preview → confirm** workflow ensuring no irreversible action happens without admin approval.

**Key Capabilities:**
- Store encrypted GST portal credentials per client per GSTIN
- Configure Tally connections (host, port, company name)
- Test Tally connectivity via ping
- Fetch data from GST Portal: returns, invoices, filing calendar
- Fetch data from Tally: chart of accounts, ledgers, vouchers
- Cache fetched data for preview
- Push reconciliation entries to Tally as journal vouchers
- Push/filing data to GST Portal (returns)
- Admin preview → confirm → execute workflow
- Full sync operation audit trail
- Cancel pending operations

---

## 2. Technical Architecture

### 2.1 Module Structure

Both modules follow the exact same layered pattern as the existing `src/clients/` module:

```
module/
├── __init__.py      — Module doc + public imports
├── models.py        — Dataclasses for type safety
├── schema.py        — SQLite CREATE TABLE statements
├── db.py            — Raw SQL operations (no business logic)
└── service.py       — Public API with business rules
```

Additionally:
- `providers/` — Pluggable communication backends (email, whatsapp)
- `gst_portal/` — GSTN API client
- `tally/` — Tally HTTP XML client

### 2.2 Layer Responsibilities

| Layer | Role | Rules |
|-------|------|-------|
| `models.py` | Pure dataclasses | No logic, no DB access |
| `schema.py` | `CREATE TABLE IF NOT EXISTS` | Idempotent, called once at startup |
| `db.py` | Raw SQL + column mapping | No validation, no business rules |
| `service.py` | Public API | All validation, business rules, error handling |
| `providers/*.py` | External communication | Returns `(success, error_message)` tuple |
| `client.py` (API) | External HTTP calls | Returns `(success, data_or_error)` tuple |

### 2.3 Data Flow Patterns

**Communications flow:**
```
UI (communication_state.py)
  ─► service.py (send_followup)
    ─► db.py (insert_log, list_providers)
    ─► templates.py (render_template)
      ─► providers/email.py (send)  OR  providers/whatsapp.py (send)
    ─► db.py (update_log_status)
```

**GST/Tally flow:**
```
UI (gst_tally_state.py)
  ─► service.py (fetch_from_gst_portal / fetch_from_tally / push_*)
    ─► db.py (create_sync_op)
    ─► client.py (authenticate, fetch)

Admin reviews → confirms
  ─► service.py (confirm_sync_operation)
    ─► db.py (update_sync_op_status → "confirmed")
    ─► client.py (push_journal_entry / push_return)  — only for push ops
    ─► db.py (update_sync_op_status → "completed")
```

---

## 3. Database Schema

### 3.1 Communications Tables (`followup_*`)

```sql
-- Communication providers (SMTP, SendGrid, Twilio, WATI, Interakt)
CREATE TABLE followup_providers (
    provider_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT    NOT NULL CHECK(channel IN ('email', 'whatsapp')),
    provider_type TEXT    NOT NULL,  -- 'smtp', 'sendgrid', 'twilio', 'wati', 'interakt'
    label         TEXT,
    config_json   TEXT    NOT NULL DEFAULT '{}',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Reusable message templates
CREATE TABLE followup_templates (
    template_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,  -- 'missing_invoice', 'missing_entries', etc.
    channel         TEXT    NOT NULL CHECK(channel IN ('email', 'whatsapp', 'both')),
    subject_template TEXT,
    body_template   TEXT    NOT NULL,
    variables       TEXT    NOT NULL DEFAULT '[]',  -- JSON list
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- History of all sent communications
CREATE TABLE communication_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL,
    channel         TEXT    NOT NULL CHECK(channel IN ('email', 'whatsapp')),
    template_name   TEXT,
    recipient_email TEXT,
    recipient_phone TEXT,
    recipient_name  TEXT,
    subject         TEXT,
    message_body    TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'sent', 'failed', 'read')),
    provider_ref    TEXT,
    error_message   TEXT,
    sent_at         TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Per-client follow-up contact preferences
CREATE TABLE client_followup_prefs (
    pref_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES end_clients(client_id),
    contact_id  INTEGER,                     -- optional link to contact_directory
    email       TEXT,
    phone       TEXT,
    preferred_channel TEXT NOT NULL DEFAULT 'email'
                        CHECK(preferred_channel IN ('email', 'whatsapp', 'both')),
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(client_id, contact_id)
);
```

### 3.2 GST/Tally Tables

```sql
-- Encrypted GST portal credentials (one per GSTIN)
CREATE TABLE gst_credentials (
    credential_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        INTEGER NOT NULL REFERENCES end_clients(client_id),
    gstin            TEXT    NOT NULL,
    username         TEXT    NOT NULL,
    encrypted_password TEXT NOT NULL,
    is_active        INTEGER NOT NULL DEFAULT 1,
    last_synced_at   TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Tally connection config (one per company per client)
CREATE TABLE tally_connections (
    conn_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL REFERENCES end_clients(client_id),
    host         TEXT    NOT NULL DEFAULT 'localhost',
    port         INTEGER NOT NULL DEFAULT 9000,
    company_name TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    last_synced_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Sync operations queue
CREATE TABLE sync_operations (
    op_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    direction    TEXT    NOT NULL CHECK(direction IN ('fetch', 'push')),
    source       TEXT    NOT NULL CHECK(source IN ('gst_portal', 'tally')),
    data_type    TEXT    NOT NULL,
    period       TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'previewing', 'confirmed',
                                     'completed', 'failed', 'cancelled')),
    summary_json TEXT    NOT NULL DEFAULT '{}',
    details_json TEXT    NOT NULL DEFAULT '{}',
    error_message TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

-- Cached GST return data
CREATE TABLE gst_returns (
    return_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    gstin        TEXT    NOT NULL,
    return_type  TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    filing_status TEXT,
    filed_at     TEXT,
    raw_json     TEXT    NOT NULL DEFAULT '{}',
    synced_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Cached Tally data
CREATE TABLE tally_data_cache (
    data_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL,
    data_type    TEXT    NOT NULL,
    period       TEXT,
    raw_xml      TEXT,
    raw_json     TEXT    NOT NULL DEFAULT '{}',
    synced_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

---

## 4. Service API Contracts

### 4.1 Communications Service (`src/communications/service.py`)

```python
# Initialisation
init_communications(db_path=None) → None

# Provider management
list_providers(*, channel: str | None = None, db_path=None) → list[dict]
save_provider(provider_id: int | None, *, channel, provider_type, label, config: dict) → int

# Template management
list_templates(*, channel: str | None = None, db_path=None) → list[dict]

# Core send flow
send_followup(request: FollowUpRequest, *, db_path=None) → int  # returns log_id
# Raises CommunicationError if no provider configured or missing contact info

# Communication log
list_logs(*, client_id: int | None = None, limit=50) → list[dict]

# Client follow-up preferences
get_client_followup_prefs(client_id) → list[dict]
save_followup_pref(client_id, *, contact_id, email, phone, preferred_channel) → int
```

### 4.2 GST/Tally Service (`src/gst_tally_integration/service.py`)

```python
# Initialisation
init_gst_tally(db_path=None) → None

# GST credentials
list_gst_credentials(client_id: int | None = None) → list[dict]
save_gst_credential(client_id, gstin, username, password) → int

# Tally connections
list_tally_connections(client_id: int | None = None) → list[dict]
save_tally_connection(conn_id, client_id, *, host, port, company_name) → int
test_tally_connection(conn_id) → (bool, str | None)

# Fetch operations
fetch_from_gst_portal(client_id, gstin, data_type, period) → int  # returns op_id
fetch_from_tally(conn_id, client_id, data_type, *, period) → int  # returns op_id

# Push operations
push_reconciliation_to_tally(conn_id, client_id, period, entries: list[dict]) → int
push_to_gst_portal(client_id, gstin, return_type, period, data: dict) → int

# Confirm/cancel workflow
confirm_sync_operation(op_id, confirmed_by: str) → None
cancel_sync_operation(op_id) → None

# History
list_sync_operations(*, client_id, status, limit=50) → list[dict]
get_sync_operation(op_id) → dict | None
```

---

## 5. Provider Interfaces

### 5.1 Email Provider

```python
# providers/email.py
def send_email(config: dict, *, to_email: str, to_name: str | None,
               subject: str, body_text: str) -> (bool, str | None):
    """Returns (success, error_message). On success, error_message is the
    provider reference (message ID)."""
```

Config shape:
```json
{
  "provider_type": "smtp",
  "host": "smtp.sendgrid.net",
  "port": 587,
  "username": "apikey",
  "password": "SG.xxxxx",
  "from_address": "noreply@setu-platform.com",
  "from_name": "Setu Accounts Team"
}
```

### 5.2 WhatsApp Provider

```python
# providers/whatsapp.py
def send_whatsapp(config: dict, *, to_phone: str,
                  message_body: str) -> (bool, str | None):
    """Returns (success, provider_ref_or_error)."""
```

Supports 3 providers:
- `twilio` — uses Twilio Messages API
- `wati` — uses WATI sendSessionMessage API
- `interakt` — uses Interakt send-message API

### 5.3 GST Portal Client

```python
# gst_portal/client.py
class GstPortalClient:
    def __init__(self, gstin, username, password, *, sandbox=True,
                 client_id=None, client_secret=None):
    def authenticate(self) → (bool, str | None)
    def fetch_returns(self, period: str) → (bool, data_or_error)
    def fetch_return_status(self, return_type, period) → (bool, data_or_error)
    def fetch_invoices(self, period, return_type="GSTR1") → (bool, data_or_error)
    def push_return(self, return_type, period, data: dict) → (bool, data_or_error)
    def fetch_filing_calendar(self) → (bool, data_or_error)
```

### 5.4 Tally Client

```python
# tally/client.py
class TallyClient:
    def __init__(self, host="localhost", port=9000, company_name=None):
    def ping(self) → (bool, str | None)
    def fetch_chart_of_accounts(self) → (bool, list_or_error)
    def fetch_vouchers(self, from_date, to_date) → (bool, list_or_error)
    def fetch_ledgers(self) → (bool, list_or_error)
    def push_journal_entry(self, entry: dict) → (bool, str | None)
    def push_reconciliation_entry(self, period, gstin, entries: list[dict]) → (bool, str | None)
```

---

## 6. UI Component Patterns

### 6.1 Communications Page (`/follow-ups`)

| Section | Component | Description |
|---------|-----------|-------------|
| Client selector | Grid of client buttons | Full-width card, 3-column grid, highlights selected |
| Recipient details | Form card | Email, phone, name, channel selector — all editable |
| Message composer | Form card | Template selector + context fields (period, recon_type, etc.) |
| Preview | Card | Live rendered subject + body, refreshable |
| Send action | Button row | "Send Follow-up" (primary) + "Clear" (secondary) |
| Communication log | Card with rows | History of sent messages with status pills |

### 6.2 GST/Tally Integration Page (`/integrations`)

| Tab | Sections | Description |
|-----|----------|-------------|
| GST Portal | Client selector, fetch form | Select GSTIN, data type, period → fetch button |
| Tally | Fetch form, Push form | Fetch COA/ledgers/vouchers; push reconciliation entries as JSON |
| Connections | GST credentials form, Tally connection form | Add/edit credentials; test Tally connectivity |
| Sync History | Pending ops, All ops | Preview → confirm workflow; status tracking |

---

## 7. Integration Points with Existing Code

### 7.1 Files to Modify

| File | Change |
|------|--------|
| `src/db.py` or startup | Add `init_communications_schema(conn)` and `init_gst_tally_schema(conn)` calls |
| `setu/views/__init__.py` | Export `communication_page` and `gst_tally_page` |
| `setu/state/__init__.py` | Export `CommunicationState` and `GstTallyState` |
| Shell/sidebar config | Add nav entries for `/follow-ups` and `/integrations` routes |
| `rxconfig.py` or app config | Register the two new pages |

### 7.2 Files NOT Modified

| File | Reason |
|------|--------|
| `src/clients/service.py` | New modules read client data but don't modify it |
| `src/clients/schema.py` | New tables only, no schema alterations |
| `src/db.py` (schema) | Only additive `CREATE TABLE IF NOT EXISTS` statements |
| `setu/foundation/*` | New pages use existing Foundation components |
| Any existing view/state | New pages are additive, no refactoring needed |

### 7.3 Dependencies on Existing Modules

| New Module | Depends On | For |
|------------|------------|-----|
| Communications | `src/clients/service.py` | Reading client list, contacts |
| Communications | `src/db.py` | DB connection factory |
| GST/Tally | `src/clients/service.py` | Reading clients, branches (GSTINs) |
| GST/Tally | `src/db.py` | DB connection factory |
| GST/Tally | `src/vault/` | **TODO**: password encryption |
| Both UI | `setu/foundation/components.py` | Cards, pills, page_header, info_banner, empty_state |
| Both UI | `setu/foundation/tokens.py` | Color tokens, spacing |

---

## 8. Implementation Order

### Phase 2A: Communications Module (Priority)

```
Day 1:
  └── src/communications/models.py      ✅ Done
  └── src/communications/schema.py       ✅ Done
  └── src/communications/templates.py    ✅ Done

Day 2:
  └── src/communications/db.py           ✅ Done
  └── src/communications/service.py      ✅ Done
  └── src/communications/providers/email.py ✅ Done
  └── src/communications/providers/whatsapp.py ✅ Done

Day 3:
  └── setu/state/communication_state.py  ✅ Done
  └── setu/views/communication_page.py   ✅ Done

Day 4:
  └── Integration: register routes, sidebar, schema init
  └── Testing: send test emails, WhatsApp messages
```

### Phase 2B: GST/Tally Integration (Priority)

```
Day 4-5:
  └── src/gst_tally_integration/models.py       ✅ Done
  └── src/gst_tally_integration/schema.py        ✅ Done
  └── src/gst_tally_integration/db.py            ✅ Done
  └── src/gst_tally_integration/gst_portal/client.py ✅ Done
  └── src/gst_tally_integration/tally/client.py  ✅ Done

Day 6:
  └── src/gst_tally_integration/service.py       ✅ Done
  └── setu/state/gst_tally_state.py              ✅ Done
  └── setu/views/gst_tally_page.py              ✅ Done

Day 7:
  └── Integration: register routes, sidebar, schema init
  └── Testing: preview/confirm workflow
```

---

## 9. Testing Strategy

### 9.1 Unit Tests

```
tests/communications/
  test_templates.py       — Test {{variable}} substitution
  test_schema.py          — Test table creation
  test_db.py              — Test CRUD operations (in-memory SQLite)

tests/gst_tally_integration/
  test_schema.py          — Test table creation
  test_db.py              — Test CRUD operations (in-memory SQLite)
  test_gst_client.py      — Mock GSTN API responses
  test_tally_client.py    — Mock Tally XML responses
  test_service.py         — Test preview → confirm workflow
```

### 9.2 Integration Tests

```
  test_communications_with_client_data.py  — Test with real client data
  test_gst_tally_workflow.py               — Test full fetch → preview → confirm
```

### 9.3 Manual Testing Checklist

**Communications:**
- [ ] Client selector loads all clients
- [ ] Contact prefs auto-load when client selected
- [ ] Email/phone/name editable without persisting
- [ ] All 4 templates render correctly with variables
- [ ] Preview shows correct subject and body
- [ ] Send via email works (SMTP)
- [ ] Send via WhatsApp works (or graceful failure if no provider)
- [ ] Communication log shows sent/failed status
- [ ] Save as default persists prefs
- [ ] Log shows per-client history

**GST/Tally:**
- [ ] Client selector loads with branches/GSTINs
- [ ] GST credentials can be saved
- [ ] Tally connection can be saved and tested
- [ ] Fetch from GST Portal creates preview sync op
- [ ] Fetch from Tally creates preview sync op
- [ ] Preview shows operation details
- [ ] Confirm executes the operation
- [ ] Cancel marks as cancelled
- [ ] Sync history shows all operations with status
- [ ] Push to Tally goes through preview → confirm

---

## 10. Security Considerations

1. **GST Passwords:** Stored as plaintext in current implementation. **Must** be encrypted using `src/vault/service.py` before production deployment. The `TODO` is marked in `service.py`.

2. **SMTP/WhatsApp API keys:** Stored in `config_json` as JSON plaintext. Should be encrypted at rest in production.

3. **Tally access:** Tally HTTP API has no built-in authentication. The connection should be over a trusted network (VPN or localhost only).

4. **Audit trail:** All communications are logged in `communication_log`. All sync operations are logged in `sync_operations` with `confirmed_by` tracking.

5. **Preview → Confirm:** Push operations (GST filing, Tally journal entries) are never executed without explicit admin confirmation. All pending operations are cancellable.

---

## 11. File Manifest

### Phase 2 Complete File List

```
phase2/
├── README.md                           — Integration guide
├── context.md                          — This file
│
├── communications/                    → src/communications/ (8 files)
│   ├── __init__.py
│   ├── models.py
│   ├── schema.py
│   ├── db.py
│   ├── service.py
│   ├── templates.py
│   └── providers/
│       ├── __init__.py
│       ├── email.py
│       └── whatsapp.py
│
├── gst_tally_integration/             → src/gst_tally_integration/ (9 files)
│   ├── __init__.py
│   ├── models.py
│   ├── schema.py
│   ├── db.py
│   ├── service.py
│   ├── gst_portal/
│   │   ├── __init__.py
│   │   └── client.py
│   └── tally/
│       ├── __init__.py
│       └── client.py
│
└── setu_state/                        → setu/state/ (2 files)
│   ├── communication_state.py
│   └── gst_tally_state.py
│
└── setu_views/                        → setu/views/ (2 files)
    ├── communication_page.py
    └── gst_tally_page.py
```

**Total: 23 files** ready for integration.

### To Deploy:

1. Copy files to the main tree:
```bash
cp -r phase2/communications/ src/communications/
cp -r phase2/gst_tally_integration/ src/gst_tally_integration/
cp phase2/setu_state/*.py setu/state/
cp phase2/setu_views/*.py setu/views/
```

2. Register schema init + routes (per README.md)

3. Add sidebar nav entries

That's it — zero existing code changes required.
