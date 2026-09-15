"""Phase-4 FastAPI delivery surface (Module 2A + 3).

POST /api/clients/{client_id}/recon   multipart upload (GSTR-2B JSON/Excel,
                                       Tally CSV/Excel, optional TB XLSX)
                                       -> runs the full Phase-1..3 pipeline
                                       -> returns review-packet JSON + URLs
GET  /api/reports/{client_id}/{period}/{name}   serves emitted report files (HTML inline, others download)
GET  /api/health                                   liveness probe

All uploads are written to a per-request work dir; reports are emitted under
reports/<client_id>/<period>/ so concurrent clients never collide. The
response JSON is exactly the engine packet (`ReviewPacket.to_dict()`) plus
report URLs, review-queue summary and the verification gate result.

Run locally:
    uvicorn recon_engine.api:app --reload
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from .agent import auto_reconcile
from .realtime_agent import JobStore, detect_role, sniff_role
from .review_queue import ReviewQueue
from .frontend import (home_page, workbench_page, is_valid_client_id)

HERE = Path(__file__).resolve().parent
BASE_REPORTS = HERE.parent / "reports"

app = FastAPI(
    title="Setu GST 2B vs Books Reconciliation API",
    description=("Submits GSTR-2B + Tally purchase register, runs the "
                 "deterministic reconciliation engine (F5 gate, independent "
                 "verification, review queue) and returns the review packet."),
    version="4.0.0",
)

@app.middleware("http")
async def _no_cache_html(request, call_next):
    """All pages & inline report HTML are dynamic - never serve stale cache."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


ALLOWED_PORTAL = {".json", ".xlsx", ".xlsm"}
ALLOWED_TALLY = {".csv", ".xlsx", ".xlsm"}
ALLOWED_TB = {".xlsx", ".xlsm"}


def _safe_name(name: str) -> str:
    """Basename only - prevent path traversal when serving reports."""
    return Path(name).name


def _save(upload: UploadFile, dest: Path, allowed: set[str]) -> None:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type {suffix or '(none)'} for "
                   f"{upload.filename!r}; allowed: {sorted(allowed)}")
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "setu-recon", "stage": "phase4"}


@app.post("/api/clients/{client_id}/recon")
async def run_recon(
    client_id: str,
    gstr2b: UploadFile = File(...),
    tally: UploadFile = File(...),
    tb: Optional[UploadFile] = File(None),
    period: str = Query("", description="YYYY-MM; inferred from data if blank"),
    output_tax: str = Query("0", description="Output GST for the period"),
    materiality: Optional[str] = Query(None,
        description="2A materiality gate (exceptions >= this need review)"),
    invoice_edit_max: Optional[int] = Query(None,
        description="Invoice-no Levenshtein tolerance (default 2)"),
    date_window_days: Optional[int] = Query(None,
        description="Date matching window in days, +/- (default 7)"),
    value_tolerance: Optional[float] = Query(None,
        description="Taxable-value tolerance as a fraction (default 0.01 = 1%)"),
    require_gstin_exact: Optional[bool] = Query(None,
        description="Require exact GSTIN for a match (default true)"),
) -> dict[str, Any]:
    """Run the full pipeline for one client and return the review packet."""
    workdir = Path(tempfile.mkdtemp(prefix="setu_recon_"))
    try:
        g2b = workdir / ("gstr2b" + Path(gstr2b.filename or "").suffix.lower())
        tly = workdir / ("tally" + Path(tally.filename or "").suffix.lower())
        _save(gstr2b, g2b, ALLOWED_PORTAL)
        _save(tally, tly, ALLOWED_TALLY)

        tb_path = None
        if tb is not None and tb.filename:
            tb_path = workdir / ("tb" + Path(tb.filename or "").suffix.lower())
            _save(tb, tb_path, ALLOWED_TB)

        match_rules: dict[str, Any] = {}
        if materiality:
            match_rules["materiality_amount"] = materiality
        if invoice_edit_max is not None:
            match_rules["invoice_edit_max"] = invoice_edit_max
        if date_window_days is not None:
            match_rules["date_window_days"] = date_window_days
        if value_tolerance is not None:
            match_rules["value_tolerance"] = value_tolerance
        if require_gstin_exact is not None:
            match_rules["require_gstin_exact"] = require_gstin_exact
        client_config = ({"match_rules": match_rules} if match_rules else None)
        reports_dir = BASE_REPORTS / client_id / (period or "period")

        try:
            res = auto_reconcile(
                gstr2b_path=g2b, tally_path=tly, tb_path=tb_path,
                client_id=client_id, period=period,
                output_tax=output_tax, client_config=client_config,
                reports_dir=reports_dir,
            )
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e))

        packet = res["packet"]
        period_used = packet.period
        reports_dir = BASE_REPORTS / client_id / period_used

        def url(name: str) -> str:
            return f"/api/reports/{client_id}/{period_used}/{name}"

        report_urls = {
            "markdown": url("pre_vs_post_reconciliation.md"),
            "excel": url("Pre_Post_Reconciliation_Report.xlsx"),
            "html": url("gst_2b_reconciliation_report.html"),
            "csv_pre": url("pre_recon_open_items.csv"),
            "csv_post": url("post_recon_exceptions.csv"),
            "review_queue": url("review_queue.json"),
        }

        review = res["review"]
        return {
            "client_id": client_id,
            "period": period_used,
            "packet": packet.to_dict(),
            "reports": report_urls,
            "review": review.summary(),
            "verification": res["verification"],
            "llm_used": res.get("llm_used", False),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.get("/api/reports/{client_id}/{period}/{name}")
def get_report(client_id: str, period: str, name: str,
               download: Optional[bool] = Query(False,
                   description="Force attachment download even for HTML")):
    """Serve an emitted report file for a client/period pack.

    Default: HTML reports are returned inline (text/html, no attachment
    header) so the browser renders them - directly or embedded in the
    workbench iframe. All other files download as before.

    `?download=1` forces Content-Disposition: attachment for ANY file,
    including HTML - the workbench uses this for its Download buttons.
    """
    rel = BASE_REPORTS / client_id / period / _safe_name(name)
    if not rel.exists() or not rel.is_file():
        raise HTTPException(status_code=404, detail=f"report not found: {rel}")
    if download:
        return FileResponse(rel, filename=_safe_name(name))
    if rel.suffix.lower() == ".html":
        return HTMLResponse(rel.read_text(encoding="utf-8"))
    return FileResponse(rel, filename=_safe_name(name))


# ---------------------------------------------------------------------------
# Web front-end: client picker (home) + per-client workbench
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    """Home page: choose an existing client or create a new one."""
    return home_page(BASE_REPORTS)


@app.get("/clients", include_in_schema=False)
def clients_redirect(client_id: str = Query("", alias="id")) -> RedirectResponse:
    """No-JS fallback for the 'new client' form: /clients?id=acme -> /clients/acme."""
    if client_id and is_valid_client_id(client_id):
        return RedirectResponse(url=f"/clients/{client_id}", status_code=302)
    return RedirectResponse(url="/", status_code=302)


@app.get("/clients/{client_id}", response_class=HTMLResponse, include_in_schema=False)
def client_workbench(client_id: str) -> str:
    """Per-client workbench: upload documents, run reconciliation, open report."""
    if not is_valid_client_id(client_id):
        raise HTTPException(status_code=400, detail="invalid client id")
    return workbench_page(client_id, BASE_REPORTS / client_id)


@app.get("/api/firmwide", include_in_schema=False)
def firmwide_portfolio() -> FileResponse:
    """Serve the batch firm-wide portfolio dashboard if one exists."""
    f = BASE_REPORTS / "firmwide_portfolio.html"
    if not f.exists():
        raise HTTPException(status_code=404, detail="no firm-wide portfolio yet - run a batch first")
    return FileResponse(f)


# ---------------------------------------------------------------------------
# Realtime reconciliation agent
#   POST /api/agent/reconcile  upload the file set -> runs the agent
#   GET  /api/agent/jobs/{id}  poll status + report URLs
#   GET  /agent                drag-and-drop upload page (polls the job)
# ---------------------------------------------------------------------------
AGENT_JOBS = JobStore()

ALLOWED_AGENT = {".json", ".xlsx", ".xlsm", ".csv"}


@app.post("/api/agent/reconcile")
async def agent_reconcile(
    files: list[UploadFile] = File(...),
    client_id: str = Query("client",
        description="client code; used only for the reports/ folder path"),
    period: str = Query("", description="YYYY-MM; inferred from data if blank"),
    output_tax: str = Query("", description="Output GST; auto-derived if blank"),
    llm: bool = Query(True,
        description="Use the Sarvam judgement layer (narration); set false for deterministic only"),
) -> dict[str, Any]:
    """Upload the accountant's file set; the agent detects roles, normalises
    the files and runs the full pipeline (Sarvam-105B when configured) to
    produce the interactive HTML report. Returns a job id to poll."""
    workdir = Path(tempfile.mkdtemp(prefix="setu_agent_"))
    roles: dict[str, Path] = {}
    try:
        for uf in files:
            if not uf.filename:
                continue
            suffix = Path(uf.filename).suffix.lower()
            if suffix not in ALLOWED_AGENT:
                raise HTTPException(
                    status_code=400,
                    detail=f"unsupported file type {suffix or '(none)'} "
                           f"for {uf.filename!r}")
            dest = workdir / Path(uf.filename).name
            with dest.open("wb") as f:
                shutil.copyfileobj(uf.file, f)
            role = detect_role(uf.filename) or sniff_role(dest)
            roles[role] = dest if role else roles.get("_unknown", dest)
            if role is None:
                roles.pop("_unknown", None)
                roles["_unknown"] = dest
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"upload failed: {e}")

    job = AGENT_JOBS.start(
        files=roles, client_id=client_id, period=period,
        output_tax=output_tax, out_root=BASE_REPORTS, llm=llm,
    )
    return {"job_id": job.job_id, "status": "queued",
            "poll": f"/api/agent/jobs/{job.job_id}"}


@app.get("/api/agent/jobs/{job_id}")
def agent_job(job_id: str) -> dict[str, Any]:
    job = AGENT_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return {"job_id": job.job_id, "client_id": job.client_id,
            "period": job.period, "status": job.status,
            "progress": job.progress, "result": job.result,
            "error": job.error}


_AGENT_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Setu — Realtime Reconciliation Agent</title>
<style>
 body{font-family:ui-sansserif,system-ui,sans-serif;background:#f6f8fa;margin:2rem auto;max-width:860px}
 h1{font-size:1.5rem} .card{background:#fff;border:1px solid #d8dee6;border-radius:12px;padding:1.2rem 1.4rem;margin-bottom:1rem}
 input[type=file]{display:block;margin:.4rem 0;font-size:.9rem}
 button{background:#1f6feb;color:#fff;border:0;border-radius:8px;padding:.55rem 1.2rem;font-size:.95rem;cursor:pointer}
 button:disabled{opacity:.55}
 .role{color:#57606a;font-size:.82rem;margin-left:.6rem}
 .pill{display:inline-block;padding:.1rem .5rem;border-radius:999px;background:#e6edf3;color:#57606a;font-size:.75rem}
 #status{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:.85rem;color:#1a1f24}
 a{color:#0969da;text-decoration:none}
 img.busy{width:14px;height:14px;vertical-align:middle}
</style></head><body>
<h1>Setu — Realtime Reconciliation Agent</h1>
<div class="card">
 <p>Upload the accountant's file set. The agent detects each file's role
 (GSTR-2B workbook / IMS B2B csv / Tally purchase register / credit-note
 register), normalises the formats, runs the reconciliation engine and serves
 a clickable, downloadable HTML report.</p>
 <form id="f">
  <div>GSTR-2B (portal workbook&nbsp;<i>.xlsx</i>)<input type="file" name="files" data-role="portal_2b" multiple></div>
  <div>IMS B2B csv (optional cross-check)<input type="file" name="files" data-role="portal_ims" multiple></div>
  <div>Tally purchase register<input type="file" name="files" data-role="books_purchase" multiple></div>
  <div>Tally credit-note register<input type="file" name="files" data-role="books_credit" multiple></div>
  <div>Client code <input id="cid" value="client" style="width:10rem"></div>
  <button id="go" onclick="startJob()">Run reconciliation agent</button>
 </form>
 <div id="roles" class="role"></div>
</div>
<div class="card"><div id="status">Waiting for upload…</div></div>
<script>
 const ROLES={portal_2b:'portal · GSTR-2B workbook',portal_ims:'portal · IMS B2B csv',
              books_purchase:'books · Tally purchase register',books_credit:'books · credit-note register'};
 let filesSel=[];
 function onFileChange(){
  filesSel=[];const out=[];
  document.querySelectorAll('input[type=file]').forEach(i=>{
   for(const f of i.files){filesSel.push(f);out.push(f.name+' → '+ (ROLES[i.dataset.role]||'?'));}
  });
  document.getElementById('roles').textContent  ='';
  document.getElementById('roles').textContent = out.join('\n');
 }
 document.querySelectorAll('input[type=file]').forEach(i=>i.addEventListener('change',onFileChange));
 async function startJob(){
  const d=new FormData();
  filesSel.forEach(f=>d.append('files',f));
  d.append('client_id',document.getElementById('cid').value||'client');
  const go=document.getElementById('go');go.disabled=true;
  const st=document.getElementById('status');
  try{
   const r=await fetch('/api/agent/reconcile',{method:'POST',body:d});
   const j=await r.json();st.textContent='job '+j.job_id+' → '+j.status+'\n';
   poll(j.job_id);
  }catch(e){st.textContent='ERROR '+e;go.disabled=false;}
 }
 async function poll(id){
  const st=document.getElementById('status');
  const r=await fetch('/api/agent/jobs/'+id);
  const j=await r.json();
  if(j.status==='done'){
   const rep=j.result.reports;
   st.textContent='✔ report ready\n';
   const box=document.getElementById('links');
   if(!box){const b=document.createElement('div');b.id='links';b.className='card';
     b.innerHTML='<b>Report</b><br><a href="'+rep.html+'" target="_blank">Open interactive HTML report</a> &nbsp;•&nbsp; '+
       '<a href="'+rep.html+'?download=1">Download HTML</a><br>'+
       '<a href="'+rep.markdown+'">Markdown</a> &nbsp;•&nbsp; <a href="'+rep.excel+'">Excel</a> &nbsp;•&nbsp; '+
       '<a href="'+rep.csv_pre+'">Pre-recon CSVs</a> &nbsp;•&nbsp; <a href="'+rep.csv_post+'">Exceptions CSV</a> &nbsp;•&nbsp; '+
       '<a href="'+rep.review_queue+'">Review queue JSON</a>'+
       '<div style="color:#57606a;font-size:.8rem;margin-top:.4rem">F5 gate: '+j.result.f5.pass+
       ' · verification: '+j.result.verification.pass+' · LLM used: '+j.result.llm_used+'</div>';
     document.body.appendChild(b);}
   }else if(j.status==='error'){
   st.textContent='✖ job failed:\n'+j.error;
   }else{
   st.textContent=(st.textContent||'')+ (j.progress?(' → '+j.progress+'\n'):'');
   setTimeout(()=>poll(id),3000);
   }
 }
 document.addEventListener('DOMContentLoaded',()=>document.querySelectorAll('input[type=file]').forEach(i=>i.addEventListener('change',onFileChange)));
</script></body></html>"""


@app.get("/agent", response_class=HTMLResponse, include_in_schema=False)
def agent_page() -> str:
    return _AGENT_PAGE
