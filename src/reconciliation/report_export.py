"""Reconciliation report export — HTML, PDF and Excel from ONE result (§3).

Every format is rendered from the same ``build_export_data`` structure, which
itself comes from ``src.reconciliation.run_model.load_run_model`` — the same
model the Review screen renders. No format recomputes a figure, so the live
screen and all three exports agree exactly.

  * HTML  — a single self-contained file: inline CSS, inline SVG charts, no
            JS, no network fetches, system-font fallback.
  * Excel — four sheets with LIVE formulas (SUM / COUNTIF / SUMIF) on
            Overview referencing the detail sheets, landscape + fit-to-width,
            frozen header, status fills, ₹#,##0.00.
  * PDF   — printed by headless Chromium (Playwright), deliberately NOT
            wkhtmltopdf/QtWebKit, which collapses CSS Grid and inverts the
            theme.

Presentation only: no matching, classification or storage changes.
"""

from __future__ import annotations

import html as _html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from src.data_paths import DATA_ROOT, source_data_path
from src.reconciliation import review
from src.reconciliation import run_model

FORMATS = ("html", "pdf", "xlsx")

SHEET_OVERVIEW = "Overview"
SHEET_INVOICES = "GST Invoices"
SHEET_CREDIT_NOTES = "Credit Notes"
SHEET_QUALITY = "Data Quality"

# Column letters on the invoice sheet — the Overview formulas reference these,
# so the layout and the formulas are defined in one place.
_INV_LAST = "P"
_INV_CLASS = "L"      # Classification
_INV_TAX = "J"        # Total tax
_INV_VALUE = "K"      # Invoice value (gross)

# Foundation semantic tokens, resolved to hex for static output. A chart in any
# format uses these roles and never introduces a new colour.
_ROLE_HEX = {
    "rule": "#1F7A4D",
    "ai": "#B26A00",
    "danger": "#C0392B",
    "accent": "#2F6FED",
    "neutral": "#9CA3AF",
}
_TEXT_PRIMARY = "#16181D"
_TEXT_SECONDARY = "#6B7280"
_TEXT_MUTED = "#9CA3AF"
_BORDER = "#E7E9EC"
_SURFACE = "#FFFFFF"
_PAGE_BG = "#EEF0F2"

# Lead-in rows on the source portal sheets before data begins (GSTN's two-row
# header, then records).
_CREDIT_NOTE_SHEET = "B2B-CDNR"


class ExportError(Exception):
    """Raised for expected export failures."""


# ---------------------------------------------------------------------------
# Structured data — the single source every format renders from
# ---------------------------------------------------------------------------


def _portal_file_for_run(run: dict[str, Any]) -> Optional[Path]:
    """The portal (GSTR-2B / IMS) workbook this run consumed, if it is still
    on disk. Credit notes are read from it, not re-derived."""
    folder = run.get("client") or ""
    period = run.get("period") or ""
    names = list(run.get("source_file_names") or [])
    for source_type in ("gstr2b", "ims"):
        directory = source_data_path(folder, period, source_type)
        if not directory.exists():
            continue
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def _num(v: Any) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _credit_notes_for_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The portal credit notes (B2B-CDNR) this run declared as unreconciled.

    These are a separate document class from invoices — they are NOT in the
    match results and must never be folded into the invoice counts (§6).
    """
    path = _portal_file_for_run(run)
    if path is None:
        return []
    try:
        from src.ingestion_ai.portal_coverage import _data_rows, _find_header_row, _read_sheet

        raw = _read_sheet(path, _CREDIT_NOTE_SHEET)
        if raw is None or raw.empty:
            return []
        header_row = _find_header_row(raw, "gstin of supplier")
        if header_row is None:
            return []
        rows = _data_rows(raw, header_row)
    except Exception:  # noqa: BLE001
        return []

    notes: list[dict[str, Any]] = []
    for _, r in rows.iterrows():
        igst, cgst, sgst, cess = (_num(r.iloc[i]) for i in (10, 11, 12, 13))
        taxable = _num(r.iloc[9])
        note_value = _num(r.iloc[6])
        notes.append({
            "reference": review.clean(r.iloc[2]),
            "party": review.clean(r.iloc[1]),
            "gstin": review.clean(r.iloc[0]),
            "note_type": review.clean(r.iloc[3]),
            "date": review.format_date(review.clean(r.iloc[5])),
            "place_of_supply": review.clean(r.iloc[7]),
            "reverse_charge": review.clean(r.iloc[8]),
            "taxable_value": round(taxable, 2),
            "igst": round(igst, 2),
            "cgst": round(cgst, 2),
            "sgst": round(sgst, 2),
            "cess": round(cess, 2),
            "total_tax": round(igst + cgst + sgst + cess, 2),
            "note_value": round(note_value, 2),
            "itc_availability": review.clean(r.iloc[22]) if len(r) > 22 else "",
        })
    return notes


def build_export_data(run_id: int, *, db_path=None) -> dict[str, Any]:
    """Everything the report needs, from the ONE shared review model."""
    run, model = run_model.load_run_model(run_id, db_path=db_path)
    kpi = model["kpi"]

    invoices: list[dict[str, Any]] = []
    for item in model["items"]:
        portal = item.get("portal") or {}
        books = item.get("books") or {}
        source = portal or books
        invoices.append({
            "party": item["party"] or "Unknown supplier",
            "gstin": item["gstin"],
            "reference": item["reference"] or review._UNAVAILABLE,
            "date": item["date_label"],
            "taxable_value": round(review.to_number(source.get("taxable_value")) or 0.0, 2),
            "igst": round(review.to_number(source.get("igst")) or 0.0, 2),
            "cgst": round(review.to_number(source.get("cgst")) or 0.0, 2),
            "sgst": round(review.to_number(source.get("sgst")) or 0.0, 2),
            "cess": round(review.to_number(source.get("cess")) or 0.0, 2),
            "total_tax": round(item["tax"], 2),
            # The invoice's own value (the side that carries it), NOT the
            # flagged difference. The column is "Invoice value", and the
            # Overview's gross-on-exceptions SUMIF reads this column, so it
            # must hold a gross value — a residual/delta here would repeat the
            # mismatch this fix removes.
            "invoice_value": round(item["books_value"] or item["portal_value"], 2),
            "classification": item["classification"],
            "status": "Reviewed" if item["reviewed"] else "Not reviewed",
            "difference_type": item["difference_type"],
            "confidence": item["confidence_label"],
            "match_reason": item["match_reason"],
        })

    credit_notes = _credit_notes_for_run(run)

    quality = review.data_quality_notes(
        model["items"],
        recon_type=model["recon_type"],
        source_files=run.get("source_file_names") or [],
        caveats=run.get("caveats") or [],
        run_notes=run.get("run_notes") or [],
    )

    itc = model.get("itc") or {}
    credit_note_tax = round(sum(n["total_tax"] for n in credit_notes), 2)

    return {
        "run_id": run_id,
        "client": run.get("client") or "",
        "client_name": run.get("client") or "",
        "period": run.get("period") or "",
        "period_label": review.format_period(run.get("period") or ""),
        "recon_type": model["recon_type"],
        # §2 — the header GSTIN is the SELECTED CLIENT's own registration, read
        # from the client record. It is never derived from a transaction row,
        # where the first/most-frequent GSTIN is a supplier's. If the client
        # record carries no GSTIN the field is left blank — never borrowed from
        # a supplier row.
        "gstin": model.get("client_gstin") or "",
        "generated_at": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"),
        "source_files": list(run.get("source_file_names") or []),
        "kpi": {
            # §1 — the headline is TAX at stake, with its share of period ITC.
            "itc_at_stake_tax": kpi["itc_at_stake_tax"],
            "itc_at_stake_display": kpi["itc_at_stake_display"],
            "itc_at_stake_pct": kpi["itc_at_stake_pct"],
            "itc_at_stake_pct_display": kpi["itc_at_stake_pct_display"],
            "period_itc_total": kpi["period_itc_total"],
            "period_itc_total_display": kpi["period_itc_total_display"],
            "invoice_count": kpi["total_count"],
            "matched_count": kpi["matched_count"],
            "exception_count": kpi["exception_count"],
            "match_rate": (round(kpi["matched_count"] / kpi["total_count"] * 100, 1)
                           if kpi["total_count"] else 0.0),
            "gross_value": kpi["gross_value"],
            "gross_value_display": kpi["gross_value_display"],
            "reviewed_count": kpi["reviewed_count"],
            "credit_note_count": len(credit_notes),
            "credit_note_tax": credit_note_tax,
            "credit_note_tax_display": review.format_money(credit_note_tax),
            "credit_note_value": round(sum(n["note_value"] for n in credit_notes), 2),
        },
        "classification_slices": [
            s for s in model["classification_slices"] if int(s.get("count") or 0) > 0
        ],
        "itc": itc,
        "suppliers": model["suppliers"][:8],
        "invoices": invoices,
        "credit_notes": credit_notes,
        "quality_notes": [
            {"key": n.key, "title": n.title, "what": n.what, "why": n.why, "how": n.how}
            for n in quality
        ],
    }


# ---------------------------------------------------------------------------
# Static SVG charts (no JS — identical offline and in print)
# ---------------------------------------------------------------------------


def _donut_svg(
    slices: list[dict[str, Any]], *, center_label: str = "",
    center_value: str = "", size: int = 200,
    value_key: str = "value", value_fmt: Optional[Callable[[float], str]] = None,
) -> str:
    """A donut drawn as stroked circles. One surviving slice renders as a
    single full ring with a centre label (§2) — never a degenerate arc.

    ``value_key`` names the ONE basis that drives BOTH the arc proportions and
    the legend figure, so the two can never disagree. The classification donut
    passes ``value_key="count"`` (its centre label reads "Records N" and a
    count basis is what makes 35/39 matched visible); the ITC donut keeps the
    monetary ``value``. A basis is never mixed across slices.
    """
    fmt = value_fmt or review.format_money
    live = [s for s in slices if float(s.get(value_key) or 0) > 0]
    if not live:
        return f'<p class="muted">No data to chart for this run.</p>'

    total = sum(float(s[value_key]) for s in live)
    r, stroke = 70.0, 26.0
    circ = 2 * 3.141592653589793 * r
    parts: list[str] = []
    if len(live) == 1:
        parts.append(
            f'<circle cx="100" cy="100" r="{r}" fill="none" '
            f'stroke="{_ROLE_HEX.get(live[0].get("color_role"), _ROLE_HEX["neutral"])}" '
            f'stroke-width="{stroke}"/>'
        )
    else:
        offset = 0.0
        for s in live:
            frac = float(s[value_key]) / total
            dash = frac * circ
            parts.append(
                f'<circle cx="100" cy="100" r="{r}" fill="none" '
                f'stroke="{_ROLE_HEX.get(s.get("color_role"), _ROLE_HEX["neutral"])}" '
                f'stroke-width="{stroke}" stroke-dasharray="{dash:.3f} {circ - dash:.3f}" '
                f'stroke-dashoffset="{-offset:.3f}" transform="rotate(-90 100 100)"/>'
            )
            offset += dash
    label = _html.escape(center_label or (live[0]["name"] if len(live) == 1 else "Total"))
    value = _html.escape(center_value or fmt(total))
    parts.append(
        f'<text x="100" y="97" text-anchor="middle" font-size="12" '
        f'fill="{_TEXT_SECONDARY}">{label}</text>'
        f'<text x="100" y="115" text-anchor="middle" font-size="15" font-weight="700" '
        f'fill="{_TEXT_PRIMARY}">{value}</text>'
    )
    legend = "".join(
        f'<li><span class="dot" style="background:'
        f'{_ROLE_HEX.get(s.get("color_role"), _ROLE_HEX["neutral"])}"></span>'
        f'{_html.escape(s["name"])}'
        f'<strong>{_html.escape(fmt(float(s[value_key])))}</strong></li>'
        for s in live
    )
    return (
        f'<div class="donut"><svg viewBox="0 0 200 200" width="{size}" height="{size}" '
        f'role="img" aria-label="{label}">{ "".join(parts) }</svg>'
        f'<ul class="legend">{legend}</ul></div>'
    )


def _bar_svg(rows: list[dict[str, Any]], *, size: int = 320) -> str:
    """Top suppliers by unmatched value — horizontal bars, pure SVG."""
    live = [r for r in rows if float(r.get("value") or 0) > 0]
    if not live:
        return '<p class="muted">No unmatched value with a named supplier in this run.</p>'
    top = live[:6]
    peak = max(float(r["value"]) for r in top) or 1.0
    bar_h, gap, label_w = 22, 10, 150
    height = len(top) * (bar_h + gap) + 10
    parts: list[str] = []
    for idx, r in enumerate(top):
        y = idx * (bar_h + gap) + 5
        width = max(float(r["value"]) / peak * (size - label_w - 70), 2)
        name = _html.escape(str(r.get("party") or "")[:22])
        parts.append(
            f'<text x="0" y="{y + 15}" font-size="11" fill="{_TEXT_SECONDARY}">{name}</text>'
            f'<rect x="{label_w}" y="{y}" width="{width:.1f}" height="{bar_h}" rx="3" '
            f'fill="{_ROLE_HEX["danger"]}"/>'
            f'<text x="{label_w + width + 6:.1f}" y="{y + 15}" font-size="11" '
            f'fill="{_TEXT_PRIMARY}">{review.format_money(float(r["value"]))}</text>'
        )
    return (
        f'<svg viewBox="0 0 {size} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="Top suppliers by unmatched invoice value">{"".join(parts)}</svg>'
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_HTML_CSS = f"""
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 28px;
  background: {_PAGE_BG}; color: {_TEXT_PRIMARY};
  /* System-font stack: the report must render identically with no network. */
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, "Noto Sans", sans-serif;
  font-size: 13px; line-height: 1.45;
}}
.page {{ max-width: 1080px; margin: 0 auto; }}
h1 {{ font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }}
h2 {{ font-size: 15px; margin: 0 0 10px; }}
.card {{
  background: {_SURFACE}; border: 1px solid {_BORDER}; border-radius: 10px;
  padding: 16px 18px; margin-bottom: 16px;
}}
.sub {{ color: {_TEXT_SECONDARY}; font-size: 12px; margin: 0; }}
.muted {{ color: {_TEXT_MUTED}; font-size: 12px; margin: 6px 0 0; }}
.headline {{ font-size: 34px; font-weight: 700; letter-spacing: -0.03em; line-height: 1.05; }}
.badge {{
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  background: #FDEBEA; color: {_ROLE_HEX["danger"]}; font-weight: 600; font-size: 12px;
  border: 1px solid #F6C9C5;
}}
.kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.kpi {{ border: 1px solid {_BORDER}; border-radius: 9px; padding: 12px 14px; background: {_SURFACE}; }}
.kpi .label {{ color: {_TEXT_SECONDARY}; font-size: 11px; text-transform: uppercase;
               letter-spacing: 0.05em; font-weight: 600; }}
.kpi .value {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
.kpi .note {{ color: {_TEXT_MUTED}; font-size: 11px; margin-top: 2px; }}
.charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.bar-card {{ grid-column: 1 / -1; }}
.donut {{ display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
.legend {{ list-style: none; margin: 0; padding: 0; font-size: 12px; }}
.legend li {{ display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }}
.legend strong {{ margin-left: 6px; }}
.dot {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ text-align: left; text-transform: uppercase; font-size: 10px; letter-spacing: 0.05em;
      color: {_TEXT_SECONDARY}; border-bottom: 1px solid {_BORDER}; padding: 7px 8px; }}
td {{ padding: 7px 8px; border-bottom: 1px solid {_BORDER}; vertical-align: top; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
tr.matched td {{ background: #F3FAF6; }}
tr.amount-difference td {{ background: #FDF4E3; }}
tr.not-in-books td {{ background: #FDEBEA; }}
tr.not-in-portal td {{ background: #EAF0FB; }}
.status {{ font-weight: 600; }}
.dq {{ border-bottom: 1px solid {_BORDER}; padding: 10px 0; }}
.dq strong {{ display: block; margin-bottom: 4px; }}
.dq dl {{ margin: 0; font-size: 12px; }}
.dq dt {{ font-weight: 600; color: {_TEXT_SECONDARY}; display: inline-block; min-width: 108px; }}
.dq dd {{ display: inline; margin: 0 0 0 4px; }}
@media print {{
  body {{ background: #FFFFFF; padding: 0; }}
  .card {{ break-inside: avoid; }}
  thead {{ display: table-header-group; }}
}}
"""


def _kpi_card(label: str, value: str, note: str = "") -> str:
    extra = f'<div class="note">{_html.escape(note)}</div>' if note else ""
    return (f'<div class="kpi"><div class="label">{_html.escape(label)}</div>'
            f'<div class="value">{_html.escape(value)}</div>{extra}</div>')


def _invoice_rows_html(invoices: list[dict[str, Any]]) -> str:
    if not invoices:
        return '<tr><td colspan="10" class="muted">No invoice records in this run.</td></tr>'
    out: list[str] = []
    for inv in invoices:
        cls = (inv["classification"] or "").strip().lower().replace(" ", "-")
        out.append(
            f'<tr class="{_html.escape(cls)}">'
            f'<td>{_html.escape(inv["party"])}</td>'
            f'<td>{_html.escape(inv["gstin"])}</td>'
            f'<td>{_html.escape(inv["reference"])}</td>'
            f'<td>{_html.escape(inv["date"])}</td>'
            f'<td class="num">{review.format_money(inv["taxable_value"])}</td>'
            f'<td class="num">{review.format_money(inv["total_tax"])}</td>'
            f'<td class="num">{review.format_money(inv["invoice_value"])}</td>'
            f'<td>{_html.escape(inv["classification"])}</td>'
            f'<td class="status">{_html.escape(inv["status"])}</td>'
            f'<td>{_html.escape(inv["confidence"])}</td>'
            f'</tr>'
        )
    return "".join(out)


def _credit_note_rows_html(notes: list[dict[str, Any]]) -> str:
    if not notes:
        return '<tr><td colspan="8" class="muted">No credit notes were declared for this run.</td></tr>'
    return "".join(
        f'<tr><td>{_html.escape(n["party"])}</td><td>{_html.escape(n["gstin"])}</td>'
        f'<td>{_html.escape(n["reference"])}</td><td>{_html.escape(n["note_type"])}</td>'
        f'<td>{_html.escape(n["date"])}</td>'
        f'<td class="num">{review.format_money(n["taxable_value"])}</td>'
        f'<td class="num">{review.format_money(n["total_tax"])}</td>'
        f'<td class="num">{review.format_money(n["note_value"])}</td></tr>'
        for n in notes
    )


def _quality_html(notes: list[dict[str, Any]]) -> str:
    if not notes:
        return '<p class="muted">No data-quality issues detected.</p>'
    return "".join(
        f'<div class="dq"><strong>{_html.escape(n["title"])}</strong><dl>'
        f'<dt>What</dt><dd>{_html.escape(n["what"])}</dd><br/>'
        f'<dt>Why it matters</dt><dd>{_html.escape(n["why"])}</dd><br/>'
        f'<dt>How to fix</dt><dd>{_html.escape(n["how"])}</dd>'
        f'</dl></div>'
        for n in notes
    )


def render_html(data: dict[str, Any]) -> str:
    """A single self-contained HTML document — no scripts, no network."""
    k = data["kpi"]
    itc = data.get("itc") or {}
    itc_single = bool(itc.get("single"))
    itc_center_label = itc.get("single_label") if itc_single else "Total ITC"
    itc_center_value = itc.get("single_display") if itc_single else review.format_money(
        float(itc.get("total") or 0.0)
    )
    itc_chart = (
        _donut_svg(itc.get("slices") or [], center_label=itc_center_label or "",
                   center_value=itc_center_value or "")
        if itc.get("slices")
        else f'<p class="muted">{_html.escape(itc.get("empty_note") or "No ITC recorded for this run.")}</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_html.escape(data["recon_type"])} Reconciliation Report — {_html.escape(data["client_name"])} {_html.escape(data["period_label"])}</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<div class="page">

  <div class="card">
    <h1>{_html.escape(data["recon_type"])} Reconciliation Report</h1>
    <p class="sub">
      {_html.escape(data["client_name"])} · {_html.escape(data["period_label"])}
      {("· GSTIN " + _html.escape(data["gstin"])) if data["gstin"] else ""}
      · Run {data["run_id"]}
    </p>
    <p class="sub">Generated {_html.escape(data["generated_at"])} · Source files:
      {_html.escape(", ".join(data["source_files"]) or "—")}</p>
  </div>

  <div class="card">
    <div class="headline">{_html.escape(k["itc_at_stake_display"])}</div>
    <div><span class="label">ITC at stake (tax on exceptions)</span>
      &nbsp;<span class="badge">{_html.escape(k["itc_at_stake_pct_display"])}</span>
      <span class="muted">of {_html.escape(k["period_itc_total_display"])} period ITC</span>
    </div>
    <p class="sub">{k["exception_count"]} item(s) · Run {data["run_id"]} ·
      {_html.escape(data["period_label"])} {_html.escape(data["recon_type"])}</p>
  </div>

  <div class="card">
    <h2>Key figures</h2>
    <div class="kpis">
      {_kpi_card("ITC at stake (tax)", k["itc_at_stake_display"], k["itc_at_stake_pct_display"] + " of period ITC")}
      {_kpi_card("Period ITC", k["period_itc_total_display"], "total tax in this run")}
      {_kpi_card("Invoices", str(k["invoice_count"]), f'{k["matched_count"]} matched')}
      {_kpi_card("Exceptions", str(k["exception_count"]), f'{k["reviewed_count"]} reviewed')}
      {_kpi_card("Credit notes", str(k["credit_note_count"]), "excluded from invoice counts")}
      {_kpi_card("Credit note tax", k["credit_note_tax_display"], "not reconciled this run")}
    </div>
    <p class="muted">Gross invoice value on the exception items was
      {_html.escape(k["gross_value_display"])} — shown here for context only; it is
      not the exposure.</p>
  </div>

  <div class="charts">
    <div class="card">
      <h2>Classification</h2>
      {_donut_svg(data["classification_slices"], center_label="Records",
                  center_value=str(k["invoice_count"]),
                  value_key="count",
                  value_fmt=lambda v: f"{int(round(v))}")}
    </div>
    <div class="card">
      <h2>Input tax credit split</h2>
      {itc_chart}
    </div>
    <div class="card bar-card">
      <h2>Top suppliers by unmatched invoice value</h2>
      {_bar_svg(data["suppliers"])}
    </div>
  </div>

  <div class="card">
    <h2>GST invoices ({len(data["invoices"])})</h2>
    <table>
      <thead><tr>
        <th>Supplier</th><th>GSTIN</th><th>Invoice no.</th><th>Date</th>
        <th class="num">Taxable value</th><th class="num">Total tax</th>
        <th class="num">Invoice value</th><th>Classification</th>
        <th>Status</th><th>Confidence</th>
      </tr></thead>
      <tbody>{_invoice_rows_html(data["invoices"])}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>Credit notes ({len(data["credit_notes"])})</h2>
    <p class="sub">Held separately from the invoice reconciliation and represented
      for completeness only.</p>
    <table>
      <thead><tr>
        <th>Supplier</th><th>GSTIN</th><th>Note no.</th><th>Type</th><th>Date</th>
        <th class="num">Taxable value</th><th class="num">Total tax</th>
        <th class="num">Note value</th>
      </tr></thead>
      <tbody>{_credit_note_rows_html(data["credit_notes"])}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>Data quality &amp; scope notes</h2>
    {_quality_html(data["quality_notes"])}
  </div>

</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

_MONEY_FMT = '"₹"#,##0.00'


def _sheet_setup(ws, *, landscape: bool = True) -> None:
    """Landscape, fit-to-one-page-wide, frozen header row."""
    from openpyxl.worksheet.properties import PageSetupProperties

    ws.freeze_panes = "A2"
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = "1:1"


def _status_fill(classification: str):
    from openpyxl.styles import PatternFill

    return {
        "Matched": PatternFill("solid", fgColor="E4F4EA"),
        "Amount Difference": PatternFill("solid", fgColor="FDF4E3"),
        "Not in Books": PatternFill("solid", fgColor="FDEBEA"),
        "Not in Portal": PatternFill("solid", fgColor="EAF0FB"),
    }.get(classification, PatternFill("solid", fgColor="F5F6F7"))


_INVOICE_HEADERS = [
    "Supplier", "GSTIN", "Invoice no.", "Invoice date",
    "Taxable value", "IGST", "CGST", "SGST", "Cess", "Total tax",
    "Invoice value", "Classification", "Status", "Difference type",
    "Confidence", "Match reason",
]

_CREDIT_NOTE_HEADERS = [
    "Supplier", "GSTIN", "Note no.", "Note type", "Note date",
    "Taxable value", "IGST", "CGST", "SGST", "Cess", "Total tax",
    "Note value", "ITC availability", "Reverse charge",
]


def _recalculate_workbook(path: Path) -> None:
    """Bake cached formula values into a written workbook (§3).

    openpyxl writes formulas with NO cached result. Excel recomputes them on
    open, but any reader that does not evaluate formulas — a preview pane, a
    programmatic cached-value reader — sees an entirely blank KPI section. A
    LibreOffice headless recalculation pass writes real cached values next to
    every formula, so the workbook is complete on its own.

    Best-effort: if LibreOffice is unavailable the live formulas are left
    intact (still correct on open), so an export never fails over this.
    """
    import shutil
    import subprocess
    import tempfile

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return
    with tempfile.TemporaryDirectory(prefix="setu_recalc_") as tmp:
        try:
            subprocess.run(
                [
                    soffice, "--headless", "--calc",
                    # A private profile avoids fighting a running desktop session.
                    f"-env:UserInstallation=file://{tmp}/loprofile",
                    "--convert-to", "xlsx", "--outdir", tmp, str(path),
                ],
                check=True, capture_output=True, timeout=240,
            )
        except Exception:  # noqa: BLE001 - keep the live-formula workbook
            return
        produced = Path(tmp) / path.name
        if produced.exists():
            shutil.move(str(produced), str(path))


def write_xlsx(data: dict[str, Any], output_path: Path) -> Path:
    """Four sheets. Every Overview total is a LIVE formula over the detail
    sheets — nothing is copied from the app's in-memory result."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.utils import get_column_letter

    k = data["kpi"]
    wb = Workbook()
    bold = Font(bold=True)
    title_font = Font(bold=True, size=14)
    thin = Side(style="thin", color="E7E9EC")

    # ---- Overview -------------------------------------------------------
    ws = wb.active
    ws.title = SHEET_OVERVIEW
    ws["A1"] = f"{data['recon_type']} Reconciliation Report"
    ws["A1"].font = title_font
    ws["A2"] = (f"{data['client_name']} · {data['period_label']} · Run {data['run_id']}"
                + (f" · GSTIN {data['gstin']}" if data["gstin"] else ""))
    ws["A3"] = f"Generated {data['generated_at']}"
    ws["A4"] = "Source files: " + (", ".join(data["source_files"]) or "—")

    # Detail-sheet ranges the formulas read. The generous bound lets a reviewer
    # add rows without breaking a total.
    inv_last = max(len(data["invoices"]) + 1, 2)
    inv_range_end = 100000
    cls_col = f"'{SHEET_INVOICES}'!${_INV_CLASS}$2:${_INV_CLASS}${inv_range_end}"
    tax_col = f"'{SHEET_INVOICES}'!${_INV_TAX}$2:${_INV_TAX}${inv_range_end}"
    value_col = f"'{SHEET_INVOICES}'!${_INV_VALUE}$2:${_INV_VALUE}${inv_range_end}"
    inv_count_col = f"'{SHEET_INVOICES}'!$A$2:$A${inv_range_end}"
    cn_count_col = f"'{SHEET_CREDIT_NOTES}'!$A$2:$A${inv_range_end}"
    cn_tax_col = f"'{SHEET_CREDIT_NOTES}'!$K$2:$K${inv_range_end}"

    r = 6
    ws.cell(row=r, column=1, value="Key figures (live formulas)").font = bold
    r += 1
    rows: list[tuple[str, Any, Optional[str], Optional[str]]] = [
        ("ITC at stake (tax on Not in Books)", f"=SUMIF({cls_col},\"Not in Books\",{tax_col})",
         _MONEY_FMT, "§1 headline — tax, not gross invoice value"),
        ("Period ITC (total tax in run)", f"=SUM({tax_col})", _MONEY_FMT, ""),
        ("ITC at stake % of period ITC", None, "0.00%", "=B7/B8"),
        ("Invoices", f"=COUNTA({inv_count_col})", "0", ""),
        ("Matched", f"=COUNTIF({cls_col},\"Matched\")", "0", ""),
        ("Exceptions", f"=COUNTA({inv_count_col})-COUNTIF({cls_col},\"Matched\")", "0", ""),
        ("Not in books", f"=COUNTIF({cls_col},\"Not in Books\")", "0", ""),
        ("Not in portal", f"=COUNTIF({cls_col},\"Not in Portal\")", "0", ""),
        ("Amount difference", f"=COUNTIF({cls_col},\"Amount Difference\")", "0", ""),
        ("Credit notes", f"=COUNTA({cn_count_col})", "0", "tracked separately from invoices"),
        ("Credit note tax", f"=SUM({cn_tax_col})", _MONEY_FMT, "not reconciled this run"),
        # §4 — column K (Invoice value), NOT column J (Total tax). The label is
        # "gross invoice value", so it must sum the invoice-value column; the
        # cell previously summed tax and shipped a note admitting it.
        ("Gross invoice value on exceptions (context only)", f"=SUMIF({cls_col},\"<>Matched\",{value_col})",
         _MONEY_FMT, ""),
    ]
    itc_row = None
    period_row = None
    for label, value, fmt, note in rows:
        ws.cell(row=r, column=1, value=label)
        cell = ws.cell(row=r, column=2)
        if value is None and note and note.startswith("="):
            cell.value = note  # the % formula, which references the rows above
        else:
            cell.value = value
        if fmt:
            cell.number_format = fmt
        if label.startswith("ITC at stake (tax"):
            itc_row = r
        if label.startswith("Period ITC"):
            period_row = r
        if note and not note.startswith("="):
            ws.cell(row=r, column=3, value=note).font = Font(size=9, color="6B7280")
        r += 1
    if itc_row and period_row:
        ws.cell(row=itc_row + 2, column=2).value = f"=IFERROR(B{itc_row}/B{period_row},0)"

    r += 1
    ws.cell(row=r, column=1, value="Credit-note detail (from the portal file)").font = bold
    r += 1
    for label, value, fmt in [
        ("Credit note tax total", f"=SUM({cn_tax_col})", _MONEY_FMT),
        ("Credit note count", f"=COUNTA({cn_count_col})", "0"),
    ]:
        ws.cell(row=r, column=1, value=label)
        ws.cell(row=r, column=2, value=value).number_format = fmt
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Data quality & scope notes").font = bold
    r += 1
    for n in data["quality_notes"]:
        ws.cell(row=r, column=1, value=n["title"]).font = bold
        r += 1
        for lbl in ("what", "why", "how"):
            cell = ws.cell(row=r, column=1, value=n[lbl])
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            r += 1

    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 44
    ws.print_title_rows = "1:1"

    # ---- GST Invoices ---------------------------------------------------
    wsi = wb.create_sheet(SHEET_INVOICES)
    for col, head in enumerate(_INVOICE_HEADERS, start=1):
        c = wsi.cell(row=1, column=col, value=head)
        c.font = bold
        c.border = Border(bottom=thin)
    for idx, inv in enumerate(data["invoices"], start=2):
        values = [
            inv["party"], inv["gstin"], inv["reference"], inv["date"],
            inv["taxable_value"], inv["igst"], inv["cgst"], inv["sgst"], inv["cess"],
            inv["total_tax"], inv["invoice_value"], inv["classification"],
            inv["status"], inv["difference_type"], inv["confidence"], inv["match_reason"],
        ]
        for col, value in enumerate(values, start=1):
            cell = wsi.cell(row=idx, column=col, value=value)
            if 5 <= col <= 11:
                cell.number_format = _MONEY_FMT
            if col == 16:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        fill = _status_fill(inv["classification"])
        for col in range(1, len(_INVOICE_HEADERS) + 1):
            wsi.cell(row=idx, column=col).fill = fill
    _sheet_setup(wsi)
    for col in range(1, len(_INVOICE_HEADERS) + 1):
        wsi.column_dimensions[get_column_letter(col)].width = 30 if col == 16 else 18
    wsi.column_dimensions["A"].width = 34

    # ---- Credit Notes ---------------------------------------------------
    wsc = wb.create_sheet(SHEET_CREDIT_NOTES)
    for col, head in enumerate(_CREDIT_NOTE_HEADERS, start=1):
        c = wsc.cell(row=1, column=col, value=head)
        c.font = bold
        c.border = Border(bottom=thin)
    for idx, n in enumerate(data["credit_notes"], start=2):
        values = [
            n["party"], n["gstin"], n["reference"], n["note_type"], n["date"],
            n["taxable_value"], n["igst"], n["cgst"], n["sgst"], n["cess"],
            n["total_tax"], n["note_value"], n["itc_availability"], n["reverse_charge"],
        ]
        for col, value in enumerate(values, start=1):
            cell = wsc.cell(row=idx, column=col, value=value)
            if 6 <= col <= 12:
                cell.number_format = _MONEY_FMT
    _sheet_setup(wsc)
    for col in range(1, len(_CREDIT_NOTE_HEADERS) + 1):
        wsc.column_dimensions[get_column_letter(col)].width = 34 if col == 1 else 18

    # ---- Data Quality ---------------------------------------------------
    wsq = wb.create_sheet(SHEET_QUALITY)
    for col, head in enumerate(["Note", "What", "Why it matters", "How to fix"], start=1):
        c = wsq.cell(row=1, column=col, value=head)
        c.font = bold
        c.border = Border(bottom=thin)
    for idx, n in enumerate(data["quality_notes"], start=2):
        for col, key in enumerate(("title", "what", "why", "how"), start=1):
            cell = wsq.cell(row=idx, column=col, value=n[key])
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    _sheet_setup(wsq, landscape=False)
    for col, width in zip("ABCD", (40, 52, 52, 52)):
        wsq.column_dimensions[col].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    # §3 — ship cached values, not formulas that depend on the opener.
    _recalculate_workbook(output_path)
    return output_path


# ---------------------------------------------------------------------------
# PDF — headless Chromium, never a Qt/WKHTML converter
# ---------------------------------------------------------------------------

PDF_RENDERER = "playwright-chromium"


def write_pdf(html_text: str, output_path: Path) -> Path:
    """Print the report HTML with headless Chromium.

    Chromium is used deliberately: wkhtmltopdf/QtWebKit silently collapses the
    CSS Grid layout to a single column and inverts the colour theme.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ExportError(
            "PDF export needs Playwright. Install it with "
            "`pip install playwright && playwright install chromium`."
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html_text, wait_until="load")
                page.emulate_media(media="print", color_scheme="light")
                page.pdf(
                    path=str(output_path),
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=False,
                )
            finally:
                browser.close()
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExportError(f"The PDF renderer failed: {exc}") from exc
    return output_path


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _exports_dir(data: dict[str, Any]) -> Path:
    d = DATA_ROOT / (data["client"] or "unknown") / (data["period"] or "unknown") / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_report(run_id: int, fmt: str, *, db_path=None, output_path=None) -> Path:
    """Write one format for a run and return the path written."""
    fmt = (fmt or "").lower().lstrip(".")
    if fmt not in FORMATS:
        raise ExportError(f"Unknown export format {fmt!r} — expected one of {', '.join(FORMATS)}.")

    data = build_export_data(run_id, db_path=db_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{data['recon_type']}_run{run_id}_{data['period']}_{stamp}"
    out = Path(output_path) if output_path else _exports_dir(data) / f"Reconciliation_Report_{base}.{fmt}"
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "html":
        out.write_text(render_html(data), encoding="utf-8")
        return out
    if fmt == "xlsx":
        return write_xlsx(data, out)
    return write_pdf(render_html(data), out)


def export_payload(run_id: int, fmt: str, *, db_path=None) -> dict[str, Any]:
    """The bytes (base64) plus the suggested filename, for a browser download."""
    import base64

    path = export_report(run_id, fmt, db_path=db_path)
    mime = {
        "html": "text/html",
        "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[fmt]
    return {
        "filename": path.name,
        "mime": mime,
        "b64": base64.b64encode(path.read_bytes()).decode(),
        "path": str(path),
    }
