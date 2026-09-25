"""Run orchestration — connects ingestion, matching and persistence.

execute_run() is the single entry point: load config, load + normalize
sources (Unit 2), dispatch to the GST or TDS matcher (Units 3/4), fingerprint
every result, and persist the run + results as one atomic transaction.

Runs are immutable (see src/db.py docstring): every execution inserts a new
`runs` row and its own `match_results` rows. Nothing is ever updated in
place. Review status lives outside this table entirely, in `review_state`,
keyed by fingerprint — see src/queries.py for how it's carried forward.

TDS recon_type dispatches to all three TDS engines built in Unit 4
(deductor-side, deductee-side, and books-internal section-rate validation)
and combines their results, since a single books export can contain both
deductor and deductee transactions. GST dispatches to the single GST engine.
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src import db
from src.config_loader import CONFIG_PATH, load_config
from src.fingerprint import assign_fingerprints
from src.ingestion import load_gst_pair, load_other_pair, load_tds_pair
from src.ingestion_ai.normalizer import IngestionBlockedError, load_canonical_pair
from src.matching.gst_matcher import match_gst
from src.matching.other_matcher import match_other
from src.matching.tds_matcher import (
    match_tds_deductee,
    match_tds_deductor,
    validate_section_rates,
)

VALID_RECON_TYPES = ("GST", "TDS", "OTHER")

# When True, ingestion goes through the unified AI normalization layer
# (src/ingestion_ai/normalizer.py) so the engine only ever receives a
# canonical DataFrame. Set False only to fall back to the legacy
# config-driven loader for debugging.
USE_CANONICAL_INGESTION = True


class RunExecutionError(RuntimeError):
    """Raised when a run fails during ingestion or matching. The caller
    (execute_run) guarantees no partial run row or result rows persist
    when this is raised."""


def _run_gst(
    client: str,
    period: str,
    gst_config: dict,
    selected_files: dict[str, str] | None = None,
    *,
    client_id: int | None = None,
    actor: str = "system",
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    if USE_CANONICAL_INGESTION:
        books_df, portal_df, source_files, caveats, run_notes = load_canonical_pair(
            client, period, "GST", selected_files, client_id=client_id, actor=actor
        )
    else:
        books_df, portal_df = load_gst_pair(client, period, selected_files)
        source_files = sorted(set(books_df["source_file"]) | set(portal_df["source_file"]))
        caveats = []
        run_notes = []
    results = match_gst(books_df, portal_df, gst_config)
    # Invoice-number keys that collide within one GSTIN are REPORTED, never
    # silently used (D3) — surfaced as run notes.
    try:
        from src.matching.gst_matcher import invoice_key_collisions

        for c in invoice_key_collisions(books_df, portal_df, gst_config):
            run_notes.append({
                "code": "invoice_key_collision",
                "kind": "data_quality",
                "title": (
                    f"Invoice-number key '{c['key']}' collides within GSTIN "
                    f"{c['gstin']} on the {c['side']} side"
                ),
                "detail": (
                    f"These {c['side']}-side invoice numbers all normalise to the same key "
                    f"'{c['key']}': {', '.join(c['numbers'])}. The key cannot tell them apart, "
                    "so it was not used as an exact match for this supplier."
                ),
                "count": len(c["numbers"]),
            })
    except Exception:  # noqa: BLE001
        pass
    return results, source_files, caveats, run_notes


def _run_tds(
    client: str,
    period: str,
    tds_config: dict,
    selected_files: dict[str, str] | None = None,
    *,
    client_id: int | None = None,
    actor: str = "system",
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    if USE_CANONICAL_INGESTION:
        books_df, portal_df, source_files, caveats, run_notes = load_canonical_pair(
            client, period, "TDS", selected_files, client_id=client_id, actor=actor
        )
    else:
        books_df, portal_df = load_tds_pair(client, period, selected_files)
        source_files = sorted(set(books_df["source_file"]) | set(portal_df["source_file"]))
        caveats = []
        run_notes = []

    results: list[dict[str, Any]] = []
    results.extend(match_tds_deductor(books_df, portal_df, tds_config))
    results.extend(match_tds_deductee(books_df, portal_df, tds_config))
    results.extend(validate_section_rates(books_df, tds_config))
    return results, source_files, caveats, run_notes


def _run_other(
    client: str,
    period: str,
    other_config: dict,
    selected_files: dict[str, str] | None = None,
    *,
    client_id: int | None = None,
    actor: str = "system",
) -> tuple[list[dict[str, Any]], list[str]]:
    """2C — layer the six "Other" sources onto the SAME shared engine.

    The source_key is chosen from the selected files (the first 2C source
    that has a file), so a single 2C run reconciles one source at a time
    against the books side. Adding a seventh source is a configuration
    change (Module 2's MatchingKeyConfig), not a new code path here.
    """
    from src.module2.service import OTHER_SOURCES

    sel = selected_files or {}
    source_key = None
    for key, _label in OTHER_SOURCES:
        if sel.get(key):
            source_key = key
            break
    if source_key is None:
        # Fall back to the first 2C source that has a file on disk.
        for key, _label in OTHER_SOURCES:
            try:
                load_other_pair(client, period, key, selected_files)
                source_key = key
                break
            except FileNotFoundError:
                continue
    if source_key is None:
        raise FileNotFoundError(
            f"No 2C source file found for client={client!r} period={period!r}"
        )
    if USE_CANONICAL_INGESTION:
        books_df, portal_df, source_files, caveats, run_notes = load_canonical_pair(
            client, period, "OTHER", {**(selected_files or {}), "__source_key": source_key},
            client_id=client_id, actor=actor,
        )
    else:
        books_df, portal_df = load_other_pair(client, period, source_key, selected_files)
        source_files = sorted(set(books_df["source_file"]) | set(portal_df["source_file"]))
        caveats = []
        run_notes = []
    results = match_other(books_df, portal_df, other_config)
    return results, source_files, caveats, run_notes


def execute_run(
    client: str,
    period: str,
    recon_type: str,
    source_files: list[str] | None,
    config: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    selected_files: dict[str, str] | None = None,
    client_id: int | None = None,
    actor: str = "system",
) -> int:
    """Execute one reconciliation run and persist it atomically.

    Args:
        client: Client identifier.
        period: Period string, 'YYYY-MM'.
        recon_type: 'GST' or 'TDS'.
        source_files: Optional explicit list of source filenames to record
            in the run row. If None, the filenames actually used by
            ingestion are recorded instead (the more reliable source of
            truth — see the summary print for what was actually loaded).
        config: The full loaded config dict (as returned by load_config()).
        db_path: Optional override for the SQLite file (used by tests).
        selected_files: Optional mapping of source_type -> filename that
            pins exactly which on-disk file feeds the run. Passed through
            to ingestion; when absent, ingestion auto-discovers per
            source_type as before.
        client_id: Optional F2 client_id, used by the ingestion layer's
            shape cache and for downstream exception generation.
        actor: Who triggered the run, for the audit trail.

    Returns:
        The new run_id.

    Raises:
        RunExecutionError: if ingestion or matching fails. On any failure
        the transaction is rolled back — no run row and no result rows
        are left behind.
    """
    recon_type = recon_type.upper()
    if recon_type not in VALID_RECON_TYPES:
        raise ValueError(f"recon_type must be one of {VALID_RECON_TYPES}, got {recon_type!r}")

    start_time = time.monotonic()
    conn = db.get_connection(db_path)

    try:
        try:
            if recon_type == "GST":
                results, used_files, caveats, run_notes = _run_gst(
                    client, period, config["gst"], selected_files,
                    client_id=client_id, actor=actor,
                )
            elif recon_type == "TDS":
                results, used_files, caveats, run_notes = _run_tds(
                    client, period, config["tds"], selected_files,
                    client_id=client_id, actor=actor,
                )
            else:
                results, used_files, caveats, run_notes = _run_other(
                    client, period, config["other"], selected_files,
                    client_id=client_id, actor=actor,
                )
        except IngestionBlockedError as exc:
            # A file that couldn't be normalized into a canonical frame.
            # This is an expected, explainable outcome — NOT a crash — so
            # it is surfaced with its own plain-language message rather
            # than the generic "malformed source file" text.
            print(
                f"[runner] Ingestion BLOCKED for client={client!r} period={period!r} "
                f"recon_type={recon_type!r}: {exc}"
            )
            raise RunExecutionError(str(exc)) from exc
        except Exception as exc:
            detail = traceback.format_exc()
            print(
                f"[runner] Matching FAILED for client={client!r} period={period!r} "
                f"recon_type={recon_type!r}:\n{detail}"
            )
            raise RunExecutionError(
                f"Reconciliation failed for {client}/{period}/{recon_type}: {exc}"
            ) from exc

        assign_fingerprints(results, recon_type, period)

        config_snapshot = _read_config_text()
        run_timestamp = datetime.now(timezone.utc).isoformat()
        final_source_files = source_files if source_files is not None else used_files

        run_id = db.insert_run(
            conn,
            client=client,
            period=period,
            recon_type=recon_type,
            run_timestamp=run_timestamp,
            config_snapshot=config_snapshot,
            source_file_names=final_source_files,
            caveats=caveats,
            run_notes=run_notes,
        )

        for r in results:
            r["run_id"] = run_id

        db.insert_match_results(conn, results)

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    else:
        conn.close()

    elapsed = time.monotonic() - start_time
    _print_run_summary(run_id, results, elapsed)
    return run_id


def _read_config_text(path: Path | str | None = None) -> str:
    cfg_path = Path(path) if path else CONFIG_PATH
    return cfg_path.read_text(encoding="utf-8")


def _print_run_summary(run_id: int, results: list[dict[str, Any]], elapsed_seconds: float) -> None:
    from collections import Counter

    by_classification = Counter(r["classification"] for r in results)
    by_band = Counter(r["confidence_band"] for r in results)
    by_diff_type = Counter(r.get("difference_type") for r in results if r.get("difference_type"))

    print(f"\n[runner] Run {run_id} complete in {elapsed_seconds:.2f}s — {len(results)} result(s).")
    print(f"  By classification: {dict(by_classification)}")
    print(f"  By confidence band: {dict(by_band)}")
    if by_diff_type:
        print(f"  By difference_type: {dict(by_diff_type)}")
