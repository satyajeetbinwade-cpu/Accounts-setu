"""Phase-4 batch runner: month-end close over many clients.

Point the runner at an inbox folder laid out as:

    inbox/
      <client_id>/
        gstr2b.json           (or gstr2b.xlsx)
        tally.csv             (or tally.xlsx)
        tb.xlsx               (optional trial balance)
        config.json           (optional: {match_rules: {...}})

For each completed client folder the runner runs auto_reconcile with a
per-client reports_dir (reports/<client_id>/<period>/), remembers the
verification/F5 gate, and writes a firm-wide aggregate `batch_summary.json`.

Exit code contract (same as the agent):
    0  all clients PASS (F5 + verification)
    1  one or more clients FAILED/BLOCKED
    2  inbox missing / no client folders

Usage:
    python -m recon_engine.batch --inbox ./inbox [--output-tax 120000]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import auto_reconcile
from .config import Settings
from .htmlreport import write_html as _write_html

HERE = Path(__file__).resolve().parent
REPORTS_DIR = HERE.parent / "reports"


def discover(inbox: Path) -> list[Path]:
    """Client folders = immediate subdirectories of the inbox."""
    if not inbox.exists():
        return []
    return sorted(p for p in inbox.iterdir() if p.is_dir())


def _slurp_config(folder: Path) -> dict | None:
    cfg = folder / "config.json"
    if cfg.exists():
        return json.loads(cfg.read_text(encoding="utf-8"))
    return None


def run_batch(inbox: str | Path, output_tax: str = "0",
              settings: Settings | None = None) -> dict[str, Any]:
    """Run every client folder in the inbox. Returns the aggregate result."""
    settings = settings or Settings()
    inbox = Path(inbox)
    clients = discover(inbox)
    results: list[dict[str, Any]] = []

    for folder in clients:
        client_id = folder.name
        g2b = next((folder / f for f in ("gstr2b.json", "gstr2b.xlsx",
                                         "gstr2b.xlsm") if (folder / f).exists()), None)
        tally = next((folder / f for f in ("tally.csv", "tally.xlsx",
                                           "tally.xlsm") if (folder / f).exists()), None)
        tb = next((folder / f for f in ("tb.xlsx", "tb.xlsm") if (folder / f).exists()), None)

        entry: dict[str, Any] = {
            "client_id": client_id,
            "ok": False,
            "error": "",
            "period": "",
            "matched": 0,
            "eligible_itc": "0",
            "net_payable": "0",
            "verification": False,
            "validation": False,
            "reports_dir": "",
        }

        if g2b is None or tally is None:
            entry["error"] = (f"missing inputs (found gstr2b={g2b.name if g2b else '-'}, "
                              f"tally={tally.name if tally else '-'})")
            entry["ok"] = False
            results.append(entry)
            continue

        try:
            res = auto_reconcile(
                gstr2b_path=g2b, tally_path=tally, tb_path=tb,
                client_id=client_id, period="",
                output_tax=output_tax,
                client_config=_slurp_config(folder),
                reports_dir=REPORTS_DIR / client_id,
            )
            pkt = res["packet"]
            entry.update(
                ok=True,
                period=pkt.period,
                matched=pkt.summary.get("matched", 0),
                eligible_itc=str(pkt.itc.eligible_itc),
                net_payable=str(pkt.itc.gstr3b_draft["net_payable"]),
                verification=bool(res["verification"].get("pass")),
                validation=bool(pkt.validation.get("pass")),
                reports_dir=str(REPORTS_DIR / client_id),
            )
        except Exception as e:  # per-client isolation - never abort the batch
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["ok"] = False
        results.append(entry)

    passed = sum(1 for r in results if r["ok"] and r["verification"] and r["validation"])
    failed = len(results) - passed
    portfolio = [
        {"client_id": r["client_id"], "period": r["period"],
         "eligible_itc": r["eligible_itc"], "net_payable": r["net_payable"],
         "verification": r["verification"], "validation": r["validation"]}
        for r in results
    ]
    aggregate = {
        "generated_at": datetime.utcnow().isoformat(),
        "inbox": str(inbox),
        "clients": len(results),
        "passed": passed,
        "failed": failed,
        "results": results,
        "portfolio": portfolio,
    }
    return aggregate


class _DummyPacket:
    """Minimal packet facade so write_html can render a firm-wide dashboard."""

    def __init__(self, agg: dict):
        self.client_id = "firm-wide"
        self.period = f"{len(agg['results'])} clients"
        self.stage = "batch"
        self.summary = {"matched": 0, "amount_diff": 0, "not_in_books": 0,
                        "not_in_portal": 0, "total_portal_invoices": 0}
        self.exceptions = []
        self.drafted_jvs = []
        self.ai_summary = ""
        self.itc = _DummyITC()
        self.validation = {"pass": agg["failed"] == 0}


class _DummyITC:
    eligible_itc = "0"
    ineligible_itc = "0"
    gstr3b_draft = {"credit_available": "0"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="recon_engine.batch",
        description="Month-end close: run reconciliation for every client inbox folder.",
    )
    ap.add_argument("--inbox", required=True, help="folder with <client>/ subfolders")
    ap.add_argument("--output-tax", default="0", help="output GST applied to all clients")
    ac = ap.add_argument_group("firm-wide")
    ac.add_argument("--summary", default=None,
                    help="optional path to write batch_summary.json")
    args = ap.parse_args(argv)

    inbox = Path(args.inbox)
    if not inbox.exists():
        print(f"ERROR: inbox not found: {inbox}", file=sys.stderr)
        return 2
    if not discover(inbox):
        print(f"ERROR: no client folders under {inbox}", file=sys.stderr)
        return 2

    agg = run_batch(inbox, output_tax=args.output_tax)

    # Firm-wide summary print
    print("=" * 74)
    print("FIRMWIDE MONTH-END BATCH  |  inbox=" + str(inbox))
    print("=" * 74)
    for r in agg["results"]:
        mark = "PASS" if (r["ok"] and r["verification"] and r["validation"]) else "FAIL"
        detail = (f"period={r['period']} matched={r['matched']} "
                  f"eligITC={r['eligible_itc']} netPay={r['net_payable']} "
                  f"verif={r['verification']} f5={r['validation']}")
        if r["error"]:
            detail = r["error"]
        print(f"  [{mark}] {r['client_id']:<16} {detail}")
    print("-" * 74)
    print(f"  clients={agg['clients']}  passed={agg['passed']}  failed={agg['failed']}")
    print("=" * 74)

    if args.summary:
        Path(args.summary).write_text(
            json.dumps(agg, indent=2), encoding="utf-8")
        print(f"  summary -> {args.summary}")

    # Firm-wide portfolio dashboard (M7) - aggregate view across clients
    portfolio_path = REPORTS_DIR / "firmwide_portfolio.html"
    try:
        _write_html(
            pre={}, post={}, packet=_DummyPacket(agg),
            out_path=portfolio_path, portfolio=agg["portfolio"])
        print(f"  portfolio -> {portfolio_path}")
    except Exception as e:
        print(f"  WARN portfolio dashboard: {e}", file=sys.stderr)

    return 0 if agg["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
