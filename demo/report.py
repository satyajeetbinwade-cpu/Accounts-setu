"""HTML report generation for the demo module.

Produces ONE self-contained HTML file — inline CSS, no external assets, no
JavaScript framework — so it opens standalone if downloaded and emailed to a
prospect. Nothing here depends on the Streamlit session.

The visual language reuses the project's existing design tokens
(``src/ui/design_tokens.py``) so the report reads as the same product as the
rest of Setu, while being allowed to look more "product" and less "document"
than the internal screens.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from src import queries
from src.ui.design_tokens import COLOR, FONT_FAMILY, LAYOUT

from demo import ai_insights

# Classification -> the semantic colour role it maps onto. Reuses the
# project's existing roles rather than inventing report-specific colours.
_CLASSIFICATION_ROLE = {
    "Matched": "resolved",
    "Amount Difference": "ai_suggested",
    "Not in Books": "danger",
    "Not in Portal": "danger",
}

# Difference types that are genuinely benign, shown in the resolved role
# rather than as a problem.
_BENIGN_DIFFERENCE_TYPES = {"Rounding", "Timing Difference"}


def _esc(value: Any) -> str:
    """HTML-escape any value, collapsing pandas NaN to an empty string."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return html.escape(str(value))


def _money(value: Any) -> str:
    """Format a rupee amount with Indian digit grouping."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    negative = amount < 0
    amount = abs(amount)
    whole = int(amount)
    paise = round((amount - whole) * 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    sign = "-" if negative else ""
    return f"{sign}₹{digits}.{paise:02d}"


def _badge(text: str, role: str) -> str:
    """A colour-coded classification badge."""
    return f'<span class="badge badge-{role}">{_esc(text)}</span>'


def _role_for(classification: str, difference_type: Any = None) -> str:
    if difference_type and str(difference_type) in _BENIGN_DIFFERENCE_TYPES:
        return "resolved"
    return _CLASSIFICATION_ROLE.get(str(classification), "neutral")


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------


def _css() -> str:
    """The report's stylesheet, built from the project's design tokens.

    Token values are interpolated rather than hard-coded, so a token shift in
    the polish phase propagates here too.
    """
    return f"""
    :root {{
      --page-bg: {COLOR['page_background']};
      --surface: {COLOR['surface']};
      --text-primary: {COLOR['text_primary']};
      --text-secondary: {COLOR['text_secondary']};
      --text-muted: {COLOR['text_muted']};
      --border: {COLOR['border']};
      --rule: {COLOR['resolved']};
      --rule-text: {COLOR['resolved_on']};
      --ai: {COLOR['ai_suggested']};
      --ai-text: {COLOR['ai_suggested_on']};
      --danger: {COLOR['danger']};
      --accent: {COLOR['accent']};
      --neutral: {COLOR['neutral']};
      --neutral-bg: {COLOR['neutral_bg']};
      --radius: {LAYOUT['card_radius']}px;
      --shadow: {LAYOUT['card_shadow']};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 0 0 64px;
      background: var(--page-bg);
      color: var(--text-primary);
      font-family: '{FONT_FAMILY}', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 13.5px; line-height: 1.55;
      -webkit-font-smoothing: antialiased;
    }}
    .wrap {{ max-width: 1080px; margin: 0 auto; padding: 0 28px; }}

    /* --- Header --- */
    header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 34px 0 28px; }}
    .brand {{ display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }}
    .brand-mark {{
      width: 26px; height: 26px; border-radius: 8px; background: var(--accent);
      display: inline-flex; align-items: center; justify-content: center;
      color: #fff; font-weight: 700; font-size: 13px;
    }}
    .brand-name {{ font-weight: 700; letter-spacing: .2px; }}
    h1 {{ font-size: 30px; font-weight: 700; margin: 0 0 6px; letter-spacing: -.4px; }}
    .subtitle {{ color: var(--text-secondary); font-size: 13px; }}

    /* --- Cards --- */
    .card {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); box-shadow: var(--shadow);
      padding: 22px; margin-top: 22px;
    }}
    .card-title {{ font-size: 15px; font-weight: 700; margin: 0 0 4px; }}
    .card-sub {{ color: var(--text-secondary); font-size: 12px; margin: 0 0 16px; }}
    h2 {{ font-size: 19px; font-weight: 700; margin: 40px 0 0; letter-spacing: -.2px; }}
    h2 + .card-sub {{ margin-top: 4px; }}

    /* --- Stat row --- */
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 16px; }}
    .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px 20px; }}
    .stat-label {{ color: var(--text-secondary); font-size: 11.5px; text-transform: uppercase; letter-spacing: .5px; }}
    .stat-value {{ font-size: 26px; font-weight: 700; margin-top: 6px; letter-spacing: -.5px; }}
    .stat-note {{ color: var(--text-muted); font-size: 11.5px; margin-top: 2px; }}
    /* A stat holding one line per reconciliation, so two rates don't have to
       share a single wrapping line. */
    .stat-row {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-top: 7px; }}
    .stat-row-label {{ font-size: 11px; font-weight: 600; letter-spacing: .5px; color: var(--text-secondary); text-transform: uppercase; }}
    .stat-row-value {{ font-size: 19px; font-weight: 700; letter-spacing: -.3px; white-space: nowrap; }}

    /* --- Badges --- */
    .badge {{
      display: inline-block; padding: 2px 9px; border-radius: 999px;
      font-size: 11px; font-weight: 600; white-space: nowrap;
    }}
    .badge-resolved {{ background: var(--rule); color: #fff; }}
    .badge-ai_suggested {{ background: rgba(232,163,61,.16); color: var(--ai-text); }}
    .badge-danger {{ background: rgba(226,87,76,.13); color: var(--danger); }}
    .badge-neutral {{ background: var(--neutral-bg); color: var(--text-secondary); }}
    .badge-accent {{ background: rgba(59,111,219,.12); color: var(--accent); }}

    /* --- AI insights --- */
    .insight {{ border-left: 3px solid var(--ai); background: rgba(232,163,61,.06); }}
    .insight .card-title {{ color: var(--ai-text); }}
    .headline {{ font-size: 17px; font-weight: 700; margin: 0 0 10px; line-height: 1.45; }}
    .pattern {{ border-top: 1px solid var(--border); padding-top: 16px; margin-top: 16px; }}
    .pattern-head {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
    .pattern-title {{ font-weight: 700; font-size: 14px; }}
    .pattern-count {{ color: var(--text-secondary); font-size: 12px; }}
    .pattern-body {{ margin: 8px 0 0; }}
    .actions {{ margin: 10px 0 0; padding-left: 18px; }}
    .actions li {{ margin-bottom: 4px; }}
    .actions li::marker {{ color: var(--accent); }}

    /* --- Tables --- */
    table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
    th {{
      text-align: left; font-weight: 700; color: var(--text-secondary);
      font-size: 11px; text-transform: uppercase; letter-spacing: .4px;
      padding: 8px 10px; border-bottom: 1px solid var(--border);
    }}
    td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
    tr:last-child td {{ border-bottom: none; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .reason {{ color: var(--text-secondary); font-size: 12px; }}

    /* --- Collapsible detail --- */
    details {{ margin-top: 14px; }}
    summary {{
      cursor: pointer; font-weight: 700; font-size: 13px; color: var(--accent);
      padding: 8px 0; list-style: none;
    }}
    summary::-webkit-details-marker {{ display: none; }}
    summary::before {{ content: '▸ '; display: inline-block; transition: transform .15s; }}
    details[open] summary::before {{ content: '▾ '; }}

    /* --- Misc --- */
    .empty {{ color: var(--text-muted); font-style: italic; }}
    .note {{ color: var(--text-secondary); font-size: 12px; }}
    .caveat {{ border-left: 3px solid var(--ai); padding: 2px 0 2px 12px; margin: 10px 0; }}
    footer {{ margin-top: 44px; padding-top: 20px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: 11.5px; }}
    @media print {{
      body {{ background: #fff; }}
      .card, .stat {{ box-shadow: none; }}
      details {{ display: block; }}
      details > summary {{ display: none; }}
    }}
    """


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _header(session_id: str, generated_at: str) -> str:
    return f"""
    <header>
      <div class="wrap">
        <div class="brand">
          <span class="brand-mark">S</span>
          <span class="brand-name">Setu</span>
        </div>
        <h1>Reconciliation Report</h1>
        <div class="subtitle">
          Generated {_esc(generated_at)} &nbsp;·&nbsp; Demo session {_esc(session_id)}
        </div>
      </div>
    </header>
    """


def _executive_summary(
    classifications: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> str:
    """The card row: files processed, reconciliations run, match rates, value flagged."""
    files_processed = len(classifications)
    recognized = sum(1 for c in classifications if c.get("source_type"))
    reconciliations = len(runs)

    rates: list[tuple[str, float]] = []
    total_flagged = 0.0
    for recon_type, run in runs.items():
        summary = run.get("summary") or {}
        rate = ai_insights.match_rate(summary)
        if rate is not None:
            rates.append((recon_type, rate))
        values = summary.get("value_by_classification") or {}
        for classification, value in values.items():
            if classification != "Matched":
                try:
                    total_flagged += float(value)
                except (TypeError, ValueError):
                    continue

    # One line per reconciliation rather than a single wrapping line — with
    # two recon types the rates otherwise read as one ambiguous string.
    if len(rates) == 1:
        recon_label, recon_rate = rates[0]
        rate_html = (
            f'<div class="stat-value">{recon_rate}%</div>'
            f'<div class="stat-note">{_esc(recon_label)} records matched cleanly</div>'
        )
    elif rates:
        rows = "".join(
            f'<div class="stat-row"><span class="stat-row-label">{_esc(rt)}</span>'
            f'<span class="stat-row-value">{rate}%</span></div>'
            for rt, rate in rates
        )
        rate_html = f"{rows}"
    else:
        rate_html = '<div class="stat-value">—</div>'

    return f"""
    <div class="wrap">
      <div class="stats">
        <div class="stat">
          <div class="stat-label">Files processed</div>
          <div class="stat-value">{files_processed}</div>
          <div class="stat-note">{recognized} identified</div>
        </div>
        <div class="stat">
          <div class="stat-label">Reconciliations run</div>
          <div class="stat-value">{reconciliations}</div>
          <div class="stat-note">{_esc(", ".join(runs.keys())) if runs else "none"}</div>
        </div>
        <div class="stat">
          <div class="stat-label">Match rate</div>
          {rate_html}
        </div>
        <div class="stat">
          <div class="stat-label">Value flagged</div>
          <div class="stat-value">{_money(total_flagged)}</div>
          <div class="stat-note">across all exceptions</div>
        </div>
      </div>
    </div>
    """


def _ai_insights_section(runs: dict[str, dict[str, Any]]) -> str:
    """The AI narrative, placed near the top — the differentiator vs a spreadsheet."""
    blocks: list[str] = []
    for recon_type, run in runs.items():
        insight = run.get("insight")
        if not insight:
            continue
        patterns_html = ""
        for pattern in insight.get("patterns") or []:
            count = pattern.get("count")
            count_text = f"{count} record{'s' if count != 1 else ''}" if count else ""
            diff_type = pattern.get("difference_type")
            type_badge = (
                _badge(diff_type, _role_for("Amount Difference", diff_type))
                if diff_type else ""
            )
            actions = pattern.get("actions") or []
            actions_html = ""
            if actions:
                items = "".join(f"<li>{_esc(a)}</li>" for a in actions)
                actions_html = f'<ul class="actions">{items}</ul>'
            patterns_html += f"""
            <div class="pattern">
              <div class="pattern-head">
                <span class="pattern-title">{_esc(pattern.get('title'))}</span>
                {type_badge}
                <span class="pattern-count">{_esc(count_text)}</span>
              </div>
              <p class="pattern-body">{_esc(pattern.get('narrative'))}</p>
              {actions_html}
            </div>
            """

        dq = insight.get("data_quality_note")
        dq_html = f'<p class="note" style="margin-top:16px">{_esc(dq)}</p>' if dq else ""
        model = insight.get("model")
        model_html = (
            f'<p class="note" style="margin-top:14px">Analysis by {_esc(model)}.</p>'
            if model else ""
        )

        blocks.append(f"""
        <div class="card insight">
          <div class="card-title">AI Insights — {_esc(recon_type)}</div>
          <p class="headline">{_esc(insight.get('headline'))}</p>
          <p>{_esc(insight.get('health_read'))}</p>
          {patterns_html}
          {dq_html}
          {model_html}
        </div>
        """)

    if not blocks:
        return ""
    return f"""
    <div class="wrap">
      <h2>AI Insights</h2>
      <p class="card-sub">What the engine found, in plain language.</p>
      {''.join(blocks)}
    </div>
    """


def _summary_table(summary: dict[str, Any]) -> str:
    """Counts by classification, with the rupee value in each bucket."""
    by_classification = summary.get("by_classification") or {}
    values = summary.get("value_by_classification") or {}
    if not by_classification:
        return '<p class="empty">No results.</p>'

    rows = []
    for classification, count in sorted(
        by_classification.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        role = _role_for(classification)
        rows.append(
            f"<tr><td>{_badge(classification, role)}</td>"
            f'<td class="num">{count}</td>'
            f'<td class="num">{_money(values.get(classification, 0))}</td></tr>'
        )
    return f"""
    <table>
      <thead><tr><th>Classification</th><th class="num">Records</th><th class="num">Value</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def _difference_type_table(summary: dict[str, Any]) -> str:
    by_diff = summary.get("by_difference_type") or {}
    if not by_diff:
        return ""
    rows = []
    for diff_type, count in sorted(by_diff.items(), key=lambda kv: (-kv[1], kv[0])):
        role = _role_for("Amount Difference", diff_type)
        rows.append(
            f"<tr><td>{_badge(diff_type, role)}</td><td class='num'>{count}</td></tr>"
        )
    return f"""
    <details>
      <summary>Breakdown by difference type</summary>
      <table>
        <thead><tr><th>Difference type</th><th class="num">Records</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </details>
    """


def _exceptions_table(run_id: int, recon_type: str) -> str:
    """The exception rows, with the engine's match_reason shown in full."""
    frames = []
    for classification in ("Amount Difference", "Not in Books", "Not in Portal"):
        df = queries.get_results(run_id, classification=classification)
        if not df.empty:
            frames.append(df)
    if not frames:
        return '<p class="empty">No exceptions — every record matched.</p>'

    df = pd.concat(frames, ignore_index=True)
    rows = []
    for _, row in df.iterrows():
        books = row.get("books_record") if isinstance(row.get("books_record"), dict) else {}
        portal = row.get("portal_record") if isinstance(row.get("portal_record"), dict) else {}
        classification = row.get("classification")
        diff_type = row.get("difference_type")
        role = _role_for(classification, diff_type)

        party = (
            books.get("party_name") or books.get("deductee_name")
            or portal.get("party_name") or portal.get("deductee_name")
        )
        reference = (
            books.get("invoice_number") or books.get("challan_number")
            or portal.get("invoice_number") or portal.get("challan_number")
        )
        books_value = _record_value(books)
        portal_value = _record_value(portal)

        rows.append(f"""
        <tr>
          <td>{_badge(classification, role)}{f'<br><span class="reason">{_esc(diff_type)}</span>' if diff_type else ''}</td>
          <td>{_esc(party) or '<span class="empty">—</span>'}<br><span class="reason">{_esc(reference)}</span></td>
          <td class="num">{_money(books_value) if books_value else '—'}</td>
          <td class="num">{_money(portal_value) if portal_value else '—'}</td>
          <td class="reason">{_esc(row.get('match_reason'))}</td>
        </tr>
        """)

    return f"""
    <details open>
      <summary>{len(df)} exception{'s' if len(df) != 1 else ''} — full detail</summary>
      <table>
        <thead>
          <tr>
            <th>Classification</th><th>Party / reference</th>
            <th class="num">Books</th><th class="num">Portal</th>
            <th>Why the engine flagged it</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </details>
    """


def _record_value(record: dict[str, Any]) -> float:
    for key in ("invoice_value", "amount_paid_credited", "tax_deducted", "taxable_value"):
        if record.get(key) is not None:
            try:
                return abs(float(record[key]))
            except (TypeError, ValueError):
                continue
    return 0.0


def _recon_section(recon_type: str, run: dict[str, Any]) -> str:
    run_id = run.get("run_id")
    summary = run.get("summary") or {}
    caveats = run.get("caveats") or []

    caveats_html = ""
    if caveats:
        items = "".join(
            f'<div class="caveat"><b>{_esc(c.get("label"))}</b><br>'
            f'<span class="note">{_esc(c.get("detail"))}</span></div>'
            for c in caveats
        )
        caveats_html = f"""
        <details>
          <summary>Checks this run could not perform ({len(caveats)})</summary>
          {items}
        </details>
        """

    return f"""
    <div class="wrap">
      <h2>{_esc(recon_type)} Reconciliation</h2>
      <p class="card-sub">
        Run #{_esc(run_id)} &nbsp;·&nbsp; {summary.get('total_results', 0)} records compared
      </p>
      <div class="card">
        {_summary_table(summary)}
        {_difference_type_table(summary)}
        {_exceptions_table(run_id, recon_type)}
        {caveats_html}
      </div>
    </div>
    """


def _data_quality_section(
    classifications: list[dict[str, Any]],
    ims_files: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> str:
    """Files that couldn't be classified, IMS shown as reference, load warnings."""
    unrecognized = [c for c in classifications if not c.get("source_type")]
    blocks: list[str] = []

    if unrecognized:
        rows = "".join(
            f"<tr><td>{_esc(c.get('filename'))}</td>"
            f"<td class='reason'>{_esc(c.get('reason'))}</td></tr>"
            for c in unrecognized
        )
        blocks.append(f"""
        <div class="card">
          <div class="card-title">Files that could not be identified</div>
          <p class="card-sub">
            These were left out of the reconciliation rather than guessed at.
          </p>
          <table>
            <thead><tr><th>File</th><th>Why</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """)

    if ims_files:
        rows = "".join(
            f"<tr><td>{_esc(c.get('filename'))}</td>"
            f"<td class='num'>{c.get('row_count', 0)}</td></tr>"
            for c in ims_files
        )
        blocks.append(f"""
        <div class="card">
          <div class="card-title">IMS data — reference only</div>
          <p class="card-sub">
            The Invoice Management System export is shown here as context. It is
            <b>not reconciled</b> — the engine does not match against IMS, so no
            conclusion should be drawn from these rows.
          </p>
          <table>
            <thead><tr><th>File</th><th class="num">Rows</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """)

    warnings: list[tuple[str, str]] = []
    for recon_type, run in runs.items():
        for warning in run.get("warnings") or []:
            warnings.append((recon_type, warning))
    if warnings:
        rows = "".join(
            f"<tr><td>{_esc(rt)}</td><td class='reason'>{_esc(w)}</td></tr>"
            for rt, w in warnings
        )
        blocks.append(f"""
        <div class="card">
          <div class="card-title">Data-quality notes from ingestion</div>
          <table>
            <thead><tr><th>Run</th><th>Note</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """)

    if not blocks:
        return ""
    return f"""
    <div class="wrap">
      <h2>Data Quality &amp; Context</h2>
      <p class="card-sub">What was left out, and why.</p>
      {''.join(blocks)}
    </div>
    """


def _footer() -> str:
    return """
    <div class="wrap">
      <footer>
        <b>This is a demonstration run.</b> It was produced on a subset of
        documents to show how Setu reconciles GST and TDS data. It is not a
        production reconciliation, has not been reviewed by a person, and must
        not be relied on for filing or for any accounting decision.
      </footer>
    </div>
    """


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def build_report(
    session_id: str,
    classifications: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    *,
    generated_at: Optional[str] = None,
) -> str:
    """Build the complete, self-contained HTML report.

    Args:
        session_id: The demo session id, shown in the header.
        classifications: ``classify.classify_files()`` output.
        runs: ``{recon_type: {run_id, summary, insight, caveats, warnings}}``
            — one entry per reconciliation that actually ran.
        generated_at: Override for the timestamp (used by tests).

    Returns:
        A complete HTML document as a string.
    """
    timestamp = generated_at or datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    ims_files = [c for c in classifications if c.get("source_type") == "ims"]

    body = "".join([
        _header(session_id, timestamp),
        _executive_summary(classifications, runs),
        _ai_insights_section(runs),
        "".join(
            _recon_section(recon_type, run)
            for recon_type, run in runs.items()
        ),
        _data_quality_section(classifications, ims_files, runs),
        _footer(),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Setu Reconciliation Report</title>
<style>{_css()}</style>
</head>
<body>
{body}
</body>
</html>"""