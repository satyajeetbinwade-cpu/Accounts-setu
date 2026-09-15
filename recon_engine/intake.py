"""Document intake layer: classify incoming files, infer client/period, scan
folders and assemble the inputs for a reconciliation run.

Two-layer design, consistent with the rest of the engine:

  * Tier-A deterministic structural detection (file extension, sheet names,
    header-token signatures, invoice content) -- always available, no LLM.
  * Sarvam-105B assist for AMBIGUOUS files only -- when structural detection
    cannot decide the document type, or client/period inference is empty.
    The model proposes a classification; intake validates it against known
    types and falls back to the deterministic result on any failure
    (graceful degradation). The LLM never touches matching or arithmetic.

PRD anchors: Module F3 (auto-filing/classification with low-confidence
fallback to a human queue), Module 2 (manual/batch ingestion), F5
(validation at the door).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Settings
from .parsers import (
    parse_gstr2b_excel,
    parse_gstr2b_json,
    parse_tally_excel,
    parse_tally_purchase_rows,
)
from .parsers.excel_common import locate_header, sheet_names
from .parsers.gstr2b_excel import B2B_ALIASES
from .parsers.tally_excel import TALLY_ALIASES
from .sarvam import SarvamClient

# Document kinds the intake understands
KINDS = (
    "gstr2b_json", "gstr2b_excel",
    "tally_csv", "tally_excel",
    "trial_balance",
    "unknown",
)
SOURCE_BY_KIND = {
    "gstr2b_json": "portal", "gstr2b_excel": "portal",
    "tally_csv": "books", "tally_excel": "books",
    "trial_balance": "context",
}

_ALLOWED_LLM_TYPES = set(KINDS)

# ---------------------------------------------------------------------------
# Filename heuristics (client / period inference) -- Tier-A
# ---------------------------------------------------------------------------
_DOC_STOP_WORDS = {
    "gstr2b", "gstr2", "gstr", "2b", "gst", "gst2b",
    "tally", "purchase", "register", "registerxlsx", "sales", "salesregister",
    "tb", "trial", "balance", "trialbalance", "books", "portal",
    "excel", "xlsx", "csv", "json", "sample", "full", "final", "finalised",
    "v1", "v2", "v3", "updated", "update", "export", "report", "download",
    "data", "file", "doc", "sheet", "copy",
    "aug", "sep", "oct", "nov", "dec", "jan", "feb", "mar", "apr", "may",
    "jun", "jul",
    "aug24", "sep24", "oct24",
    "2024", "2025", "2023", "202408", "202409", "202410", "08", "09", "10",
    "fy", "fy24", "fy25", "fy2425", "q1", "q2", "q3", "q4",
}
_MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_PERIOD_PATTERNS = [
    re.compile(r"(20\d{2})[-_. ]?(\d{2})$"),                 # 2024-08 / 2024_08
    re.compile(r"(?:^|[-_. ])(20\d{2})[-_. ]?(\d{2})(?:$|[-_. ])"),
    re.compile(r"(20\d{2})(\d{2})$"),                        # 202408
    re.compile(r"(20\d{2})[-_. ]?(\d{2})"),                  # loose
    re.compile(r"(?:^|[-_. ])([A-Za-z]{3})[-_. ]?(20)?(\d{2})(?:$|[-_. ])"),  # Aug-24
    re.compile(r"fy[-_. ]?(?:20)?(\d{2})[-_. ]?(\d{2})", re.IGNORECASE),      # FY24-25
]


def period_from_name(stem: str) -> tuple[str, str]:
    """Return (period, note) inferred from a filename stem. '' if none."""
    low = stem.lower()
    for pat in _PERIOD_PATTERNS[:-1]:
        m = pat.search(stem)
        if m:
            y, mo = m.group(1), m.group(2)
            return f"{y}-{int(mo):02d}", "filename"
    m = _PERIOD_PATTERNS[-1].search(low)
    if m:
        y1, y2 = m.group(1), m.group(2)
        return f"20{y1}-04", "filename-fy"  # FY start (Apr)
    m = re.search(r"(?:^|[-_. ])([A-Za-z]{3})[-_. ]?(20)?(\d{2})(?:$|[-_. ])", stem)
    if m:
        mo = _MONTH_NUM.get(m.group(1).lower())
        yy = m.group(3) if m.group(2) is None else m.group(2)
        year = 2000 + int(yy) if int(yy) > 50 else 2000 + int(yy)
        year = (2000 + int(yy)) if len(yy) == 2 else int(yy)
        if mo:
            return f"{year}-{mo:02d}", "filename"
    return "", ""


def client_from_name(stem: str) -> tuple[str, str]:
    """Return (client, note) inferred from a filename stem. '' if none."""
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", stem) if t]
    keep = [t for t in toks if t.lower() not in _DOC_STOP_WORDS]
    if not keep:
        return "", ""
    # "RESERVOIR_DIGITAL_GSTR2B_Aug24" -> join leading meaningful tokens
    client = keep[0].lower()
    add = [t.lower() for t in keep[1:3]
           if not re.fullmatch(r"\d{2,4}", t)]
    return "_".join([client] + add), "filename"


def period_from_invoices(invoices) -> tuple[str, str]:
    dates = [i.date for i in invoices if i.date]
    if not dates:
        return "", ""
    d = max(dates)
    return f"{d.year}-{d.month:02d}", "content"


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
@dataclass
class DetectedFile:
    path: Path
    kind: str = "unknown"
    source: str = ""
    client: str = ""
    period: str = ""
    period_note: str = ""
    confidence: str = "low"          # high | medium | low
    method: str = "structural"       # structural | llm_assist | needs_review
    issues: list[str] = field(default_factory=list)
    preview: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.kind != "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path), "kind": self.kind, "source": self.source,
            "client": self.client, "period": self.period,
            "period_note": self.period_note, "confidence": self.confidence,
            "method": self.method, "issues": self.issues,
            "preview": self.preview,
        }


@dataclass
class IntakeScan:
    files: list[DetectedFile] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[DetectedFile]:
        return [f for f in self.files if f.kind == kind]

    @property
    def portal_candidates(self) -> list[DetectedFile]:
        return [f for f in self.files if f.source == "portal"]

    @property
    def books_candidates(self) -> list[DetectedFile]:
        return [f for f in self.files if f.source == "books"]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for f in self.files:
            counts[f.kind] = counts.get(f.kind, 0) + 1
        return {
            "files": len(self.files),
            "by_kind": counts,
            "unknown": len(self.by_kind("unknown")),
            "portal": [str(f.path) for f in self.portal_candidates],
            "books": [str(f.path) for f in self.books_candidates],
            "issues": self.issues,
        }


# ---------------------------------------------------------------------------
# Structural detection (Tier-A)
# ---------------------------------------------------------------------------
def _detect(path: Path) -> tuple[str, dict[str, Any]]:
    """Return (kind, signals) by structural inspection. Never raises."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            invs = parse_gstr2b_json(path)
        except Exception as e:
            return "unknown", {"error": str(e)}
        if invs:
            return "gstr2b_json", {"invoices": len(invs)}
        return "unknown", {"error": "json parsed but no invoices"}

    if suffix == ".csv":
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
        except Exception as e:
            return "unknown", {"error": str(e)}
        low = head.lower()
        if "gstin" in low and ("voucher" in low or "taxable" in low
                               or "invoice" in low):
            return "tally_csv", {"header_sample": low.splitlines()[0][:120]
                                 if low.splitlines() else ""}
        return "unknown", {"error": "csv header not recognised"}

    if suffix in (".xlsx", ".xlsm"):
        try:
            sheets = sheet_names(path)
        except Exception as e:
            return "unknown", {"error": f"not a valid workbook: {e}"}
        try:
            from .parsers.excel_common import load_workbook
            wb = load_workbook(path)
            try:
                active = wb.active
                b2b_map: dict[str, int] = {}
                tally_map: dict[str, int] = {}
                for ws in wb.worksheets:
                    if not b2b_map:
                        _, b2b_map = locate_header(ws, B2B_ALIASES)
                    if not tally_map:
                        _, tally_map = locate_header(ws, TALLY_ALIASES)
                    if b2b_map and tally_map:
                        break
                looks_like_b2b_sheet = any(
                    re.match(r"^(b2b|b2ba)$", s.strip(), re.I) for s in sheets)
                has_b2b_cols = ("invoice_no" in b2b_map and
                                ("gstin" in b2b_map or "taxable" in b2b_map))
                has_tally_cols = ("taxable" in tally_map and
                                  ("gstin" in tally_map or "tax" in tally_map or
                                   "voucher_no" in tally_map))
                # Trial balance signature (Particulars / Opening / Closing)
                tb_sign = False
                for ws in wb.worksheets[:5]:
                    h, _m = locate_header(
                        ws, {"particulars": ("particulars",)}, min_score=1)
                    if h is not None:
                        tb_sign = True
                        break

                if looks_like_b2b_sheet or has_b2b_cols:
                    return "gstr2b_excel", {"sheets": sheets,
                                            "b2b_cols": sorted(b2b_map)}
                if has_tally_cols:
                    return "tally_excel", {"sheets": sheets,
                                           "tally_cols": sorted(tally_map)}
                if tb_sign:
                    return "trial_balance", {"sheets": sheets}
            finally:
                wb.close()
        except Exception as e:
            return "unknown", {"error": str(e)}
        return "unknown", {"sheets": sheets}

    return "unknown", {"unsupported_extension": suffix}


# ---------------------------------------------------------------------------
# Sarvam-105B assist (ambiguous files only)
# ---------------------------------------------------------------------------
async def _llm_assist(
    path: Path,
    signals: dict[str, Any],
    settings: Settings,
    llm_client: SarvamClient | None = None,
) -> dict[str, Any] | None:
    """Ask Sarvam-105B to classify an ambiguous file. Returns parsed dict or
    None on any failure (never raises)."""
    try:
        client = llm_client or SarvamClient(settings)
        if not client.available():
            return None
        prompt = (
            "You are the document-intake classifier for a GST reconciliation "
            "platform. Classify the uploaded file below and return STRICT JSON "
            "with exactly these keys: "
            '{"document_type": "...", "client": "...", "period": "...", '
            '"confidence": "high|medium|low"}\n\n'
            "document_type must be one of: gstr2b_excel, gstr2b_json, "
            "tally_excel, tally_csv, trial_balance, unknown.\n"
            "period format: YYYY-MM ('' if not inferable). client: short "
            "lowercase slug ('' if unknown).\n\n"
            f"filename: {path.name}\n"
            f"signals: {json.dumps(signals, default=str)[:800]}\n"
        )
        reply = await client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=200,
        )
        text = reply.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        if not isinstance(data, dict) or "document_type" not in data:
            return None
        data["document_type"] = str(data["document_type"]).strip().lower()
        return data
    except Exception:
        return None


def _run_coro(coro):
    """Run an async coroutine from a sync context (used for LLM assist)."""
    import asyncio
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # already inside an event loop (e.g. future async server) -> new loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def classify_file(
    path: str | Path,
    settings: Settings | None = None,
    use_llm: bool = True,
    llm_client: SarvamClient | None = None,
) -> DetectedFile:
    """Classify a single file: deterministic detection + optional LLM assist."""
    path = Path(path)
    settings = settings or Settings()
    kind, signals = _detect(path)
    conf = "high" if kind != "unknown" else "low"

    stem = path.stem
    client, _cnote = client_from_name(stem)
    period, pnote = period_from_name(stem)

    d = DetectedFile(
        path=path, kind=kind, source=SOURCE_BY_KIND.get(kind, ""),
        client=client, period=period, period_note=pnote,
        confidence=conf, method="structural", preview=signals,
    )

    # LLM assist ONLY where structural detection is ambiguous.
    ambiguous = (kind == "unknown") or (not client and not period)
    if use_llm and ambiguous:
        data = _run_coro(_llm_assist(path, signals, settings, llm_client))
        if data and data.get("document_type") in _ALLOWED_LLM_TYPES:
            if kind == "unknown" or data["document_type"] != "unknown":
                d.kind = data["document_type"]
                d.source = SOURCE_BY_KIND.get(d.kind, "")
                d.method = "llm_assist"
                d.confidence = str(data.get("confidence", "medium"))
                if data.get("client") and not d.client:
                    d.client = str(data["client"]).lower()[:40]
                if data.get("period") and not d.period:
                    d.period = re.sub(r"[^0-9-]", "", str(data["period"]))[:10]
                d.issues.append("classified via Sarvam-105B assist")
        elif data is not None and data.get("document_type") not in _ALLOWED_LLM_TYPES:
            d.issues.append(f"llm proposed unknown type {data['document_type']!r}; "
                            "kept deterministic result")

    # Content-based period fallback (portal & books kinds)
    if not d.period:
        try:
            if d.kind == "gstr2b_json":
                period, note = period_from_invoices(parse_gstr2b_json(path))
            elif d.kind == "gstr2b_excel":
                period, note = period_from_invoices(parse_gstr2b_excel(path))
            elif d.kind == "tally_excel":
                period, note = period_from_invoices(parse_tally_excel(path))
            else:
                period, note = "", ""
            if period:
                d.period, d.period_note = period, note
        except Exception:
            pass

    if d.kind == "unknown":
        d.method = "needs_review" if d.method != "llm_assist" else d.method
        if not any("llm" in i for i in d.issues):
            d.issues.append("unrecognised - routes to human 'unfiled/needs "
                            "review' queue (PRD F3)")
    return d


def scan_folder(
    folder: str | Path,
    settings: Settings | None = None,
    use_llm: bool = True,
    patterns: tuple[str, ...] = ("*.xlsx", "*.xlsm", "*.json", "*.csv"),
) -> IntakeScan:
    """Scan a folder of incoming documents and classify every file."""
    folder = Path(folder)
    scan = IntakeScan()
    if not folder.exists():
        scan.issues.append(f"folder does not exist: {folder}")
        return scan
    files = [p for pat in patterns for p in sorted(folder.glob(pat))]
    for p in files:
        scan.files.append(classify_file(p, settings=settings, use_llm=use_llm))
    return scan


def build_run_inputs(
    scan: IntakeScan,
    client_override: str = "",
    period_override: str = "",
    output_tax: str | Decimal = "0",
) -> dict[str, Any]:
    """Pick the portal/books/tb files from a scan and assemble run inputs."""
    portal = (scan.by_kind("gstr2b_json") + scan.by_kind("gstr2b_excel"))
    books = (scan.by_kind("tally_excel") + scan.by_kind("tally_csv"))
    tb = scan.by_kind("trial_balance")

    issues = list(scan.issues)
    if not portal:
        issues.append("missing GSTR-2B input (json or excel)")
    if not books:
        issues.append("missing Tally purchase register (excel or csv)")

    clients = [f.client for f in scan.files if f.client]
    periods = [f.period for f in scan.files if f.period]
    return {
        "gstr2b_path": portal[0].path if portal else None,
        "tally_path": books[0].path if books else None,
        "tb_path": tb[0].path if tb else None,
        "client_id": client_override or (clients[0] if clients
                                         else "client_001"),
        "period": period_override or (periods[0] if periods else ""),
        "output_tax": output_tax,
        "issues": issues,
        "ready": bool(portal) and bool(books),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Scan an inbox folder and classify GST/Tally documents "
                    "(Excel-first intake).")
    ap.add_argument("folder", help="folder containing incoming documents")
    ap.add_argument("--no-llm", action="store_true",
                    help="disable Sarvam-105B assist (deterministic only)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="print machine-readable scan summary")
    args = ap.parse_args(argv)

    settings = Settings()
    scan = scan_folder(args.folder, settings=settings,
                       use_llm=not args.no_llm)
    if args.as_json:
        print(json.dumps({
            "summary": scan.summary(),
            "files": [f.to_dict() for f in scan.files],
        }, indent=2))
        return 0

    print(f"INTAKE SCAN  |  {Path(args.folder).resolve()}")
    print("=" * 100)
    hdr = f"{'file':46} {'kind':16} {'client':14} {'period':10} {'conf':7} {'method':12} issues"
    print(hdr)
    print("-" * 100)
    for f in scan.files:
        print(f"{f.path.name:46} {f.kind:16} {f.client or '-':14} "
              f"{f.period or '-':10} {f.confidence:7} {f.method:12} "
              f"{'; '.join(f.issues)[:40]}")
    print("-" * 100)
    s = scan.summary()
    print(f"  {s['files']} file(s)  |  portal: {len(s['portal'])}  "
          f"books: {len(s['books'])}  unknown: {s['unknown']}")
    run = build_run_inputs(scan)
    print(f"  ready to reconcile: {run['ready']}  "
          f"client={run['client_id']}  period={run['period'] or '?'}")
    for i in run["issues"]:
        print(f"  ! {i}")
    if settings.has_api_key:
        print("  LLM assist: enabled (Sarvam-105B)")
    else:
        print("  LLM assist: disabled (no SARVAM_API_KEY) - deterministic only")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
