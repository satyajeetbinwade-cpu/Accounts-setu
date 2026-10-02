# Phase 2 — Additional Modules

## Overview

Two new modules built as standalone packages, ready to be merged into the
main Accounts Setu codebase:

1. **Communications**  — WhatsApp & email follow-ups to clients
2. **GST/Tally Integration** — Fetch data from GST Portal & Tally, push reconciliation entries

## File Structure

```
phase2/
├── README.md
│
├── communications/                    → src/communications/
│   ├── __init__.py
│   ├── models.py                      → Data classes
│   ├── schema.py                      → DB schema (4 tables)
│   ├── db.py                          → SQL operations
│   ├── service.py                     → Public API
│   ├── templates.py                   → Message templates
│   └── providers/
│       ├── __init__.py
│       ├── email.py                   → SMTP/SendGrid
│       └── whatsapp.py               → Twilio/WATI/Interakt
│
├── gst_tally_integration/            → src/gst_tally_integration/
│   ├── __init__.py
│   ├── models.py                      → Data classes
│   ├── schema.py                      → DB schema (5 tables)
│   ├── db.py                          → SQL operations
│   ├── service.py                     → Public API
│   ├── gst_portal/
│   │   ├── __init__.py
│   │   └── client.py                 → GSTN API client
│   └── tally/
│       ├── __init__.py
│       └── client.py                 → Tally HTTP XML client
│
├── setu_state/                       → setu/state/
│   ├── communication_state.py
│   └── gst_tally_state.py
│
└── setu_views/                       → setu/views/
    ├── communication_page.py
    └── gst_tally_page.py
```

## Integration Steps

### Step 1: Copy files into the main source tree

```bash
# Backend modules
cp -r phase2/communications/ src/communications/
cp -r phase2/gst_tally_integration/ src/gst_tally_integration/

# Reflex state files
cp phase2/setu_state/*.py setu/state/

# Reflex view files
cp phase2/setu_views/*.py setu/views/
```

### Step 2: Register DB schema initialisation

Edit `src/db.py` (or wherever `init_db()` is called) to add:

```python
from src.communications.schema import init_communications_schema
from src.gst_tally_integration.schema import init_gst_tally_schema

# In your app startup sequence:
conn = get_connection(db_path)
init_communications_schema(conn)   # New
init_gst_tally_schema(conn)        # New
```

### Step 3: Register routes in the app

Edit `rxconfig.py` or the main app config to register the new pages:

```python
# In your app.add_page calls:
from setu.views.communication_page import communication_page
from setu.views.gst_tally_page import gst_tally_page

app.add_page(communication_page, route="/follow-ups", title="Client Follow-ups")
app.add_page(gst_tally_page, route="/integrations", title="GST & Tally Integration")
```

### Step 4: Add sidebar navigation entries

Edit `setu/state/auth_state.py` to add the new routes to the nav areas.
Suggested placement:

- Under **"Reconcile"** area: add a "Follow-ups" link → `/follow-ups`
- Under **"All tools"**: add an "Integrations" link → `/integrations`

Or create a new **"Integrations"** area with both routes.

### Step 5: Install additional dependencies (optional)

For email sending, no additional packages are needed (uses Python's built-in
`smtplib`). For WhatsApp providers you may want:

```bash
# For Twilio specifically (optional — the HTTP client works without it)
pip install twilio
```

## Architecture Decisions

### Why separate service/db/schema layers?
- Matches the existing codebase pattern (`src/clients/`, `src/auth/`)
- Service layer enforces business rules; DB layer is raw SQL
- Schema can be unit-tested independently

### Why provider abstraction for communications?
- Email can use SMTP, SendGrid, or any SMTP-compatible service
- WhatsApp can use Twilio, WATI, or Interakt — configurable per-client
- New providers can be added by implementing the same function signature

### Why preview → confirm workflow for GST/Tally?
- External API calls are irreversible (filing returns, pushing to Tally)
- Admin reviews data before it's committed
- Full audit trail in `sync_operations` table
- Cancellable before confirmation

### Why store credentials as plain text (currently)?
- This is a development placeholder
- In production, credentials should be encrypted using `src/vault/service.py`
- The TODO comment is marked where encryption should be added

## Testing

```bash
# Run communications tests
python -m pytest src/communications/tests/ -v

# Run GST/Tally tests
python -m pytest src/gst_tally_integration/tests/ -v
```

(Test files are not yet created — they follow when the modules are merged.)
