# Client-ID identity migration — STEP 0 inspection report

Branch: `client-id-migration`. No code changed yet (this is the inspection).

## 0. Prerequisites / STOP conditions

| Check | Result |
|---|---|
| `client_picker` / `period_picker` / `recon_type_picker` exist in `setu/foundation/components.py` | **YES** — `client_picker:613`, `recon_type_picker:671`, `period_picker:699`. Proceed. |
| `src/fingerprint.py` derives from / includes the client name? | **NO — STOP condition NOT hit.** Fingerprints key on source-record identity only: GST = `gstin + normalised invoice + doc type`; TDS = `pan + section + period + amount`; OTHER = `source_key + reference + period + amount`. Client name is never an input. Safe to re-key `review_state` by `client_id` without touching stored fingerprint values. |
| `end_clients.client_id` type | `INTEGER PRIMARY KEY AUTOINCREMENT` (see `src/clients/schema.py`). IDs are never reused. No table rebuild needed. |
| `schema_migrations` table exists? | **NO.** A migration framework must be added. |

## 1. Client-identity tables & columns

There are **two parallel client identities** in the DB today:

- **Name string** (folder name / F2-derived): `runs.client`, `review_state.client`,
  `result_field_overrides.client`, `per_client_team_assignments.client`,
  `users.end_client_ref`, `ingestion_results.client_ref` (+ nullable `client_id`),
  `ingestion_shape_cache.client_ref` (+ nullable `client_id`),
  and the composed `ingestion_results.upload_key` = `"<client>/<period>/<source_type>/<filename>"`.
- **Integer `client_id`** already used by: `raw_uploads`, `documents`,
  `column_mapping_profiles`, `invoice_uploads`, `filing_*`, `f5` (`validation_blocks`,
  `sync_health_status`), `f6_*`, `c5` (`instructions`), `rules` (`rule_versions`,
  `rule_change_log`), `vault` (`encrypted_credentials`, `dpdp_requests`),
  `communications`, `gst_tally_integration`, `module2` (`reconciliation_exceptions`),
  `action_center`, `eligible_credit_figures`, `recon_of_recon_results`,
  `edit_history_entries`, `ai_fallback_events`, `vault_security_events`.

### Live values (local `db/poc.db`)

- `end_clients`: `1 = "Test Client"` (gstin `07AAACP9472A1ZJ`), `2 = "Meridian Fabrics"` (pan `AABCM1234K`, gstin `27AABCM1234K1Z9`).
- `runs.client`: `"Test Client"` ×16, `"Meridian Fabrics"` ×1 (17 runs, all `2026-08` GST).
- `review_state.client`: `"Test Client"` ×1 (fingerprint `b1813227ef868e0666eab127`, reviewed, run 7).
- `ingestion_results`: `client_ref` = `"Test Client"`×9, `"Meridian Fabrics"`×3, `"TestClient"`×1; `client_id` = 1×5, NULL×5, 2×3.
- `ingestion_shape_cache`: `client_ref` = `"Test Client"`×1 (client_id=1).
- `column_mapping_profiles`: `client_id` = 1×3.
- `users.end_client_ref`: all NULL.
- `result_field_overrides`, `per_client_team_assignments`, `dpdp_requests`: empty.
- `tally_export_batches`: no client column (holds `upload_ids`).

## 2. Name → client mapping (STEP 0.5)

All names are **exact / normalised-exact** matches. **No fuzzy, no ambiguous**:

| Name (source) | Folders | → client_id | Match kind |
|---|---|---|---|
| `Test Client` | `data/Test Client/` | **1** | exact |
| `TestClient` | — | **1** | normalised-exact |
| `Meridian Fabrics` | `data/Meridian Fabrics/` | **2** | exact |

No names without an F2 client. No new migrated clients needed for the local DB
(the migration must still create them for any unmatched name found on other
servers — the deployed Fly DB may differ).

### Data folders

```
data/Test Client/2026-07/{credit_notes,gstr2b,ims,tally}
data/Test Client/2026-08/{exports,gstr2b,ims,tally}
data/Meridian Fabrics/2026-08/{credit_notes,exports,gstr2b,ims,tally}
```
→ move to `data/1/...` and `data/2/...`.

## 3. Code paths that build a path or match a name

**Path building (client name as first segment):**
`src/data_paths.py:22` `source_data_path(client, period, source_type)`;
`src/export.py:28,169`; `src/reconciliation/report_export.py:92,1128,1143`;
`src/ingestion_ai/service.py:384,517,1308`; `src/ingestion_ai/normalizer.py:2306,2323,2405,2477,2599,2670`;
`src/ingestion.py:124,286,491`; `setu/state/reconcile_state.py:960,1140,2583`;
`verify_report_defects.py:76`.

**Name matching / normalisation — 7 near-duplicate `norm()` + exact-then-prefix matchers:**
`src/ingestion_ai/service.py:284` `_folder_name_for_client`;
`src/queries.py:156` `_client_id_for_run`; `src/reconciliation/run_model.py:19` `resolve_client_id`;
`src/action_center/report.py:92` + `:95`; `src/action_center/service.py:362,396`;
`setu/state/phase1_state.py:839`; `setu/state/reconcile_state.py:2592`.

**Client listing for pickers:**
`discovery.list_clients()` (`src/shared/discovery.py:117`) callers:
`setu/state/phase1_state.py:189`, `setu/state/reconcile_state.py:548,584`,
`setu/state/admin_state.py:256`.

**Name-keyed state vars:** `Phase1State.clients/ctx_client`,
`ReconcileState.clients/ctx_client`, `AdminState.team_clients/team_selected_client`,
`ActionCenterState.filter_client/client_options`,
`IngestionAiState.client_ref/client_label_map/filter_client`,
`DocumentsState.vault_client_name/client_names/detail_client`,
`CommunicationState.selected_client_name`, `GstTallyState.selected_client_name`,
`ReportState.client_folder/client_name`, `InvoiceExtractState.upload_client_name/client_names`.

**Name-parameter functions (identity/lookup):** `src/queries.py` (`mark_reviewed`,
`clear_review`, `get_review_state`, `set_field_override`, `get_field_overrides`,
`apply_record_overrides`, `run_ids_for_fingerprint`, `list_runs`),
`src/runner.py` (`execute_run`, `_run_gst/tds/other`),
`src/ingestion.py` (`load_source_file`, `load_gst_pair`, `load_tds_pair`, `load_other_*`),
`src/export.py:152` `export_run` (uses `run["client"]`),
`src/auth/db.py:448,459,475` and `src/auth/service.py:490,498,514,530`
(`assign_user_to_client`, `team_for_client`, `visible_clients`),
`src/ingestion_ai/normalizer.py` (`upload_key`, `normalize_source_file`, `get_stored_result`,
`_canonical_frame_for`, `load_canonical_pair`, `load_note_frames`),
`src/ingestion_ai/service.py` (`upload_and_infer`, `materialize_upload_to_disk`,
`set_upload_period`, `_move_upload_on_disk`, `confirm_mapping`, `re_run_*`),
`src/action_center/report.py:468` `build_period_report(folder, period)`.

## 4. Decisions taken

1. `runs`: add `client_id` (FK) + rename old `client` → `client_name_at_run`
   (immutable display snapshot; never a lookup key).
2. `review_state` PK → `(client_id, period, recon_type, fingerprint)`.
3. `result_field_overrides` PK → `(client_id, period, recon_type, fingerprint, side, field)`.
4. `ingestion_results.upload_key` re-keyed to `"<client_id>/<period>/<source_type>/<filename>"`;
   `client_ref` column dropped, `client_id` made NOT NULL, backfilled from the mapping.
5. `ingestion_shape_cache`: unique key → `(client_id, source_type, header_signature)`.
6. `per_client_team_assignments.client` → `client_id INTEGER`.
7. `users.end_client_ref` → `end_client_id INTEGER` (all NULL today).
8. `end_clients`: add `origin` (`created`|`migrated`, default `created`);
   partial UNIQUE index on non-null `gstin` for active clients. PAN/TAN stay non-unique.
9. Data folder layout → `data/<client_id>/<period>/<source_type>/`.
10. Deployed Fly DB may hold different names than local; migration is data-driven
    (scans `runs`, `review_state`, `ingestion_*`, and `data/` folders), not hardcoded.

## 5. STOP-condition outcome

**No STOP hit.** Proceeding to STEP 1.
