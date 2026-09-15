# Setu — GST 2B vs Books Reconciliation Engine (Module 2A)

Sarvam-105B based reconciliation engine for CA-firm month-end close. Implements
the reconciled Setu flow: **ingest → normalise → match → classify → compute ITC
→ review packet**, with an **F5 data-integrity gate** that must pass before the
output is trusted.

## Quickstart — run it locally

Requirements: **Python 3.9+** and `git`.

```bash
# 1. Clone the repository (or your fork)
git clone <repo-url>
cd recon_engine

# 2. Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the Phase-4 web app
uvicorn recon_engine.api:app --port 8124

# 5. Open the UI
#    http://localhost:8124/
```

The home page lists existing clients (auto-discovered from `reports/`) and lets
you create a new one. Each client's **workbench** accepts the two required
documents — **GSTR-2B** (`sample_data/demo_*/gstr2b*.json|.xlsx`) and **Invoices
(Tally purchase register)** (`tally*.csv|.xlsx`) — plus optional trial balance,
period, output-tax and materiality. Hit **Run** and the interactive HTML
dashboard opens in the embedded viewer with download links.

Run the test suite (91 tests):

```bash
pytest -q
```

Run the automation agent (files in → review packet + reports, no server needed):

```bash
python -m recon_engine.agent \
    --gstr2b sample_data/gstr2b_rules.json \
    --tally  sample_data/tally_purchase_sample_full.csv \
    --client acme --period 2024-08 --output-tax 120000
```

Optionally enable the Sarvam-105B judgement layer (never required; without a
key the pipeline runs fully deterministically):

```bash
export SARVAM_API_KEY=sk-...        # then restart uvicorn
```

### Realtime reconciliation agent

Drop the accountant's usual file set in and get an interactive HTML report
back - no folder gymnastics needed. The agent detects which file is which,
normalises the official GSTN / Tally formats, joins missing books amounts
from the portal by (GSTIN + invoice-ref core), and runs the pipeline
(Sarvam-105B narration layer when configured).

Endpoints:

| Endpoint | What it does |
|---|---|
| `POST /api/agent/reconcile` | multipart upload of any of: GSTR-2B workbook (`portal_2b`), IMS B2B csv (`portal_ims`), Tally purchase register (`books_purchase`), Tally credit-note register (`books_credit`). Auto-detects roles by filename + content. Query params: `client_id`, `period` (auto-inferred), `output_tax` (auto-derived), `llm` (default true). Returns `{job_id, poll}`. |
| `GET /api/agent/jobs/{job_id}` | poll: `{status: queued\|running\|done\|error, progress, result, error}`; `result.reports` holds the HTML / MD / XLSX / CSV / review-queue URLs. |
| `GET /agent` | drag-and-drop upload page that polls the job and links the ready report. |

Try it live:

```bash
curl -X POST "http://localhost:8124/api/agent/reconcile?client_id=demo" \
  -F "files=@gstr2b.xlsx" -F "files=@purchase.xlsx" -F "files=@credit_note.xlsx"
# -> {"job_id": "...", "poll": "/api/agent/jobs/..."}
curl "http://localhost:8124/api/agent/jobs/<job_id>"
```

File roles are guessed from the name first (GSTR2B / IMS / Purchase / Credit Note),
then by content when the name is generic (sheet names `B2B`, `Purchase Register`,
`Debit Note Register`). Supported source attachments live under
`recon_engine/adapters/official_files.py` (deterministic, Tier-A only) -
extend `detect_role`/`sniff_role` there for new exports.

## Demo scenarios — upload & reconcile in the UI

`sample_data/demo_*` folders hold ready-to-upload document pairs engineered to
exercise different engine paths (all four pass the F5 gate and independent
verification):

| Client id | Docs in `sample_data/` | What the dashboard shows |
|---|---|---|
| `democlean` | `demo_clean/gstr2b.json` + `tally.csv` | Tidy run: 9 matched (2 fuzzy — a typo `DL/240913` and a 12-day-late booking), 1 *not in books*, 1 *not in portal* |
| `demoexceptions` | `demo_exceptions/gstr2b.json` + `tally.csv` | Messy books: amount diffs, GSTIN mismatch, blocked ITC, RCM supply, late booking, open items both ways |
| `demoigst` | `demo_igst/gstr2b.json` + `tally.csv` | 100% inter-state IGST, near-perfect matched view |
| `demoexcel` | `demo_excel/gstr2b_portal_b2b.xlsx` + `tally_purchase_register.xlsx` | Same clean data as portal/Tally **Excel** exports (Excel intake path) |

Upload the two files on the workbench (`http://localhost:8124/clients/<id>`),
set the period (or leave blank — the engine infers it), click **Run**, and the
interactive HTML dashboard opens in the embedded viewer.


## Design rules (from the reconciled scope)

| Rule | Implementation |
|---|---|
| LLM used for judgement ONLY | `recon_engine/sarvam.py` — summaries, narrations, classification rationale. Deterministic Tier-A code does every match/arithmetic operation. |
| Sarvam-105B is the sole model | `sarvam-30b` deprecated. Single model in `config.py` (`SARVAM_MODEL`). |
| Configurable match tolerance | `MatchRules` (Levenshtein ≤2, ±7d, ±1% are v1 defaults; override via `client_config.match_rules`). |
| F5 data integrity gate | `recon_engine/integrity.py` — structural validity, source control totals, reconciliation-of-the-reconciliation. Blocks the packet if any check fails. |
| Drafter/Verifier guard on LLM output | `sarvam.py::verify_summary` — a second 105B call checks the draft didn't invent figures; on failure, falls back to a deterministic summary. |
| Graceful degradation | No API key / API down → deterministic results, empty `ai_summary`. Gate never blocks on the LLM. |

## Layout

```
recon_engine/
├── api.py              # Phase-4 FastAPI delivery surface (upload -> run -> reports)
├── frontend.py         # dependency-free HTML web app (client picker + workbench)
├── agent.py            # automation agent: files in -> review packet + reports
├── engine.py           # ReconciliationEngine orchestrator (6-step flow)
├── config.py           # Settings + per-client MatchRules (match tolerance)
├── models.py           # Invoice, MatchResult, ITCComputation, ReviewPacket, DraftedJV
├── normalizer.py       # Tier-A: GSTIN/invoice/date/amount normalisation + F5 validation
├── matcher.py          # Tier-A: Levenshtein fuzzy matcher + 4-way classification
├── ims.py              # IMS thin-rules layer (Accept/Reject/Pending/N-A)
├── itc.py              # Tier-A: ITC eligibility + GSTR-3B draft figures
├── integrity.py        # F5: structural, control totals, reconciliation-of-recon
├── sarvam.py           # Sarvam-105B client (judgement only, Drafter/Verifier)
├── review_packet.py    # Deterministic balanced JV drafting for 'Not in Books'
├── review_queue.py     # Module-3 approve/modify/reject/post workflow + audit trail
├── htmlreport.py       # interactive single-file HTML dashboard
├── prepost_report.py   # pre/post MD + XLSX + CSV report writer
├── verification.py     # independent cross-check from raw inputs
├── batch.py            # firm-wide batch runner -> portfolio dashboard
├── intake.py           # deterministic file classification + client/period inference
├── gst_position.py     # books-side GST position extraction (TB)
├── sample_scenario.py  # (re)generate the canonical sample docs
├── demo.py, tb_demo.py, probe_sarvam.py   # CLI demos / TB demo / Sarvam probe
└── parsers/            # GSTR-2B JSON/Excel + Tally CSV/Excel + Trial Balance ingest
    ├── gstr2b.py           # GSTR-2B JSON + Tally purchase-register rows
    ├── gstr2b_excel.py     # GSTR-2B Excel export (B2B/B2BA sheets)
    ├── tally_excel.py      # Tally purchase-register Excel
    ├── trial_balance.py    # Tally Trial Balance Excel
    └── excel_common.py     # header-tolerant sheet scanning
sample_data/           # canonical fixtures + demo_* upload scenarios (see Quickstart)
tests/                 # 91 pytest tests (matcher, normalizer, ITC, integrity, phase-2/4, API, frontend)
```

## Run

```bash
# deterministic only (no API key)
python -m recon_engine.demo

# with Sarvam-105B judgement layer
SARVAM_API_KEY=sk_... python -m recon_engine.demo

# tests
python -m pytest tests/ -q
```

## Using as a library

```python
from recon_engine import ReconciliationEngine, Settings
from recon_engine.normalizer import make_invoice

engine = ReconciliationEngine(Settings())

# Already-normalised invoices
portal = [make_invoice("29ABCDE1234F1Z5", "INV-1001", "2026-08-03", 100000,
                       cgst=9000, sgst=9000)]
books  = [make_invoice("29ABCDE1234F1Z5", "INV-1001", "2026-08-03", 100000,
                       cgst=9000, sgst=9000)]

packet = engine.run_sync(
    client_id="client_001", period="2026-08",
    portal_invoices=portal, book_invoices=books,
    # optional F5 control totals from authoritative sources
    portal_ct=(1, Decimal("118000")),
    tally_ct=(1, Decimal("118000")),
    output_tax="120000",
)
print(packet.ai_summary)      # LLM explanation (or fallback)
print(packet.itc.gstr3b_draft)
for jv in packet.drafted_jvs: # balanced corrective entries
    assert jv.is_balanced()
```

## Output packet structure

`packet.to_dict()` → `{client_id, period, stage, summary, exceptions[],
itc, drafted_jvs[], ai_summary, validation, created_at}` — this is the JSON
shape consumed by the FastAPI `/api/clients/{id}/recon/gst_2b` endpoint and the
review packet viewer.

## Trial Balance input (books-side GST position)

`SampleTB-AI.xlsx` is a Tally **Trial Balance** export, not a purchase register
or GSTR-2B. It has no invoice-level data, so the 4-way invoice matcher can't run
on it. What it CAN produce is the **books-side GST position** and TB-integrity
checks (a Gate-4 pre-TB checklist item):

```bash
python -m recon_engine.tb_demo sample_data/SampleTB-AI.xlsx
```

Includes:
- `parsers/trial_balance.py` — Tally TB Excel parser (identity header,
  group vs leaf rows, Grand Total capture)
- `gst_position.py` — extracts Input CGST/SGST/IGST (ITC availed), Output tax,
  "Claim Next Year" carry-forward ITC, TDS payable
- TB-integrity: grand-total balance check (dr == cr)

To run the full invoice-level GST 2B vs Books matcher, provide the **GSTR-2B
JSON** (portal side) alongside an invoice-level Tally purchase register.

## Automation agent (files in -> review packet + reports)

```bash
python -m recon_engine.agent \
    --gstr2b sample_data/gstr2b_sample_full.json \
    --tally  sample_data/tally_purchase_sample_full.csv \
    --tb     sample_data/SampleTB-AI.xlsx \
    --period 2024-08 --output-tax 120000
```

The agent runs the full pipeline: ingest -> normalise -> match/classify ->
ITC + GSTR-3B draft -> F5 gate -> **independent verification** ->
pre/post MD + XLSX + **HTML dashboard** + CSV reports. Exit code is
non-zero if F5 or verification fails (so it is CI / scheduling safe).

The HTML report (`reports/gst_2b_reconciliation_report.html`) is a
self-contained visual dashboard -- KPI cards, SVG donut/bar charts for
classification and ITC, pre->post comparison, exceptions, drafted JVs,
GSTR-3B draft and both quality gates. Zero external dependencies (inline
SVG + CSS), opens straight in a browser and prints cleanly.

Programmatically:
```python
from recon_engine.agent import auto_reconcile
out = auto_reconcile("2b.json", "tally.csv", output_tax="120000")
```

### Optional Sarvam-105B judgement layer
Set `SARVAM_API_KEY` (and optionally `SARVAM_BASE_URL`,
`SARVAM_MODEL=sarvam-105b`, `SARVAM_MAX_TOKENS=2048`) to enable the LLM.
It is used ONLY for plain-English summaries and JV narrations on
pre-computed numbers; matching and arithmetic always remain deterministic
Tier-A code. Without a key the pipeline runs fully deterministically and
never blocks.

Validate the wiring before committing to production:

```bash
SARVAM_API_KEY=sk_... python -m recon_engine.probe_sarvam --run-agent
```

`probe_sarvam` checks config, live-pings the `sarvam-105b` endpoint (auth +
latency) and -- with `--run-agent` -- runs the full agent end-to-end with
the judgement layer on, reporting `ai_summary`, JV narrations and whether
verification still passes.

Notes on Sarvam-105B specifics:
- endpoint `POST /v1/chat/completions`, auth header `api-subscription-key`
  (the docs also accept `Authorization: Bearer <key>`).
- thinking mode is ON by default; keep the token budget generous
  (`SARVAM_MAX_TOKENS`, default 2048) so short replies are never consumed
  entirely by reasoning tokens -- the client treats an empty `content` as a
  failure and hits the graceful-fallback path.

## Independent verification

`python -m recon_engine.verification` recomputes the expected outcome from the
RAW input files using a separate implementation (different matcher, different
ITC loop) and cross-checks the engine packet row-by-row: parse counts, match
statuses, not-in-portal set, ITC totals, F5 gate, JV balance, report figures.
This caught and fixed two real defects during development:
- Tally parser ignored `taxable`/`tax` key aliases (silently zero-valued rows)
- F5 duplicate detection missed a duplicate of the FIRST record (falsy index 0)

## Sample-document test scenario + Pre vs Post report

```bash
python -m recon_engine.sample_scenario   # (re)generate sample docs
python -m recon_engine.prepost_report    # run engine + write reports/
```

Outputs in `reports/`: `pre_vs_post_reconciliation.md`,
`Pre_Post_Reconciliation_Report.xlsx`,
`gst_2b_reconciliation_report.html` (visual dashboard),
`pre_recon_open_items.csv`, `post_recon_exceptions.csv`.

## Phase 1: Excel-first intake (Excel from portal download + email/attachments)

The agent no longer requires JSON/CSV. GSTR-2B Excel exports (GSTN portal
download: B2B / B2BA sheets) and Tally purchase-register Excel exports are
parsed directly with header-tolerant column mapping:

```bash
python -m recon_engine.intake sample_data --no-llm   # scan a folder
```

The intake layer (`recon_engine/intake.py`):

- **Deterministic structural detection** (Tier-A, always available): classifies
  each file as `gstr2b_json | gstr2b_excel | tally_excel | tally_csv |
  trial_balance | unknown` via extension + sheet names + header-token
  signatures; infers client + period from filename and content
  (max invoice date).
- **Sarvam-105B assist** (default ON, graceful): ONLY for ambiguous files
  (unknown type, or no client/period inferable). The model proposes a
  classification in strict JSON; intake validates it against known document
  types and falls back to the deterministic result on any failure. No key ->
  deterministic only (PRD F3 low-confidence fallback pattern).
- **`scan_folder()` / `classify_file()` / `build_run_inputs()`** programmatic
  API returns a ready-to-reconcile input bundle.

Excel support is wired straight into the agent:

```bash
python -m recon_engine.agent \
    --gstr2b sample_data/gstr2b_sample.xlsx \
    --tally  sample_data/tally_purchase_sample.xlsx \
    --client acme --period 2024-08 --output-tax 120000
```

Sample Excel fixtures (`sample_data/gstr2b_sample.xlsx`,
`sample_data/tally_purchase_sample.xlsx`) mirror the JSON/CSV dataset
byte-for-byte in value (parity is test-locked); the B2B fixture uses the
portal's R/N status convention, mapped back by the parser to the engine's
`active` / `non_filer` vocabulary.

## Phase 2: Engine business rules (§17(5)/RCM, IMS, materiality)

The engine now enforces the PRD business rules deterministically (Tier-A):

- **§17(5) + RCM flags** propagate through `Invoice`: the GSTR-2B JSON parser
  reads `rcm`/`isReverseCharge` (+ `isBlocked`/`blocked`/`itcAvailability`)
  and the Excel parser reads `Reverse Charge` / `ITC Availability` columns
  (portal R/N + Avail/Not Avail vocabulary mapped back). ITC eligibility
  EXCLUDES matched-but-blocked and matched-but-RCM rows.
- **IMS thin rules layer** (`recon_engine/ims.py`): `recommend_ims(result)`
  returns `Accept | Reject | Pending | N/A`, conservative by design:
  fuzzy match / amount diff / not-in-books / supplier-not-filed -> Pending;
  §17(5) / RCM -> Reject; books-only rows -> N/A.
- **2A materiality gate** (`MatchRules.materiality_amount`, firm-wide default
  + per-client override): exceptions are tagged `above`/`below`; above-threshold
  items require individual review (never batch-approved).
- **Live credit position** (`ITCComputation.itc_position`): `eligible_now`,
  `blocked` (§17(5)+RCM), `awaiting_supplier`, `disputed`, `unbooked` —
  sum always reconciles to total portal ITC (Module 7 feed).

```bash
python -m recon_engine.agent \
    --gstr2b sample_data/gstr2b_rules.xlsx \
    --tally  sample_data/tally_purchase_sample.xlsx \
    --client acme --period 2024-08 --output-tax 120000
```

Rules fixture (`sample_data/gstr2b_rules.json` / `.xlsx`): INV-24001 is
§17(5)-blocked, CN-8841 is RCM -> eligible ITC drops from 64,260 to **25,560**,
net payable 94,440; independent verification recomputes the same figures from
the raw files (JSON and Excel intake both verified gate-passing).

## Phase 3: Review queue — Approve / Modify / Reject / Post workflow

`recon_engine/review_queue.py` implements the Module-3 corrective-entry
workflow as a deterministic state machine with a full audit trail:

    PENDING  --approve-->  APPROVED        (JV items require is_balanced)
    PENDING  --modify--->  MODIFIED        (balance re-validated; unbalanced
                                             lines raise UnbalancedJVError)
    MODIFIED --approve-->  APPROVED/REJECTED
    APPROVED --modify--->  MODIFIED        (re-open for correction)
    APPROVED --post----->  POSTED          (only through the Tally post gate)

- **Audit trail** — every action records `{action, actor, note, timestamp}`;
  blocked attempts (`modify_blocked`, `post_failed`) are recorded too.
- **Batch approval (M3 locked #2)** — `batch_approve` approves ONLY items
  tagged batch-approvable: matched + HIGH confidence + below materiality.
  JVs, above-threshold, and IMS-Reject items always need individual review.
- **Post gate** — `TallyPoster` stub: posting requires APPROVED/MODIFIED
  status AND a configured target (TALLY_ODBC_DSN); otherwise the action is
  refused and audited. The engine itself never writes to Tally.
- **Persistence** — `ReviewQueue.save()/load()` (JSON) with full
  rehydration of JV lines, match evidence, materiality/IMS flags and audit.

Wired into the agent — every run now also emits `reports/review_queue.json`
and returns the queue object:

```bash
python -m recon_engine.agent \
    --gstr2b sample_data/gstr2b_rules.json \
    --tally  sample_data/tally_purchase_sample_full.csv \
    --client acme --period 2024-08 --output-tax 120000 --materiality 50000

# run a review round over the persisted queue
python -m recon_engine.review_queue reports/review_queue.json \
    --batch --approve BL/2024/0815 --reject INV-24001 \
    --actor partner1 --note "round 1" --out reports/review_queue_after_round1.json

python -m recon_engine.review_queue reports/review_queue_after_round1.json \
    --post BL/2024/0815 --actor ops --out reports/review_queue_final.json
```

On the rules fixture: batch approves 3 (INV-24002, ITF-2024-08-112,
CN-8850), the corrective JV (BL/2024/0815) awaits individual approval,
INV-24001 (blocked, above gate) stays pending for partner sign-off.

## Interactive HTML dashboard (Phase-4)

The agent and the batch runner now emit a **fully interactive, single-file
HTML report** (`gst_2b_reconciliation_report.html`) — pure HTML + CSS +
inline vanilla JS + inline SVG, no CDN, no external dependencies, works
offline and in print (`@media print` expands all sections).

Click-driven workflow (no developer tools needed):

| Action | What happens |
|---|---|
| Tab bar | 6 sections — Overview / Exceptions / Corrective JVs / GSTR-3B / Quality gates / Review queue; deep-linkable via `#tab-exceptions` etc. |
| KPI cards (Matched, Amount diff, Not in books, Not in portal) | jumps to the Exceptions tab pre-filtered to that classification |
| Exceptions table | live search box, status filter chips with counts, click any column header to sort, click any row to open the **evidence modal** |
| Evidence modal | full portal + books-side invoice breakdown (GSTIN, tax split, reverse-charge / blocked-credit flags), match deltas (edit distance, date delta, value delta %, match score), reasons, raw engine evidence |
| Corrective JV cards | click to expand lines + narration + rationale; **Copy narration** button |
| Review queue tab | item status / priority chips, batch-approvable flags, expandable per-item **audit trail** |

The JS consumes a `window.RECON_DATA` JSON blob embedded in the file, so the
page renders identically from the API, the agent, or the batch runner —
including the review queue statuses from `reports/review_queue.json`.

```bash
# agent run also writes the interactive dashboard
python -m recon_engine.agent --gstr2b sample_data/gstr2b_rules.json \
    --tally sample_data/tally_purchase_sample_full.csv \
    --client acme --period 2024-08 --output-tax 120000 --materiality 50000
# open reports/acme/2024-08/gst_2b_reconciliation_report.html

# firm-wide batch aggregates a portfolio dashboard too
python -m recon_engine.batch --inbox <inbox> --output-tax 120000
# -> reports/firmwide_portfolio.html
```

The report remains deterministic Tier-A: the charts, numbers and gates are
computed in Python; the JS only filters/sorts/expands what Python already
put in the payload.

## Web front-end (client picker + workbench)

Dependency-free HTML pages served by the same FastAPI app (no template engine,
no CDN assets):

| Route | Purpose |
|---|---|
| `GET /` | Home: pick an existing client (auto-discovered from `reports/`) or create a new one |
| `GET /clients/{client_id}` | Workbench: upload GSTR-2B + Invoices (Tally purchase register) + optional TB, set period/output-tax/materiality, run reconciliation, open the generated dashboard |

Required inputs: **GSTR-2B** (portal invoices, `.json/.xlsx/.xlsm`) and **Invoices**
(books side, i.e. the vendor purchase register exported from Tally, `.csv/.xlsx/.xlsm`).
Optional inputs: trial balance (`.xlsx/.xlsm`), period, output tax, materiality gate,
and an "Advanced" section exposing the match rules (invoice-no edit distance, date
window, value tolerance, exact-GSTIN flag). Each optional input maps to a query
parameter on `POST /api/clients/{id}/recon` (`invoice_edit_max`, `date_window_days`,
`value_tolerance`, `require_gstin_exact`) and is forwarded to the engine's
`client_config.match_rules`; the independent verification pass mirrors them so a
custom-tolerance run still cross-checks cleanly.
| `GET /api/firmwide` | Batch firm-wide portfolio dashboard (if a batch has run) |

Flow: home -> workbench -> upload documents -> `POST /api/clients/{id}/recon`
(multipart, called in-line from the page) -> packet summary + verification gates
+ report links rendered in place; every new run also appears in the
"Previous runs" table on the workbench.

```bash
uvicorn recon_engine.api:app --port 8124
# open http://localhost:8124/
```
