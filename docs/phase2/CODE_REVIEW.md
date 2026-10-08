# Phase 2 — Code Review

Review of the `phase-2` branch (commit `3b53d12`) performed while merging it into the default
branch and integrating it into the app tree.

**Legend:** 🔴 blocker · 🟠 high · 🟡 medium · ⚪ low · ✅ fixed during integration · ⏳ open recommendation

Every 🔴/🟠 item below was verified with a test (see `tests/test_phase2_*`) or a manual run.

---

## 1. Blockers (the code could not run as merged)

### 🔴 ✅ 1.1 Syntax error — `src/gst_tally_integration/db.py`
`create_sync_op()` had a malformed keyword-only marker:
```python
    client_id: int, direction: str, source: str, data_type: str,
*    period: Optional[str] = None,          # SyntaxError
```
A bare `*` must be followed by a parameter **without** a default, so the whole module failed to
import (`SyntaxError: named arguments must follow bare *`). Fixed to a trailing `*,` on the
previous line.

### 🔴 ✅ 1.2 Every row→dict helper used the wrong `PRAGMA` column
All list/get functions in both new DB modules built dicts with `d[0]` from
`PRAGMA table_info(...)`, but each row is `(cid, name, type, notnull, dflt_value, pk)` — so the
keys were integers (`0, 1, 2, …`), not column names. Every `list_*` / `get_*` call therefore
returned integer-keyed dicts and any caller doing `row["gstin"]` / `row["name"]` raised
`KeyError`. This silently broke the entire data-access surface of both modules.

Affected (10 sites): `communications/db.py` — `list_providers`, `list_templates`, `get_template`,
`list_logs`; `gst_tally_integration/db.py` — `list_gst_credentials`, `list_tally_connections`,
`list_sync_ops`, `get_sync_op`, `list_gst_returns`, `list_tally_data`.
Fixed `d[0]` → `d[1]` throughout.

### 🔴 ✅ 1.3 Non-existent client API — `setu/state/gst_tally_state.py`
`select_client()` called `client_svc.list_branches(client_id, include_inactive=False)`, but the
client model was flattened to a single `gstin` column on `end_clients` and **`list_branches` does
not exist**. Every client selection raised `AttributeError`. Rewritten to read
`client.get("gstin")` directly, and the branch/GSTIN `<select>` in the view became a GSTIN input.

### 🔴 ✅ 1.4 Wrong import paths — `setu/views/*.py`
Both pages imported their state from `phase2.setu_state.*`, which only works while the code sits in
the staging folder and breaks the moment it is copied to `setu/state/` (as the README instructs).
Fixed to `from setu.state.communication_state import CommunicationState` and
`from setu.state.gst_tally_state import GstTallyState`.

### 🔴 ✅ 1.5 Invalid Reflex expressions (pages would not compile)
- `communication_page.py`: `rx.cond(A != "" or B != "")` — Python `or` on state Vars raises
  `VarTypeError`. Replaced with bitwise `|` (`(A != "") | (B != "")`).
- `communication_page.py`: `log.get("channel", "").upper()` and
  `(log.get("subject", "") or "(no subject)")` inside `rx.foreach` — Vars have no `.upper()` and
  can't be used with `or`. Replaced with `.to(str).upper()` and nested `rx.cond`.
- `gst_tally_page.py`: same `.upper()`/concatenation issues in the sync-history row; replaced with
  multiple `rx.text(...)` children.
- `gst_tally_page.py`: `c.pill(op["status"], variant=rx.match(...))` — `pill()` resolves its
  `variant` at Python build time (`_pill_colors` dict lookup), so a Var variant raised `KeyError`.
  Added a `_status_pill()` helper that returns literal-variant pills per status branch.

---

## 2. High-severity logic bugs

### 🟠 ✅ 2.1 Provider updates silently discarded — `src/communications/db.py`
`upsert_provider()` committed on the INSERT path but **not** on the UPDATE path, so editing an
existing provider (e.g. rotating an API key) returned success while the change was lost. Added the
missing `conn.commit()`. Pinned by `test_provider_update_persists`.

### 🟠 ✅ 2.2 Duplicate follow-up preferences — `src/communications/db.py`
`upsert_followup_pref()` looked up the existing row with `WHERE client_id=? AND contact_id=?`.
When `contact_id` is `NULL`, `= NULL` is never true in SQL, so it always inserted a new row (and
`UNIQUE(client_id, contact_id)` does not dedupe NULLs either). Switched to `contact_id IS ?`,
which is NULL-safe. Pinned by `test_followup_prefs_upsert_is_single_row`.

### 🟠 ✅ 2.3 GST push payload was dropped — `src/gst_tally_integration/service.py`
`push_to_gst_portal()` stored only `data_keys` in `details_json`, but `_execute_push()` later read
`details.get("data", {})` and pushed an **empty payload**. Now the payload is persisted in the
preview details. Pinned by `test_gst_push_retains_payload`.

### 🟠 ✅ 2.4 Push executed against the wrong connection/credential
`_execute_push()` used `next(iter(...))` — the *first* Tally connection / GST credential for the
client, ignoring which one the operation was created with. With multiple GSTINs/companies this
pushed to the wrong place. Now the connection/GSTIN is stored in the preview details and the push
resolves it explicitly (falling back to the first only if missing). Pinned by
`test_tally_confirm_executes_and_uses_the_right_connection`.

### 🟠 ✅ 2.5 Plaintext credentials at rest
`save_gst_credential()` carried a `# TODO: Encrypt password using src/vault/service.py` and stored
the password verbatim in the `encrypted_password` column, while the API/UI treated it as encrypted.
Now encrypted with the C4 vault (Fernet) on write and decrypted only at the exact point of use
(fetch/confirm), with a legacy-plaintext fallback. Pinned by
`test_credentials_encrypted_at_rest_and_hidden`.

### 🟠 ✅ 2.6 XML injection / malformed XML — `src/gst_tally_integration/tally/client.py`
`push_journal_entry()` interpolated ledger names, narration and voucher numbers straight into XML.
A ledger named `ITC & Co` or `Output <GST>` produces malformed XML (or worse). Added
`xml.sax.saxutils.escape()` on all user-supplied values. Pinned by
`test_tally_xml_escapes_special_characters`.

---

## 3. Medium-severity issues

### 🟡 ✅ 3.1 `confirmed_by` never recorded the real user — `setu/state/gst_tally_state.py`
`confirm_op()` did `getattr(AuthState, "username", "admin")`, which reads the **class-level field
default** (`""`), not the signed-in session value — so the audit trail recorded an empty user.
Both new states now subclass `AuthState` (matching every other page state) and use
`self.username`.

### 🟡 ✅ 3.2 Confirm/list lookups scan a capped list
`confirm_sync_operation()` fetched `list_sync_ops(status="previewing")` (default limit 50) and
`get_sync_operation()` fetched `limit=1000`, then filtered in Python — so a busy queue could fail
to find an operation that exists. Added `db.get_sync_op(conn, op_id)` and used it for both direct
lookups.

### 🟡 ✅ 3.3 Reconciliation totals summed the wrong amount
`push_reconciliation_to_tally()` computed `total_debit`/`total_credit` from `e["amount"]` (the
entry), not `leg["amount"]` (the ledger leg), so the preview totals were wrong for any real entry.
Fixed to sum the leg amounts. Pinned by `test_push_to_tally_preview_totals_use_leg_amounts`.

### 🟡 ⏳ 3.4 Inconsistent period formats (open)
Three different conventions coexist with no normaliser:
`MMYYYY` (`082026`) for GST, `YYYYMMDD` (`20260831`) for Tally vouchers, and `2026-08` in the
communications templates. `fetch_from_tally()` also derives `from_date = period[:6] + "01"`, which
silently produces a wrong range if a `YYYY-MM` value is supplied. Recommend a single
`normalize_period()` helper in `src/shared/` and use it at every boundary.

### 🟡 ⏳ 3.5 Blocking I/O inside Reflex event handlers (open)
`send_followup`, `fetch_*`, `test_tally_connection` and `confirm_*` perform synchronous SMTP/HTTP
calls (30–120 s timeouts) inside event handlers. This can pin a worker and make the UI feel hung.
Consider making these handlers `async` and running the network calls via
`asyncio.to_thread(...)`, or moving them to a background task.

### 🟡 ⏳ 3.6 Provider secrets still plaintext (open)
The C4 vault is now used for GST passwords, but SMTP passwords and WhatsApp API tokens remain
plaintext JSON in `followup_providers.config_json`. Route them through the vault too.

### 🟡 ⏳ 3.7 Weak duplicate guard on credentials (open, mitigated)
`gst_credentials` previously relied on a `SELECT`-then-`INSERT` upsert. A unique index on
`(client_id, gstin)` was added; a fully atomic `ON CONFLICT ... DO UPDATE` would be better.

---

## 4. Low-severity / polish

| # | Status | Item |
|---|--------|------|
| ⚪ | ✅ | `whatsapp.py` used `urllib.parse.urlencode` while only importing `urllib.request`/`urllib.error` (worked only via an implicit transitive import). Added the explicit import. |
| ⚪ | ✅ | `providers/email.py` imported `json` without using it. Removed. |
| ⚪ | ⏳ | `models.py` dataclasses (`ProviderConfig`, `FollowUpTemplate`, `CommunicationLogEntry`, `GstReturnData`, `TallyData`, `SyncOperation`) are largely unused — the service layer returns dicts. Either adopt them or drop them. |
| ⚪ | ⏳ | `save_gst_credential()` sets `last_synced_at=datetime('now')` on update, which is semantically wrong (saving a credential is not a sync). |
| ⚪ | ⏳ | `test_tally_connection()` loads *all* connections then filters in Python instead of a direct lookup. |
| ⚪ | ⏳ | The "Clear" button on the follow-up page calls `toggle_send_form()`, which only hides the form — it does not clear the fields. Misleading label. |
| ⚪ | ⏳ | `send_followup()` with channel `both` sends two log rows but returns only the first `log_id`. |
| ⚪ | ⏳ | `list_providers`/`list_*` run `PRAGMA table_info` on every call; a module-level column cache would avoid repeated work. |
| ⚪ | ⏳ | `_summarize_fetched_data()` calls `len(json.dumps(data))` just to report a size — potentially expensive for large fetches. |
| ⚪ | ⏳ | `md`/docs: `context.md` file manifest says "23 files" while the folder shipped 25; `SyncOperation.status` docstring omits `cancelled`. |

---

## 5. What's genuinely good

- Clean, consistent layering (`models / schema / db / service`) that matches the existing
  `src/clients` module — easy to reason about and test.
- The **preview → confirm** workflow is the right shape for irreversible external calls, and it is
  backed by a real audit trail (`sync_operations` + `confirmed_by`/timestamps).
- The provider abstraction for email/WhatsApp is a sensible seam.
- Schema is additive only (`CREATE TABLE IF NOT EXISTS`) and idempotent, and was correctly attached
  to the single boot list (`src/bootstrap.py`).
- Unit tests are meaningful (they caught 7 of the defects above) and fast, using the existing
  seeded-DB fixture pattern.

---

## 6. Verification performed

```
venv/bin/python -m pytest tests/ -q          →  514 passed   (493 baseline + 21 new)
venv/bin/python -c "import setu.setu"         →  app builds; /follow-ups and /integrations registered
bootstrap.init_all(tmp_db)                    →  9 new tables created, 4 templates seeded
```
