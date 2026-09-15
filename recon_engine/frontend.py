"""Dependency-free web front-end pages for the Setu reconciliation API.

GET /                        home: pick an existing client or create a new one
GET /clients/{client_id}     workbench: upload documents, run reconciliation,
                             then open the per-client report dashboard

Both pages are single-file HTML (inline CSS + vanilla JS) and talk to the
existing JSON API from the browser:

    POST /api/clients/{client_id}/recon     multipart upload + run
    GET  /api/reports/{client}/{period}/..  report files (HTML embedded in the UI)

No template engine, no CDN assets - f-strings + one JS constant.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

_CLIENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

CSS = """
:root {
  --bg:#f4f6fb; --card:#ffffff; --ink:#1e293b; --mut:#64748b;
  --line:#e2e8f0; --acc:#0ea5e9; --ok:#16a34a; --warn:#f59e0b;
  --fail:#dc2626; --nav:#0f172a;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
     background:var(--bg);color:var(--ink);padding:24px;line-height:1.45}
.wrap{max-width:1080px;margin:0 auto}
header.hero{background:linear-gradient(120deg,#0f172a,#1e3a5f 60%,#155e75);color:#fff;
     border-radius:14px;padding:26px 30px;margin-bottom:16px}
header.hero h1{font-size:24px;font-weight:700}
header.hero .sub{color:#cbd5e1;margin-top:4px;font-size:14px}
header.hero .meta{display:flex;gap:18px;margin-top:14px;flex-wrap:wrap;font-size:13px;color:#e2e8f0}
.back{display:inline-block;margin-bottom:10px;color:var(--acc);text-decoration:none;font-size:13.5px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;
     box-shadow:0 1px 2px rgba(15,23,42,.04)}
.card h2{font-size:15px;margin-bottom:12px;color:#0f172a;border-bottom:1px solid var(--line);padding-bottom:8px}
.client-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.client-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
     box-shadow:0 1px 2px rgba(15,23,42,.04)}
.client-card h3{font-size:16px;color:#0f172a;margin-bottom:4px}
.client-card .meta{font-size:12.5px;color:var(--mut);margin-bottom:12px}
.empty,.muted{color:var(--mut);font-size:13.5px}
label{display:block;font-size:12px;font-weight:600;color:var(--mut);margin:12px 0 4px;
     text-transform:uppercase;letter-spacing:.4px}
input[type=text],input[type=number]{width:100%;padding:9px 11px;border:1px solid var(--line);
     border-radius:8px;font-size:14px;font-family:inherit;background:#fff;color:var(--ink)}
input[type=text]:focus,input[type=number]:focus{outline:none;border-color:var(--acc);
     box-shadow:0 0 0 3px rgba(14,165,233,.15)}
input[type=file]{width:100%;padding:9px;border:1px dashed #cbd5e1;border-radius:8px;
     background:#f8fafc;font-size:13px;color:var(--mut);cursor:pointer}
input[type=file]:hover{border-color:var(--acc)}
.hint{font-size:12px;color:var(--mut);margin-top:3px}
.form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.adv{margin-top:14px;border:1px solid var(--line);border-radius:10px;background:#f8fafc}
.adv summary{cursor:pointer;padding:10px 12px;font-size:12px;font-weight:600;color:var(--mut);
     text-transform:uppercase;letter-spacing:.4px;user-select:none}
.adv[open] summary{border-bottom:1px solid var(--line)}
.adv-inner{padding:10px 12px 12px}
.match-row{display:flex;align-items:center;gap:8px;margin-top:12px}
.match-row input[type=checkbox]{width:16px;height:16px;accent-color:var(--acc)}
.match-row label{margin:0;text-transform:none;letter-spacing:0;font-size:13px;color:var(--ink)}
.inputs-key{font-size:12.5px;color:var(--mut);background:#f8fafc;border:1px solid var(--line);
     border-radius:9px;padding:9px 12px;margin-bottom:4px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:none;border-radius:9px;
     padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;transition:.15s;text-decoration:none}
.btn-sm{padding:7px 12px;font-size:12.5px;border-radius:8px}
.btn-primary{background:var(--nav);color:#fff}
.btn-primary:hover{background:#1e293b}
.btn-primary:disabled{opacity:.6;cursor:progress}
.btn-ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.btn-ghost:hover{border-color:#cbd5e1;background:#f8fafc}
.inline-form{display:flex;gap:10px;flex-wrap:wrap}
.inline-form input[type=text]{flex:1;min-width:220px}
.spin{display:none;width:14px;height:14px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
     border-radius:50%;animation:sp .6s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.hidden{display:none!important}
.err{display:none;background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;border-radius:10px;
     padding:12px 14px;font-size:13.5px;margin-top:14px}
.mini-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:12px}
.mini-kpi{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#f8fafc}
.mini-kpi .k-label{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.mini-kpi .k-value{font-size:18px;font-weight:700;margin-top:2px}
.mini-kpi.ok .k-value{color:var(--ok)}
.mini-kpi.warn .k-value{color:var(--warn)}
.mini-kpi.bad .k-value{color:var(--fail)}
.chip{display:inline-block;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:600;
     border:1px solid var(--line);background:#fff;color:var(--mut);margin:2px 4px 0 0}
.chip.ok{background:#dcfce7;color:#166534;border-color:#86efac}
.chip.bad{background:#fee2e2;color:#991b1b;border-color:#fecaca}
.ok-banner{background:linear-gradient(120deg,#14532d,#166534);color:#fff;border-radius:12px;
     padding:16px 20px;margin-top:14px}
.ok-banner.bad{background:linear-gradient(120deg,#7f1d1d,#991b1b)}
.banner-title{font-size:16px;font-weight:700}
.banner-sub{color:#e2e8f0;font-size:13px;margin-top:3px}
.rlinks{display:grid;gap:8px;margin-top:10px}
.rlink{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:9px 12px;
     border:1px solid var(--line);border-radius:9px;background:#f8fafc;font-size:13px;color:var(--ink)}
.rlink:hover{border-color:var(--acc)}
.rlink-act{display:flex;gap:6px;align-items:center;flex-shrink:0}
.rlink .tag{font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:55%}
.checks{margin-top:12px;display:grid;gap:6px}
.check-row{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mut)}
.tbl{width:100%;border-collapse:collapse;font-size:13.5px}
.tbl th{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.4px;text-align:left;
     padding:8px 10px;border-bottom:1px solid var(--line)}
.tbl td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left}
.rv{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.rv input[type=text]{flex:1;min-width:200px;font-size:12.5px;padding:7px 10px;color:var(--mut)}
.rv label{flex-basis:100%;margin:0;text-transform:none;letter-spacing:0;font-size:12.5px;color:var(--mut)}
.rv-actions{display:flex;gap:8px}
#report-frame{width:100%;height:720px;border:1px solid var(--line);border-radius:10px;background:#fff}
#report-frame.compact{height:300px}
"""

# JS constants are plain strings (braces free; no f-string). ----------------

_ROUTER_JS = """
document.addEventListener('DOMContentLoaded', function(){
  var f = document.getElementById('new-client-form');
  if (!f) return;
  f.addEventListener('submit', function(e){
    e.preventDefault();
    var v = document.getElementById('new-client-id').value.trim();
    if (!v) return;
    if (!/^[A-Za-z0-9_.-]+$/.test(v)) { alert('Client ID: letters, digits, dash, underscore or dot only.'); return; }
    location.href = '/clients/' + encodeURIComponent(v);
  });
});
"""

_WORKBENCH_JS = """
(function(){
  "use strict";
  function $(s){ return document.querySelector(s); }
  function esc(v){ return String(v == null ? '' : v).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  }); }
  function num(v){ var n = Number(v); return isFinite(n) ? n.toLocaleString('en-IN') : esc(v); }
  function chip(label, ok){ return '<span class="chip ' + (ok ? 'ok' : 'bad') + '">' + esc(label) + '</span>'; }

  function loadReport(href){
    var f = $('#report-frame');
    if (!f) return;                      // no viewer on this page
    var u = $('#report-url');
    if (u) u.value = href;
    document.getElementById('report-period-label').textContent = href.split('/').slice(-2, -1)[0] || '';
    f.classList.remove('compact');
    f.src = href;
  }
  function resizeReport(){
    var f = $('#report-frame');
    if (f) f.classList.toggle('compact');
  }
  function downloadCurrent(){
    var u = $('#report-url'), href = u ? u.value.trim() : '';
    if (!href){ return; }
    if (href.indexOf('?') === -1){ href = href + '?download=1'; }
    else { href = href + '&download=1'; }
    window.open(href, '_blank');
  }

  var form = $('#recon-form');
  if (!form) return;

  form.addEventListener('submit', function(e){
    e.preventDefault();
    var btn = $('#run-btn'), spin = $('#spin'), txt = $('#btn-txt');
    var result = $('#result'), err = $('#err');
    var fd = new FormData(form);
    if (!fd.get('tb')) fd.delete('tb');
    btn.disabled = true; spin.style.display = 'inline-block'; txt.textContent = 'Running reconciliation…';
    result.classList.add('hidden'); err.style.display = 'none';

    var qs = [];
    ['period', 'output_tax', 'materiality', 'invoice_edit_max',
     'date_window_days', 'value_tolerance'].forEach(function(k){
      var el = document.getElementById(k), v = el ? el.value.trim() : '';
      if (v) qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
    });
    // checkbox: always explicit so the engine sees the reviewer's intent
    ['require_gstin_exact'].forEach(function(k){
      var el = document.getElementById(k);
      if (el) qs.push(encodeURIComponent(k) + '=' + (el.checked ? 'true' : 'false'));
    });
    var url = form.getAttribute('action') + (qs.length ? '?' + qs.join('&') : '');

    fetch(url, { method: 'POST', body: fd })
      .then(function(r){ return r.json().then(function(d){ return { ok: r.ok, d: d }; }); })
      .then(function(x){
        if (!x.ok){
          var msg = (x.d && x.d.detail) ? x.d.detail : 'Reconciliation failed.';
          err.textContent = 'Error: ' + msg; err.style.display = 'block'; return;
        }
        render(x.d);
      })
      .catch(function(ex){ err.textContent = 'Request failed: ' + ex; err.style.display = 'block'; })
      .finally(function(){ btn.disabled = false; spin.style.display = 'none'; txt.textContent = 'Run reconciliation'; });
  });

  function render(d){
    var p = d.packet || {}, s = p.summary || {}, itc = p.itc || {}, g3 = itc.gstr3b_draft || {};
    var v = d.verification || {};
    var okGates = (p.validation && p.validation.pass) && v.pass;

    var kpis = [
      ['Portal invs', s.total_portal_invoices],
      ['Books vouchers', s.total_book_invoices],
      ['Matched', s.matched, 'ok'],
      ['Amount diff', s.amount_diff, 'warn'],
      ['Not in books', s.not_in_books, 'warn'],
      ['Not in portal', s.not_in_portal, 'bad'],
      ['Eligible ITC', num(itc.eligible_itc)],
      ['Net payable', num(g3.net_payable)]
    ];
    var kpiHtml = kpis.map(function(k){
      return '<div class="mini-kpi ' + (k[2] || '') + '"><div class="k-label">' + esc(k[0]) +
             '</div><div class="k-value">' + esc(k[1]) + '</div></div>';
    }).join('');

    var rl = d.reports || {};
    var links = [
      ['html', 'Open interactive dashboard'],
      ['excel', 'Pre / post Excel workbook'],
      ['markdown', 'Pre vs post markdown'],
      ['csv_pre', 'Open items (pre)'],
      ['csv_post', 'Exceptions (post)'],
      ['review_queue', 'Review queue JSON']
    ];
    var linkHtml = links.filter(function(l){ return rl[l[0]]; }).map(function(l){
      var href = esc(rl[l[0]]);
      return '<div class="rlink">' +
             '<span>' + l[1] + '</span><span class="tag mono">' + href + '</span>' +
             '<span class="rlink-act">' +
               '<button class="btn btn-ghost btn-sm" onclick="window.loadReport(&quot;' + href + '&quot;);return false;">View</button>' +
               '<a class="btn btn-ghost btn-sm" target="_blank" rel="noopener" href="' + href + '">Open</a>' +
               \'<a class="btn btn-ghost btn-sm" href="\' + href + \'?download=1">Download</a>\' +
             '</span></div>';
    }).join('');

    var rv = d.review || {};
    var gateHtml = chip('F5 gate ' + (p.validation && p.validation.pass ? 'PASS' : 'BLOCKED'),
                        !!(p.validation && p.validation.pass))
                 + chip('Verification ' + (v.pass ? 'PASS' : 'FAIL'), !!v.pass)
                 + (d.llm_used ? chip('LLM judgement layer', true) : '');

    var checkHtml = '';
    if (v.checks && v.checks.length){
      checkHtml = '<div class="checks">' + v.checks.map(function(c){
        var name = (c && c.name) || JSON.stringify(c);
        var ok = c && c.pass !== false;
        return '<div class="check-row">' + chip(typeof ok === 'boolean' ? (ok ? 'pass' : 'fail') : 'note', ok) +
               '<span>' + esc(name) + '</span></div>';
      }).join('') + '</div>';
    }

    result.innerHTML =
      '<div class="ok-banner' + (okGates ? '' : ' bad') + '">' +
        '<div class="banner-title">' + (okGates ? 'Reconciliation completed - all gates green' :
                                                    'Reconciliation completed - review needed') + '</div>' +
        '<div class="banner-sub">client ' + esc(d.client_id) + ' | period ' + esc(d.period) +
        (rv.total != null ? ' | ' + num(rv.total) + ' items in review queue' : '') + '</div>' +
      '</div>' +
      '<div class="card">' +
        '<h2>Packet summary</h2>' + gateHtml +
        '<div class="mini-kpis">' + kpiHtml + '</div>' +
      '</div>' +
      '<div class="card">' +
        '<h2>Generated reports</h2><div class="rlinks">' + linkHtml + '</div>' + checkHtml +
      '</div>';
    result.classList.remove('hidden');
    result.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  window.loadReport = loadReport;
  window.resizeReport = resizeReport;
  window.downloadCurrent = downloadCurrent;
})();
"""


def _esc(v) -> str:
    return html.escape(str(v))


def _ts(secs: float) -> str:
    try:
        return datetime.fromtimestamp(secs).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def is_valid_client_id(cid: str) -> bool:
    return bool(_CLIENT_RE.fullmatch(cid or "")) and cid not in (".", "..")


def discover_clients(reports_root: Path) -> list[dict]:
    """Scan reports/<client_id>/<period>/ and return an ordered client list."""
    out: list[dict] = []
    if not reports_root.is_dir():
        return out
    for child in sorted(reports_root.iterdir()):
        if not child.is_dir() or not is_valid_client_id(child.name):
            continue
        periods = []
        for p in sorted(child.iterdir()):
            if not p.is_dir():
                continue
            files = sorted(f.name for f in p.iterdir() if f.is_file())
            if not files:
                continue
            mtime = max((f.stat().st_mtime for f in p.iterdir() if f.is_file()), default=0.0)
            periods.append({
                "period": p.name,
                "files": files,
                "has_html": "gst_2b_reconciliation_report.html" in files,
                "mtime": mtime,
                "updated": _ts(mtime),
            })
        if not periods:
            continue
        periods.sort(key=lambda r: r["mtime"], reverse=True)
        out.append({
            "client_id": child.name,
            "periods": periods,
            "latest_period": periods[0]["period"],
            "updated": periods[0]["updated"],
        })
    out.sort(key=lambda c: c["periods"][0]["mtime"], reverse=True)
    return out


def _page(title: str, body: str, scripts: str = "") -> str:
    return (f"<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
            f"<title>{_esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
            f"<div class='wrap'>\n{body}\n</div>\n"
            f"<script>{scripts}</script>\n</body>\n</html>")


def home_page(reports_root: Path) -> str:
    """Client picker: existing clients + new-client creation."""
    clients = discover_clients(reports_root)
    cards = []
    fw = reports_root / "firmwide_portfolio.html"
    if fw.exists():
        cards.append(
            "<div class='client-card'><h3>Firm-wide portfolio</h3>"
            "<div class='meta'>batch aggregate across all clients</div>"
            "<a class='btn btn-ghost btn-sm' href='/api/firmwide' target='_blank' "
            "rel='noopener'>Open portfolio &rarr;</a></div>")
    cards.append(
        "<div class='client-card'><h3>Realtime reconciliation agent</h3>"
        "<div class='meta'>drop the accountant's file set (GSTR-2B workbook, IMS csv, "
        "Tally registers) - the agent detects roles, reconciles with Sarvam-105B "
        "judgement and serves a clickable, downloadable HTML report.</div>"
        "<a class='btn btn-primary btn-sm' href='/agent' target='_blank' "
        "rel='noopener'>Open agent &rarr;</a></div>")
    for c in clients:
        cards.append(
            "<div class='client-card'><h3>" + _esc(c["client_id"]) + "</h3>"
            "<div class='meta'>" + str(len(c["periods"])) + " period(s) &middot; latest "
            + _esc(c["latest_period"]) + "<br>updated " + _esc(c["updated"]) + "</div>"
            "<a class='btn btn-ghost btn-sm' href='/clients/" + _esc(c["client_id"])
            + "'>Open workbench &rarr;</a></div>")
    grid = "\n".join(cards) if cards else (
        "<div class='empty'>No clients yet. Create one below to upload documents "
        "and run reconciliation.</div>")

    body = f"""
<header class='hero'>
  <h1>Setu &middot; GST 2B vs Books Reconciliation</h1>
  <div class='sub'>Pick a client, attach the GSTR-2B + Tally documents, run the
  engine and open the generated report dashboards (embedded in the workbench)
  - or use the <a href='/agent' style='color:#7dd3fc'>realtime agent</a> for a
  one-shot file set with auto role detection.</div>
</header>
<div class='card'>
  <h2>Choose a client</h2>
  <div class='client-grid'>{grid}</div>
</div>
<div class='card'>
  <h2>New client</h2>
  <form id='new-client-form' class='inline-form'>
    <input type='text' id='new-client-id' placeholder='e.g. acme, techno-pvt, delhi_office'
           required pattern='[A-Za-z0-9_.-]+'>
    <button class='btn btn-primary' type='submit'>Create &amp; open workbench</button>
  </form>
  <div class='hint'>Letters, digits, dash, underscore or dot. This names the working
  folder only - it does not create any external system account.</div>
</div>
"""
    return _page("Setu GST 2B vs Books Reconciliation", body, scripts=_ROUTER_JS)


def workbench_page(client_id: str, client_root: Path) -> str:
    """Per-client document upload + run + previous-run links."""
    periods = []
    if client_root.is_dir():
        for p in sorted(client_root.iterdir()):
            if not p.is_dir():
                continue
            files = sorted(f.name for f in p.iterdir() if f.is_file())
            if not files:
                continue
            mtime = max((f.stat().st_mtime for f in p.iterdir() if f.is_file()), default=0.0)
            periods.append({
                "period": p.name,
                "files": files,
                "has_html": "gst_2b_reconciliation_report.html" in files,
                "updated": _ts(mtime),
            })
        periods.sort(key=lambda r: r["period"], reverse=True)

    rows = []
    for r in periods:
        if r["has_html"]:
            href = (f"/api/reports/{_esc(client_id)}/{_esc(r['period'])}/"
                    "gst_2b_reconciliation_report.html")
            dash = ("<button class='btn btn-ghost btn-sm' "
                    "onclick='window.loadReport(&quot;" + href + "&quot;);return false;'>"
                    "View</button>"
                    " <a class='btn btn-ghost btn-sm' href='" + href
                    + "?download=1'>Download</a>")
        else:
            dash = "<span class='muted'>no html</span>"
        rows.append(
            "<tr><td>" + _esc(r["period"]) + "</td><td>" + str(len(r["files"]))
            + "</td><td>" + _esc(r["updated"]) + "</td><td>" + dash + "</td></tr>")
    table = ("<table class='tbl'><thead><tr><th>Period</th><th>Files</th>"
             "<th>Updated</th><th></th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table>") if rows else (
                 "<div class='empty'>No reconciliation runs yet for this client.</div>")

    action = f"/api/clients/{client_id}/recon"
    body = f"""
<a class='back' href='/'>↩ All clients</a>
<header class='hero'>
  <h1>Client workbench &middot; {_esc(client_id)}</h1>
  <div class='sub'>Attach the period documents, run the reconciliation engine, then
  open the generated report dashboards - they render inside this page.</div>
</header>
<div class='inputs-key'>Required inputs: <b>GSTR-2B</b> (portal invoices) and
<b>Invoices</b> (books &middot; vendor purchase register from Tally).
Optional: trial balance, period, output tax, materiality gate, match tolerances.</div>
<div class='grid'>
  <div class='card' style='min-width:0'>
    <h2>1 &middot; Attach documents &amp; run</h2>
    <noscript><p class='hint'>Inline submission needs JavaScript; use the curl
    commands in the README otherwise.</p></noscript>
    <form id='recon-form' action='{action}' method='post' enctype='multipart/form-data'>
      <label for='gstr2b'>GSTR-2B &middot; required *</label>
      <input type='file' id='gstr2b' name='gstr2b' required accept='.json,.xlsx,.xlsm'>
      <div class='hint'>portal export: .json, .xlsx, .xlsm</div>

      <label for='tally'>Invoices &middot; Tally purchase register (books) &middot; required *</label>
      <input type='file' id='tally' name='tally' required accept='.csv,.xlsx,.xlsm'>
      <div class='hint'>vendor purchase-invoice register from Tally: .csv, .xlsx, .xlsm</div>

      <label for='tb'>Trial balance &middot; optional</label>
      <input type='file' id='tb' name='tb' accept='.xlsx,.xlsm'>
      <div class='hint'>books context: .xlsx, .xlsm</div>

      <div class='form-row'>
        <div><label for='period'>Period (YYYY-MM)</label>
        <input type='text' id='period' name='period' placeholder='auto-detect'
               pattern='[0-9]{{4}}-[0-9]{{2}}'></div>
        <div><label for='output_tax'>Output tax (GST)</label>
        <input type='number' id='output_tax' name='output_tax' value='0'
               step='0.01' min='0'></div>
        <div><label for='materiality'>Materiality gate</label>
        <input type='number' id='materiality' name='materiality' step='0.01'
               min='0' placeholder='none'></div>
      </div>

      <details class='adv'>
        <summary>Advanced &middot; match rules (tolerances)</summary>
        <div class='adv-inner'>
          <div class='form-row'>
            <div><label for='invoice_edit_max'>Invoice-no edit distance</label>
            <input type='number' id='invoice_edit_max' name='invoice_edit_max' min='0'
                   max='10' step='1' placeholder='default 2'></div>
            <div><label for='date_window_days'>Date window (days)</label>
            <input type='number' id='date_window_days' name='date_window_days' min='0'
                   step='1' placeholder='default 7'></div>
            <div><label for='value_tolerance'>Value tolerance (fraction)</label>
            <input type='number' id='value_tolerance' name='value_tolerance' min='0'
                   max='1' step='0.01' placeholder='default 0.01'></div>
          </div>
          <div class='match-row'>
            <input type='checkbox' id='require_gstin_exact' name='require_gstin_exact' checked>
            <label for='require_gstin_exact'>Require exact GSTIN for a match</label>
          </div>
        </div>
      </details>

      <button id='run-btn' class='btn btn-primary' type='submit' style='margin-top:16px'>
        <span class='spin' id='spin'></span><span id='btn-txt'>Run reconciliation</span>
      </button>
    </form>
    <div class='err' id='err'></div>
    <div id='result' class='hidden'></div>
  </div>

  <div class='card' style='min-width:0'>
    <h2>2 &middot; Previous runs for {_esc(client_id)}</h2>
    {table}
    <div class='hint' style='margin-top:12px'>Refresh this page after a run to see the
    new period appear in the table above - then press its <b>View</b> button to open the
    dashboard right here.</div>
  </div>
</div>

<div class='card'>
  <h2>3 &middot; Report viewer</h2>
  <div class='rv'>
    <label for='report-url'>Paste / pick a report URL, or use a <b>View</b> button above
    (HTML reports render inline - no more downloads).</label>
    <input type='text' id='report-url' placeholder='/api/reports/{_esc(client_id)}/YYYY-MM/gst_2b_reconciliation_report.html'>
    <div class='rv-actions'>
      <button class='btn btn-ghost btn-sm' onclick='window.loadReport(document.getElementById(&quot;report-url&quot;).value);return false;'>Load</button>
      <button class='btn btn-ghost btn-sm' onclick='window.resizeReport();return false;'>Toggle height</button>
      <button class='btn btn-ghost btn-sm' onclick='window.downloadCurrent();return false;'>Download current</button>
    </div>
  </div>
  <div class='hint' style='margin-bottom:8px'>Period: <span id='report-period-label' class='muted'>-</span></div>
  <iframe id='report-frame' src='about:blank' title='Reconciliation report'></iframe>
</div>
"""
    return _page(f"Workbench &middot; {client_id}", body, scripts=_WORKBENCH_JS)