"""Module 8 — Visual Reconciliation Report (presentation layer).

Read-only aggregation over Module 2's EXISTING reconciliation output. This
module introduces NO new data model and NO new storage: every number it
returns is derived on the fly from records that already exist (`match_results`,
`reconciliation_exceptions`, `eligible_credit_figures`, `tds_chain_stages`,
F5's validation/sync/recon-of-recon tables).

Deterministic vs AI-drafted boundary (per the build prompt): match status,
amounts and every figure behind every chart are deterministic Tier-A output
from Module 2. The ONLY AI-drafted artefact is the "Accountant's Read"
narrative paragraph, and it passes through a Drafter/Verifier guard
(`verify_narrative`) that rejects any number or classification the report did
not actually contain. A chart is never itself AI-generated or AI-adjusted.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from src import db as recon_db
from src import queries
from src.clients import service as clients
from src.module2 import service as m2
from src.reconciliation.review import drop_zero_slices

# ---------------------------------------------------------------------------
# Classification buckets
# ---------------------------------------------------------------------------

GST_BUCKETS = ["Matched", "Amount Difference", "Not in Books", "Not in Portal"]
# TDS gets the identical treatment once TDS data is present, with the two
# portal/books gaps re-labelled for the TDS domain.
TDS_BUCKETS = ["Matched", "Amount Difference", "Missing", "Late Deposit"]

_TDS_BUCKET_OF = {
    "Matched": "Matched",
    "Amount Difference": "Amount Difference",
    "Not in Books": "Missing",
    "Not in Portal": "Late Deposit",
}

# Foundation semantic tokens only — never a new hex introduced for charts.
# ok/green = Matched, warn/amber = Amount Diff, fail/red = Not in Books,
# informational/blue = Not in Portal.
_BUCKET_COLOR_ROLE = {
    "Matched": "rule",
    "Amount Difference": "ai",
    "Not in Books": "danger",
    "Not in Portal": "accent",
    "Missing": "danger",
    "Late Deposit": "accent",
}


class ReportError(Exception):
    """Raised for expected report-build failures."""


# ---------------------------------------------------------------------------
# Run discovery
# ---------------------------------------------------------------------------


def list_report_runs(
    *, client: Optional[str] = None, period: Optional[str] = None, recon_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Every completed run the report can be built for, newest first."""
    df = queries.list_runs(client=client, period=period, recon_type=recon_type)
    runs: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        rt = row.get("recon_type")
        runs.append({
            "run_id": int(row["run_id"]),
            "client": row.get("client") or "",
            "period": row.get("period") or "",
            "recon_type": rt,
            "label": f"{row.get('client')} · {row.get('period')} · {rt}",
        })
    return runs


# ---------------------------------------------------------------------------
# Client resolution (folder key → F2 client)
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def resolve_client_id(folder: str) -> Optional[int]:
    """Map a run's `client` folder key (e.g. "AcmeTextiles") onto F2's
    end_clients row (exact, then normalized prefix). Best-effort."""
    if not folder:
        return None
    try:
        rows = clients.list_clients(include_inactive=True)
    except Exception:  # noqa: BLE001
        return None
    target = _normalize_name(folder)
    for c in rows:
        if _normalize_name(c["legal_name"]) == target:
            return int(c["client_id"])
    for c in rows:
        norm = _normalize_name(c["legal_name"])
        if norm and (norm.startswith(target) or target.startswith(norm)):
            return int(c["client_id"])
    return None


def _client_display_name(client_id: Optional[int], folder: str) -> str:
    if client_id is not None:
        try:
            row = clients.get_client(client_id)
            if row and row.get("legal_name"):
                return row["legal_name"]
        except Exception:  # noqa: BLE001
            pass
    return folder


# ---------------------------------------------------------------------------
# Record value helpers (borrowed shape from Module 2's match records)
# ---------------------------------------------------------------------------


def _load(record: Any) -> Optional[dict[str, Any]]:
    if record is None:
        return None
    if isinstance(record, dict):
        return record
    try:
        return json.loads(record)
    except (TypeError, ValueError):
        return None


def _num(record: Optional[dict[str, Any]], keys: tuple[str, ...]) -> float:
    if not isinstance(record, dict):
        return 0.0
    for k in keys:
        v = record.get(k)
        if v is None or v == "":
            continue
        try:
            return abs(float(v))
        except (TypeError, ValueError):
            continue
    return 0.0


def _tax_of(record: Optional[dict[str, Any]]) -> float:
    if not isinstance(record, dict):
        return 0.0
    tt = _num(record, ("total_tax",))
    if tt:
        return tt
    return sum(_num(record, (c,)) for c in ("cgst", "sgst", "igst", "cess"))


def _item_value(record: Optional[dict[str, Any]], recon_type: str) -> float:
    if recon_type == "GST":
        return _num(record, ("invoice_value", "taxable_value")) or _tax_of(record)
    if recon_type == "OTHER":
        return _num(record, ("amount", "invoice_value"))
    return _num(record, ("amount_paid_credited", "tax_deducted", "tax_deposited"))


def _party_of(record: Optional[dict[str, Any]]) -> str:
    if not isinstance(record, dict):
        return ""
    for k in ("party_name", "deductee_name", "vendor_name", "supplier_name", "name", "party"):
        v = record.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _ref_of(record: Optional[dict[str, Any]]) -> str:
    if not isinstance(record, dict):
        return ""
    for k in ("invoice_number", "challan_number", "voucher_number", "reference", "note_number"):
        v = record.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _date_of(record: Optional[dict[str, Any]]) -> str:
    if not isinstance(record, dict):
        return ""
    for k in ("invoice_date", "deposit_date", "date", "voucher_date"):
        v = record.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _clean(v: Any) -> str:
    """Normalize a pandas/None value to a display string. pandas NaN is truthy,
    so a bare `or ""` leaks the literal "nan" into labels."""
    if v is None:
        return ""
    try:
        import pandas as pd

        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _classify(recon_type: str, classification: str, difference_type: str) -> str:
    if classification == "Matched":
        return "Matched"
    if recon_type == "TDS":
        return _TDS_BUCKET_OF.get(classification, classification)
    return classification


# ---------------------------------------------------------------------------
# Report build
# ---------------------------------------------------------------------------


def build_report(run_id: int, *, allow_ai: bool = False, db_path=None) -> dict[str, Any]:
    """Aggregate a completed run into the visual report's data model.

    Deterministic throughout — the narrative is attached separately by
    `generate_narrative`. No writes, no new tables.
    """
    run = queries.get_run(run_id, db_path=db_path)
    if run is None:
        raise ReportError(f"Run {run_id} not found.")
    recon_type = run["recon_type"]
    folder = run["client"]
    period = run["period"]

    client_id = resolve_client_id(folder)
    client_name = _client_display_name(client_id, folder)

    df = queries.get_results(run_id, db_path=db_path)
    records: list[dict[str, Any]] = []
    unmatched_value = 0.0
    matched_count = 0

    bucket_stat: dict[str, dict[str, Any]] = {}
    buckets = TDS_BUCKETS if recon_type == "TDS" else (GST_BUCKETS if recon_type == "GST" else GST_BUCKETS)

    party_stat: dict[str, dict[str, Any]] = {}

    total_deducted = 0.0
    total_deposited = 0.0

    for _, row in df.iterrows():
        books = _load(row.get("books_record"))
        portal = _load(row.get("portal_record"))
        classification = _clean(row.get("classification"))
        difference_type = _clean(row.get("difference_type"))
        party = _party_of(books) or _party_of(portal)
        ref = _ref_of(books) or _ref_of(portal)
        rec_date = _date_of(books) or _date_of(portal)
        value = _item_value(books, recon_type) or _item_value(portal, recon_type)
        books_value = _item_value(books, recon_type)
        portal_value = _item_value(portal, recon_type)

        bucket = _classify(recon_type, classification, difference_type)

        if classification == "Matched":
            matched_count += 1
        else:
            unmatched_value += value
            if party:
                p = party_stat.setdefault(party, {"party": party, "value": 0.0, "count": 0})
                p["value"] += value
                p["count"] += 1

        b = bucket_stat.setdefault(bucket, {"bucket": bucket, "count": 0, "value": 0.0})
        b["count"] += 1
        b["value"] += value

        if recon_type == "TDS":
            total_deducted += _num(books, ("tax_deducted",)) or _num(portal, ("tax_deducted",))
            total_deposited += _num(books, ("tax_deposited",)) or _num(portal, ("tax_deposited",))

        records.append({
            "result_id": int(row["result_id"]),
            "classification": classification,
            "bucket": bucket,
            "difference_type": difference_type,
            "party": party,
            "reference": ref,
            "date": rec_date,
            "value": round(value, 2),
            "books_value": round(books_value, 2),
            "portal_value": round(portal_value, 2),
            "confidence_band": _clean(row.get("confidence_band")),
            "confidence_score": int(row.get("confidence_score") or 0),
            "match_reason": _clean(row.get("match_reason")),
            "books_fields": _record_fields(books),
            "portal_fields": _record_fields(portal),
        })

    total = len(records)
    match_rate = round(matched_count / total * 100, 1) if total else 0.0

    # Classification donut datasets (count + value per bucket, chart colors as
    # Foundation roles so the view resolves them via tokens).
    classification_slices = []
    for name in buckets:
        stat = bucket_stat.get(name, {"count": 0, "value": 0.0})
        classification_slices.append({
            "key": name,
            "name": name,
            "count": int(stat["count"]),
            "value": round(float(stat["value"]), 2),
            "color_role": _BUCKET_COLOR_ROLE.get(name, "neutral"),
        })

    # Exceptions-by-supplier (top unmatched value, descending).
    suppliers = sorted(party_stat.values(), key=lambda p: p["value"], reverse=True)
    supplier_rows = [
        {"party": p["party"], "value": round(p["value"], 2), "count": p["count"]}
        for p in suppliers
    ]

    # GST: ITC split + eligible-credit read.
    gst_block = None
    if recon_type == "GST":
        credit = m2.eligible_credit_for_client(client_id, period=period, db_path=db_path) if client_id else None
        itc_slices = None
        itc_note = ""
        if credit:
            eligible = round(float(credit.get("eligible_credit") or 0.0), 2)
            total_itc = round(float(credit.get("total_itc_claimed") or 0.0), 2)
            # §2 — "Ineligible" means §17(5) blocked credit plus reverse-charge
            # ITC, taken from the run's OWN eligibility markers. It is NOT
            # (claimed − eligible): that difference is ITC on invoices that did
            # not match, which is a different quantity and was mislabelled
            # "Ineligible" — painting a red sliver on runs that have none.
            blocked = round(float(credit.get("blocked_credit_itc") or 0.0), 2)
            reverse_charge = round(float(credit.get("reverse_charge_itc") or 0.0), 2)
            ineligible = round(blocked + reverse_charge, 2)
            # Zero-value segments are dropped BEFORE the chart sees them, so a
            # nil bucket can never render an arc.
            itc_slices = drop_zero_slices([
                {"name": "Eligible ITC", "value": eligible, "color_role": "rule"},
                {"name": "Ineligible ITC", "value": ineligible, "color_role": "danger"},
            ])
            if total_itc <= 0:
                itc_note = "No input tax was captured in this run's records, so the split is nil."
            elif len(itc_slices) == 1:
                itc_note = f"100% {itc_slices[0]['name']} — no ineligible ITC in this run."
        else:
            itc_note = "No eligible-credit figure was computed for this run (2A did not run)."
        gst_block = {
            "credit": credit,
            "itc_slices": itc_slices,
            "itc_single": bool(itc_slices) and len(itc_slices) == 1,
            "itc_single_label": itc_slices[0]["name"] if itc_slices and len(itc_slices) == 1 else "",
            "itc_note": itc_note,
        }

    # TDS: deducted vs deposited.
    tds_block = None
    if recon_type == "TDS":
        tds_block = {
            "deducted_vs_deposited": [
                {"name": "Tax deducted", "value": round(total_deducted, 2), "color_role": "accent"},
                {"name": "Tax deposited", "value": round(total_deposited, 2), "color_role": "rule"},
            ],
            "total_deducted": round(total_deducted, 2),
            "total_deposited": round(total_deposited, 2),
        }

    # Period-over-period trend (this vs prior period, same client + recon type).
    trend = _trend(run_id, folder, period, recon_type, match_rate, sql_path=db_path)

    # Data quality & scope notes.
    data_quality = _data_quality(run, folder, client_name, recon_type, df, records)

    # Integrity strip (F5 gate + independent verification).
    integrity = _integrity(run_id, client_id, records, bucket_stat, db_path=db_path)

    return {
        "run_id": run_id,
        "client_folder": folder,
        "client_name": client_name,
        "client_id": client_id,
        "period": period,
        "recon_type": recon_type,
        "recon_type_label": {"GST": "GST (2A)", "TDS": "TDS (2B)", "OTHER": "Other (2C)"}.get(recon_type, recon_type),
        "generated_at": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"),
        "source_files": run.get("source_file_names") or [],
        "kpi": {
            "match_rate": match_rate,
            "matched_count": matched_count,
            "total_count": total,
            "exception_count": total - matched_count,
            "exceptions_value": round(unmatched_value, 2),
            "itc_eligible": (
                round(float((gst_block or {}).get("credit", {}).get("eligible_credit") or 0.0), 2)
                if recon_type == "GST" and gst_block and gst_block.get("credit") else None
            ),
            "net_payable": None,  # no output-tax/GSTR-3B source exists in this build
            "deducted_vs_deposited_gap": (
                round(total_deducted - total_deposited, 2) if recon_type == "TDS" else None
            ),
        },
        "classification_slices": classification_slices,
        "suppliers": supplier_rows,
        "gst": gst_block,
        "tds": tds_block,
        "trend": trend,
        "records": records,
        "data_quality": data_quality,
        "integrity": integrity,
    }


def list_report_periods() -> list[dict[str, Any]]:
    """Distinct client+period combinations that have at least one run, newest
    period first. This is the report's unit of selection — a period report
    shows GST and TDS together."""
    df = queries.list_runs()
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in df.iterrows():
        folder = row.get("client") or ""
        period = row.get("period") or ""
        key = (folder, period)
        entry = seen.setdefault(key, {"client_folder": folder, "period": period, "recon_types": set()})
        entry["recon_types"].add(row.get("recon_type"))
    out = []
    for entry in seen.values():
        entry["recon_types"] = sorted(entry["recon_types"])
        entry["client_name"] = _client_display_name(resolve_client_id(entry["client_folder"]), entry["client_folder"])
        entry["label"] = f"{entry['client_name']} · {entry['period']}"
        out.append(entry)
    # Real calendar periods (YYYY-MM) first, newest first; demo-session keys
    # afterwards (they sort lexicographically otherwise and dominate).
    out.sort(key=lambda e: (_is_calendar_period(e["period"]), e["period"], e["client_name"]), reverse=True)
    return out


def _is_calendar_period(period: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}", period or ""))


def _latest_run(folder: str, period: str, recon_type: str) -> Optional[int]:
    conn = recon_db.get_connection()
    try:
        row = conn.execute(
            "SELECT run_id FROM runs WHERE client = ? AND period = ? AND recon_type = ? "
            "ORDER BY run_id DESC LIMIT 1",
            (folder, period, recon_type),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else None


def build_period_report(folder: str, period: str, *, allow_ai: bool = False) -> dict[str, Any]:
    """Compose the GST and TDS reports for one client+period into the single
    report the screen renders. Either side may be absent — the view renders a
    clear, actionable empty state rather than a blank card."""
    gst_run = _latest_run(folder, period, "GST")
    tds_run = _latest_run(folder, period, "TDS")

    gst = build_report(gst_run) if gst_run else None
    tds = build_report(tds_run) if tds_run else None

    if gst is None and tds is None:
        raise ReportError(f"No reconciliation run exists for {folder} · {period}.")

    primary = gst or tds
    client_id = primary["client_id"]
    client_name = primary["client_name"]

    # Combined KPI row.
    gst_kpi = (gst or {}).get("kpi", {})
    tds_kpi = (tds or {}).get("kpi", {})
    exception_count = int(gst_kpi.get("exception_count", 0)) + int(tds_kpi.get("exception_count", 0))
    exceptions_value = round(
        float(gst_kpi.get("exceptions_value", 0.0)) + float(tds_kpi.get("exceptions_value", 0.0)), 2
    )
    match_rate = gst_kpi.get("match_rate") if gst else tds_kpi.get("match_rate")
    matched_count = int(gst_kpi.get("matched_count", 0)) + int(tds_kpi.get("matched_count", 0))
    total_count = int(gst_kpi.get("total_count", 0)) + int(tds_kpi.get("total_count", 0))

    # Combined exceptions-by-supplier (top unmatched value across both sides).
    combined: dict[str, dict[str, Any]] = {}
    for side in (gst, tds):
        for s in (side or {}).get("suppliers", []):
            c = combined.setdefault(s["party"], {"party": s["party"], "value": 0.0, "count": 0})
            c["value"] += s["value"]
            c["count"] += s["count"]
    suppliers = sorted(combined.values(), key=lambda p: p["value"], reverse=True)
    suppliers = [
        {"party": p["party"], "value": round(p["value"], 2), "count": p["count"],
         "key": p["party"], "kind": "party"}
        for p in suppliers
    ]

    # Annotate every chart datum with the identity a click needs to filter by.
    for side_label, side in (("GST", gst), ("TDS", tds)):
        if not side:
            continue
        for s in side["classification_slices"]:
            s["side"] = side_label
            s["kind"] = "bucket"
        if side.get("gst") and side["gst"].get("itc_slices"):
            for s in side["gst"]["itc_slices"]:
                s["side"] = side_label
                s["kind"] = "itc"
        if side.get("tds") and side["tds"].get("deducted_vs_deposited"):
            for s in side["tds"]["deducted_vs_deposited"]:
                s["side"] = side_label
                s["kind"] = "tds_deposit"

    # Trend uses the GST side where present (the primary reconciliation).
    trend = (gst or tds)["trend"]

    # Data quality: union of both sides' notes, de-duplicated by title.
    dq_notes: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for side in (gst, tds):
        for n in (side or {}).get("data_quality", {}).get("notes", []):
            if n["title"] in seen_titles:
                continue
            seen_titles.add(n["title"])
            dq_notes.append(n)

    # Integrity: the primary side's strip (F5 gate is client-scoped anyway).
    integrity = (gst or tds)["integrity"]

    report = {
        "client_folder": folder,
        "client_name": client_name,
        "client_id": client_id,
        "period": period,
        "generated_at": primary["generated_at"],
        "gst": gst,
        "tds": tds,
        "has_gst": gst is not None,
        "has_tds": tds is not None,
        "kpi": {
            "match_rate": match_rate,
            "matched_count": matched_count,
            "total_count": total_count,
            "exception_count": exception_count,
            "exceptions_value": exceptions_value,
            "itc_eligible": gst_kpi.get("itc_eligible") if gst else None,
            "net_payable": None,
            "deducted_vs_deposited_gap": tds_kpi.get("deducted_vs_deposited_gap") if tds else None,
        },
        "suppliers": suppliers,
        "trend": trend,
        "data_quality": {"notes": dq_notes, "has_caveats": any(n["kind"] == "caveat" for n in dq_notes)},
        "integrity": integrity,
    }
    report["narrative"] = generate_narrative(report, allow_ai=allow_ai)
    return report


def _record_fields(record: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(record, dict):
        return []
    out = []
    for k, v in record.items():
        if k.startswith("_") or v in (None, ""):
            continue
        out.append({"label": str(k).replace("_", " "), "value": str(v)})
    return out


def _trend(run_id, folder, period, recon_type, this_rate, *, sql_path=None) -> dict[str, Any]:
    """Prior-period comparison where data exists. Never a broken/empty chart."""
    conn = recon_db.get_connection(sql_path)
    try:
        prior = conn.execute(
            "SELECT run_id, period FROM runs WHERE client = ? AND recon_type = ? AND period < ? "
            "ORDER BY period DESC LIMIT 1",
            (folder, recon_type, period),
        ).fetchone()
    finally:
        conn.close()

    if prior is None:
        return {
            "available": False,
            "message": "First period — no trend yet",
            "prior_period": "",
            "this_rate": this_rate,
            "prior_rate": None,
        }

    prior_run_id, prior_period = int(prior[0]), prior[1]
    pdf = queries.get_results(prior_run_id, db_path=sql_path)
    p_total = len(pdf)
    p_matched = int((pdf["classification"] == "Matched").sum()) if p_total else 0
    prior_rate = round(p_matched / p_total * 100, 1) if p_total else 0.0
    return {
        "available": True,
        "message": "",
        "prior_period": prior_period,
        "prior_run_id": prior_run_id,
        "this_rate": this_rate,
        "prior_rate": prior_rate,
        "this_net_payable": None,
        "prior_net_payable": None,
    }


def _data_quality(run, folder, client_name, recon_type, df, records) -> dict[str, Any]:
    """Specific, run-derived notes — never generic boilerplate."""
    notes: list[dict[str, str]] = []

    # 1. Caveats the ingestion layer recorded for this run.
    for c in run.get("caveats") or []:
        notes.append({
            "title": c.get("label") or "Check not performed",
            "detail": c.get("detail") or "",
            "kind": "caveat",
        })

    # 2. The client label in the run vs F2's legal name.
    if client_name and _normalize_name(client_name) != _normalize_name(folder):
        notes.append({
            "title": "Run label differs from the client master",
            "detail": (
                f"This run is filed under \u201c{folder}\u201d, matched to client master "
                f"\u201c{client_name}\u201d. Confirmed by name matching, not by an explicit link."
            ),
            "kind": "note",
        })

    # 3. Missing portal/books side.
    if records:
        no_books = sum(1 for r in records if not r["books_fields"])
        no_portal = sum(1 for r in records if not r["portal_fields"])
        if no_books:
            notes.append({
                "title": f"{no_books} record(s) have no books side",
                "detail": (
                    "These were seen only on the portal side, so no books value could be "
                    "compared — they are reported as \u201cnot in books\u201d as filed."
                ),
                "kind": "caveat",
            })
        if no_portal:
            notes.append({
                "title": f"{no_portal} record(s) have no portal side",
                "detail": (
                    "These were seen only in the books, so no portal value could be compared "
                    "\u2014 they are reported as \u201cnot in portal\u201d as filed."
                ),
                "kind": "caveat",
            })

    # 4. The permanent scope note: what this report IS.
    notes.append({
        "title": "What this report covers",
        "detail": (
            "This report reconciles the source files listed above for this period only. "
            "It does not perform a statutory filing, and it does not pull live portal data "
            "\u2014 both are out of scope for the reconciliation engine."
        ),
        "kind": "scope",
    })

    return {
        "notes": notes,
        "source_files": run.get("source_file_names") or [],
        "has_caveats": any(n["kind"] == "caveat" for n in notes),
    }


def _integrity(run_id, client_id, records, bucket_stat, *, db_path=None) -> dict[str, Any]:
    """Compact PASS/FAIL strip: F5's own gate + an independent recompute."""
    f5_checks: list[dict[str, str]] = []
    verification: list[dict[str, str]] = []

    # --- F5 gate -------------------------------------------------------
    open_blocks = 0
    sync_ok = None
    ties_out = None
    if client_id is not None:
        try:
            from src.f5 import service as f5

            blocks = f5.list_blocks(client_id=client_id, status="open")
            open_blocks = len(blocks)
            health = f5.sync_health_for_client(client_id)
            failed = [h for h in health if h.get("status") == "attempted_failed"]
            sync_ok = len(failed) == 0
            ror = f5.recon_of_recon_for_client(client_id)
            match = [r for r in ror if r.get("run_id") == run_id]
            ties_out = bool(match[0]["ties_out"]) if match else None
        except Exception:  # noqa: BLE001
            pass

    f5_checks.append({
        "label": "Structural validity (open F5 blocks)",
        "result": "PASS" if open_blocks == 0 else "FAIL",
        "detail": f"{open_blocks} open block(s)",
    })
    f5_checks.append({
        "label": "Source sync health",
        "result": ("PASS" if sync_ok else "FAIL") if sync_ok is not None else "—",
        "detail": "All sources in sync" if sync_ok else "A source has failed",
    })
    f5_checks.append({
        "label": "Reconciliation of the reconciliation",
        "result": ("PASS" if ties_out else "FAIL") if ties_out is not None else "—",
        "detail": "Matched + unmatched ties to ingested totals" if ties_out else "No tied-out check for this run",
    })

    # --- Independent recompute ----------------------------------------
    # A genuinely separate code path recomputes counts straight from the raw
    # records and compares against the aggregated buckets.
    recomputed: dict[str, int] = {}
    for r in records:
        recomputed[r["bucket"]] = recomputed.get(r["bucket"], 0) + 1
    counts_consistent = all(
        int(bucket_stat.get(k, {}).get("count", 0)) == v for k, v in recomputed.items()
    ) and sum(int(v.get("count", 0)) for v in bucket_stat.values()) == len(records)

    verification.append({
        "label": "Parse counts",
        "result": "PASS" if counts_consistent else "FAIL",
        "detail": f"{len(records)} record(s) recounted",
    })

    total_value = round(sum(r["value"] for r in records), 2)
    bucket_value = round(sum(float(v.get("value", 0.0)) for v in bucket_stat.values()), 2)
    verification.append({
        "label": "Value totals",
        "result": "PASS" if abs(total_value - bucket_value) < 0.05 else "FAIL",
        "detail": f"{bucket_value:,.2f} reconciled across buckets",
    })

    # Every record must carry exactly one bucket.
    tagged = sum(1 for r in records if r["bucket"])
    verification.append({
        "label": "Every record classified",
        "result": "PASS" if tagged == len(records) else "FAIL",
        "detail": f"{tagged}/{len(records)} classified",
    })

    overall = "PASS" if all(c["result"] != "FAIL" for c in f5_checks + verification) else "FAIL"
    return {
        "f5": f5_checks,
        "verification": verification,
        "overall": overall,
    }


# ---------------------------------------------------------------------------
# Accountant's Read — deterministic narrative + Drafter/Verifier guard
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"₹?\s?([\d][\d,]*(?:\.\d+)?)")


def _money(v: float) -> str:
    return f"₹{v:,.2f}"


def _deterministic_narrative(report: dict[str, Any]) -> str:
    """Plain-language read built ONLY from the report's own figures. This is
    always available; the AI-drafted variant is optional and guard-verified.

    Works on the period report shape (gst/tds sub-reports) so a single
    paragraph reads the whole period."""
    kpi = report["kpi"]
    lines: list[str] = []

    rate = kpi["match_rate"]
    matched = kpi["matched_count"]
    total = kpi["total_count"]
    if total:
        if kpi["exception_count"] == 0:
            lines.append(
                f"All {total} records reconciled this period tie back cleanly — a {rate}% "
                "match rate with nothing requiring attention."
            )
        else:
            lines.append(
                f"Of the {total} records reconciled this period, {matched} matched cleanly "
                f"({rate}%). {kpi['exception_count']} need a look, worth "
                f"{_money(kpi['exceptions_value'])}."
            )

    # The dominant exception bucket across both sides, in plain language.
    exception_buckets: list[dict[str, Any]] = []
    for side in (report.get("gst"), report.get("tds")):
        for s in (side or {}).get("classification_slices", []):
            if s["key"] != "Matched" and s["count"] > 0:
                exception_buckets.append(s)
    exception_buckets.sort(key=lambda s: s["value"], reverse=True)
    if exception_buckets:
        top = exception_buckets[0]
        lines.append(
            f"The largest group is \u201c{top['name']}\u201d \u2014 {top['count']} record(s) "
            f"worth {_money(top['value'])}."
        )

    # Where to look first.
    suppliers = report.get("suppliers") or []
    if suppliers:
        s = suppliers[0]
        lines.append(
            f"Concentration is highest with {s['party']} ({_money(s['value'])} across "
            f"{s['count']} record(s)) — start there."
        )

    # GST position.
    gst = report.get("gst")
    if gst and gst.get("gst") and gst["gst"].get("credit"):
        credit = gst["gst"]["credit"]
        lines.append(
            f"Eligible input tax credit stands at {_money(float(credit.get('eligible_credit') or 0.0))} "
            f"against {_money(float(credit.get('total_itc_claimed') or 0.0))} claimed."
        )
    # TDS position.
    tds = report.get("tds")
    if tds and tds.get("tds"):
        t = tds["tds"]
        gap = tds["kpi"].get("deducted_vs_deposited_gap")
        lines.append(
            f"Tax deducted totals {_money(t['total_deducted'])} against "
            f"{_money(t['total_deposited'])} deposited"
            + (f" — an undeposited gap of {_money(gap)}." if gap else ".")
        )

    lines.append(
        "Recommended next step: review the records in the largest group above, then confirm "
        "the treatment of any above-materiality items before filing."
    )
    return " ".join(lines)


# Only exception-classification names are guarded as phrases — these are the
# claims that could be falsely invented. "Matched" is deliberately excluded:
# it legitimately appears as a verb ("0 matched cleanly") and the run's true
# match rate is already verified as a figure above.
_REPORT_ALLOWED_WORDS = {
    "amount difference": "Amount Difference",
    "amount diff": "Amount Difference",
    "not in books": "Not in Books",
    "not in portal": "Not in Portal",
    "late deposit": "Late Deposit",
}


def verify_narrative(narrative: str, report: dict[str, Any]) -> dict[str, Any]:
    """Drafter/Verifier guard: reject any figure or classification the
    deterministic report did not actually produce. This is what keeps the
    narrative from ever contradicting the charts and tables."""
    violations: list[str] = []

    # Allowed numbers: every figure behind every chart/KPI, to 2dp.
    allowed: set[str] = set()
    kpi = report["kpi"]
    for key in ("match_rate", "matched_count", "total_count", "exception_count", "exceptions_value"):
        v = kpi.get(key)
        if v is not None:
            allowed.add(_norm_num(v))
    for key in ("itc_eligible", "deducted_vs_deposited_gap"):
        v = kpi.get(key)
        if v is not None:
            allowed.add(_norm_num(v))

    present_buckets: set[str] = set()
    for side in (report.get("gst"), report.get("tds")):
        if not side:
            continue
        for s in side.get("classification_slices", []):
            allowed.add(_norm_num(s["count"]))
            allowed.add(_norm_num(s["value"]))
            if s["count"] > 0:
                present_buckets.add(s["key"])
        if side.get("gst") and side["gst"].get("credit"):
            c = side["gst"]["credit"]
            for k in ("eligible_credit", "total_itc_claimed", "matched_itc", "at_risk_itc", "blocked_credit_itc"):
                if c.get(k) is not None:
                    allowed.add(_norm_num(c[k]))
        if side.get("tds"):
            allowed.add(_norm_num(side["tds"]["total_deducted"]))
            allowed.add(_norm_num(side["tds"]["total_deposited"]))
    for s in report.get("suppliers") or []:
        allowed.add(_norm_num(s["value"]))
        allowed.add(_norm_num(s["count"]))

    # The period itself is legitimate context (e.g. "2025-06" → 2025, 06).
    for token in re.findall(r"\d+", str(report.get("period") or "")):
        allowed.add(_norm_num(token))

    for m in _NUM_RE.finditer(narrative or ""):
        raw = m.group(1)
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if _norm_num(val) not in allowed:
            violations.append(f"Unverified figure in narrative: {raw}")

    lowered = (narrative or "").lower()
    for phrase, bucket in _REPORT_ALLOWED_WORDS.items():
        if phrase in lowered and bucket not in present_buckets:
            violations.append(
                f"Narrative claims \u201c{bucket}\u201d but this run produced no such classification."
            )

    return {
        "passed": not violations,
        "violations": violations,
        "checks": [
            "Every figure matches a chart/KPI value",
            "Every classification named exists in this run",
        ],
    }


def _norm_num(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


_SYSTEM_PROMPT = (
    "You write one short, plain-language paragraph for an Indian chartered "
    "accountant, reading a completed GST/TDS reconciliation. Use ONLY the "
    "figures and classifications you are given — never invent an invoice "
    "number, amount, party, date or classification. If the data does not "
    "support a conclusion, say so plainly. Be concise. "
    'Respond with a single JSON object: {"narrative": "<the paragraph>"}'
)


def generate_narrative(report: dict[str, Any], *, allow_ai: bool = False) -> dict[str, Any]:
    """Return {text, source, guard}. Deterministic by default; when `allow_ai`
    is set and a model is configured, an AI draft is produced and MUST pass
    the guard, else the deterministic text is used."""
    deterministic = _deterministic_narrative(report)
    guard = verify_narrative(deterministic, report)
    result = {"text": deterministic, "source": "deterministic", "guard": guard, "ai_error": ""}

    if not allow_ai:
        return result

    try:
        from src.ingestion_ai import llm

        # A compact, entirely report-derived brief for the model.
        brief = _narrative_brief(report)
        parsed, _latency, model_id, _raw = llm.call_llm_json(
            _SYSTEM_PROMPT, brief, model_override=None
        )
        draft = str(parsed.get("narrative") or "").strip()
        if not draft:
            raise llm.LLMError("parse_error|empty narrative")
        a_guard = verify_narrative(draft, report)
        if a_guard["passed"]:
            return {
                "text": draft,
                "source": f"ai ({model_id})",
                "guard": a_guard,
                "ai_error": "",
            }
        # Guard rejected the draft — fall back, never surface an unverified claim.
        result["ai_error"] = "AI draft rejected by the verifier guard: " + "; ".join(a_guard["violations"][:3])
        result["guard"] = a_guard
        return result
    except Exception as exc:  # noqa: BLE001
        result["ai_error"] = str(exc)
        return result


def _narrative_brief(report: dict[str, Any]) -> str:
    kpi = report["kpi"]
    parts = [
        f"Client: {report['client_name']}",
        f"Period: {report['period']}",
        f"Records: {kpi['total_count']}, matched: {kpi['matched_count']} ({kpi['match_rate']}%)",
        f"Exceptions: {kpi['exception_count']} worth Rs {kpi['exceptions_value']}",
    ]
    for label, side in (("GST", report.get("gst")), ("TDS", report.get("tds"))):
        if not side:
            continue
        parts.append(
            f"{label} buckets (name: count / value Rs): "
            + "; ".join(f"{s['name']}: {s['count']} / {s['value']}" for s in side["classification_slices"])
        )
        if side.get("gst") and side["gst"].get("credit"):
            c = side["gst"]["credit"]
            parts.append(f"GST ITC claimed Rs {c.get('total_itc_claimed')}, eligible Rs {c.get('eligible_credit')}")
        if side.get("tds"):
            parts.append(
                f"TDS deducted Rs {side['tds']['total_deducted']}, deposited Rs {side['tds']['total_deposited']}"
            )
    if report.get("suppliers"):
        parts.append(
            "Top unmatched parties (value Rs): "
            + "; ".join(f"{s['party']}: {s['value']}" for s in report["suppliers"][:5])
        )
    parts.append(
        "Write one paragraph in the same tone as a senior accountant explaining this period's "
        "position, naming only the figures above."
    )
    return "\n".join(parts)