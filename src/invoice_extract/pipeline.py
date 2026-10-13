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
#: ``run_chain`` when the worker finishes that run. Process-local BY DESIGN —
#: the memory copy is only a hand-off; the STATE lives in the database (see
#: ``invoice_pipeline_chains`` below), which is why a restart is recoverable.
_CHAINS: dict[int, dict[str, Any]] = {}
_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# The chain's PERSISTED state
# ---------------------------------------------------------------------------
#
# The memory hand-off above cannot answer "has this upload finished?": after a
# restart it is empty, and between "triage run row finalised" and "extraction
# job started" there is a window in which NOTHING is running and NOTHING has
# been read — a poller that only looks at run rows and extraction rows sees a
# finished-looking upload. The window is small but it is real, and it made the
# chained-pipeline test fail 9 times in 10.

CHAIN_QUEUED = "queued"
CHAIN_TRIAGE_RUNNING = "triage_running"
CHAIN_TRIAGE_DONE = "triage_done_extraction_pending"
CHAIN_EXTRACTION_RUNNING = "extraction_running"
CHAIN_COMPLETE = "complete"
CHAIN_FAILED = "failed"
CHAIN_INTERRUPTED = "interrupted"

#: A chain in one of these is NOT finished, whatever the other tables say.
CHAIN_RUNNING_STATES = frozenset({
    CHAIN_QUEUED, CHAIN_TRIAGE_RUNNING, CHAIN_TRIAGE_DONE, CHAIN_EXTRACTION_RUNNING,
})
#: No further progress will happen without a person (resume) or a new run.
CHAIN_TERMINAL_STATES = frozenset({CHAIN_COMPLETE, CHAIN_FAILED, CHAIN_INTERRUPTED})

_CHAIN_DDL = """
CREATE TABLE IF NOT EXISTS invoice_pipeline_chains (
    run_id     INTEGER PRIMARY KEY,
    upload_id  INTEGER NOT NULL,
    state      TEXT    NOT NULL,
    actor      TEXT,
    detail     TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
)
"""


def chain_schema_statements() -> tuple[str, ...]:
    """The DDL, as statements for a caller's OWN connection.

    Migration 0007 executes these inside the framework's transaction; the
    runtime safety net below executes them on a connection of its own.
    """
    return (_CHAIN_DDL,)


def _chain_connect(db_path=None):
    from src import db as recon_db

    return recon_db.get_connection(db_path)


def _chain_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db_path=None) -> None:
    """Create ``invoice_pipeline_chains`` if it is not there yet.

    Idempotent and cheap: called on EVERY write path, so a database that missed
    the boot still gets the table the first time the pipeline touches it. Read
    paths tolerate its absence (a database that never ran the v2 pipeline has
    no chains at all).

    A SAFETY NET, not the front door: migration 0007 creates the same table
    through the migration framework, and this only covers a database that never
    ran the boot.
    """
    conn = _chain_connect(db_path)
    try:
        for statement in chain_schema_statements():
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def set_chain_state(
    run_id: int, state: str, *, upload_id: Optional[int] = None,
    actor: Optional[str] = None, detail: Optional[str] = None, db_path=None,
) -> None:
    """Write one transition. THE only writer of this table's state column."""
    ensure_schema(db_path)
    conn = _chain_connect(db_path)
    try:
        now = _chain_now()
        conn.execute(
            "INSERT INTO invoice_pipeline_chains "
            "(run_id, upload_id, state, actor, detail, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET state = excluded.state, "
            "actor = COALESCE(excluded.actor, actor), detail = excluded.detail, "
            "updated_at = excluded.updated_at",
            (int(run_id), int(upload_id or 0), state, actor, detail, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def chain_row(*, run_id: Optional[int] = None, upload_id: Optional[int] = None, db_path=None):
    """The chain row for a run (or the newest one for an upload), or None."""
    conn = _chain_connect(db_path)
    try:
        try:
            if run_id is not None:
                cur = conn.execute(
                    "SELECT * FROM invoice_pipeline_chains WHERE run_id = ?", (int(run_id),)
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM invoice_pipeline_chains WHERE upload_id = ? "
                    "ORDER BY run_id DESC LIMIT 1", (int(upload_id or 0),),
                )
            row = cur.fetchone()
            return {c[0]: row[i] for i, c in enumerate(cur.description)} if row else None
        except Exception:  # noqa: BLE001 - the table may not exist yet
            return None
    finally:
        conn.close()


def chain_state(*, run_id: Optional[int] = None, upload_id: Optional[int] = None,
                db_path=None, promote: bool = True) -> Optional[str]:
    """The chain's state, self-healed from the tables the work actually writes.

    ``extraction_running`` has no writer of its own: the extraction job ends on
    a worker thread, so the transition to ``complete``/``failed`` is made HERE,
    from the extraction rows, the first time anyone asks — and persisted, so
    every later reader sees the same answer.
    """
    row = chain_row(run_id=run_id, upload_id=upload_id, db_path=db_path)
    if row is None:
        return None
    state = str(row["state"])
    if promote and state == CHAIN_EXTRACTION_RUNNING:
        from src.invoice_extract import extract as extract_mod

        target = int(row["run_id"])
        try:
            counts = extract_mod.extraction_status_counts(int(row["upload_id"]), db_path=db_path)
        except Exception:  # noqa: BLE001
            return state
        total = sum(int(v) for v in counts.values())
        unfinished = sum(int(counts.get(name, 0)) for name in ("queued", "running"))
        if total and not unfinished:
            failed = sum(int(counts.get(name, 0)) for name in ("failed",))
            new_state = CHAIN_FAILED if failed == total else CHAIN_COMPLETE
            set_chain_state(target, new_state, upload_id=int(row["upload_id"]), db_path=db_path,
                            detail=f"extraction finished: {counts}")
            return new_state
    return state


def recover_interrupted_chains(db_path=None) -> list[int]:
    """Mark chains left RUNNING by a previous process as ``interrupted``.

    Called on BOOT. An interrupted chain is a state a person can act on — not
    complete (nothing was read) and not failed (nothing went wrong): the work
    simply stopped, and ``resume_pipeline`` continues the unfinished pieces.
    """
    ensure_schema(db_path)
    conn = _chain_connect(db_path)
    try:
        placeholders = ",".join("?" for _ in CHAIN_RUNNING_STATES)
        rows = conn.execute(
            f"SELECT run_id FROM invoice_pipeline_chains WHERE state IN ({placeholders})",
            tuple(CHAIN_RUNNING_STATES),
        ).fetchall()
        run_ids = [int(r[0]) for r in rows]
        if run_ids:
            conn.execute(
                f"UPDATE invoice_pipeline_chains SET state = ?, updated_at = ?, "
                f"detail = 'the app restarted while this was running' "
                f"WHERE state IN ({placeholders})",
                (CHAIN_INTERRUPTED, _chain_now(), *CHAIN_RUNNING_STATES),
            )
            conn.commit()
        return run_ids
    finally:
        conn.close()


def wait_for_chain(run_id: int, *, db_path=None, timeout: float = 300.0, poll: float = 0.05) -> Optional[str]:
    """Block until a chain reaches a terminal state (or the timeout)."""
    import time as _time

    deadline = _time.monotonic() + max(1.0, float(timeout))
    while True:
        state = chain_state(run_id=run_id, db_path=db_path)
        if state in CHAIN_TERMINAL_STATES or state is None:
            return state
        if _time.monotonic() > deadline:
            return state
        _time.sleep(max(0.01, float(poll)))


def resume_pipeline(upload_id: int, *, actor: str, db_path=None) -> dict[str, Any]:
    """Continue an interrupted (or failed) chain — only its unfinished pieces.

    * triage stopped part-way → ``resume_triage`` re-runs just its unfinished
      pages, and the chain is re-registered so the extraction still follows;
    * triage finished but the extraction never started (or stopped) →
      ``start_extraction`` picks up the documents that have no pass yet.
    """
    row = chain_row(upload_id=upload_id, db_path=db_path)
    state = chain_state(upload_id=upload_id, db_path=db_path) if row else None
    if row is None:
        raise InvoiceExtractError(f"Upload {upload_id} has no pipeline chain to resume.")
    if state not in (CHAIN_INTERRUPTED, CHAIN_FAILED):
        return {"upload_id": int(upload_id), "resumed": False, "state": state,
                "reason": "this chain is not interrupted"}

    from src.invoice_extract import extract as extract_mod
    from src.invoice_extract import triage as triage_mod

    run_id = int(row["run_id"])
    run = triage_mod.get_run(run_id, db_path=db_path) or {}
    pages_failed = int(run.get("pages_failed") or 0)
    if run and run.get("status") != triage_mod.RUN_COMPLETE and (
        pages_failed or int(run.get("pages_ok") or 0) > 0
    ):
        triage_mod.resume_triage(run_id, actor=actor, db_path=db_path, include_disabled=True)
        register_chain(run_id, upload_id, actor=actor, db_path=db_path, include_disabled=True)
        set_chain_state(run_id, CHAIN_TRIAGE_RUNNING, upload_id=int(upload_id), actor=actor,
                        detail="resumed by a person", db_path=db_path)
        return {"upload_id": int(upload_id), "resumed": True, "leg": "triage", "run_id": run_id}

    summary = extract_mod.start_extraction(upload_id, actor=actor, db_path=db_path, force=True)
    set_chain_state(run_id, CHAIN_EXTRACTION_RUNNING, upload_id=int(upload_id), actor=actor,
                    detail="resumed by a person", db_path=db_path)
    return {"upload_id": int(upload_id), "resumed": True, "leg": "extraction",
            "run_id": run_id, "extraction": summary}


def register_chain(
    run_id: int, upload_id: int, *, actor: str, db_path=None, include_disabled: bool = False,
) -> None:
    with _LOCK:
        _CHAINS[int(run_id)] = {
            "upload_id": int(upload_id), "actor": actor,
            "db_path": db_path, "include_disabled": bool(include_disabled),
        }
    # The PERSISTED half of the hand-off: this row is what survives a restart
    # and what every poller reads. Written here so a chain exists on disk from
    # the moment the run is queued.
    set_chain_state(run_id, CHAIN_QUEUED, upload_id=int(upload_id), actor=actor,
                    detail="triage queued", db_path=db_path)


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
    # Triage is DONE and the extraction has NOT started: the state a poller must
    # never mistake for a finished upload.
    set_chain_state(run_id, CHAIN_TRIAGE_DONE, upload_id=upload_id, actor=actor,
                    detail="triage finished — extraction follows", db_path=db_path)
    if run.get("status") == triage_mod.RUN_FAILED:
        set_chain_state(run_id, CHAIN_FAILED, upload_id=upload_id, actor=actor,
                        detail=f"triage run {run_id} failed — nothing to extract from",
                        db_path=db_path)
        return {"upload_id": upload_id, "run_id": int(run_id), "started": False,
                "reason": f"triage run {run_id} failed — nothing to extract from"}

    info = triage_mod.get_triage(upload_id, db_path=db_path)
    extractable = [
        d for d in (info.get("documents") or [])
        if not d.get("excluded") and d.get("category") in extract_mod.TARGET_CATEGORIES
    ]
    if not extractable:
        set_chain_state(run_id, CHAIN_COMPLETE, upload_id=upload_id, actor=actor,
                        detail="triage-only: no extractable document", db_path=db_path)
        return {
            "upload_id": upload_id, "run_id": int(run_id), "started": False,
            "reason": "triage found no extractable document (only tax_invoice and "
                      "no_gst_expense are extracted)",
            "documents": len(info.get("documents") or []),
        }

    set_chain_state(run_id, CHAIN_EXTRACTION_RUNNING, upload_id=upload_id, actor=actor,
                    detail="extraction starting", db_path=db_path)
    try:
        summary = extract_mod.start_extraction(
            upload_id, actor=actor, db_path=db_path, force=include_disabled,
        )
    except InvoiceExtractError as exc:
        # e.g. a pass is still unfinished — the extraction IS running, which is
        # what the chain state must say; anything else is a failure.
        unfinished = "unfinish" in str(exc).lower()
        set_chain_state(
            run_id, CHAIN_EXTRACTION_RUNNING if unfinished else CHAIN_FAILED,
            upload_id=upload_id, actor=actor, detail=str(exc), db_path=db_path,
        )
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
    start_p.add_argument("--allow-real-db", action="store_true", help="allow writing the repository's own db/poc.db")
    start_p.add_argument(
        "--wait", action="store_true",
        help="block until the chained extraction finishes (for scripts; the app never waits)",
    )

    args = parser.parse_args(_cli_argv(argv))
    from src import db as _db
    _db.require_real_db_allowed(
        getattr(args, "db", None), allow_real_db=getattr(args, "allow_real_db", False),
    )
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
