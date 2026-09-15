"""Realtime reconciliation agent.

An accountant drops the usual file set into a folder (or uploads it to the
API) and the agent:

  1. auto-detects which file is which (GSTR-2B workbook / IMS B2B csv /
     Tally purchase register / Tally credit-note register) by name + content;
  2. normalises them with the official-file adapters (incl. joining portal
     values onto books registers that carry no amount columns);
  3. runs the full reconciliation pipeline (Sarvam-105B judgement layer when
     configured) and writes the interactive HTML report + artifacts;
  4. reports progress via a job store so a browser can poll in realtime.

Deterministic matching / arithmetic stay in the Tier-A engine; the LLM layer
is used only for narration and explanation (guarded by the verifier).
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .adapters.official_files import (
    core_inv,
    join_portal_values,
    parse_gstr2b_workbook,
    parse_ims_csv,
    parse_tally_credit_notes,
    parse_tally_purchase_register,
    write_engine_inputs,
)
from .agent import _infer_period, _load_books, _load_portal, _mtime_sources
from .config import Settings
from .engine import ReconciliationEngine
from .htmlreport import write_html
from .prepost_report import (
    build_post_state,
    build_pre_state,
    write_csvs,
    write_excel,
    write_markdown,
)
from .review_queue import ReviewQueue
from .verification import verify_packet

ROLES = ("portal_2b", "portal_ims", "books_purchase", "books_credit")

_PORTAL_KEYWORDS = ("gstr2b", "gstr-2b", "gstr_2b", "2b_", "ims")
_BOOKS_KEYWORDS = ("purchase", "tally", "credit note", "debit note",
                   "credit_note", "debit_note", "input", "vendor")


def detect_role(filename: str) -> Optional[str]:
    """Classify an uploaded file name into an agent input role."""
    name = (filename or "").strip().lower()
    if not name:
        return None
    if "gstr" in name and "2b" in name:
        return "portal_ims" if "ims" in name else "portal_2b"
    if "ims" in name:
        return "portal_ims"
    if "credit note" in name or "debit note" in name:
        return "books_credit"
    if "purchase" in name or "tally" in name:
        return "books_purchase"
    return None


def sniff_role(path: Path) -> Optional[str]:
    """Content-level fallback when the file name is too generic."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        sheets = " ".join(wb.sheetnames)
        wb.close()
        low = sheets.lower()
        if ("read me" in low and "b2b" in low) or ("gstr" in low and "b2b" in low):
            return "portal_2b"
        if "debit note register" in low or "credit note register" in low:
            return "books_credit"
        if "purchase register" in low:
            return "books_purchase"
    except Exception:
        pass
    return None


@dataclass
class Job:
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    client_id: str = "client"
    period: str = ""
    created: float = field(default_factory=time.time)
    status: str = "queued"           # queued|running|done|error
    progress: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    _thread: Optional[threading.Thread] = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def start(self, files: dict[str, Path], client_id: str, period: str,
              output_tax: str, out_root: Path, llm: bool = True) -> Job:
        job = Job(client_id=client_id, period=period)

        def run() -> None:
            job.status = "running"
            try:
                job.result = build_report(
                    files=files, client_id=job.client_id, period=job.period,
                    output_tax=output_tax, out_root=out_root,
                    llm=llm,
                    on_progress=lambda m: setattr(job, "progress", m),
                )
                job.period = job.result.get("period", job.period)
                job.status = "done"
            except Exception as e:  # noqa: BLE001 - surfaced to the caller
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"

        t = threading.Thread(target=run, daemon=True)
        job._thread = t
        self._jobs[job.job_id] = job
        t.start()
        return job


def _ctl(invs: list[Any]) -> tuple[int, Decimal]:
    return (len(invs), sum((i.taxable_value + i.tax_amount for i in invs), Decimal("0")))


def build_report(files: dict[str, Path], client_id: str, period: str,
                 output_tax: str = "", out_root: Optional[Path] = None,
                 llm: bool = True, on_progress=None) -> dict:
    """Runs the full pipeline for a detected file set and returns a manifest.

    The outbox layout mirrors agent.auto_reconcile so the same
    /api/reports/... URLs work.
    """
    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    out_root = Path(out_root or (Path(__file__).parent.parent / "reports"))
    progress("reading GSTR-2B / IMS portal files")
    portal_rows: list[dict] = []
    if files.get("portal_2b"):
        portal_rows = parse_gstr2b_workbook(files["portal_2b"])
    if files.get("portal_ims"):
        ims_rows = parse_ims_csv(files["portal_ims"])
        if portal_rows:
            progress(f"portal: GSTR-2B workbook {len(portal_rows)} rows, "
                     f"IMS cross-check {len(ims_rows)} rows")
        else:
            portal_rows = ims_rows
    if not portal_rows:
        raise ValueError("no usable portal (GSTR-2B) data found")

    book_rows: list[dict] = []
    if files.get("books_purchase"):
        book_rows = parse_tally_purchase_register(files["books_purchase"])
    if files.get("books_credit"):
        book_rows += parse_tally_credit_notes(files["books_credit"])
    if not book_rows:
        raise ValueError("no usable books (Tally) data found")

    progress(f"normalising inputs: {len(portal_rows)} portal, {len(book_rows)} books")
    book_rows = join_portal_values(book_rows, portal_rows)

    work = out_root / "_work"
    portal_json, books_csv = write_engine_inputs(portal_rows, book_rows, work)

    portal = _load_portal(portal_json)
    books = _load_books(books_csv)
    if not period:
        period = _infer_period(portal)
    if not output_tax:
        output_tax = str(int(sum(float(_d(i.igst)) + float(_d(i.cgst))
                                 + float(_d(i.sgst)) for i in portal)))

    settings = Settings()
    engine = ReconciliationEngine(settings)
    progress(f"engine: running matching + ITC + F5 (Sarvam={engine.sarvam.available()}, LLM={llm})")

    async def _run_async():
        kwargs = dict(
            client_id=client_id, period=period,
            portal_invoices=portal, book_invoices=books,
            portal_ct=_ctl(portal), tally_ct=_ctl(books),
            output_tax=output_tax,
        )
        if not llm:
            return engine.run_sync(**kwargs)
        return await engine.run(**kwargs)

    packet = asyncio.run(_run_async())
    progress("engine: writing reports + verification")
    pre = build_pre_state(portal, books, [])
    post = build_post_state(packet)

    out_dir = (out_root / client_id / period)
    write_markdown(pre, post, out_dir=out_dir)
    write_excel(pre, post, packet, out_dir=out_dir)
    write_csvs(pre, post, out_dir=out_dir)
    verification = verify_packet(
        packet, portal_path=portal_json, books_path=books_csv,
        report_md=out_dir / "pre_vs_post_reconciliation.md")
    review = ReviewQueue.from_packet(packet)
    review.save(out_dir / "review_queue.json")
    html_path = out_dir / "gst_2b_reconciliation_report.html"
    write_html(pre, post, packet, verification=verification,
               out_path=html_path,
               sources=_mtime_sources(portal_json, books_csv, None),
               review=review)

    def url(name: str) -> str:
        return f"/api/reports/{client_id}/{period}/{name}"

    progress("done")
    return {
        "client_id": client_id,
        "period": period,
        "counts": {"portal": len(portal_rows), "books": len(book_rows),
                   "llm_used": bool(settings.llm_enabled and settings.has_api_key)},
        "verification": verification,
        "f5": packet.validation,
        "llm": llm,
        "reports": {
            "html": url("gst_2b_reconciliation_report.html"),
            "markdown": url("pre_vs_post_reconciliation.md"),
            "excel": url("Pre_Post_Reconciliation_Report.xlsx"),
            "csv_pre": url("pre_recon_open_items.csv"),
            "csv_post": url("post_recon_exceptions.csv"),
            "review_queue": url("review_queue.json"),
        },
        "report_path": str(html_path),
        "llm_used": bool(settings.llm_enabled and settings.has_api_key),
    }


def _d(x) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")
