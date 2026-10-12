"""Run the v2 pipeline for one upload: triage, then extraction.

The review screen wants one action — "read this file" — not two, and it must
survive the browser being closed halfway. So the two passes are CHAINED inside
the triage worker thread: when a triage run finishes, the extraction for the
documents it found starts on that same persisted worker (``triage._EXECUTOR``).
Nothing is polled from a websocket and nothing runs in a request handler.

What the chain will NOT do:

* It will not extract when triage found nothing extractable. A run whose
  documents are all tickets or vouchers ends as triage-only, with the reason
  recorded in the returned summary rather than an empty extraction pass.
* It will not start a second pass while one is unfinished — ``start_extraction``
  refuses, and the chain reports that instead of forcing past it.
* It will not bypass the cost cap. ``start_extraction`` reads
  ``invoice_extract.v2.max_cost_usd`` and the extraction job stops when it is
  reached; the chain only reports the cap it was given.

Both flags are respected: ``invoice_extract.triage.enabled`` for the triage leg
and ``invoice_extract.v2.enabled`` for the extraction leg. The CLI passes
``include_disabled=True`` so the scoring harness can drive a disabled database.

CLI::

    python -m src.invoice_extract.pipeline start <upload_id> [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from typing import Any, Optional

from src.invoice_extract.service import InvoiceExtractError

#: run_id -> {"upload_id", "actor", "db_path", "include_disabled"}.
#: Registered by ``start_v2_pipeline`` BEFORE the triage run is queued, taken by
#: ``run_chain`` when the worker finishes that run. Process-local by design: a
#: restart loses the chain, and the resume paths (``resume_triage`` /
#: ``resume_extraction``) are what recover the work itself.
_CHAINS: dict[int, dict[str, Any]] = {}
_LOCK = threading.Lock()


def register_chain(
    run_id: int, upload_id: int, *, actor: str, db_path=None, include_disabled: bool = False,
) -> None:
    with _LOCK:
        _CHAINS[int(run_id)] = {
            "upload_id": int(upload_id), "actor": actor,
            "db_path": db_path, "include_disabled": bool(include_disabled),
        }


def take_chain(run_id: int) -> Optional[dict[str, Any]]:
    with _LOCK:
        return _CHAINS.pop(int(run_id), None)


def pending_chains() -> dict[int, dict[str, Any]]:
    """A copy of the registered chains — for tests and diagnostics."""
    with _LOCK:
        return dict(_CHAINS)


# ---------------------------------------------------------------------------
# The chain itself
# ---------------------------------------------------------------------------


def run_chain(run_id: int, *, actor: str = "pipeline", db_path=None) -> Optional[dict[str, Any]]:
    """Continue a finished triage run into extraction. A no-op when not chained.

    Called by ``triage._execute_run`` at the end of every run, so it must be
    cheap and harmless when nothing registered a chain: the first thing it does
    is look for one.
    """
    chain = take_chain(run_id)
    if chain is None:
        return None
    actor = chain.get("actor") or actor
    db_path = chain.get("db_path") or db_path
    upload_id = int(chain["upload_id"])
    include_disabled = bool(chain.get("include_disabled"))

    from src.invoice_extract import extract as extract_mod
    from src.invoice_extract import triage as triage_mod

    run = triage_mod.get_run(run_id, db_path=db_path) or {}
    if run.get("status") == triage_mod.RUN_FAILED:
        return {"upload_id": upload_id, "run_id": int(run_id), "started": False,
                "reason": f"triage run {run_id} failed — nothing to extract from"}

    info = triage_mod.get_triage(upload_id, db_path=db_path)
    extractable = [
        d for d in (info.get("documents") or [])
        if not d.get("excluded") and d.get("category") in extract_mod.TARGET_CATEGORIES
    ]
    if not extractable:
        return {
            "upload_id": upload_id, "run_id": int(run_id), "started": False,
            "reason": "triage found no extractable document (only tax_invoice and "
                      "no_gst_expense are extracted)",
            "documents": len(info.get("documents") or []),
        }

    try:
        summary = extract_mod.start_extraction(
            upload_id, actor=actor, db_path=db_path, force=include_disabled,
        )
    except InvoiceExtractError as exc:
        # e.g. a pass is still unfinished. Reported, never forced.
        return {"upload_id": upload_id, "run_id": int(run_id), "started": False,
                "reason": str(exc)}
    return {
        "upload_id": upload_id, "run_id": int(run_id), "started": True,
        "documents": len(extractable), "extraction": summary,
    }


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def start_v2_pipeline(
    upload_id: int, *, actor: str, include_disabled: bool = False, db_path=None,
) -> dict[str, Any]:
    """Queue triage for an upload and chain the extraction behind it.

    Returns as soon as the triage run is queued — the work happens on the
    persisted worker. Poll ``review_api.get_upload_progress(upload_id)`` (or the
    triage/extraction rows) for progress.
    """
    from src.invoice_extract import extract as extract_mod
    from src.invoice_extract import triage as triage_mod

    if not include_disabled:
        extract_mod.require_enabled(db_path)

    queued = triage_mod.run_triage(
        upload_id, actor=actor, db_path=db_path, include_disabled=include_disabled,
        force_supersede=False,
    )
    register_chain(
        int(queued["run_id"]), upload_id, actor=actor, db_path=db_path,
        include_disabled=include_disabled,
    )
    return {**queued, "chained": True, "actor": actor}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_argv(argv: Optional[list[str]] = None) -> list[str]:
    return list(sys.argv[1:] if argv is None else argv)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.invoice_extract.pipeline",
        description="Run triage then the v2 extraction for one upload.",
    )
    sub = parser.add_subparsers(dest="cmd")
    start_p = sub.add_parser("start", help="triage the upload, then extract its documents")
    start_p.add_argument("upload_id", type=int)
    start_p.add_argument("--db", default=None, help="database path (default: the app database)")
    start_p.add_argument(
        "--wait", action="store_true",
        help="block until the chained extraction finishes (for scripts; the app never waits)",
    )

    args = parser.parse_args(_cli_argv(argv))
    try:
        if args.cmd == "start":
            # The CLI is a harness: it drives a database whose flags may be off,
            # exactly like `triage run` and `extract score` already do.
            result = start_v2_pipeline(
                args.upload_id, actor="cli:pipeline", include_disabled=True, db_path=args.db,
            )
            print(json.dumps(result, indent=2, default=str))
            if args.wait:
                from src.invoice_extract import extract as extract_mod
                from src.invoice_extract import triage as triage_mod

                triage_mod.wait_for_run(int(result["run_id"]), db_path=args.db, timeout=3600)
                print(json.dumps(
                    {"extraction_counts": extract_mod.wait_for_extraction(
                        args.upload_id, db_path=args.db, timeout=3600)},
                    indent=2, default=str,
                ))
            return 0
        parser.print_help()
        return 0
    except InvoiceExtractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
