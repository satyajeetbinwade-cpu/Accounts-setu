"""Self-contained, INTERACTIVE HTML visualisation of the reconciliation report.

Produces a single-file `reports/gst_2b_reconciliation_report.html` with:

  * Tab navigation (Overview / Exceptions / Corrective JVs / GSTR-3B /
    Quality gates / Review queue) - deep-linkable via #tab-<name>
  * KPI banner - every card is CLICKABLE and jumps to the exceptions list
    pre-filtered by that classification
  * Exceptions workspace - live search, status filter chips, sortable
    columns, and click-any-row to open an EVIDENCE MODAL (full portal +
    book invoice details, match deltas, materiality, IMS)
  * Corrective JVs - expandable cards with lines, balance badge and a
    "copy narration" button
  * Review queue tab - item statuses / priority / batch-approvable flags /
    audit trail straight from the Phase-3 workflow
  * Zero external dependencies - pure HTML + CSS + inline vanilla JS + SVG

Usage:
    from recon_engine.htmlreport import write_html
    path = write_html(pre, post, packet, verification=None, review=queue)
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORTS_DIR = HERE.parent / "reports"


# ---------------------------------------------------------------------------
# Tiny SVG chart helpers (deterministic, no JS)
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    if isinstance(v, Decimal):
        return f"{v:,.2f}"
    if isinstance(v, float):
        return f"{v:,.4f}"
    return str(v)


def _donut(
    segments: list[tuple[str, float, str]],
    size: int = 180,
    thickness: int = 26,
    label: str = "",
    sub: str = "",
) -> str:
    """Inline-SVG donut chart. `segments` = [(label, value, color), ...]."""
    total = sum(v for _, v, _ in segments) or 1.0
    cx = cy = size / 2
    r = (size - thickness) / 2 - 4
    start = -90.0  # 12 o'clock
    parts: list[str] = []
    for _label, val, color in segments:
        frac = val / total
        angle = start + frac * 360.0
        if frac > 0.001:
            a1 = math.radians(start)
            a2 = math.radians(angle)
            large = 1 if (angle - start) > 180.0 else 0
            x1 = cx + r * math.cos(a1)
            y1 = cy + r * math.sin(a1)
            x2 = cx + r * math.cos(a2)
            y2 = cy + r * math.sin(a2)
            parts.append(
                f'<path d="M {x1:.2f} {y1:.2f} A {r} {r} 0 {large} 1 '
                f'{x2:.2f} {y2:.2f}" fill="none" stroke="{color}" '
                f'stroke-width="{thickness}"/>'
            )
        start = angle

    legend = "".join(
        f'<div class="legend-row"><span class="swatch" style="background:{c}"></span>'
        f'<span class="legend-label">{l}</span>'
        f'<span class="legend-val">{_fmt(v)}</span></div>'
        for l, v, c in segments
    )
    return (
        f'<div class="chart-wrap">'
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'role="img" aria-label="{label}">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
        f'stroke="#eef1f6" stroke-width="{thickness}"/>'
        + "".join(parts)
        + f'<text x="{cx}" y="{cy - 2}" text-anchor="middle" class="donut-main">{label}</text>'
        + (f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" '
           f'class="donut-sub">{sub}</text>' if sub else "")
        + "</svg>"
        + f'<div class="legend">{legend}</div></div>'
    )


def _hbar(rows: list[tuple[str, float, str]], width: int = 300, unit: str = "") -> str:
    """Inline-SVG horizontal bar chart. rows = [(label, value, color), ...]."""
    maxv = max((v for _, v, _ in rows), default=1.0) or 1.0
    row_h = 26
    height = max(40, len(rows) * row_h + 8)
    parts: list[str] = [
        f'<svg viewBox="0 0 {width + 110} {height}" width="{width + 110}" '
        f'height="{height}" role="img" aria-label="comparison">'
    ]
    for i, (label, val, color) in enumerate(rows):
        y = 8 + i * row_h
        bw = int((val / maxv) * width)
        bw = max(bw, 2 if val > 0 else 0)
        parts.append(
            f'<text x="0" y="{y + 14}" class="hbar-label">{label}</text>'
            f'<rect x="100" y="{y}" width="{bw}" height="16" rx="3" fill="{color}"/>'
            f'<text x="{110 + bw}" y="{y + 13}" class="hbar-val">{unit}{_fmt(val)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Static CSS (no f-string, so braces are free)
# ---------------------------------------------------------------------------
CSS = """
:root {
  --bg:#f4f6fb; --card:#ffffff; --ink:#1e293b; --mut:#64748b;
  --line:#e2e8f0; --acc:#0ea5e9; --ok:#16a34a; --warn:#f59e0b;
  --fail:#dc2626; --violet:#8b5cf6; --rose:#e11d48; --nav:#0f172a;
}
* { box-sizing:border-box; margin:0; padding:0; }
html { scroll-behavior:smooth; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg); color:var(--ink); padding:24px; line-height:1.45; }
.wrap { max-width:1240px; margin:0 auto; }
header.hero { background:linear-gradient(120deg,#0f172a,#1e3a5f 60%,#155e75);
              color:#fff; border-radius:14px; padding:26px 30px; margin-bottom:14px; }
header.hero h1 { font-size:24px; font-weight:700; letter-spacing:.2px; }
header.hero .sub { color:#cbd5e1; margin-top:4px; font-size:14px; }
header.hero .meta { display:flex; gap:20px; margin-top:14px; flex-wrap:wrap; font-size:13px; }
header.hero .meta span { color:#e2e8f0; }

/* ---- tab bar ---- */
.tabbar { position:sticky; top:0; z-index:40; display:flex; gap:4px; flex-wrap:wrap;
          background:rgba(244,246,251,.92); backdrop-filter:blur(6px);
          border:1px solid var(--line); border-radius:12px; padding:6px; margin-bottom:18px; }
.tab { flex:1 1 auto; min-width:110px; border:none; background:transparent; cursor:pointer;
       padding:10px 14px; border-radius:9px; font-size:13.5px; font-weight:600; color:var(--mut);
       display:flex; align-items:center; justify-content:center; gap:7px; transition:.15s; }
.tab:hover { background:#e8edf5; color:var(--ink); }
.tab.active { background:var(--nav); color:#fff; box-shadow:0 2px 6px rgba(15,23,42,.25); }
.tab .tab-count { background:rgba(255,255,255,.18); border-radius:999px; padding:1px 8px;
                  font-size:11px; font-weight:700; }
.tab.active .tab-count { background:rgba(255,255,255,.22); }
.tab-section { display:none; animation:fadein .18s ease; }
.tab-section.active { display:block; }
@keyframes fadein { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }

/* ---- KPI cards ---- */
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:18px; }
.kpi { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px;
       box-shadow:0 1px 2px rgba(15,23,42,.04); cursor:pointer; transition:.15s; position:relative; }
.kpi:hover { transform:translateY(-2px); box-shadow:0 6px 16px rgba(15,23,42,.09); border-color:#cbd5e1; }
.kpi .k-label { font-size:12px; color:var(--mut); text-transform:uppercase; letter-spacing:.5px; }
.kpi .k-value { font-size:24px; font-weight:700; margin-top:4px; }
.kpi .k-foot { font-size:11.5px; color:var(--mut); margin-top:3px; }
.kpi.ok .k-value { color:var(--ok); } .kpi.warn .k-value { color:var(--warn); }
.kpi.fail .k-value { color:var(--fail); } .kpi.acc .k-value { color:var(--acc); }
.kpi .k-hint { position:absolute; top:8px; right:10px; font-size:10px; color:#94a3b8; opacity:0; transition:.15s; }
.kpi:hover .k-hint { opacity:1; }

.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:16px; margin-bottom:18px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px 20px;
        box-shadow:0 1px 2px rgba(15,23,42,.04); }
.card h2 { font-size:15px; margin-bottom:12px; color:#0f172a;
           border-bottom:1px solid var(--line); padding-bottom:8px; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }
.muted { color:var(--mut); font-size:13px; }

/* ---- exceptions workspace ---- */
.toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
.chips { display:flex; gap:6px; flex-wrap:wrap; }
.chip { border:1px solid var(--line); background:#fff; border-radius:999px; padding:5px 12px;
        cursor:pointer; font-size:12.5px; font-weight:600; color:var(--mut); transition:.15s; }
.chip:hover { border-color:#94a3b8; }
.chip.active { background:var(--nav); color:#fff; border-color:var(--nav); }
.search { flex:1 1 220px; border:1px solid var(--line); border-radius:9px; padding:8px 12px;
          font-size:13px; background:#fff; }
.search:focus { outline:2px solid var(--acc); border-color:transparent; }
.rs-count { font-size:12.5px; color:var(--mut); }
.tbl { width:100%; border-collapse:collapse; font-size:13px; }
.tbl th { text-align:left; color:var(--mut); font-weight:600; font-size:11.5px;
          text-transform:uppercase; letter-spacing:.4px; padding:8px 10px;
          border-bottom:2px solid var(--line); white-space:nowrap; }
.tbl th.sortable { cursor:pointer; user-select:none; }
.tbl th.sortable:hover { color:var(--ink); }
.tbl th .arrow { font-size:10px; margin-left:2px; }
.tbl td { padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
.tbl tbody tr { cursor:pointer; transition:background .12s; }
.tbl tbody tr:hover { background:#f1f5f9; }
.tbl tbody tr.selected { background:#e0f2fe; }
.tbl-compact td { padding:5px 10px; }
.row-click-hint { font-size:9.5px; color:#94a3b8; }

/* ---- badges ---- */
.badge { display:inline-block; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:600;
         text-transform:uppercase; letter-spacing:.3px; white-space:nowrap; }
.badge-matched,.badge-ok { background:#dcfce7; color:#15803d; }
.badge-amount_diff { background:#fef3c7; color:#b45309; }
.badge-not_in_books,.badge-fail { background:#fee2e2; color:#b91c1c; }
.badge-not_in_portal { background:#e0e7ff; color:#4338ca; }
.badge-warn { background:#fef3c7; color:#b45309; }
.badge-pending { background:#e2e8f0; color:#334155; }
.badge-approved,.badge-modified { background:#dcfce7; color:#15803d; }
.badge-rejected { background:#fee2e2; color:#b91c1c; }
.badge-posted { background:#dcfce7; color:#15803d; }
.badge-high { background:#fee2e2; color:#b91c1c; }
.badge-normal { background:#e0e7ff; color:#4338ca; }
.badge-batch { background:#bbf7d0; color:#166534; }
.evidence-link { text-decoration:none; color:var(--acc); font-weight:600; }
.evidence-link:hover { text-decoration:underline; }

/* ---- JV cards ---- */
.jv-card { border:1px solid var(--line); border-radius:12px; padding:0; margin-bottom:12px;
           background:#fafbfd; overflow:hidden; }
.jv-head { display:flex; gap:12px; align-items:center; font-size:13px; padding:12px 16px;
           cursor:pointer; flex-wrap:wrap; }
.jv-head:hover { background:#f1f5f9; }
.jv-head .chev { margin-left:auto; transition:transform .15s; color:var(--mut); }
.jv-card.open .jv-head .chev { transform:rotate(90deg); }
.jv-body { display:none; padding:4px 16px 14px; border-top:1px dashed var(--line); }
.jv-card.open .jv-body { display:block; }
.jv-narr { font-size:12.5px; color:var(--mut); margin-top:8px; font-style:italic; }
.jv-rationale { font-size:12.5px; color:var(--mut); margin-top:6px; }
.btn { border:1px solid var(--line); background:#fff; border-radius:8px; padding:4px 10px;
       font-size:12px; font-weight:600; color:var(--ink); cursor:pointer; }
.btn:hover { border-color:#94a3b8; background:#f8fafc; }
.btn.small { font-size:11px; padding:2px 8px; }
.btn-dl { display:inline-flex; align-items:center; gap:7px; background:#fff; color:#0f172a;
          border:1px solid #e2e8f0; border-radius:8px; padding:8px 14px; font-size:12.5px;
          font-weight:700; cursor:pointer; transition:.15s; text-decoration:none; }
.btn-dl:hover { border-color:#94a3b8; background:#f1f5f9; }
.btn-dl svg { width:14px; height:14px; }

/* ---- review queue ---- */
.review-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; margin-bottom:16px; }
.rv-item { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
.rv-item .rv-lbl { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.4px; }
.rv-item .rv-val { font-size:20px; font-weight:700; margin-top:3px; }
.q-item { border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:9px; background:#fff; }
.q-item .q-head { display:flex; gap:10px; align-items:center; flex-wrap:wrap; font-size:13px; }
.q-item .q-audit-toggle { margin-left:auto; font-size:11.5px; color:var(--acc); cursor:pointer; }
.q-audit { display:none; margin-top:8px; border-top:1px dashed var(--line); padding-top:8px; font-size:12px; }
.q-audit.open { display:block; }
.q-audit .audit-row { color:var(--mut); padding:2px 0; }

/* ---- modal ---- */
.overlay { display:none; position:fixed; inset:0; background:rgba(15,23,42,.55); z-index:100;
           align-items:flex-start; justify-content:center; padding:5vh 16px; overflow-y:auto; }
.overlay.open { display:flex; }
.modal { background:#fff; border-radius:14px; max-width:860px; width:100%; padding:22px 24px;
         box-shadow:0 24px 60px rgba(15,23,42,.35); position:relative; }
.modal h3 { font-size:16px; margin-bottom:4px; }
.modal .close { position:absolute; top:14px; right:16px; border:none; background:#f1f5f9;
                width:30px; height:30px; border-radius:50%; cursor:pointer; font-size:15px; color:var(--mut); }
.modal .close:hover { background:#e2e8f0; color:var(--ink); }
.ev-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:14px; }
.ev-panel { border:1px solid var(--line); border-radius:10px; padding:12px 14px; background:#fafbfd; }
.ev-panel h4 { font-size:12px; text-transform:uppercase; letter-spacing:.4px; color:var(--mut); margin-bottom:8px; }
.ev-row { display:flex; justify-content:space-between; gap:10px; font-size:12.5px; padding:2.5px 0; border-bottom:1px dotted #eef1f6; }
.ev-row:last-child { border-bottom:none; }
.ev-row .k { color:var(--mut); }
.ev-row .v { font-weight:600; text-align:right; word-break:break-word; }
.modal-actions { display:flex; gap:8px; margin-top:16px; flex-wrap:wrap; }
@media (max-width:720px) { .ev-grid { grid-template-columns:1fr; } }

/* ---- misc ---- */
.pill { display:inline-block; padding:3px 12px; border-radius:999px; font-size:11.5px; font-weight:600;
        background:#e2e8f0; color:#334155; }
.pill-on { background:#dcfce7; color:#15803d; }
.src-chip { display:inline-block; margin:6px 8px 0 0; padding:3px 10px; border-radius:999px;
        background:rgba(255,255,255,.12); color:#e2e8f0; font-size:11.5px; }
.pipeline-note { font-size:12.5px; color:var(--mut); margin-top:12px; }
.section-title { font-size:17px; font-weight:700; margin:22px 0 10px; }
.compare { display:flex; gap:16px; flex-wrap:wrap; }
.stat-box { flex:1 1 200px; background:#f8fafc; border:1px solid var(--line); border-radius:10px;
            padding:12px 14px; }
.stat-box .lbl { font-size:11.5px; color:var(--mut); text-transform:uppercase; letter-spacing:.4px; }
.stat-box .val { font-size:17px; font-weight:700; margin-top:2px; }
footer { margin-top:26px; color:var(--mut); font-size:11.5px; text-align:center; }
@media print {
  body { padding:0; }
  .tabbar, .toolbar, .btn, .modal-actions, .q-audit-toggle, .close { display:none !important; }
  .tab-section { display:block !important; }
  .jv-body, .q-audit { display:block !important; }
  .card, .kpi { box-shadow:none; break-inside:avoid; }
}
"""


# ---------------------------------------------------------------------------
# Static vanilla JS (no f-string, so template literals are free)
# ---------------------------------------------------------------------------
JS = """
(function () {
  'use strict';
  var DATA = window.RECON_DATA || { exceptions: [], jvs: [], review: null };
  var state = { status: 'all', q: '', sortKey: 'status', sortDir: 1 };

  // ---------- tabs ----------
  var TABS = ['overview', 'exceptions', 'jvs', 'g3b', 'gates', 'review'];
  function showTab(name) {
    if (TABS.indexOf(name) === -1) name = 'overview';
    TABS.forEach(function (t) {
      var el = document.getElementById('tab-' + t);
      var btn = document.querySelector('.tab[data-tab="' + t + '"]');
      if (!el || !btn) return;
      var on = t === name;
      el.classList.toggle('active', on);
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (location.hash !== '#tab-' + name) { history.replaceState(null, '', '#tab-' + name); }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () { showTab(btn.getAttribute('data-tab')); });
  });
  function tabFromHash() {
    var m = location.hash.match(/^#tab-(\\w+)/);
    if (m) showTab(m[1]);
  }
  window.addEventListener('hashchange', tabFromHash);

  // ---------- helpers ----------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function badge(cls, text) { return '<span class="badge badge-' + esc(cls) + '">' + esc(text) + '</span>'; }
  function matBadge(m) {
    if (m === 'above') return badge('warn', 'above');
    if (m === 'below') return badge('ok', 'below');
    return '<span class="muted">&mdash;</span>';
  }
  function imsBadge(rec) {
    if (rec === 'Accept') return badge('ok', 'Accept');
    if (rec === 'Reject') return badge('fail', 'Reject');
    if (rec === 'Pending') return badge('warn', 'Pending');
    return '<span class="muted">&mdash;</span>';
  }
  function statusBadge(st) { return badge(st, st); }

  // ---------- exceptions table ----------
  var COLS = [
    { k: 'status', label: 'Status', sort: true },
    { k: 'confidence', label: 'Confidence', sort: true },
    { k: 'portal_invoice_no', label: 'Portal invoice (evidence)', sort: true },
    { k: 'book_invoice_no', label: 'Book invoice', sort: true },
    { k: 'portal_value_num', label: 'Portal value', sort: true },
    { k: 'book_value_num', label: 'Book value', sort: true },
    { k: 'edit_distance', label: 'EditDist', sort: true },
    { k: 'materiality', label: 'Materiality', sort: true },
    { k: 'ims', label: 'IMS', sort: true },
    { k: 'gstin', label: 'GSTIN', sort: false },
    { k: 'reasons', label: 'Reasons', sort: false }
  ];

  function filtered() {
    var out = DATA.exceptions.filter(function (e) {
      var okStatus = state.status === 'all' || e.status === state.status;
      if (!okStatus) return false;
      if (!state.q) return true;
      var hay = [e.portal_invoice_no, e.book_invoice_no, e.gstin,
                 (e.reasons || []).join(' '), e.status, e.confidence].join(' ').toLowerCase();
      return hay.indexOf(state.q.toLowerCase()) !== -1;
    });
    var col = COLS.filter(function (c) { return c.k === state.sortKey; })[0];
    var key = state.sortKey;
    out.sort(function (a, b) {
      var av = a[key], bv = b[key];
      if (av == null) av = ''; if (bv == null) bv = '';
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * state.sortDir;
      return String(av).localeCompare(String(bv)) * state.sortDir;
    });
    return out;
  }

  function renderExceptions() {
    var tbody = document.getElementById('exc-tbody');
    var rows = filtered();
    tbody.innerHTML = '';
    rows.forEach(function (e) {
      var tr = document.createElement('tr');
      tr.setAttribute('data-idx', e.idx);
      tr.innerHTML =
        '<td>' + statusBadge(e.status) + '</td>' +
        '<td>' + esc(e.confidence) + '</td>' +
        '<td><a class="evidence-link" href="#tab-exceptions" title="Click row for full evidence">' + esc(e.portal_invoice_no) + '</a></td>' +
        '<td>' + esc(e.book_invoice_no) + '</td>' +
        '<td>' + esc(e.portal_value) + '</td>' +
        '<td>' + esc(e.book_value) + '</td>' +
        '<td>' + esc(e.edit_distance) + '</td>' +
        '<td>' + matBadge(e.materiality) + '</td>' +
        '<td>' + imsBadge(e.ims) + '</td>' +
        '<td><span class="mono">' + esc(e.gstin) + '</span></td>' +
        '<td>' + esc((e.reasons || []).join('; ')) + '</td>';
      tr.addEventListener('click', function (ev) {
        if (ev.target.closest && ev.target.closest('a')) { ev.preventDefault(); openModal(e.idx); return; }
        openModal(e.idx);
      });
      tbody.appendChild(tr);
    });
    document.getElementById('rs-count').textContent =
      rows.length + ' of ' + DATA.exceptions.length + ' exceptions';
  }

  function setSort(key) {
    if (state.sortKey === key) { state.sortDir *= -1; }
    else { state.sortKey = key; state.sortDir = 1; }
    renderExceptions();
    var ths = document.querySelectorAll('#exc-table th.sortable');
    ths.forEach(function (th) {
      var arrow = th.querySelector('.arrow');
      if (arrow) arrow.textContent = th.getAttribute('data-key') === key
        ? (state.sortDir === 1 ? '\\u25b2' : '\\u25bc') : '';
    });
  }

  function initExceptions() {
    var thead = document.getElementById('exc-thead');
    thead.innerHTML = COLS.map(function (c) {
      var arrow = c.sort ? '<span class="arrow"></span>' : '';
      return c.sort
        ? '<th class="sortable" data-key="' + c.k + '" aria-sort="none">' + c.label + arrow + '</th>'
        : '<th>' + c.label + '</th>';
    }).join('');
    thead.querySelectorAll('th.sortable').forEach(function (th) {
      th.addEventListener('click', function () { setSort(th.getAttribute('data-key')); });
    });
    document.getElementById('exc-search').addEventListener('input', function (e) {
      state.q = e.target.value; renderExceptions();
    });
    renderExceptions();
  }

  // status filter chips
  function initChips() {
    var statuses = ['all'].concat(DATA.exceptions.map(function (e) { return e.status; })
      .filter(function (v, i, a) { return a.indexOf(v) === i; }));
    var box = document.getElementById('chips');
    box.innerHTML = statuses.map(function (st) {
      var n = st === 'all' ? DATA.exceptions.length
        : DATA.exceptions.filter(function (e) { return e.status === st; }).length;
      return '<button class="chip' + (st === 'all' ? ' active' : '') + '" data-status="' + st + '">' +
        esc(st) + ' (' + n + ')</button>';
    }).join('');
    box.querySelectorAll('.chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        state.status = chip.getAttribute('data-status');
        box.querySelectorAll('.chip').forEach(function (c) { c.classList.remove('active'); });
        chip.classList.add('active');
        renderExceptions();
      });
    });
  }

  // KPI cards -> jump to exceptions filtered
  function initKpis() {
    document.querySelectorAll('.kpi[data-status]').forEach(function (kpi) {
      kpi.addEventListener('click', function () {
        state.status = kpi.getAttribute('data-status');
        state.q = '';
        var srch = document.getElementById('exc-search'); if (srch) srch.value = '';
        document.querySelectorAll('.chip').forEach(function (c) {
          c.classList.toggle('active', c.getAttribute('data-status') === state.status);
        });
        showTab('exceptions');
        renderExceptions();
      });
    });
  }

  // ---------- evidence modal ----------
  var modalIdx = null;
  function evRow(k, v) {
    return '<div class="ev-row"><span class="k">' + esc(k) + '</span><span class="v">' + esc(v) + '</span></div>';
  }
  function invoicePanel(title, inv) {
    if (!inv) return '<div class="ev-panel"><h4>' + title + '</h4><p class="muted">no match on this side</p></div>';
    var rows = [
      evRow('Invoice no', inv.invoice_no), evRow('GSTIN', inv.gstin),
      evRow('Date', inv.date), evRow('Taxable value', inv.taxable_value),
      evRow('CGST', inv.cgst), evRow('SGST', inv.sgst), evRow('IGST', inv.igst),
      evRow('Total', inv.total),
      evRow('Reverse charge', String(inv.reverse_charge)),
      evRow('Blocked credit', String(inv.blocked_credit)),
      evRow('Source', inv.source || '-'), evRow('Status', inv.status || '-'),
      evRow('Reference', inv.reference || '-')
    ];
    return '<div class="ev-panel"><h4>' + title + '</h4>' + rows.join('') + '</div>';
  }
  function openModal(idx) {
    var e = DATA.exceptions[idx]; if (!e) return;
    modalIdx = idx;
    document.getElementById('modal-title').textContent = e.portal_invoice_no + ' - evidence';
    document.getElementById('modal-status').innerHTML =
      statusBadge(e.status) + ' ' + esc(e.confidence) + ' confidence' +
      ' &nbsp;' + matBadge(e.materiality) + ' ' + imsBadge(e.ims);
    var deltas = [
      evRow('Edit distance', String(e.edit_distance)),
      evRow('Date delta (days)', String(e.date_delta)),
      evRow('Value delta %', e.value_delta_pct == null ? '-' : String(e.value_delta_pct)),
      evRow('Match score', String(e.match_score))
    ].join('');
    var reasons = (e.reasons || []).length
      ? '<div class="ev-panel"><h4>Reasons</h4><p style="font-size:12.5px">' +
        esc(e.reasons.join('; ')) + '</p></div>' : '';
    var evid = e.evidence && Object.keys(e.evidence).length
      ? '<div class="ev-panel"><h4>Engine evidence</h4><pre class="mono" style="font-size:11px;white-space:pre-wrap">' +
        esc(JSON.stringify(e.evidence, null, 2)) + '</pre></div>' : '';
    document.getElementById('modal-deltas').innerHTML = deltas;
    document.getElementById('modal-reasons').innerHTML = reasons + evid;
    document.getElementById('modal-portal').innerHTML = invoicePanel('Portal (GSTR-2B)', e.portal);
    document.getElementById('modal-book').innerHTML = invoicePanel('Books (Tally)', e.book);
    document.getElementById('overlay').classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    document.getElementById('overlay').classList.remove('open');
    document.body.style.overflow = '';
    modalIdx = null;
  }
  function initModal() {
    document.getElementById('modal-close').addEventListener('click', closeModal);
    document.getElementById('overlay').addEventListener('click', function (ev) {
      if (ev.target === this) closeModal();
    });
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') closeModal(); });
    document.getElementById('modal-jump').addEventListener('click', function () {
      if (modalIdx == null) return;
      closeModal();
      var ref = DATA.exceptions[modalIdx].portal_invoice_no;
      var jv = document.getElementById('jv-' + CSSesc(ref));
      if (jv) { jv.scrollIntoView({ behavior: 'smooth' }); }
      showTab('jvs');
    });
  }
  function CSSesc(s) { return String(s).replace(/[^a-zA-Z0-9_-]/g, '_'); }

  // ---------- JVs ----------
  function initJVs() {
    var box = document.getElementById('jv-box');
    if (!DATA.jvs.length) {
      box.innerHTML = '<p class="muted">No corrective JVs proposed.</p>';
      return;
    }
    box.innerHTML = DATA.jvs.map(function (jv) {
      var lines = jv.lines.map(function (ln) {
        return '<tr><td>' + esc(ln.account) + '</td>' +
               '<td>' + (Number(ln.dr) ? 'Dr ' + Number(ln.dr).toLocaleString('en-IN', {minimumFractionDigits: 2}) : '') + '</td>' +
               '<td>' + (Number(ln.cr) ? 'Cr ' + Number(ln.cr).toLocaleString('en-IN', {minimumFractionDigits: 2}) : '') + '</td></tr>';
      }).join('');
      var bal = jv.balanced ? badge('ok', 'balanced') : badge('fail', 'UNBALANCED');
      return '<div class="jv-card" id="jv-' + CSSesc(jv.ref) + '">' +
        '<div class="jv-head"><span class="chev">\\u25b6</span>' +
        '<span class="mono">' + esc(jv.ref) + '</span>' +
        '<span>' + esc(jv.date) + '</span>' + bal +
        '<span class="muted">Dr ' + esc(jv.total_dr) + ' = Cr ' + esc(jv.total_cr) + '</span>' +
        '</div><div class="jv-body">' +
        '<table class="tbl tbl-compact"><tbody>' + lines + '</tbody></table>' +
        '<p class="jv-narr">' + esc(jv.narration) + '</p>' +
        (jv.rationale ? '<p class="jv-rationale">' + esc(jv.rationale) + '</p>' : '') +
        '<div class="modal-actions" style="margin-top:10px">' +
        '<button class="btn small copy-narr" data-narr="' + esc(jv.narration) + '">Copy narration</button>' +
        '</div></div></div>';
    }).join('');
    box.querySelectorAll('.jv-head').forEach(function (head) {
      head.addEventListener('click', function () { head.parentElement.classList.toggle('open'); });
    });
    box.querySelectorAll('.copy-narr').forEach(function (b) {
      b.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var text = b.getAttribute('data-narr');
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            b.textContent = 'copied!';
            setTimeout(function () { b.textContent = 'Copy narration'; }, 1200);
          });
        }
      });
    });
  }

  // ---------- review queue ----------
  function initReview() {
    var rv = DATA.review;
    var box = document.getElementById('review-box');
    if (!rv) {
      box.innerHTML = '<p class="muted">Review queue not attached (run with the agent to populate).</p>';
      return;
    }
    var sum = rv.summary || {};
    var stat = sum.status || {};
    var cards = [
      ['Total items', sum.total || 0],
      ['Pending', stat.pending || 0],
      ['Approved', stat.approved || 0],
      ['Modified', stat.modified || 0],
      ['Rejected', stat.rejected || 0],
      ['Posted', stat.posted || 0],
      ['Batch-approvable', sum.batch_approvable || 0],
      ['Ready to post', sum.ready_to_post || 0]
    ].map(function (c) {
      return '<div class="rv-item"><div class="rv-lbl">' + c[0] + '</div><div class="rv-val">' + c[1] + '</div></div>';
    }).join('');
    var items = (rv.items || []).map(function (it, i) {
      var flags = '';
      if (it.batch_approvable) flags += badge('batch', 'batch');
      if (it.priority === 'high') flags += ' ' + badge('high', 'high');
      flags += ' ' + badge(it.status, it.status);
      var audit = (it.audit || []).map(function (a) {
        return '<div class="audit-row">' + esc(a.at.slice(0, 19)) + ' &middot; <b>' +
               esc(a.action) + '</b> &middot; ' + esc(a.actor) + (a.note ? ' &middot; ' + esc(a.note) : '') + '</div>';
      }).join('');
      return '<div class="q-item"><div class="q-head">' +
        '<span class="mono">' + esc(it.ref) + '</span>' +
        '<span class="muted">' + esc(it.kind) + '</span>' + flags +
        '<span class="q-audit-toggle" data-i="' + i + '">audit ' + (it.audit || []).length + ' &darr;</span>' +
        '</div><div class="q-audit" id="q-audit-' + i + '">' + audit + '</div></div>';
    }).join('');
    box.innerHTML = '<div class="review-grid">' + cards + '</div>' + items;
    box.querySelectorAll('.q-audit-toggle').forEach(function (t) {
      t.addEventListener('click', function () {
        var el = document.getElementById('q-audit-' + t.getAttribute('data-i'));
        el.classList.toggle('open');
        t.innerHTML = el.classList.contains('open')
          ? 'audit &uarr;' : 'audit &darr;';
      });
    });
  }

  // ---------- boot ----------
  document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('exc-tbody')) initExceptions();
    if (document.getElementById('chips')) initChips();
    if (document.getElementById('jv-box')) initJVs();
    if (document.getElementById('review-box')) initReview();
    if (document.getElementById('overlay')) initModal();
    initKpis();
    tabFromHash();
    if (!location.hash) showTab('overview');
  });
})();
"""


# ---------------------------------------------------------------------------
# HTML document
# ---------------------------------------------------------------------------
def write_html(
    pre: dict,
    post: dict,
    packet,
    verification: dict | None = None,
    out_path: str | Path | None = None,
    sources: dict | None = None,
    portfolio: list[dict] | None = None,
    review=None,
) -> Path:
    """Write the interactive HTML report. Returns the output Path.

    sources:   per-source freshness timestamps {"portal": "...", "books": ...}
    portfolio: firm-wide rows for the portfolio summary section
    review:    ReviewQueue object -> rendered as an actions tab (Phase 3)
    """
    out_path = Path(out_path) if out_path else REPORTS_DIR / "gst_2b_reconciliation_report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    s = packet.summary
    v = verification or {}
    v_pass = bool(v.get("pass"))
    v_checks = v.get("checks", [])
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    portal_total_v = pre.get("portal_total_value", Decimal("0"))
    book_total_v = pre.get("book_total_value", Decimal("0"))

    # ---- charts ------------------------------------------------------------
    n_portal = s.get("total_portal_invoices") or (
        s.get("matched", 0) + s.get("amount_diff", 0) + s.get("not_in_books", 0))
    class_donut = _donut(
        [
            ("Matched", float(s.get("matched", 0)), "#16a34a"),
            ("Amount diff", float(s.get("amount_diff", 0)), "#f59e0b"),
            ("Not in books", float(s.get("not_in_books", 0)), "#dc2626"),
        ],
        label=str(n_portal), sub="portal invoices",
    )
    itc_eligible = float(post.get("eligible_itc") or 0)
    itc_ineligible = float(post.get("ineligible_itc") or 0)
    itc_donut = _donut(
        [
            ("Eligible ITC", itc_eligible, "#16a34a"),
            ("Ineligible ITC", itc_ineligible, "#e11d48"),
        ],
        label=f"{itc_eligible + itc_ineligible:,.0f}", sub="total input tax",
    )
    bars = _hbar(
        [
            ("Gross gap", float(portal_total_v - book_total_v), "#8b5cf6"),
            ("Eligible ITC", itc_eligible, "#16a34a"),
            ("Net GST payable", float(post.get("net_payable") or 0), "#0ea5e9"),
        ],
        unit="Rs ",
    )

    # ---- RECON_DATA payload -------------------------------------------------
    exceptions_data = []
    for i, e in enumerate(packet.exceptions):
        pinv, binv = e.portal_invoice, e.book_invoice
        pv = (pinv.taxable_value + pinv.tax_amount) if pinv else Decimal("0")
        bv = (binv.taxable_value + binv.tax_amount) if binv else None
        exceptions_data.append({
            "idx": i,
            "status": e.status.value,
            "confidence": e.confidence.value,
            "portal_invoice_no": pinv.invoice_no if pinv else "-",
            "book_invoice_no": binv.invoice_no if binv else "-",
            "gstin": (pinv.gstin if pinv else (binv.gstin if binv else "-")),
            "portal_value": _fmt(pv),
            "portal_value_num": float(pv),
            "book_value": _fmt(bv) if bv is not None else "-",
            "book_value_num": float(bv or 0),
            "edit_distance": e.edit_distance,
            "date_delta": e.date_delta,
            "value_delta_pct": e.value_delta_pct,
            "match_score": round(e.match_score, 4),
            "materiality": e.materiality,
            "ims": e.ims_recommendation,
            "reasons": e.reasons,
            "portal": pinv.to_dict() if pinv else None,
            "book": binv.to_dict() if binv else None,
            "evidence": e.evidence,
        })

    jvs_data = []
    for jv in packet.drafted_jvs:
        dr = sum(Decimal(l.get("dr", 0)) for l in jv.lines)
        cr = sum(Decimal(l.get("cr", 0)) for l in jv.lines)
        jvs_data.append({
            "ref": jv.exception_ref,
            "date": jv.voucher_date.isoformat(),
            "narration": jv.narration,
            "rationale": jv.rationale,
            "balanced": jv.is_balanced(),
            "total_dr": str(dr),
            "total_cr": str(cr),
            "lines": [
                {"account": l["account"], "dr": str(l.get("dr", 0)),
                 "cr": str(l.get("cr", 0))}
                for l in jv.lines
            ],
        })

    g3b = post.get("gstr3b_draft") or {}
    f5_list = post.get("validation_checks", [])
    ver_list = v_checks
    review_data = review.to_dict() if review is not None else None

    data = {
        "generated_at": now,
        "client_id": packet.client_id,
        "period": packet.period,
        "exceptions": exceptions_data,
        "jvs": jvs_data,
        "g3b": {k: _fmt(val) if isinstance(val, Decimal) else val
                for k, val in g3b.items()},
        "f5": f5_list,
        "verification": ver_list,
        "review": review_data,
        "portfolio": portfolio or [],
        "sources": sources or {},
    }

    # ---- static fragments (kept dumb so the file works without JS too) ------
    def badge(cls, text):
        return f"<span class='badge badge-{cls}'>{text}</span>"

    exc_static_rows = "".join(
        f"<tr><td>{badge(e.status.value, e.status.value)}</td>"
        f"<td>{e.confidence.value}</td>"
        f"<td><span class='mono'>{e.portal_invoice.invoice_no if e.portal_invoice else '-'}</span></td>"
        f"<td>{e.book_invoice.invoice_no if e.book_invoice else '-'}</td>"
        f"<td>{_fmt((e.portal_invoice.taxable_value + e.portal_invoice.tax_amount) if e.portal_invoice else 0)}</td>"
        f"<td>{_fmt((e.book_invoice.taxable_value + e.book_invoice.tax_amount) if e.book_invoice else 0)}</td></tr>"
        for e in packet.exceptions
    )
    f5_rows = "".join(
        f"<tr><td>{c.get('name')}</td>"
        f"<td>{badge('ok' if c.get('pass') else ('warn' if c.get('pass') is None else 'fail'), 'PASS' if c.get('pass') else ('~' if c.get('pass') is None else 'FAIL'))}</td></tr>"
        for c in f5_list
    )
    ver_rows = "".join(
        f"<tr><td>{c.get('name')}</td>"
        f"<td>{badge('ok' if c.get('pass') else 'fail', 'PASS' if c.get('pass') else 'FAIL')}</td></tr>"
        for c in ver_list
    )
    llm_note = (
        "<p class='pipeline-note'><span class='pill pill-on'>Sarvam-105B judgement layer: ACTIVE</span> "
        "summaries/narrations drafted by model, verified by Drafter/Verifier guard. "
        "Matching &amp; arithmetic remain deterministic Tier-A code.</p>"
        if packet.ai_summary else
        "<p class='pipeline-note'><span class='pill'>Sarvam-105B: not enabled</span> "
        "deterministic-only run (no API key). Set SARVAM_API_KEY to enable the "
        "judgement layer.</p>"
    )
    src_meta = "".join(f"<span class='src-chip'>{k}: <b>{v}</b></span>"
                       for k, v in (sources or {}).items())
    portfolio_html = ""
    if portfolio:
        prow = "".join(
            f"<tr><td>{r.get('client_id')}</td><td>{r.get('period')}</td>"
            f"<td>Rs {_fmt(r.get('eligible_itc'))}</td>"
            f"<td>Rs {_fmt(r.get('net_payable'))}</td>"
            f"<td class='{'ok' if r.get('verification') else 'fail'}'>"
            f"{'PASS' if r.get('verification') else 'FAIL'}</td>"
            f"<td class='{'ok' if r.get('validation') else 'fail'}'>"
            f"{'PASS' if r.get('validation') else 'FAIL'}</td></tr>"
            for r in portfolio
        )
        passed = sum(1 for r in portfolio if r.get('verification') and r.get('validation'))
        portfolio_html = (
            f"<div class='section-title'>Firm-wide portfolio ({len(portfolio)} clients, "
            f"{passed} passed)</div>"
            f"<div class='card'><table class='tbl'><thead><tr><th>Client</th>"
            f"<th>Period</th><th>Eligible ITC</th><th>Net payable</th>"
            f"<th>Verification</th><th>Validation</th></tr></thead>"
            f"<tbody>{prow}</tbody></table></div>"
        )

    # ---- assemble (concatenation, no f-string over JS/CSS braces) ----------
    doc = []
    doc.append("<!DOCTYPE html>\n<html lang='en'>\n<head>")
    doc.append("<meta charset='utf-8'>")
    doc.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    doc.append(f"<title>GST 2B vs Books - Reconciliation Report - {packet.period}</title>")
    doc.append("<style>" + CSS + "</style>")
    doc.append("</head><body><div class='wrap'>")

    # header
    doc.append("<header class='hero'>")
    doc.append("<h1>GST 2B vs Books - Reconciliation Report</h1>")
    doc.append("<div class='sub'>Pre vs Post engine run - entity visualization (interactive)</div>")
    doc.append("<div class='meta'>")
    doc.append(f"<span>Client: <b>{packet.client_id}</b></span>")
    doc.append(f"<span>Period: <b>{packet.period}</b></span>")
    doc.append(f"<span>Generated: <b>{now}</b></span>")
    doc.append(f"<span>Stage: <b>{packet.stage} - review packet</b></span>")
    doc.append("</div>")
    if src_meta:
        doc.append("<div style='margin-top:10px'>" + src_meta + "</div>")
    doc.append("<div style='margin-top:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap'>"
               "<button class='btn-dl' onclick='downloadThisReport()' type='button'>"
               "&#11015; Download HTML report</button>"
               "<span style='font-size:11.5px;color:#e2e8f0'>single self-contained file - "
               "download, share, print or file it</span></div>")
    doc.append("</header>")

    # tab bar
    count_label = lambda t, n: f"<span class='tab-count'>{n}</span>"
    tabs = [
        ("overview", "Overview", ""),
        ("exceptions", "Exceptions", count_label("x", len(packet.exceptions))),
        ("jvs", "Corrective JVs", count_label("jv", len(packet.drafted_jvs))),
        ("g3b", "GSTR-3B", ""),
        ("gates", "Quality gates", ""),
        ("review", "Review queue", ""),
    ]
    doc.append("<nav class='tabbar' role='tablist' aria-label='Report sections'>")
    for name, label, extra in tabs:
        doc.append(
            f"<button class='tab' role='tab' data-tab='{name}' "
            f"aria-controls='tab-{name}' aria-selected='false'>{label} {extra}</button>")
    doc.append("</nav>")

    # ---- overview tab ----
    doc.append("<section class='tab-section' id='tab-overview' role='tabpanel'>")
    doc.append("<div class='kpis'>")
    kpi = [
        ("ok", "Matched", str(s.get('matched', 0)), f"of {n_portal} portal invoices", "matched"),
        ("warn", "Amount diff", str(s.get('amount_diff', 0)), "medium-confidence, verify value", "amount_diff"),
        ("fail", "Not in books", str(s.get('not_in_books', 0)), "JV auto-drafted", "not_in_books"),
        ("", "Not in portal", str(s.get('not_in_portal', 0)), "investigate RC / pending GST", "not_in_portal"),
        ("ok", "Eligible ITC", "Rs " + _fmt(post.get('eligible_itc') or 0), f"{post.get('eligible_count', 0)} invoices", ""),
        ("acc", "Net GST payable", "Rs " + _fmt(post.get('net_payable') or 0), "down from Rs " + _fmt(pre.get('pre_net_payable') or 0) + " (pre)", ""),
        ("ok" if post.get('validation_pass') else "fail", "F5 gate",
         "PASS" if post.get('validation_pass') else "BLOCKED", "integrity checks", ""),
        ("ok" if v_pass else "fail", "Verification", "PASS" if v_pass else "-", "independent recompute", ""),
    ]
    for cls, label, val, foot, ds in kpi:
        extra = "<span class='k-hint'>view &#8594;</span>" if ds else ""
        doc.append(
            f"<div class='kpi {cls}' {'data-status=' + repr(ds) if ds else ''} "
            f"role='button' tabindex='0' aria-label='{label}'>{extra}"
            f"<div class='k-label'>{label}</div><div class='k-value'>{val}</div>"
            f"<div class='k-foot'>{foot}</div></div>")
    doc.append("</div>")

    doc.append("<div class='grid'>")
    doc.append("<div class='card'><h2>Classification - portal invoices</h2>" + class_donut + "</div>")
    doc.append("<div class='card'><h2>Input tax credit split</h2>" + itc_donut + "</div>")
    doc.append("<div class='card'><h2>Key figures</h2>" + bars +
               f"<p class='muted' style='margin-top:10px'>Gross gap (portal - books): <b>Rs {_fmt(pre.get('gross_gap') or 0)}</b>"
               f" - Portal total: Rs {_fmt(portal_total_v)} - Books total: Rs {_fmt(book_total_v)}</p></div>")
    doc.append("</div>")

    doc.append("<div class='section-title'>Pre -&gt; Post comparison</div>")
    doc.append("<div class='compare'>")
    compare = [
        ("Portal invoices (pre)", str(pre.get('portal_invoices', 0))),
        ("Book vouchers (pre)", str(pre.get('book_invoices', 0))),
        ("Eligible ITC pre", "Rs 0.00"),
        ("Eligible ITC post", "Rs " + _fmt(post.get('eligible_itc') or 0)),
        ("Net payable pre", "Rs " + _fmt(pre.get('pre_net_payable') or 0)),
        ("Net payable post", "Rs " + _fmt(post.get('net_payable') or 0)),
    ]
    for lbl, val in compare:
        doc.append(f"<div class='stat-box'><div class='lbl'>{lbl}</div><div class='val'>{val}</div></div>")
    doc.append("</div>")
    if portfolio_html:
        doc.append(portfolio_html)
    doc.append("</section>")

    # ---- exceptions tab ----
    doc.append("<section class='tab-section' id='tab-exceptions' role='tabpanel'>")
    doc.append("<div class='card'>")
    doc.append("<div class='toolbar'>")
    doc.append("<input class='search' id='exc-search' type='search' placeholder='Search invoice, GSTIN, reason...' aria-label='Search exceptions'>")
    doc.append("<div class='chips' id='chips'></div>")
    doc.append("<span class='rs-count' id='rs-count'></span>")
    doc.append("</div>")
    doc.append("<p class='muted' style='margin-bottom:8px'>Click any row for full evidence (portal + books side, match deltas). "
               "<span class='row-click-hint'>sortable columns &#8597;</span></p>")
    doc.append("<table class='tbl' id='exc-table'><thead id='exc-thead'></thead><tbody id='exc-tbody'></tbody></table>")
    doc.append("</div>")
    doc.append("</section>")

    # ---- JVs tab ----
    doc.append("<section class='tab-section' id='tab-jvs' role='tabpanel'>")
    doc.append("<div class='card'><h2>Drafted corrective JVs</h2><div id='jv-box'></div></div>")
    doc.append("</section>")

    # ---- GSTR-3B tab ----
    doc.append("<section class='tab-section' id='tab-g3b' role='tabpanel'>")
    doc.append("<div class='card'>")
    doc.append("<h2>GSTR-3B draft</h2>")
    doc.append("<table class='tbl'><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>")
    doc.append(f"<tr><td>Output tax (period)</td><td>Rs {_fmt(pre.get('pre_net_payable') or 0)} <span class='muted'>(sample)</span></td></tr>")
    doc.append(f"<tr><td>Eligible ITC utilised</td><td>Rs {_fmt(post.get('eligible_itc') or 0)}</td></tr>")
    doc.append(f"<tr><td>Ineligible input tax (non-filer + unmatched)</td><td>Rs {_fmt(post.get('ineligible_itc') or 0)}</td></tr>")
    doc.append(f"<tr><td><b>Net GST payable</b></td><td><b>Rs {_fmt(post.get('net_payable') or 0)}</b></td></tr>")
    doc.append(f"<tr><td>Credit available</td><td>Rs {_fmt(g3b.get('credit_available', 0))}</td></tr>")
    doc.append("</tbody></table>")
    if post.get("itc_position"):
        doc.append("<div class='section-title' style='font-size:14px'>Live credit position</div>")
        doc.append("<table class='tbl tbl-compact'><thead><tr><th>Bucket</th><th>Amount</th></tr></thead><tbody>")
        for k_, vv in post["itc_position"].items():
            doc.append(f"<tr><td>{k_}</td><td>Rs {_fmt(vv)}</td></tr>")
        doc.append("</tbody></table>")
    doc.append(llm_note)
    doc.append("</div></section>")

    # ---- gates tab ----
    doc.append("<section class='tab-section' id='tab-gates' role='tabpanel'>")
    doc.append("<div class='grid'><div class='card'><h2>F5 data-integrity</h2>")
    doc.append("<table class='tbl tbl-compact'><thead><tr><th>Check</th><th>Result</th></tr></thead><tbody>" + f5_rows + "</tbody></table>")
    doc.append(f"<p class='muted' style='margin-top:8px'>Overall: <b>{'PASS' if post.get('validation_pass') else 'BLOCKED'}</b></p>")
    doc.append("</div><div class='card'><h2>Independent verification</h2>")
    doc.append("<table class='tbl tbl-compact'><thead><tr><th>Check</th><th>Result</th></tr></thead><tbody>" +
               (ver_rows or "<tr><td colspan=2 class='muted'>not run (agent only)</td></tr>") + "</tbody></table>")
    doc.append("<p class='muted' style='margin-top:8px'>Separate code path recomputes counts, ITC &amp; totals from raw files.</p>")
    doc.append("</div></div></section>")

    # ---- review queue tab ----
    doc.append("<section class='tab-section' id='tab-review' role='tabpanel'>")
    doc.append("<div class='card'><h2>Review queue (Phase-3 workflow)</h2><div id='review-box'></div></div>")
    doc.append("</section>")

    doc.append("<footer>Setu Reconciliation Engine (Module 2A) - matching &amp; arithmetic are deterministic Tier-A - "
               "Sarvam-105B optional judgement layer - generated " + now + "</footer>")
    doc.append("</div>")

    # modal
    doc.append("<div class='overlay' id='overlay' role='dialog' aria-modal='true' aria-labelledby='modal-title'>")
    doc.append("<div class='modal'>")
    doc.append("<button class='close' id='modal-close' aria-label='Close'>&times;</button>")
    doc.append("<h3 id='modal-title'></h3>")
    doc.append("<div id='modal-status' style='font-size:13px;margin-top:2px'></div>")
    doc.append("<div class='ev-grid'>")
    doc.append("<div id='modal-portal'></div>")
    doc.append("<div id='modal-book'></div>")
    doc.append("</div>")
    doc.append("<div class='ev-grid'>")
    doc.append("<div id='modal-deltas'><div class='ev-panel'><h4>Match deltas</h4></div></div>")
    doc.append("<div id='modal-reasons'></div>")
    doc.append("</div>")
    doc.append("<div class='modal-actions'>")
    doc.append("<button class='btn' id='modal-jump'>Go to corrective JV</button>")
    doc.append("<button class='btn' id='modal-close2'>Close</button>")
    doc.append("</div>")
    doc.append("</div></div>")

    doc.append("<script>window.RECON_DATA = " + json.dumps(data, default=str) + ";</script>")
    doc.append("<script>" + JS + "</script>")
    doc.append("""
<script>
function downloadThisReport() {
  try {
    if (location.protocol.indexOf('http') === 0) {
      var href = location.href.split('#')[0];
      href = href + (href.indexOf('?') === -1 ? '?download=1' : '&download=1');
      window.location.href = href;
    } else {
      var html = '<!DOCTYPE html>\n' + document.documentElement.outerHTML;
      var blob = new Blob([html], {type: 'text/html'});
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'gst_2b_reconciliation_report.html';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function(){ try{ URL.revokeObjectURL(a.href); }catch(e){} }, 2000);
    }
  } catch (e) { alert('Download failed: ' + e); }
}
</script>
""")   # close the doc.append( opened above
    doc.append("</body></html>")

    out_path.write_text("\n".join(doc), encoding="utf-8")
    return out_path
