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
from typing import Optional

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
    ("invoice_gst_rules", "Invoice Extraction — GST Rules",
     "Rules the Invoice Extraction deterministic layer reads: the service/SAC prefix, the "
     "totals and tax tolerances, and the legal-form suffixes stripped when matching a "
     "client's own name.", 7),
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

    # --- Invoice Extraction GST rules (Phase 3) ---
    #
    # These are the Invoice Extraction deterministic layer's rules. They sit in
    # their own category rather than under gst_tolerance, because C1's existing
    # gst.amount_tolerance.* / gst.rounding_tolerance govern RECONCILIATION
    # MATCHING (books vs portal) and carry very different values (Rs 10, Rs 2) —
    # reusing them here would silently change what "reconciles" means on an
    # invoice. Each is a firm-wide default with the usual per-client override.
    ("invoice_gst_rules", "gst.service_sac_prefix", "Service / SAC code prefix", "text", "", False,
     "An HSN/SAC starting with this prefix is a SERVICE. Line quantity and rate are then "
     "optional for that document (a service line rarely prints either). Never makes a field "
     "required — this rule can only relax.", 1, "99"),
    ("invoice_gst_rules", "gst.total_tolerance_rupees", "Totals tolerance (Rs)", "number", "Rs", False,
     "Allowed rupee difference when checking taxable + tax +/- round off against the printed "
     "invoice total.", 2, "0.05"),
    ("invoice_gst_rules", "gst.tax_tolerance_rupees", "Tax tolerance (Rs)", "number", "Rs", False,
     "Allowed rupee difference when checking a printed tax amount against rate x taxable.", 3, "1.00"),
    ("invoice_gst_rules", "gst.client_legal_form_suffixes", "Client legal-form suffixes", "text", "", False,
     "Comma-separated legal-form words stripped when comparing a document's party name to the "
     "client's registered name. Kept deliberately limited: a longer list starts erasing real "
     "words from a trading name.", 4,
     "limited,ltd,pvt,private,llp,llpin,inc,incorporated,corp,corporation,company,co,and,&"),
    ("invoice_gst_rules", "gst.individual_name_stopwords", "Business words (individual detector)", "text", "", False,
     "Comma-separated words that mark a party name as a BUSINESS rather than an individual. "
     "Used only to decide whether a recipient is a person (never for matching), so a miss is "
     "safe: an unrecognised business name simply stays undetermined instead of being "
     "mis-called.", 5,
     "traders,trading,enterprises,enterprise,solutions,services,industries,industry,stores,"
     "store,agency,agencies,associates,consultants,consultancy,technologies,exports,imports,"
     "suppliers,supply,distributors,marketing,corporation,company,limited,ltd,llp,pvt,private,"
     "hospital,hotels,pharma,pharmaceuticals,textiles,apparels,garments,constructions,"
     "builders,infra,logistics,transport,shipping,printing,press,energies,power,steel,"
     "cement,chemicals,foods,agro,auto,motors,electricals,engineering,equipments,systems"),
]

# ---------------------------------------------------------------------------
# GST rate slabs as DATED periods (Phase 3)
# ---------------------------------------------------------------------------
#
# The valid-slab set CHANGED on 2025-09-22: 12% and 28% were withdrawn and 40%
# was introduced. A single open-ended slab list therefore cannot answer "which
# rates were legal on the date this invoice was printed", so the slabs are
# seeded as two dated periods and the check reads the period of the INVOICE
# DATE.
#
# The original C1 seed carried five slab rows with effective_from 2024-04-01 and
# effective_to NULL. An open-ended row matches EVERY date, so leaving them as
# they were would make 12% and 28% look valid after 2025-09-22 — a wrong PASS on
# exactly the check this data exists for. They are therefore re-dated here to
# the first period. This is the one place this seed touches an existing row, and
# it only ever rewrites SEED OWN rows (seed_as_of == SEED_AS_OF and a
# 'GST rate slab' name); a Manager's edited value is never touched. The
# correction is idempotent and is logged to the rule change log.
#
# Both periods are UNVERIFIED seed data: confirm against the GST rate
# notifications before relying on them.

# (effective_from, effective_to, slabs, note)
SEED_GST_SLAB_PERIODS: list[tuple[str, Optional[str], tuple[str, ...], str]] = [
    (
        "2017-07-01", "2025-09-21", ("0", "0.25", "3", "5", "12", "18", "28"),
        "UNVERIFIED — seeded from general knowledge. 2017-07-01 is the GST rollout date, used as the open start "
        "of this period; confirm it.",
    ),
    (
        "2025-09-22", None, ("0", "0.25", "3", "5", "18", "40"),
        "UNVERIFIED — seeded from general knowledge. The 2025-09-22 change withdrew 12% and 28% and introduced "
        "40%; confirm all six slabs and the date.",
    ),
]

# Slab values the ORIGINAL C1 seed already carried, as open-ended rows
# (effective_from 2024-04-01, effective_to NULL).
_ORIGINAL_SLAB_VALUES = ("0", "5", "12", "18", "28")
_REORIGINAL_EFFECTIVE_FROM = "2024-04-01"


def _slab_label(value: str) -> str:
    """'0' -> '0%', '0.25' -> '0.25%' (never '0.25.0%')."""
    text = str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)
    return f"{text}%"


def _seed_gst_slabs(conn: sqlite3.Connection) -> None:
    """Re-date the original open-ended slab rows, then seed both dated periods.

    ORDER MATTERS: the re-dating runs first and is pinned to the original rows
    (``effective_from = '2024-04-01' AND effective_to IS NULL``), so it can
    never touch a row this function seeds for the second period. On a later run
    the same condition matches nothing, which makes the whole function
    idempotent.
    """
    first_from, first_to, _slabs, first_note = SEED_GST_SLAB_PERIODS[0]
    for value in _ORIGINAL_SLAB_VALUES:
        conn.execute(
            "UPDATE regulatory_rules SET effective_from = ?, effective_to = ?, notes = ? "
            "WHERE domain = 'GST' AND section IS NULL AND name = ? AND seed_as_of = ? "
            "AND last_reviewed_at IS NULL AND effective_from = ? AND effective_to IS NULL",
            (
                first_from, first_to, first_note,
                f"GST rate slab — {_slab_label(value)}", SEED_AS_OF,
                _REORIGINAL_EFFECTIVE_FROM,
            ),
        )
    conn.commit()

    for effective_from, effective_to, slabs, note in SEED_GST_SLAB_PERIODS:
        for value in slabs:
            # Keyed on (domain, section, name, effective_from), so a second run
            # updates its own row instead of inserting a duplicate.
            db.upsert_regulatory_rule(
                conn, domain="GST", section=None,
                name=f"GST rate slab — {_slab_label(value)}",
                rate_or_rule=f"{value}%", effective_from=effective_from,
                effective_to=effective_to, seed_as_of=SEED_AS_OF, notes=note,
            )


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
    # F6 (Source File Ingestion & Format Registry) — the two canonical field
    # additions its build required confirming before build start (§12/§13
    # of the F6 build prompt). Recorded here as taxonomy entries so the
    # decision is visible/auditable in C1's own screen, not just a code
    # comment. See src/ingestion.py's GST_CANONICAL_FIELDS (rounding_
    # adjustment) and src/f6/schema.py's NormalizedRow (drill-down rows).
    ("canonical_field_addition", "rounding_adjustment", "rounding_adjustment (GST)",
     "Rounding/round-off residual carried per source row (e.g. Tally's 'Other "
     "Amt.'). taxable + Σtax + rounding_adjustment = invoice_value, ±₹1. Never "
     "folded into taxable value or tax. Confirmed for F6 build, 2026-09-18.", 1),
    ("canonical_field_addition", "pre_aggregation_drill_down", "Pre-aggregation drill-down rows",
     "Rate-bucketed source rows (e.g. GSTR-1's one-row-per-tax-rate lines, "
     "Purchase Register's per-bucket columns) are retained alongside their "
     "aggregated document-level row for drill-down, keyed by aggregation "
     "group id. Confirmed for F6 build, 2026-09-18.", 2),
]

# ---------------------------------------------------------------------------
# Regulatory rules table — pre-seeded dated rates (verify before relying)
# ---------------------------------------------------------------------------

SEED_REGULATORY: list[tuple[str, str | None, str, str, str, str | None, float, float, str]] = [
    # (domain, section, name, rate_or_rule, effective_from, effective_to,
    #  single_transaction_threshold, annual_aggregate_threshold, notes)
    # NOTE: the GST rate slabs are NOT listed here any more. They moved to
    # _seed_gst_slabs() below, which owns them as two DATED periods. Leaving a
    # second, open-ended copy here would make the two disagree the moment the
    # slab set changes again.
    ("GST", None, "ITC — blocked (personal use)", "ITC blocked", "2024-04-01", None, 0, 0, "Section 17(5) blocked credits."),
    ("TDS", "194C", "Payments to contractors", "2.0% (1.0% individual/HUF)", "2024-04-01", None, 30000, 100000, "Threshold Rs 30,000 single / Rs 1,00,000 annual."),
    ("TDS", "194J", "Professional / technical services", "10.0% professional / 2.0% technical", "2024-04-01", None, 30000, 0, "Threshold Rs 30,000."),
    ("TDS", "194I", "Rent", "10.0% land/building / 2.0% plant/machinery", "2024-04-01", None, 0, 240000, "Threshold Rs 2,40,000 annual."),
    ("TDS", "194A", "Interest other than securities", "10.0%", "2024-04-01", None, 0, 40000, "Threshold Rs 40,000."),
    ("TDS", "194H", "Commission or brokerage", "5.0%", "2024-04-01", None, 0, 15000, "Threshold Rs 15,000."),
    ("TDS", "194Q", "Purchase of goods", "0.1%", "2024-04-01", None, 0, 5000000, "Threshold Rs 50,00,000 annual."),
    ("TDS", "192", "Salary", "0.0%", "2024-04-01", None, 0, 0, "Computed on estimated annual liability — flat rate N/A."),
    ("TDS", "194B", "Winnings from lottery / game shows", "30.0%", "2024-04-01", None, 10000, 0, "Threshold Rs 10,000 per winning."),
    ("TDS", "194D", "Insurance commission", "5.0%", "2024-04-01", None, 0, 15000, "Threshold Rs 15,000 annual."),
    ("TDS", "194E", "Payments to non-resident sportsmen / entertainers", "20.0%", "2024-04-01", None, 0, 0, "No threshold — deduct on every payment."),
]

# ---------------------------------------------------------------------------
# TDS section aliases — old-Act code <-> Income-tax Act 2025 code
# ---------------------------------------------------------------------------
#
# PLACEHOLDER ONLY: we have no verified source for the Income-tax Act 2025's
# actual section renumbering, so every new_code below is seeded identical to
# its old_code (i.e. "assume unchanged until told otherwise") and flagged
# UNVERIFIED. This is deliberately NOT a guess at a plausible-looking new
# citation — that would risk being mistaken for a real regulatory fact. The
# CA firm confirms the real mapping; only then should new_code values change.

ACT_2025_EFFECTIVE_FROM = "2026-04-01"

SEED_TDS_ALIASES: list[tuple[str, str]] = [
    (section, section)
    for _domain, section, *_rest in SEED_REGULATORY
    if _domain == "TDS"
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
    _seed_gst_slabs(conn)
    _seed_tds_aliases(conn)
    _seed_due_dates(conn)
    _seed_state_codes(conn)
    conn.commit()


def _seed_state_codes(conn: sqlite3.Connection) -> None:
    """The GST state-code vocabulary (see src/rules/gst_state_codes.py)."""
    from src.rules.gst_state_codes import seed_state_codes

    seed_state_codes(conn)


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
    for (domain, section, name, rate_or_rule, effective_from, effective_to,
         single_txn_threshold, annual_threshold, notes) in SEED_REGULATORY:
        db.upsert_regulatory_rule(
            conn, domain=domain, section=section, name=name,
            rate_or_rule=rate_or_rule, effective_from=effective_from,
            effective_to=effective_to,
            single_transaction_threshold=single_txn_threshold,
            annual_aggregate_threshold=annual_threshold,
            seed_as_of=SEED_AS_OF, notes=notes,
        )


def _seed_tds_aliases(conn: sqlite3.Connection) -> None:
    for old_code, new_code in SEED_TDS_ALIASES:
        db.upsert_tds_section_alias(
            conn, old_code=old_code, new_code=new_code,
            effective_from=ACT_2025_EFFECTIVE_FROM,
            notes=(
                "UNVERIFIED placeholder — assumes no renumbering under the "
                "Income-tax Act 2025. Confirm the actual mapping with the CA "
                "firm before relying on this for filings."
            ),
        )


def _seed_due_dates(conn: sqlite3.Connection) -> None:
    for obligation, period, due_day, due_month, grace_days, description, sort_order in SEED_DUE_DATES:
        db.upsert_statutory_due_date(
            conn, obligation=obligation, period=period, due_day=due_day,
            due_month=due_month, grace_days=grace_days, description=description,
            sort_order=sort_order,
        )