"""First-run seed data for the C1 Rules, Taxonomy & Regulatory Config module.

Per the C1 build prompt:
- TaxonomyEntry is PRE-SEEDED with the Flagged Item subtypes and shared
  vocabulary locked in the Information Architecture (Step 3) document.
  Does NOT ship empty.
- RegulatoryRulesTable is PRE-SEEDED with a basic set of known current
  rates as of a stated date, tagged "Seed data as of [date] — verify
  before relying on for filings."
- Rule categories (five total, including the new Statutory Due Dates) and
  their rules are seeded with firm-wide default values.

All seed functions are additive-upsert (never reset an Admin's edits),
mirroring auth.seed._sync_new_permissions and settings' notification
prefs. Re-running the app never clobbers a Manager's saved rule value.
"""

from __future__ import annotations

import sqlite3

from src.rules import db

# The stated "as of" date for the pre-seeded regulatory rates. Everything
# seeded below is tagged with this so the UI can render the "Seed data —
# unverified" chip until a Manager reviews it.
SEED_AS_OF = "2026-09-14"

# ---------------------------------------------------------------------------
# Rule categories (five total — the original four plus Statutory Due Dates)
# ---------------------------------------------------------------------------

SEED_CATEGORIES: list[tuple[str, str, str, int]] = [
    # (key, label, description, sort_order)
    ("tds_rates", "TDS Section Rates", "Section-wise TDS deduction rates and thresholds.", 1),
    ("gst_tolerance", "GST Tolerance", "GST matching tolerances (amount, date, rounding).", 2),
    ("suspense", "Suspense Ledger Conventions", "How suspense/clearing entries are treated.", 3),
    ("aging", "Aging Thresholds", "Aging buckets for unreconciled items.", 4),
    ("statutory_due_dates", "Statutory Due Dates", "Filing due dates for C2's filing calendar.", 5),
    ("materiality", "Reconciliation Materiality", "Per-recon-type materiality thresholds for exception routing (Module 2).", 6),
]

# ---------------------------------------------------------------------------
# Rules (firm-wide defaults). Each: (category_key, key, label, value_type,
# unit, high_impact, description, sort_order, default_value)
# ---------------------------------------------------------------------------

SEED_RULES: list[tuple] = [
    # --- TDS section rates ---
    ("tds_rates", "tds.194c.default_rate", "194C — default rate", "number", "%", False,
     "Payments to contractors (default rate).", 1, "2.0"),
    ("tds_rates", "tds.194c.individual_huf", "194C — individual/HUF rate", "number", "%", False,
     "Payments to contractors (individual/HUF).", 2, "1.0"),
    ("tds_rates", "tds.194j.professional", "194J — professional services", "number", "%", False,
     "Fees for professional services.", 3, "10.0"),
    ("tds_rates", "tds.194j.technical", "194J — technical services", "number", "%", False,
     "Fees for technical services.", 4, "2.0"),
    ("tds_rates", "tds.194i.land_building", "194I — rent (land/building)", "number", "%", False,
     "Rent on land/building.", 5, "10.0"),
    ("tds_rates", "tds.194i.plant_machinery", "194I — rent (plant/machinery)", "number", "%", False,
     "Rent on plant/machinery.", 6, "2.0"),
    ("tds_rates", "tds.194a.default", "194A — interest", "number", "%", False,
     "Interest other than on securities.", 7, "10.0"),
    ("tds_rates", "tds.194h.default", "194H — commission/brokerage", "number", "%", False,
     "Commission or brokerage.", 8, "5.0"),
    ("tds_rates", "tds.no_pan_rate", "No-PAN rate", "number", "%", True,
     "Rate applied when PAN is missing/invalid.", 9, "20.0"),

    # --- GST tolerance ---
    ("gst_tolerance", "gst.amount_tolerance.absolute", "Amount tolerance (absolute)", "number", "Rs", False,
     "Allowed rupee difference for a match.", 1, "10.0"),
    ("gst_tolerance", "gst.amount_tolerance.percent", "Amount tolerance (percent)", "number", "%", False,
     "Allowed percentage difference for a match.", 2, "1.0"),
    ("gst_tolerance", "gst.rounding_tolerance", "Rounding tolerance", "number", "Rs", False,
     "Differences within this are classified as Rounding.", 3, "2.0"),
    ("gst_tolerance", "gst.date_tolerance_days", "Date tolerance (days)", "number", "days", False,
     "Days two invoice dates may differ and still match.", 4, "5"),
    ("gst_tolerance", "gst.fuzzy_threshold", "Fuzzy party-name threshold", "number", "", False,
     "0-100; higher = stricter.", 5, "85"),

    # --- Suspense ledger conventions ---
    ("suspense", "suspense.default_ledger", "Default suspense ledger", "text", "", True,
     "Ledger used for unmatched portal-side entries.", 1, "Suspense — Unmatched"),
    ("suspense", "suspense.auto_clear", "Auto-clear suspense", "boolean", "", True,
     "Whether suspense entries auto-clear on a later match.", 2, "false"),

    # --- Aging thresholds ---
    ("aging", "aging.unreconciled_days", "Unreconciled aging threshold (days)", "number", "days", False,
     "Days before an unreconciled item is flagged as aged.", 1, "30"),
    ("aging", "aging.review_escalation_days", "Review escalation (days)", "number", "days", False,
     "Days before an aged item escalates for review.", 2, "60"),

    # --- Reconciliation materiality (Module 2) ---
    # Materiality-based routing is SEPARATE per reconciliation type (2A/2B/2C,
    # not one universal number), each firm-wide-default-with-per-client-override
    # per C1's pattern, given GST and TDS carry different risk profiles.
    ("materiality", "materiality.gst", "2A GST materiality threshold", "number", "Rs", False,
     "Exceptions at or above this value escalate above the materiality threshold.", 1, "10000.0"),
    ("materiality", "materiality.tds", "2B TDS materiality threshold", "number", "Rs", False,
     "Exceptions at or above this value escalate above the materiality threshold.", 2, "5000.0"),
    ("materiality", "materiality.other", "2C Other materiality threshold", "number", "Rs", False,
     "Exceptions at or above this value escalate above the materiality threshold.", 3, "5000.0"),
]

# ---------------------------------------------------------------------------
# Taxonomy — pre-seeded Flagged Item subtypes + shared vocabulary (Step 3)
# ---------------------------------------------------------------------------

SEED_TAXONOMY: list[tuple[str, str, str, str, int]] = [
    # (category, code, label, description, sort_order)
    ("flagged_item_subtype", "amount_difference", "Amount Difference", "Books and portal disagree on value.", 1),
    ("flagged_item_subtype", "not_in_books", "Not in Books", "Portal shows a transaction missing from books.", 2),
    ("flagged_item_subtype", "not_in_portal", "Not in Portal", "Books show a transaction missing from the portal.", 3),
    ("flagged_item_subtype", "rate_mismatch", "Rate Mismatch", "Tax rate applied differs from the portal.", 4),
    ("flagged_item_subtype", "timing_difference", "Timing Difference", "Same transaction in different periods.", 5),
    ("flagged_item_subtype", "short_deduction", "Short Deduction", "Tax deducted below the section requirement.", 6),
    ("flagged_item_subtype", "excess_deduction", "Excess Deduction", "Tax deducted above the section requirement.", 7),
    ("difference_type", "tax_split_mismatch", "Tax Split Mismatch", "CGST/SGST/IGST split differs.", 1),
    ("difference_type", "taxable_value_difference", "Taxable Value Difference", "Taxable value differs.", 2),
    ("difference_type", "tax_amount_difference", "Tax Amount Difference", "Total tax differs.", 3),
    ("difference_type", "document_type_mismatch", "Document Type Mismatch", "Invoice vs credit note, etc.", 4),
    ("difference_type", "rounding", "Rounding", "Small difference from rounding.", 5),
    ("difference_type", "duplicate_in_books", "Duplicate in Books", "Same invoice appears more than once.", 6),
    ("difference_type", "unexplained", "Unexplained", "Disagreement with no pinpointed reason.", 7),
]

# ---------------------------------------------------------------------------
# Regulatory rules table — pre-seeded dated rates (verify before relying)
# ---------------------------------------------------------------------------

SEED_REGULATORY: list[tuple[str, str | None, str, str, str, str]] = [
    # (domain, section, name, rate_or_rule, effective_from, notes)
    ("GST", None, "GST rate slab — 0%", "0.0%", "2024-04-01", "Nil-rated / exempt."),
    ("GST", None, "GST rate slab — 5%", "5.0%", "2024-04-01", "Lower slab."),
    ("GST", None, "GST rate slab — 12%", "12.0%", "2024-04-01", "Standard lower slab."),
    ("GST", None, "GST rate slab — 18%", "18.0%", "2024-04-01", "Standard slab."),
    ("GST", None, "GST rate slab — 28%", "28.0%", "2024-04-01", "Upper slab."),
    ("GST", None, "ITC — blocked (personal use)", "ITC blocked", "2024-04-01", "Section 17(5) blocked credits."),
    ("TDS", "194C", "Payments to contractors", "2.0% (1.0% individual/HUF)", "2024-04-01", "Threshold Rs 30,000 single / Rs 1,00,000 annual."),
    ("TDS", "194J", "Professional / technical services", "10.0% professional / 2.0% technical", "2024-04-01", "Threshold Rs 30,000."),
    ("TDS", "194I", "Rent", "10.0% land/building / 2.0% plant/machinery", "2024-04-01", "Threshold Rs 2,40,000 annual."),
    ("TDS", "194A", "Interest other than securities", "10.0%", "2024-04-01", "Threshold Rs 40,000."),
    ("TDS", "194H", "Commission or brokerage", "5.0%", "2024-04-01", "Threshold Rs 15,000."),
    ("TDS", "194Q", "Purchase of goods", "0.1%", "2024-04-01", "Threshold Rs 50,00,000 annual."),
]

# ---------------------------------------------------------------------------
# Statutory due dates — new category for C2's filing calendar
# ---------------------------------------------------------------------------

SEED_DUE_DATES: list[tuple[str, str, int | None, int | None, int, str, int]] = [
    # (obligation, period, due_day, due_month, grace_days, description, sort_order)
    ("GSTR-1", "monthly", 11, None, 0, "Outward supplies return.", 1),
    ("GSTR-3B", "monthly", 20, None, 0, "Summary return + tax payment.", 2),
    ("GSTR-9", "annual", None, 12, 0, "Annual return.", 3),
    ("TDS return (24Q/26Q)", "quarterly", 31, None, 0, "Quarterly TDS return.", 4),
    ("TDS payment", "monthly", 7, None, 0, "Monthly TDS deposit.", 5),
]


def run_seed(conn: sqlite3.Connection) -> None:
    """Populate categories, rules, taxonomy, regulatory rates and due dates.
    Additive-only — never resets an Admin/Manager's edits."""
    _seed_categories(conn)
    _seed_rules(conn)
    _seed_taxonomy(conn)
    _seed_regulatory(conn)
    _seed_due_dates(conn)
    conn.commit()


def _seed_categories(conn: sqlite3.Connection) -> None:
    for key, label, description, sort_order in SEED_CATEGORIES:
        db.upsert_category(conn, key, label, description, sort_order)


def _seed_rules(conn: sqlite3.Connection) -> None:
    for (category_key, key, label, value_type, unit, high_impact,
         description, sort_order, default_value) in SEED_RULES:
        cat = db.get_category_by_key(conn, category_key)
        if cat is None:
            continue
        rule_id = db.upsert_rule(
            conn, category_id=cat["category_id"], key=key, label=label,
            value_type=value_type, unit=unit, high_impact=high_impact,
            description=description, sort_order=sort_order,
        )
        # Seed the firm-wide default version only if none exists yet.
        if db.latest_rule_version(conn, rule_id, "firm", None) is None:
            db.insert_rule_version(
                conn, rule_id=rule_id, scope="firm", client_id=None,
                value=default_value, effective_from=SEED_AS_OF, created_by="seed",
            )


def _seed_taxonomy(conn: sqlite3.Connection) -> None:
    existing = {e["code"] for e in db.list_taxonomy(conn)}
    for category, code, label, description, sort_order in SEED_TAXONOMY:
        if code in existing:
            continue
        db.add_taxonomy_entry(
            conn, category=category, code=code, label=label,
            description=description, sort_order=sort_order,
        )


def _seed_regulatory(conn: sqlite3.Connection) -> None:
    for domain, section, name, rate_or_rule, effective_from, notes in SEED_REGULATORY:
        db.upsert_regulatory_rule(
            conn, domain=domain, section=section, name=name,
            rate_or_rule=rate_or_rule, effective_from=effective_from,
            seed_as_of=SEED_AS_OF, notes=notes,
        )


def _seed_due_dates(conn: sqlite3.Connection) -> None:
    for obligation, period, due_day, due_month, grace_days, description, sort_order in SEED_DUE_DATES:
        db.upsert_statutory_due_date(
            conn, obligation=obligation, period=period, due_day=due_day,
            due_month=due_month, grace_days=grace_days, description=description,
            sort_order=sort_order,
        )