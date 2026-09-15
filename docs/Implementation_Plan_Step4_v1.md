# Setu — Step 4 PRD → Reconciliation Agent: Compiled Implementation Plan (v1)

**Source of truth:** `Setu_Step4_PRD_Complete (1).html` (17-module locked PRD, v1.0, 2-Sep-2026) + Step 3 shared vocabulary (Flagged Items & 5 subtypes, 6-stage lifecycle, AI Recommendation / AI Outcome pairs).
**Scope of this plan:** the **GST 2B vs Books reconciliation agent** (Module 2A core + the F5 / M3 / M7 / M8 / M9 surfaces it must feed), built on the existing `recon_engine` codebase.
**Prepared:** 2-Sep-2026 · status: **30/30 tests green**, agent pipeline proven end-to-end.

---

## 1. How the existing engine maps to the locked PRD (the reconciliation)

### 1.1 Module 2 — Reconciliation Engine (2A GST)

| PRD locked requirement | Our implementation | Status |
|---|---|---|
| Configurable matching keys + tolerance | `MatchRules` (`invoice_edit_max`, `date_window_days`, `value_tolerance`, `require_gstin_exact`), per-client via `client_config` | ✅ built |
| 4-way classification **Matched / Not in Books / Not in Portal / Amount Difference** | `MatchStatus` enum — exactly these four | ✅ built |
| Fuzzy matching with **visible confidence score**, never a bare flag | Levenshtein + `Confidence` High/Medium/Low, exposed on `MatchResult` | ✅ built |
| Exception queue — every non-clean match becomes a Reconciliation Exception | `ReviewPacket.exceptions` (carries evidence + reasons + confidence) | ✅ built |
| Summary dashboard feeding Module 7 | `build_summary` + HTML/MD/XLSX/CSV report pack | ✅ built (v1 of M7 export) |
| **IMS Accept/Reject/Pending recommendation** (thin rules layer, conservative) | Partially — supplier `status` feeds ITC eligibility; no explicit IMS recommendation object | ⚠️ gap → Phase 2 |
| **"A live eligible credit as things stand" figure** | `ITCComputation` + `gstr3b_draft` computed every run | ✅ built |
| §17(5) **blocked credit** and **reverse-charge (RCM)** items get separate handling; "a matched invoice isn't necessarily eligible" | NOT modelled — eligibility currently = matched + non-filer only | ❌ gap → Phase 2 |
| **Materiality threshold per reconciliation type** (2A/2B/2C separate; firm-wide default + per-client override) | NOT modelled — no materiality gate | ❌ gap → Phase 2 |
| Manual/batch ingestion for v1 | File-based intake (`agent.auto_reconcile`) | ✅ matches locked decision |
| AI vs rules: **core matching deterministic (~85–95%)**; fuzzy AI-assisted with visible confidence | Matching/arithmetic 100% Tier-A deterministic; LLM (Sarvam-105B) provides rationale + confidence explanation only. *Reconciled decision: deterministic fuzzy core + LLM judgement layer, guarded by Drafter/Verifier.* | ✅ (with documented deviation) |
| Explainability: every classification shows records compared + key/tolerance; confidence visible | `MatchResult.reasons`, `evidence`, `edit_distance`, `date_delta`, `value_delta_pct` | ✅ built |

### 1.2 F5 — Data Integrity & Validation Layer

| PRD requirement | Our implementation | Status |
|---|---|---|
| Completeness checks (control totals) | `control_totals_check` (count + sum vs source totals) | ✅ built |
| Structural validity (GSTIN format, dates, duplicates) | `validate_structure` + duplicate detection | ✅ built |
| **Reconciliation-of-the-reconciliation** (matched+unmatched+excluded = ingested totals, both sides) | `recon_of_recon` — exactly this | ✅ built |
| F5 **flags and blocks, never fixes** | BLOCKED packet + non-zero exit; no auto-correction | ✅ built |
| Sync health per client per source (healthy/degraded/failed + history) | Per-run F5 status only; no persisted history | ⚠️ gap → Phase 4 |
| Escalation to Partner on completeness/recon failure | Non-zero exit code (CI-safe); no alert channel | ⚠️ gap → Phase 4 |
| Override requires stated reason + permission | Not modelled (no override flow yet) | ⚠️ gap → Phase 3/5 |
| **Entirely rules-based** | ✅ matches — hard deterministic gate | ✅ built |

### 1.3 Module 3 — Corrective Entry Assistant

| PRD requirement | Our implementation | Status |
|---|---|---|
| Draft generation with rationale + linked evidence | `review_packet._draft_jv_for_portal` (Purchase/ITC Dr, Creditors Cr, narration, rationale) | ✅ built |
| **Balance validation before shown to reviewer** | `DraftedJV.is_balanced()` asserted in tests (#30) | ✅ built |
| Review Packet screen (draft + rationale + evidence in one view) | `ReviewPacket` object + HTML report card | ✅ built (screen = future UI) |
| **Approve / Modify / Reject workflow** (Stage 4) | NOT built — no state, no persistence | ❌ gap → Phase 3 |
| **Batch approval** for high-confidence low-materiality | NOT built (needs Module 2 confidence + materiality) | ❌ gap → Phase 3 |
| **Modify re-runs balance validation before approval** | N/A yet (workflow not built) | ❌ gap → Phase 3 |
| Posts to Tally **strictly after approval** | Engine never posts — safe by default; approval gate must precede any Tally write | ✅ built (by absence) + gate in Phase 3 |
| AI Recommendation/Outcome pairs per drafted entry (feed M9) | ai_summary + narrations generated; no structured paired objects | ⚠️ gap → Phase 5 |
| Audit Trail entries for approval/modify/reject/post | Not modelled | ❌ gap → Phase 3/5 |
| Draft generation AI-assisted; account/rate lookups deterministic | LLM writes narration only; accounts/amounts from Tier-A | ✅ built |

### 1.4 Module 7 — Dashboards & Reporting

| PRD requirement | Our implementation | Status |
|---|---|---|
| Exportable reports (operational status view) | MD + XLSX + **HTML dashboard** + CSVs | ✅ built |
| Per-client dashboard | HTML report is per client + period | ✅ built |
| Every number clickable through to origin | HTML is static; linked evidence fields present | ⚠️ gap → Phase 4 |
| **Data-freshness timestamp on dashboard + exports** | HTML shows "Generated" timestamp; per-source freshness not yet | ⚠️ gap → Phase 4 |
| Firm-wide portfolio view (Partner) | Not built — batch runner can aggregate | ❌ gap → Phase 4 |
| Read-only aggregation, originates nothing | ✅ matches | ✅ built |

### 1.5 Module 8 — Action Center (queue)

| PRD requirement | Our implementation | Status |
|---|---|---|
| Unified queue of Flagged Items (all subtypes) | Exceptions list only; no cross-module queue | ❌ gap → Phase 5 |
| Evidence + recommendation per item | `MatchResult` carries evidence/reasons/confidence | ✅ built (feeds M8) |
| Assignment, priority, due date | Not modelled | ❌ gap → Phase 5 |
| Materiality-gated items marked + excluded from batch | Needs Module 2 materiality first | ❌ gap → Phase 2→5 |

### 1.6 Module 9 — AI Accuracy & Model Governance

| PRD requirement | Our implementation | Status |
|---|---|---|
| **AI Recommendation / AI Outcome pairs** captured automatically | Not captured — ai_summary/narrations are ephemeral strings | ❌ gap → Phase 5 |
| Accuracy metrics per engine/client/over time (not static confidence) | Not computed | ❌ gap → Phase 5 |
| Drift detection; minimum sample size before alert | Not built | ❌ gap → Phase 5 |
| Simulation / dry-run mode for rule/tolerance changes | Not built (easy: re-run matcher with modified rules over stored data) | ❌ gap → Phase 5 |
| Accuracy never auto-adjusts thresholds | ✅ philosophy already enforced (human-gated) | ✅ built |

### 1.7 C1 / C2 alignment

- **C1 rules**: `MatchRules` + `Settings` = the firm-wide default + per-client override pattern ✅ (needs effective-dated Rule Versions later — Phase 6).
- **C2 portal connect**: v1 manual/batch ingestion ✅ exactly matches the locked decision. IMS write-back and API refresh deferred (consistent with PRD: "real-time only once API access confirmed").

---

## 2. Calculation reconciliation — our numbers vs the PRD's rules

| Calculation | PRD anchor | Our engine | Reconciled verdict |
|---|---|---|---|
| Gross gap = portal − books | M2 "eligible credit as things stand" context | `portal_total − book_total` | ✅ |
| Matched = exact (GSTIN+inv+date+value) | M2 configurable key | GSTIN-bucketed greedy + tolerance | ✅ |
| Fuzzy match: Levenshtein ≤2, date ±7d, value ±1% | C1 defaults | `MatchRules` defaults | ✅ |
| **ITC eligible / ineligible** | M2: §17(5) blocked + RCM separate; non-filer handling | eligible = matched + non-filer; else ineligible | ⚠️ **must add**: RCM flag and §17(5) blocked flag to `Invoice`/match verdict before eligibility |
| **GSTR-3B draft** (output tax, eligible, ineligible, net payable, credit) | M2 "live eligible credit" output | `ITCComputation.gstr3b_draft` | ✅ |
| **Materiality-gated routing** (2A threshold) | M2 locked decision 3 | absent | ⚠️ **must add** → Phase 2 |
| Reconciliation-of-the-reconciliation | F5 core | `recon_of_recon` sums tie back both sides | ✅ |
| JV balance (Dr == Cr) | M3 locked (balance before review) | `is_balanced()` + regression test | ✅ |
| Batch-approvability = confidence + materiality | M3 locked (inherits M2 confidence) | confidence present; materiality missing | ⚠️ Phase 2 → 3 |

---

## 3. Compiled implementation plan — the Reconciliation Agent

**Goal:** an agent that receives input documents (Excel-first, from email + portal download folders), runs the reconciliation engine + calculations, and delivers the reconciled report pack — aligned to the Step 4 PRD so the output is the seed of Modules 3, 7, 8, 9.

### Phase 0 — Foundation (DONE, this repo)
Intake → normalise → match → classify → ITC → review packet · F5 gate · deterministic Tier-A · LLM judgement-only · Drafter/Verifier · independent verification · HTML/MD/XLSX/CSV reports · 30 tests green.

### Phase 1 — Intake (Excel-first, heterogeneous sources) — ✅ **DONE (8 tests, 38/38 green)**
PRD anchors: M2 input ("GSTR-2B, IMS invoice-level data ... v1 via manual/batch ingestion"); M2 locked ("manual/batch"); F5 ("validated by F5"); F3 (auto-filing/classification, low-confidence → human queue).
1. ✅ **GSTR-2B Excel parser** — `parsers/gstr2b_excel.py`: reads B2B/B2BA invoice sheets, header-token tolerant, R/N status → active/non_filer, falls back to Invoice Value when Taxable Value column absent.
2. ✅ **Tally purchase register Excel parser** — `parsers/tally_excel.py`: header-tolerant columns, splits CGST+SGST back to total tax, feeds `parse_tally_purchase_rows`.
3. ✅ **Intake classifier + scanner** — `intake.py`: `classify_file() / scan_folder() / build_run_inputs()`; structural detection (extension, sheets, header signatures) + client/period inference (filename regex + content max-date) + **Sarvam-105B assist for ambiguous files** (strict-JSON proposal, validated against known types, graceful fallback; PRD F3).
4. ✅ **Sample Excel fixtures** — `sample_scenario.write_gstr2b_excel/write_tally_excel` mirror the canonical 9/10 dataset; value-parity with JSON/CSV test-locked.
5. ✅ **Agent accepts Excel** — `auto_reconcile` auto-detects .json/.csv/.xlsx; Excel run reproduces canonical results (matched=7, ITC 64,260, F5+verification PASS).
Acceptance met: `intake.scan(folder)` → kinds/client/period/issues; fixtures parse to identical 9/10 invoice sets.
**LLM layer:** Sarvam-105B (`sarvam-105b`) is the assist model (default in `Settings`); wiring tested via fake-client unit tests. Activate per environment with `export SARVAM_API_KEY=...` then `python -m recon_engine.intake <folder>` (LLM assist auto-enables).

### Phase 2 — Engine completeness (PRD business rules) — ✅ **DONE (11 tests, 49/49 green)**
PRD anchors: M2 locked #3 (materiality gate), M2 §6 (17(5)/RC flags), M2 §4 (IMS recommendation), M7 feed.
1. ✅ **Materiality gate** — `MatchRules.materiality_amount` (Decimal | None; firm-wide default, per-client override via `client_config.match_rules`); every `MatchResult` carries `materiality: above|below|none` (`ims.materiality_for`; `>=` threshold counts above).
2. ✅ **§17(5) + RCM flags** — `Invoice.reverse_charge` / `blocked_credit`; extracted from GSTR-2B JSON (`rcm`/`isReverseCharge`, `isBlocked`/`blocked`/`itcAvailability`), Excel (`Reverse Charge`, `ITC Availability` columns, Avail/Not Avail ↔ bool), Tally rows tolerated. `compute_itc` EXCLUDES matched-but-blocked and matched-but-RCM rows regardless of supplier status.
3. ✅ **IMS thin rules layer** — `recon_engine/ims.py::recommend_ims` → Accept|Reject|Pending|N/A, conservative (fuzzy/diff/unbooked/non-filer → Pending; §17(5)/RCM → Reject; books-only → N/A). Attached to every result at match time AND exposed via `MatchResult.to_dict()`.
4. ✅ **Live credit position** — `ITCComputation.itc_position` = eligible_now / blocked / awaiting_supplier / disputed / unbooked (sum reconciles to total portal ITC); serialised in the packet for Module 7.
5. ✅ **Independent verification extended** — `verify_packet(packet, portal_path, books_path)` with its own Excel readers + flag-aware ITC; `auto_reconcile` forwards the REAL input files (JSON/CSV/Excel) so the cross-check always reprocesses the actual run data.
Acceptance met: sample still 4-way balanced; canonical numbers unchanged (64,260 eligible); rules fixture drops to 25,560 eligible / 64,440 ineligible / net 94,440; packet carries materiality + IMS + position; F5 + verification PASS on both JSON and Excel intake paths.

### Phase 3 — Module 3 corrective-entry workflow — ✅ **DONE (14 tests, 63/63 green)**
PRD anchors: M3 locked #1 (balance re-validation before approval), M3 locked #2 (batch approval on high-confidence + below-materiality), M3 #4 ("strictly after approval" post), audit trail.
1. ✅ **Approve / Modify / Reject / Post state machine** — `recon_engine/review_queue.py`: `ReviewStatus` (pending/approved/modified/rejected/posted), `ReviewItem` (ref, kind jv|exception, priority, batch_approvable, jv/match refs), enforced transitions + `InvalidTransitionError`/`UnbalancedJVError`.
2. ✅ **Balance re-validation on Modify** — `modify()` re-checks `is_balanced()` on the new lines; unbalanced lines are refused, `modify_blocked` audited, status unchanged. `approve()` on a JV also re-checks.
3. ✅ **Batch approval (M3 locked #2)** — `batch_approve()` approves ONLY matched+HIGH+below-materiality items (inherits M2 confidence/materiality). JVs (low confidence), above-threshold and IMS-Reject items stay pending.
4. ✅ **Post gate** — `TallyPoster` stub: POST only from approved/modified AND with a configured target (TALLY_ODBC_DSN via Settings); refusal is audited (`post_failed`). Engine never writes to Tally.
5. ✅ **Audit trail + persistence** — `AuditEntry` on every action (who/when/why); `ReviewQueue.save()/load()` JSON round-trip rehydrating JV lines, match evidence, materiality/IMS flags and audit.
6. ✅ **CLI surface** — `python -m recon_engine.review_queue <queue.json> --approve/--reject/--post/--batch --actor --out`; agent emits `reports/review_queue.json` + `--materiality` CLI flag on the agent.
Acceptance met: workflow tests (approve/modify/reject -> audit trail; unbalanced blocks approval AND modify); batch = exactly the 3 below-materiality HIGH items; post refused without gate. Live round: approve JV + batch 3 + reject blocked item, post refused (no DSN) -> audit trail shows post_failed; statuses/audit survive JSON round-trip.

### Phase 4 — Delivery surface (API + batch + M7 reports)
1. **FastAPI service** (`api.py`) — `POST /api/clients/{id}/recon` (multipart Excel/JSON/CSV upload) → runs Phase 1→2 pipeline → returns review-packet JSON + report URLs; `GET /api/reports/{name}` serves HTML/XLSX/CSV.
2. **Batch runner** (`batch.py`) — point at an inbox folder; per-client report packs under `reports/<client>/`; aggregate summary; exit-code contract preserved; firmware for month-end close.
3. **M7 report upgrades** — per-source freshness timestamps on HTML; clickable links to evidence; portfolio summary (firm-wide) when batch aggregates.
Acceptance: API smoke tests (upload → packet → report download); batch over a 2-client fixture; HTML shows freshness per source.

### Phase 5 — Governance (Module 9) + Action Center hook (Module 8)
1. **AI Recommendation / AI Outcome pairs** — every LLM judgement (summary, narration, fuzzy rationale) logged as `{recommendation_id, engine, model, task, input_hash, output, confidence, created_at}`; human review (Phase 3) writes the paired Outcome (accepted/modified/rejected).
2. **Accuracy metrics** — acceptance rate per engine/client/period (exclude un-reviewed pairs; minimum sample size before drift alert) — Module 9 §6 rules.
3. **Simulation / dry-run mode** — re-run matcher with modified `MatchRules` against stored historical inputs, diff the classifications (`what would have changed`) — offered, not mandatory, logged if skipped (M9 locked).
4. **Module 8 surface** — unified queue view over exceptions (evidence + recommendation + assignment stub), materiality marks enforced.
Acceptance: governance log written on every LLM-enabled run; accuracy report computed; dry-run shows before/after classification diff.

### Phase 6 (deferred / noted)
- Effective-dated Rule Versions (C1), TDS 2B six-step chain, 2C Other sources, OCR/text extraction (F3), real-time portal API (C2) — all **explicitly out of v1** per the PRD's locked decisions; design points only.

---

## 4. Key design decisions locked for the agent (reconciled with PRD)

1. **Excel-first intake** for both GSTR-2B (portal download) and Tally register (email/export), with JSON/CSV tolerated — matches "manual/batch ingestion" (M2 locked #1) and F3's accepted types (XLS/XLSX/CSV).
2. **Matching stays deterministic Tier-A** — the PRD's "AI-assisted fuzzy" is delivered as *deterministic fuzzy + visible confidence + LLM rationale*, the LLM never touching match or math (governance guard, M9-compatible logging in Phase 5).
3. **Materiality is per-type** (2A now), firm-wide default + per-client override — PRD M2 locked #3 — added in Phase 2 before any batch-approval exists (Phase 3).
4. **No silent posting** — Phase 3 gives Module 3's approve/post gate; the agent never writes Tally without approval.
5. **§17(5)/RCM eligibility** is corrected in Phase 2 so "matched ≠ eligible" (M2 §6) is honoured before the numbers leave the engine.
6. **Governance by measurement** — Phase 5's AI Recommendation/Outcome pairs make Sarvam-105B's judgement auditable (M9 "measured AI, not assumed AI").

---

## 5. Dependency-ordered roadmap snapshot

```
Phase 1 Intake ──▶ Phase 2 Engine rules ──▶ Phase 3 M3 workflow
                      │                         │
                      └──────────▶ Phase 4 API + batch + M7 reports
                                          │
                                     Phase 5 M9 governance + M8 queue
```
Each phase ends green (tests + agent exit-code contract) and ships independently; Phases 1–4 build the agent the user runs; Phase 5 makes it governable.
