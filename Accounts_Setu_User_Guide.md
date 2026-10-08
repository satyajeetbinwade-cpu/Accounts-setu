# Accounts Setu — User Guide

> **GST Reconciliation Platform**
> Upload Documents · OCR & Ingestion · Reports & Export

---

## Table of Contents

1. [Getting Started](#1-getting-started)
2. [Managing Clients](#2-managing-clients)
3. [Uploading Reconciliation Documents](#3-uploading-reconciliation-documents)
4. [Smart Ingestion (OCR & AI Mapping)](#4-smart-ingestion-ocr--ai-mapping)
5. [Running a Reconciliation](#5-running-a-reconciliation)
6. [Reviewing Results](#6-reviewing-results)
7. [Exporting Reports](#7-exporting-reports)
8. [Reports Page](#8-reports-page)
9. [Comparing Runs](#9-comparing-runs)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Getting Started

### 1.1 What is Accounts Setu?

Accounts Setu is a GST reconciliation platform that helps you match your books
(purchase register, sales register) against portal data (GSTR-2A, GSTR-2B,
GSTR-1) and identify discrepancies. The platform ingests raw source files
(Excel, CSV), normalises them, runs configurable matching rules, and produces
detailed reports with HTML, PDF, and Excel outputs.

### 1.2 Logging In

Open the application URL in your browser. You will see the login screen. Enter
your username and password, then click **Sign In**. After successful
authentication, you land on the Dashboard.

> **Note:** If you don't have credentials, contact your system administrator
> to create an account.

### 1.3 The Dashboard

The Dashboard is your starting point. It provides quick-access cards to the
major sections:

- **Clients** - Add and manage client profiles, branches, and GST registrations
- **Reconcile** - The guided 5-stage flow:
  Context > Upload > Reconcile > Review > Export
- **Smart Ingestion** - Upload raw spreadsheets and map columns via AI
- **Reports** - Visual dashboards with charts and KPIs across periods
- **Documents** - Manage uploaded files and their mapping profiles
- **Settings** - User roles, AI model config, matching rules

> **Tip:** The sidebar on the left provides access to all sections, organised
> under "Reconcile", "All tools", and "Setup".

---

## 2. Managing Clients

### 2.1 Adding a New Client

Before you can upload documents or run reconciliations, set up a client profile.

1. Navigate to **Clients** from the Dashboard or sidebar.
2. Click the **Add Client** button.
3. Enter the client's legal name (required), PAN, TAN, and contact details.
4. Click **Save**.

> **Tip:** You can also add a new client from the Reconcile flow via the
> "Add New Client" link beside the client picker.

### 2.2 Adding Branches / GST Registrations

Each client can have one or more GST registrations (branches/GSTINs).

1. Open the client's profile page.
2. Under **Branches / GSTINs**, click **Add Branch**.
3. Enter the branch name, GSTIN, and any notes.
4. Click **Save**.

### 2.3 Contact Directory

The Contacts tab lets you manage key contacts for each client. These are used
for follow-up communications when missing invoices or entries are identified.

- Add contacts with name, role, email, and phone
- Mark primary contacts for automated follow-ups
- Contact info is editable at send-time without modifying the client profile

---

## 3. Uploading Reconciliation Documents

The guided **5-stage Reconcile flow** is the primary path for uploading
documents and running reconciliations.

### 3.1 Stage 1: Set Context

Before uploading anything, specify which client, period, and reconciliation
type you are working on.

1. On the Reconcile page, you see the **Stage 1 - Context** screen.
2. Select a **Client** from the dropdown.
3. Select the **Period** (e.g., "2026-08" for August 2026).
4. Select the **Recon type**: "GST" or "TDS".
5. Once all three are selected, click **Continue to upload**.

> **Note:** The selections read from folders under
> `data/<client>/<period>/<source_type>/` on the server. If no folders exist
> yet, the dropdowns will be empty.

### 3.2 Stage 2: Upload Files

Assign source files to each **slot**. For GST reconciliation, typical slots are:

| Slot | Side | Description |
|------|------|-------------|
| Purchase Register | Books | Purchase records from your accounting system |
| GSTR-2A | Portal | Auto-drafted purchases from GST portal |
| GSTR-2B | Portal | Static purchase statement from GST portal |
| Sales Register / GSTR-1 | Portal (optional) | For sales-side reconciliation |
| Note Register | Books (optional) | Credit/Debit notes for note-to-note matching |

**To upload:**

1. Select the **slot** from the dropdown.
2. **Drag and drop** a file (Excel .xlsx/.xls or CSV) onto the upload zone.
3. Click **Upload & Normalise**. The file is immediately validated and parsed.
4. Repeat for each slot.

> **Note:** Files are normalised automatically on upload - no additional
> processing step is required.

### 3.3 File Requirements

- **Format:** .xlsx, .xls, or .csv only
- Each file should represent **ONE source type** (not a combined report)
- The file should have a **header row** with column names
- Files are stored under `data/<client>/<period>/<source_type>/`

> **Warning:** Scanned PDFs or image-only files cannot be processed here.
> Use Smart Ingestion or the Document Vault for OCR-based extraction.

### 3.4 Re-running Through the Model

If column mapping produces unexpected results, re-run through the AI model:

1. Find the slot card for the uploaded file in Stage 2.
2. Click **Re-run through model**. A confirmation dialog appears.
3. Confirm. The model re-processes the file (~15-25 seconds).

> **Note:** Re-running discards the cached mapping. The file itself is
> unchanged.

### 3.5 Alternative: Smart Ingestion

For bulk uploads or files needing column-mapping review, use **Smart Ingestion**
under "All tools" > "Smart Ingestion". See Section 4 for details.

---

## 4. Smart Ingestion (OCR & AI Mapping)

Smart Ingestion is the tool for uploading raw source files and reviewing the
AI model's column mapping before staging them for reconciliation.

### 4.1 When to Use

- You want to **review the model's column mapping** before committing
- You are uploading **multiple files in bulk**
- You need to **file uploads to a specific period**
- You want to see **raw data previews** alongside mapped columns

### 4.2 Upload Interface

1. Navigate to **All tools** > **Smart Ingestion** from the sidebar.
2. Select a **period** (e.g., "2026-08").
3. Select a **source type** (Purchase Register, GSTR-2A, GSTR-2B, etc.)
4. **Drag and drop your file(s)** onto the upload zone, or click to browse.
5. Click **Upload**. The file is queued for AI processing.

### 4.3 AI Column Mapping

The AI model maps each column in your file to the platform's canonical fields.
Each column gets a confidence score:

| Confidence | Indicator | Action Needed |
|------------|-----------|---------------|
| High (>=80%) | Green | Mapped automatically |
| Medium (50-79%) | Yellow | Review recommended |
| Low (<50%) | Red | Manual mapping required |
| Unmapped | Grey | Not used in reconciliation |

### 4.4 Reviewing and Approving Mappings

1. Click on any upload row to open the **mapping review screen**.
2. Review the **Raw File Preview** tab to see original data.
3. Switch to the **Mapping Table** tab to see each column's mapping.
4. For low-confidence or incorrect mappings, use the dropdown to **re-map**.
5. Review **Sample Values** to verify the mapping is correct.
6. Click **Approve Mapping** to confirm. The file is staged for reconciliation.

### 4.5 OCR Capabilities

The platform handles documents through multiple paths:

| Document Type | Handling Path | OCR Needed? |
|---------------|---------------|-------------|
| Excel/CSV files | Smart Ingestion (F3-AI) | No |
| Text-based PDFs | Document Vault (F3) | No |
| Scanned PDFs (images) | Document Vault - VISUAL | Yes (AI vision) |

- **Spreadsheets** (.xlsx, .xls, .csv) are processed directly - no OCR needed
- **Text-based PDFs** are read via text extraction
- **Scanned invoices** (image-only PDFs) are routed through the VISUAL
  touchpoint which uses AI vision models to extract text
- The **Invoice Extraction** tool (under "All tools") is purpose-built for
  invoice PDFs with OCR

> **Note:** For scanned invoice PDFs, use the Invoice Extraction tool rather
> than Smart Ingestion, which is for spreadsheet/CSV data only.

### 4.6 Mapping Profiles

Once a file's columns are mapped, the profile is saved. If you upload a
similar file in a future period, the profile is re-used automatically.

- Profiles are named per source type and client
- They store field-level mappings and sample data
- Re-uploading a file with the same name overwrites the existing mapping

### 4.7 Bulk Operations

1. Select multiple uploads using **checkboxes**.
2. Click **File to period** to assign them all to a period.
3. Click **Approve selected** to approve all mappings at once.
4. Click **Delete selected** to remove unwanted uploads.

---

## 5. Running a Reconciliation

Once files are uploaded and mapped, execute the reconciliation run.

### 5.1 Stage 2: Verify File Readiness

| Indicator | Meaning |
|-----------|---------|
| Green "Ready" pill | All required columns present |
| Yellow "Needs attention" pill | Some columns missing |
| Red "Missing" pill | No file selected for this slot |

### 5.2 Stage 3: Execute the Run

1. From Stage 2, click **Continue to reconcile**.
2. On Stage 3, review the files listed. Optionally check **"Pause here"**.
3. Click **Run reconciliation**. The system processes files through the
   matching rules.
4. Wait for completion (~15-60 seconds depending on data volume).
5. A confirmation shows the run ID and caveats.
6. Click **See the results** to proceed to Review.

### 5.3 What Happens During a Run?

1. Normalises both books and portal data to a common schema
2. Matches records by invoice number, vendor GSTIN, and amount
3. Classifies each record:
   - **Matched** - Found on both sides, amounts agree
   - **Amount Difference** - Found on both sides, amounts differ
   - **Not in Books** - Found in portal but not in books
   - **Not in Portal** - Found in books but not in portal
4. Assigns **confidence scores** based on match quality
5. Identifies duplicates and applies matching rules
6. Generates a run report with summary and per-item detail

### 5.4 Re-running

To re-run with different files or config:
1. Go back to Stage 1 and click **Start fresh**.
2. Select different files in Stage 2.
3. Execute the run again. Previous results are preserved for comparison.

---

## 6. Reviewing Results

Stage 4 - Review is where you analyse reconciliation results.

### 6.1 Summary Headline

The AI generates a narrative summary describing the overall reconciliation
outcome in plain language.

### 6.2 KPI Row

Key numbers at a glance:

- Total records processed
- Matched records (value)
- Amount differences (value)
- Not in Portal (value)
- Not in Books (value)
- ITC claimed vs. eligible (GST runs)
- Percentage matched

### 6.3 Charts

| Chart | Description |
|-------|-------------|
| Classification donut | Proportion matched vs. exceptions |
| ITC donut (GST runs) | Input Tax Credit summary |
| TDS deposit bar (TDS runs) | Tax deducted at source |
| Supplier bar | Top suppliers by unmatched value |
| Trend bar | Period-over-period trends |

### 6.4 Reviewing Line Items

1. Use the **filter bar** to narrow by classification, confidence, or state.
2. Each row shows classification, confidence, and values.
3. Click **Open** on any row for side-by-side detail comparison.
4. Add a **reviewer note** if needed.
5. Click **Mark reviewed** to confirm.

### 6.5 Review States

| State | Indicator | Meaning |
|-------|-----------|---------|
| Unreviewed | Grey | Not yet reviewed |
| Reviewed | Green | Confirmed by a reviewer |
| Stale | Orange | Review no longer applies |

### 6.6 Data Quality Notes

The data quality section lists caveats and limitations - missing columns,
unparseable fields, or checks that could not be performed.

---

## 7. Exporting Reports

Stage 5 - Export produces the final deliverable in three formats.

### 7.1 Export Screen

1. From Stage 4, click **Continue to export** to reach Stage 5.
2. Review the summary card: run ID, context, record counts.
3. Look for any **caveats** listed.
4. Choose **HTML Report**, **PDF Report**, or **Excel Workbook**.
5. Click the download button.

### 7.2 HTML Report

Interactive document that opens in your browser:

- Executive summary with key findings
- Classification breakdown with counts and values
- Filterable exception table
- Side-by-side record details
- Evidence drawer for any row
- Integrity strip with caveats

> **Recommended format** for internal review.

### 7.3 PDF Report

Print-friendly version suitable for client submissions. Contains the same
sections as the HTML report in a paginated, paragraph-style layout.

### 7.4 Excel Workbook

Structured workbook with multiple sheets:

| Sheet | Contents |
|-------|----------|
| Summary | Headline numbers and totals |
| Matched | All matched records |
| Amount Differences | Records differing in value |
| Not in Books | Portal records not in books |
| Not in Portal | Books records not in portal |
| ITC / TDS | Tax credit/deduction breakdown |
| Notes | Caveats and data quality information |

**To export:**

1. Click **Generate export**.
2. Once ready, click **Download Excel workbook**.
3. The file downloads to your browser's default location.

> **Warning:** The Excel export contains ALL records, including reviewed
> and unreviewed ones. Reviewed items are marked with their status.

### 7.5 Exporting from the Review Screen

HTML and PDF reports can also be generated directly from the Review screen
(Stage 4) without going to Stage 5.

---

## 8. Reports Page

The Reports page (under "All tools" > "Reports") provides visual dashboards
and trend analysis across reconciled periods.

### 8.1 Period Picker

Select a period to view its reconciliation report from the latest run.

### 8.2 KPI Cards

Click any card to filter the detail table:

- **Total** - All records in the run
- **Matched** - Successfully matched records
- **Exceptions** - Records needing attention
- **Match Rate** - Percentage matched by value

### 8.3 Charts

- Classification split (donut)
- ITC Analysis (GST runs)
- Supplier Breakdown by exception value
- Period-over-period Trend

### 8.4 Detail Table

Full detail table with filters for classification, confidence, and review state.

### 8.5 Integrity Strip

Lists all caveats and checks that could not be performed.

---

## 9. Comparing Runs

The Compare tool (under "Reconcile" > "Compare") lets you compare two runs.

### 9.1 When to Use

- After updating matching rules
- After uploading corrected data
- Period-over-period analysis
- Config change audit trail

### 9.2 How to Compare

1. Select client, period, and recon type.
2. Choose **Run A** (baseline) and **Run B** (new).
3. The page shows:
   - Movement summary: changed/unchanged counts
   - Bucket-to-bucket movement (e.g., "Matched -> Amount Difference: 12")
   - Changed results side-by-side

> **Note:** You need at least two runs in the same context.

### 9.3 Interpreting Movement

Examples:
- "Not in Books -> Matched: 5" - Five records previously missing are now
  matched, likely from corrected data
- "Matched -> Amount Difference: 3" - Three records changed classification,
  possibly from a rule change

---

## 10. Troubleshooting

### 10.1 Upload Fails

| Cause | Solution |
|-------|----------|
| Wrong file format | Use .xlsx, .xls, or .csv |
| File too large | Split into smaller batches |
| Special characters in filename | Use simple alphanumeric names |
| Network issues | Try a wired connection |

### 10.2 Mapping Problems

- Manually re-map columns in the review screen
- Click **Re-run through model** for a fresh mapping
- Ensure a clear header row with descriptive column names
- Avoid merged cells and multi-row headers

### 10.3 No Matches Produced

- Check correct files are selected for each slot
- Verify invoice numbers, dates, and amounts are in expected formats
- Check matching rules configuration (tolerance settings)
- Ensure the period matches between files and context

### 10.4 Report Export Issues

| Issue | Solution |
|-------|----------|
| Excel truncated | Large datasets may be limited |
| PDF not generating | Ensure HTML report generated first |
| Download blocked | Allow popups in your browser |

### 10.5 Common Error Messages

**"No client data folders exist on this server yet"**
> Seed demo data or upload files first through Smart Ingestion.

**"No files on disk for this slot yet"**
> Upload a file using the upload zone above the slot cards.

**"Mapping not approved"**
> Open the file in Smart Ingestion and approve its column mapping.

**"Run config snapshot differs from current rules"**
> The matching rules changed since this run was created.

### 10.6 Getting Help

- Check the **caveats section** of your run report
- Contact your system administrator with the **run ID**
- Check **Settings > AI Models** for model configuration
- Review **matching_rules.yaml** via **Settings > Rules**
