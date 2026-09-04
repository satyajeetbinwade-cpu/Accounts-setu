# Setu — Reconciled Scope & End-to-End Flow v1.1

> **Purpose:** A single reconciled reference that merges the three Setu sources into one coherent design:
> 1. **Scope Lock** (Google Doc, v1.0) — 17 modules in 5 layers (Foundation F1–F5, Configuration C1–C4, Workflow 1–6, Insights & Action 7–9)
> 2. **System Design & Technical Architecture** (HTML) — pipeline-gated design on Sarvam AI
> 3. **Implementation Reference** (MD) — the build-focused version (already contains the **Sarvam-30B deprecation** correction)
>
> Where the sources conflict, this document states the **decision** and the reason. It is the flow-level view: what runs, in what order, under what gates, with the missing modules now present.

---

## 0. Reconciliation Summary (what this doc resolves)

| # | Conflict / Gap | Sources disagree because | Resolution (this doc) |
|---|---|---|---|
| 1 | **Sarvam-30B no longer exists** | HTML tiers 105B/30B; MD says 30B deprecated & removed | **Single model:** `sarvam-105b` for all LLM work. Optimisation via deterministic Tier-A code + Drafter/Verifier pattern + confidence gating, *not* model tiering |
| 2 | **Three NEW modules absent from HTML** | Scope adds F5, W6, M9; architecture never designed them | **Added** as first-class components (Sections 3–5 below) |
| 3 | **M8 Action Center & F4 Universal Edit missing** | Scope requires; HTML has neither | **Added** to UI/API/schema (Sections 5–6) |
| 4 | **Roles: HTML has 3, Scope requires 5+1** | Different simplification choices | **Adopt v1 role set = Partner / Manager / Senior Accountant / Article-Trainee / Admin** (map HTML's 3 into this; client login is restricted, no system access) |
| 5 | **Single vs multi-GSTIN** | HTML `clients.gstin` is singular; scope requires multi-GSTIN/branch | **Multi-GSTIN** via child table `client_gstins`; recon keys on `gstin` per client |
| 6 | **Hard-coded vs configurable match tolerance** | HTML hard-codes Levenshtein ≤2 / ±7d / ±1%; scope wants configurable | **Configurable** per client in `client_config.match_rules`; HTML values become v1 defaults |
| 7 | **Materiality-based routing missing** | Scope W4 mandates rupee-threshold human review regardless of confidence; HTML lacks it | **Added** as a gate rule + field on gate items/overrides |
| 8 | **Tenant isolation not designed** | Scope C4 wants data-layer isolation, tested; HTML only `client_id` FK | **PostgreSQL Row-Level Security (RLS)** per client + isolation test suite in CI |
| 9 | **Security vault missing** | Scope C4; HTML stores keys in env vars | **Encrypted secrets vault** (e.g., HashiCorp Vault / SOPS) used by the connector layer |
| 10 | **Filing side of C2 unmodelled** | HTML only models read-side + manual filing | **Filing module added** — filing calendar, ack capture, explicit human "Send" gate |
| 11 | **No login / security-event log** | Scope requires every login & security event logged | **`security_events` table** alongside `audit_trail` |

---

## 1. What the Scope Doc Requires (the 17-module contract)

| Layer | Modules |
|---|---|
| **Foundation** | F1 Login/Access/Mgmt · F2 Client Profile & Master Data · F3 Document & Data Repository · F4 Universal Edit & Version History · **F5 Data Integrity & Validation [NEW]** |
| **Configuration** | C1 Rules/Taxonomy/Regulatory · C2 Portal Connect & Filing · C3 System Settings · **C4 Security & Credential Vault [NEW]** |
| **Workflow** | 1 Info & Document Tracker · 2 Reconciliation Engine (2A GST / 2B TDS / 2C Other) · 3 Corrective Entry Assistant · 4 Books Readiness Check · 5 Audit & Change Tracking · **6 Anomaly & Fraud Detection [NEW]** |
| **Insights & Action** | 7 Dashboards & Reporting · 8 Action Center · **9 AI Accuracy & Model Governance [NEW]** |

**Cross-cutting non-negotiables:** no silent posting · full explainability · complete audit trail · gradual rollout · **data integrity first** (no module acts on data unless it passed F5) · **measured AI, not assumed AI** (module 9).

---

## 2. What the Architecture HTML Provides (things to keep)

These are the parts worth preserving — they are structurally correct responses to the scope:

- **Pipeline-gated state machine** (5 stages, 4 gates, PASS/BLOCK/FLAG) — the correct skeleton for "no silent posting / gate-before-advance".
- **Consistent engine pattern** (ingest → normalise → match → classify → review packet) — new engines drop in without orchestration changes.
- **Delta cycle** — a Stage-5 correction doesn't restart the pipeline; it posts, versions the TB, traces FS/tax impact.
- **Evidence + confidence + plain-English rationale in every review packet** — implements "show evidence, not just conclusions".
- **Append-only `audit_trail`** — the foundation of the audit requirement (needs widening to security events).

**Non-negotiable correction:** the HTML's **Sarvam-30B tiering does not exist anymore**. Per the implementation reference, `sarvam-105b` is the sole current chat model. All "30B" references below are replaced by deterministic Tier-A code + minimal 105B calls.

---

## 3. The Reconciled End-to-End Flow (v1.1)

This is the merged pipeline: the original 5 stages, now wrapped by the new modules. Data flows top-to-bottom; nothing advances past a gate without a human-approved review packet; F5 validates at every ingress; W6 scans within Stage 2; M9 tracks every AI output; everything worth acting on lands in M8.

```
TRIGGER: CA clicks "Start Month-End Close" for Client X
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  MODULE F5 — DATA INTEGRITY & VALIDATION  (NEW, runs at every ingress) │
│  · Completeness: control totals vs source (Tally voucher count/sum,    │
│    portal totals) — nothing advanced on missing data                   │
│  · Structural validity: GSTIN/PAN/TAN format, impossible dates, dupes  │
│  · Sync health: per source (Tally/GST/TRACES/bank) status indicator    │
│  · Reconciliation-of-the-reconciliation: matched+unmatched+excluded    │
│    sums back to ingested totals                                        │
│  Output → a validation report; any failure = GATE BLOCK each stage     │
└─────────────────────────────────────────────────────────────────────────┘
                 │  (validated data only)
                 ▼
STAGE 1 — DATA COLLECTION          (Module 1: Info & Document Tracker)
  · Intake sub-agent: WhatsApp/email/web upload
  · Auto-generated per-client checklist; escalating follow-ups (1/2/3)
  · AI-drafted messages; auto-reply parsing
                 │
                 ▼
GATE 1 — "All critical docs received & valid?"
  · BLOCK if missing or F5-invalid; review packet with what's outstanding
  · (AI-drafted clarification queries for flags → Module 1 automated)
                 │
                 ▼
STAGE 2 — RECONCILIATION           (Module 2A/2B/2C + Module 6 NEW)
  · 9 engines now run (8 recon engines + ANOMALY):
      2A: GST 2B vs Books         2C: Bank / Vendor / Opening / Loan /
      2B: TDS chain + 26AS             Salary recon
  · 6. ANOMALY & FRAUD DETECTION (NEW): within-client statistical outliers
       - unusual size/frequency vs client's own history (not generic)
       - duplicate payments (same vendor/amount/adjacent dates)
       - round-tripping / circular patterns between ledgers
       - abrupt vendor/ledger behaviour change
       - flags → M8 Action Center with reasoning + evidence (never concludes)
  · All exceptions + anomaly hits → one packet, all routed to M8
                 │
                 ▼
GATE 2 — "All exceptions & anomalies reviewed/waived?"
  · Each item resolvable: Approve / Modify / Reject / Waive (logged)
  · Materiality rule: items above client's rupee threshold → MANDATORY
    human review regardless of confidence (Module 4 rule)
                 │
                 ▼
STAGE 3 — CORRECTIVE ENTRIES       (Module 3: Corrective Entry Assistant)
  · Entry drafter: JV + rationale + evidence; balances (dr=cr) verified
    deterministically BEFORE shown to reviewer
  · Approve / Modify / Reject; batch approve high-confidence
  · Post to Tally only after approval; CRE confirmation captured
                 │
                 ▼
GATE 3 — "Books clean?"            (Module 3 + F5 re-validation after post)
  · All corrections posted or waived; F5 re-validates posted data
                 │
                 ▼
STAGE 4 — TB FINALISATION          (Module 4: Books Readiness Check)
  · 12+ item pre-TB checklist (suspense, opening, prepaid, ROC, advance
    tax, directors' remuneration, bank/GST/TDS positions, stock, dep.)
  · PASS / BLOCK / FLAG each item; overrides logged with reason
  · Materiality-based routing: threshold items need human sign-off
  · TB extracted only when gate passes → tb_versions v1 (hash stored)
                 │
                 ▼
GATE 4 — "Gate passed / overrides logged?"
                 │
                 ▼
STAGE 5 — AUDIT / TAX              (Module 5: Audit & Change Tracking)
  · Financial statement prep, tax computation, audit responses (drafted)
  · F5 + M5: on any correction, delta cycle:
       post → new TB version (v2+) → trace impact on FS/tax → blast-radius
  · Module 9 records every AI recommendation vs what human did
                 │
                 ▼
ALL STAGES FEED:  Module 8 ACTION CENTER (one queue) + Module 7 DASHBOARD
                  Module 9 AI ACCURACY & MODEL GOVERNANCE (drift/sim)
```

> Note: the diagram text above intentionally mirrors the reconciled wording; canonical text lives in Section 4.

---

## 4. Reconciled Module Map (17 modules → where they live)

| Module | Where it lives in the reconciled design | Status vs HTML |
|---|---|---|
| **F1** Login, Access & User Mgmt | JWT + RBAC, 5 roles, 2FA, per-client team assignment | **Expand** (HTML had 3 roles, no 2FA) |
| **F2** Client Profile & Master Data | `clients` + `client_gstins` + contact/health snapshot | **Expand** (multi-GSTIN) |
| **F3** Document & Data Repository | File storage + `document_checklist` + classification/versioning | **Add** auto-classification, versioning, doc→usage link |
| **F4** Universal Edit & Version History | `record_versions` (any record, old value preserved, reason) | **Add** (HTML only versioned TB) |
| **F5** Data Integrity & Validation **[NEW]** | Cross-cutting service (see flow); `ingestion_log`, `sync_status` | **Add** |
| **C1** Rules, Taxonomy, Regulatory Config | `rule_versions` effective-dated; Rules Workspace UI | **Add** versioning + UI |
| **C2** Portal Connect & Filing | Read connectors (keep) + filing calendar + ack + "Send" gate | **Add** filing side |
| **C3** System Settings | Admin console | **Add** |
| **C4** Security & Credential Vault **[NEW]** | Vault for creds + RLS tenant isolation + security_events + backup | **Add** |
| **W1** Info & Doc Tracker | Stage-1 intake (keep) + AI messages + reply parsing + ageing | **Extend** |
| **W2** Reconciliation Engine | Stage-2 8 engines (keep) + configurable match rules | **Extend** (configurable tolerance) |
| **W3** Corrective Entry Assistant | Stage-3 drafter + balance pre-check + batch approve | **Extend** |
| **W4** Books Readiness Check | Stage-4 gate + materiality routing | **Extend** |
| **W5** Audit & Change Tracking | Stage-5 delta cycles + blast-radius + FS/tax versioning | **Extend** (add FS/tax versioning) |
| **W6** Anomaly & Fraud Detection **[NEW]** | New 9th engine in Stage 2 | **Add** |
| **M7** Dashboards & Reporting | Portfolio + firm-wide summary + export + alerts | **Extend** |
| **M8** Action Center | Unified queue across all modules; assign/due-date/prio | **Add** |
| **M9** AI Accuracy & Model Governance **[NEW]** | `ai_outcomes` + drift + sim/dry-run + Partner view | **Add** |

---

## 5. Schema Additions (beyond the HTML's 11 tables)

| Table | Purpose | Key fields |
|---|---|---|
| `client_gstins` | Multi-GSTIN/branch per client | gstin, state, branch_name, is_primary |
| `record_versions` | F4 universal edit history | entity_type, entity_id, old_value JSONB, new_value JSONB, reason, changed_by, changed_at |
| `ingestion_log` | F5 completeness / control totals | client_id, period, source, expected_total, received_total, voucher_count, status |
| `sync_status` | F5 sync health dashboard | client_id, source, last_sync_at, healthy, last_error |
| `anomaly_events` | W6 flagged outliers | client_id, period, ledger, metric, severity, evidence JSONB, status, reviewed_by |
| `action_items` | M8 unified queue | module, ref_type/ref_id, priority, due_date, assigned_to, status |
| `ai_outcomes` | M9 outcome logging | engine, ref_type/ref_id, ai_recomm, human_decision (accept/modify/reject), ai_confidence, driftscore |
| `model_accuracy` | M9 metrics per engine/client/period | precision, acceptance_rate, sample_size |
| `rule_versions` | C1 effective-dated rules | rule_type, name, effective_from/to, body JSONB, changed_by |
| `security_events` | C4 login/security log | event type, user, ip, outcome, created_at |
| `materiality_rules` | W4 threshold routing (in `client_config`) | module, threshold_rupees, mandatory_review |

Plus: **RLS policies** on every client-scoped table + a CI test asserting cross-tenant isolation.

---

## 6. API Additions (on top of the HTML's list)

```
# F5 — Validation
GET   /api/clients/{id}/validation           # F5 report for a period
POST  /api/clients/{id}/validation/run       # Re-run control-total checks
GET   /api/clients/{id}/snc-status         # Sync health per source

# Module 6 — Anomaly
GET   /api/clients/{id}/anomalies           # Anomaly hits for a period
POST  /api/anomalies/{id}/review             # Resolve/waive with reason

# Module 8 — Action Center
GET   /api/action-center                     # Unified queue (filterable)
POST  /api/action-center/{id}/assign         # Assign + due date + priority
GET   /api/action-center/filters             # Available filters

# Module 9 — AI Governance
GET   /api/governance/accuracy               # Per engine/client accuracy
POST  /api/governance/simulation           # Dry-run a rule/rate change
GET   /api/governance/drift                  # Drift flags per engine

# Module 4 — Materiality routing
POST  /api/clients/{id}/materiality         # Update rupee thresholds

# Module 1 — Intake (extended)
POST  /api/clients/{id}/intake/draft-message # AI-drafted follow-up
POST  /api/clients/{id}/intake/parse-reply   # Auto-parse inbound reply

# Module 2 — Filing (C2, extended)
GET   /api/clients/{id}/filing-calendar
POST  /api/clients/{id}/filing/submit       # Human "Send" gate + ack capture

# Module 8 — Edit history (F4)
GET   /api/records/{type}/{id}/history       # Universal edit history
```

---

## 7. Agent Layer Update (from 6 sub-agents → reconciled set)

The HTML's lead + 6 sub-agents survive, renamed to `sarvam-105b` only, with two additions and deterministic Tier-A code taking the routine load:

| Agent | Model (reconciled) | Role |
|---|---|---|
| Lead / Pipeline Controller | 105b | Orchestrates stages, enforces gates, compiles packets |
| Sub-1 Intake Tracker | 105b (minimal) | WhatsApp/email intake, checklist, AI-drafted follow-ups |
| Sub-2 GST Recon Engine | 105b | 2B vs books; classification; ITC — deterministic Tier-A matching, 105b for judgement only |
| Sub-3 TDS Chain Tracker | 105b | Invoice→challan→return chain; 26AS |
| Sub-4 Entry Drafter | 105b | JV drafting w/ rationale; Drafter/Verifer pair; balance checked in Tier-A |
| Sub-5 Gate Checker | 105b (narrates only) | 12+ item checklist; predicates are Tier-A; 105b writes the plain-English report |
| Sub-6 Audit Drafter | 105b | Delta cycles, impact tracing, response drafting |
| **Sub-7 Anomaly Engine [NEW]** | 105b (explains only) | Statistical outlier + duplicate + round-trip detection in Tier-A stats code; 105b writes reasoning/evidence |
| **Sub-8 Governance Monitor [NEW]** | 105b | Logs outcomes, tracks drift, runs simulation/dry-run (results → Module 9) |

> **Cost note:** two more sub-agents exist, but they are deliberately 105b-minimal — the math is Tier-A; the model only writes explanations. The HTML's "30B vs 105B" savings no longer apply; re-estimate at current 105B rates per the implementation reference.

---

## 8. What's Out of Scope in v1 (unchanged from Scope Doc — do NOT build)

MIS & variance reporting · ITR computation · multi-entity consolidation · e-Invoice (IRN) recon · Account Aggregator integration · **SUVIT replacement** (Setu reads Tally, downstream of SUVIT) · data-security/hosting policy (prerequisite decision, not a module).

---

## 9. Next Steps (recommended build sequence)

1. **Lock this doc** as scope v1.1 (supersedes HTML as the reference for flow/module presence).
2. **Rebuild the architecture HTML** to v1.1 (or keep this MD as the source of truth and generate a fresh HTML from it) — adds F5/W6/M8/M9/C4 sections, updates sub-agents to 105b-only, adds filing, adds RLS.
3. **Update the implementation reference** to include: anomaly engine (new Phase), validation layer (F5 gate), action-center APIs, governance/simulation endpoints, security vault + RLS.
4. **Re-estimate the cost model** at 105b-only rates (implementation reference already flags this).

---

*Reconciled from: Scope Lock v1.0 (Google Doc) · System Design & Technical Architecture (HTML) · Implementation Reference (MD). Not a replacement for either — the single flow-level source of truth for build decisions.*
