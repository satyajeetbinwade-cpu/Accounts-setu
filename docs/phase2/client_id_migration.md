# Client-id identity migration — report

Branch: `client-id-migration`
Status: **Steps 0–2 complete and verified; Step 3 (call-site refactor) foundation
complete, the runtime call-sites in `runner`/`ingestion*`/`export`/`action_center`
and the Reflex `setu/` state+views remain; Step 4 tests partially done.**

Artifacts:
- `docs/phase2/client_id_migration_step0.md` — the Step 0 inspection.
- `docs/phase2/client_id_migration_report.json` — machine-readable migration
  report (mapping, folders moved, before/after row counts).
- `db/backups/poc_pre_clientid_<ts>.db` — backup written before any change.

---

## STOP conditions

| Condition | Outcome |
|---|---|
| Pickers `client_picker` / `period_picker` / `recon_type_picker` exist | **Yes** (`setu/foundation/components.py:613,671,699`). Proceeded. |
| `src/fingerprint.py` derives from the client name | **No.** GST = `gstin+invoice+doc_type`; TDS = `pan+section+period+amount`; OTHER = `source_key+reference+period+amount`. Client name is never an input → **no STOP**; `review_state` re-keyed by `client_id` without touching stored fingerprints. |

## Decisions on fuzzy / ambiguous matches

Local DB: every name matched **exactly or normalised-exactly**
(`Test Client`→1, `TestClient`→1, `Meridian Fabrics`→2). No fuzzy, no
ambiguous, no new clients created.

The resolver (`src/migrations/identity.py`) implements the ladder
exact → normalised-exact → **unique** prefix (longest overlap wins). A prefix
tie between two clients is recorded as **ambiguous** and is **never merged** —
the name is registered as its own migrated client and flagged in the report.
Names with no match become `end_clients` rows with `origin='migrated'`.

## Tables migrated (schema)

| Table | Change |
|---|---|
| `end_clients` | `+ origin ('created'|'migrated')`; partial UNIQUE index on non-blank `gstin`. PAN/TAN left non-unique. |
| `runs` | rebuilt: `+ client_id INTEGER NOT NULL → end_clients`, old name kept as `client_name_at_run` (display only). |
| `review_state` | PK `(client, period, recon_type, fingerprint)` → `(client_id, …)`. |
| `result_field_overrides` | PK → `(client_id, period, recon_type, fingerprint, side, field)`. |
| `ingestion_results` | `- client_ref`, `client_id NOT NULL`, `upload_key` re-keyed to `"<client_id>/<period>/<source_type>/<filename>"` (collision-safe). |
| `ingestion_shape_cache` | `- client_ref`, `client_id NOT NULL`, UNIQUE index → `(client_id, source_type, header_signature)`. |
| `per_client_team_assignments` | `client TEXT` → `client_id INTEGER`. |
| `users` | `end_client_ref TEXT` → `end_client_id INTEGER`. |

`schema_migrations(version, name, applied_at)` added; migration 0001 recorded.

## Data migration result (real DB copy)

- 20 inventoried tables: **row counts identical before/after**.
- `PRAGMA foreign_key_check`: **clean**.
- Runs: 16×client_id=1, 1×client_id=2; `client_name_at_run` preserved.
- `review_state`: 1 row, now `client_id=1`, still `reviewed=1`.
- `ingestion_results`: 13 rows re-keyed; one legacy name collision
  (`purchase_register_…` from `Test Client` + `TestClient`) disambiguated with
  a stable `~<result_id>` suffix — **nothing deleted**.
- Folders: `data/Test Client`→`data/1`, `data/Meridian Fabrics`→`data/2`;
  manifest at `data/_migration_manifest.json`.
- Second run: **no-op** (idempotent).

## Safety fixes found during verification

- Migration `_rebuild` now copies only columns present in the source table, so a
  legacy DB missing a later-added column (e.g. `notes_json`) still migrates and
  new-table DEFAULTs apply.
- **Boot-scoping bug fixed:** `bootstrap.init_all(temp_db)` was defaulting
  `data_root` to the *real* `data/`, so test runs migrated the real client
  folders. `data_root` is now scoped to a sibling of the DB path, so a temp DB
  can never move the real folders. (The real `data/` was restored losslessly —
  107/107 files.)
- Removed forward FKs on `users.end_client_id` and
  `per_client_team_assignments.client_id`: auth seeds at boot *before* F2
  creates `end_clients`, and SQLite needs the FK parent table present on INSERT.

## Tests

- `tests/test_client_id_migration.py` — **9 passed**: row-count preservation,
  FK-clean, review-state linkage, run-by-client_id, idempotency, dry-run writes
  nothing, unmatched→migrated client, rename-doesn't-break-lookups, same-name
  disambiguation, id non-reuse, path safety (`"../"`, bad period, unknown
  source_type rejected), and a guard that fails if a *new* module builds a
  `data/` path outside `src/data_paths.py`.
- Full suite: **477 passed / 32 failed / 39 errors** (from 408/28/103 before the
  auth fix). Remaining breakage is entirely Step 3 call-sites still passing a
  client **name**.

## Remaining work (Step 3 continuation + Step 4)

1. `src/runner.py` — `execute_run(client_id, client_name_at_run, …)`,
   `insert_run(...)`; drop the name parameter from `_run_gst/tds/other`.
2. `src/ingestion.py` + `src/ingestion_ai/{normalizer,service,db}.py` — take
   `client_id`; delete `_folder_name_for_client`; `source_data_path(client_id,…)`;
   `upload_key`/shapes keyed by `client_id`; remove name-matched shapes.
3. `src/export.py`, `src/reconciliation/report_export.py` — export dir from
   `run["client_id"]`; download filenames via `data_paths.slugify(display_name)`.
4. `src/action_center/{report,service}.py` — `resolve_client_id(folder)` and
   `_folder_key_for_client` deleted; use `runs.client_id` / assignment `client_id`.
5. `setu/state/*` + `setu/views/*` — every client Var/URL param carries
   `client_id`; pickers render `display_name` from `discovery.list_clients()`
   and return `client_id`; ingestion states stop deriving `_client_ref`.
6. Update fixtures/tests (incl. `test_pnsarees_golden.py`, the note tests) to
   pass `client_id`.
7. Let the `test_no_data_path_built_outside_data_paths` allowlist in
   `tests/test_client_id_migration.py` shrink to empty.

## Flagged (NOT changed, as requested)

- **`file_bytes` on disk vs `document_versions`** — uploads are stored both in
  `data/<id>/<period>/<source_type>/<file>` and as `document_versions.file_bytes`
  (8 rows). Source of truth is unresolved; the migration moves disk copies only.
- **`edit_history_entries` text containing client names** — not rewritten (804
  rows). Historical content preserved verbatim; only `client_id` added where the
  table already had an entity reference.
- **Concurrent uploads to the same period slot** — no locking added around
  `source_data_path` create-then-write.
- **`tally_export_batches`** has no client column (holds `upload_ids`) — left as is.
