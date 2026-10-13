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

#: (database, run_id) -> {"upload_id", "actor", "db_path", "include_disabled"}.
#: Registered by ``start_v2_pipeline`` BEFORE the triage run is queued, taken by
#: ``run_chain`` when the worker finishes THAT run. Keyed by the database as well
#: as the run id, because run ids restart at 1 in every scratch database — and a
#: copy (the scoring harness makes several) would otherwise consume another
#: database's chain.
_CHAINS: dict[tuple[str, int], dict[str, Any]] = {}
_LOCK = threading.Lock()

#: (database, run_id) -> the last outcome ``run_chain`` returned. Kept so a
#: screen (or a test) can say WHY nothing was extracted instead of guessing from
#: an empty result.
_RESULTS: dict[tuple[str, int], dict[str, Any]] = {}


def _chain_key(db_path, run_id: int) -> tuple[str, int]:
    return (str(db_path) if db_path else "", int(run_id))


def last_chain_result(run_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    """What the chain did for this run, or None while it has not run yet."""
    with _LOCK:
        result = _RESULTS.get(_chain_key(db_path, run_id))
    return dict(result) if result is not None else None


def register_chain(
    run_id: int, upload_id: int, *, actor: str, db_path=None, include_disabled: bool = False,
) -> None:
    with _LOCK:
        _CHAINS[_chain_key(db_path, run_id)] = {
            "upload_id": int(upload_id), "actor": actor,
            "db_path": db_path, "include_disabled": bool(include_disabled),
        }


def take_chain(run_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    with _LOCK:
        return _CHAINS.pop(_chain_key(db_path, run_id), None)


def pending_chains() -> dict[int, dict[str, Any]]:
    """The registered chains keyed by run id — for tests and diagnostics."""
    with _LOCK:
        return {run_id: dict(payload) for (_db, run_id), payload in _CHAINS.items()}


# ---------------------------------------------------------------------------
# The chain itself
# ---------------------------------------------------------------------------


def run_chain(run_id: int, *, actor: str = "pipeline", db_path=None) -> Optional[dict[str, Any]]:
    """Continue a finished triage run into extraction. A no-op when not chained.

    Called by ``triage._execute_run`` at the end of every run, so it must be
    cheap and harmless when nothing registered a chain: the first thing it does
    is look for one.
    """
    chain = take_chain(run_id, db_path=db_path)
    if chain is None:
        return None
    try:
        return _run_chain(run_id, chain, db_path=db_path)
    finally:
        pass


def _run_chain(run_id: int, chain: dict[str, Any], *, db_path=None) -> dict[str, Any]:
    """The chain body. Its result is RECORDED before it is returned."""
    actor = chain.get("actor") or "pipeline"
    db_path = chain.get("db_path") or db_path
    upload_id = int(chain["upload_id"])
    include_disabled = bool(chain.get("include_disabled"))

    from src.invoice_extract import extract as extract_mod
    from src.invoice_extract import triage as triage_mod

    run = triage_mod.get_run(run_id, db_path=db_path) or {}
    if run.get("status") == triage_mod.RUN_FAILED:
        return _remember(run_id, db_path, {
            "upload_id": upload_id, "run_id": int(run_id), "started": False,
            "reason": f"triage run {run_id} failed — nothing to extract from",
        })

    info = triage_mod.get_triage(upload_id, db_path=db_path)
    extractable = [
        d for d in (info.get("documents") or [])
        if not d.get("excluded") and d.get("category") in extract_mod.TARGET_CATEGORIES
    ]
    if not extractable:
        return _remember(run_id, db_path, {
            "upload_id": upload_id, "run_id": int(run_id), "started": False,
            "reason": "triage found no extractable document (only tax_invoice and "
                      "no_gst_expense are extracted)",
            "documents": len(info.get("documents") or []),
        })

    try:
        summary = extract_mod.start_extraction(
            upload_id, actor=actor, db_path=db_path, force=include_disabled,
        )
    except InvoiceExtractError as exc:
        # e.g. a pass is still unfinished. Reported, never forced.
        return _remember(run_id, db_path, {
            "upload_id": upload_id, "run_id": int(run_id), "started": False,
            "reason": str(exc),
        })
    return _remember(run_id, db_path, {
        "upload_id": upload_id, "run_id": int(run_id), "started": True,
        "documents": len(extractable), "extraction": summary,
    })


def _remember(run_id: int, db_path, result: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        _RESULTS[_chain_key(db_path, run_id)] = dict(result)
    return result


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
