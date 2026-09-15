"""Query interface for runs and results — read-only, no matching logic.

Joins review_state onto match_results by fingerprint so a re-run
automatically surfaces prior review status. Also computes the "review
stale" flag: if the classification, difference_type or confidence_band a
reviewer approved no longer matches the current run's verdict for that
fingerprint, the result is stale rather than reviewed (see src/db.py for
why review state is stored separately from match_results).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

import pandas as pd

from src import db

_RESULT_COLUMNS = [
    "result_id", "run_id", "fingerprint", "classification", "confidence_score",
    "confidence_band", "books_record", "portal_record", "match_reason",
    "difference_type", "matched_record_ids", "reviewed", "reviewer_note",
]


def mark_reviewed(
    client: str,
    period: str,
    recon_type: str,
    fingerprint: str,
    *,
    note: Optional[str] = None,
    result: Optional[dict[str, Any]] = None,
    db_path=None,
) -> None:
    """Mark a result reviewed by fingerprint. Pass `result` (a row from
    get_results()) to record the verdict in effect at review time, enabling
    stale detection on future runs."""
    from datetime import datetime, timezone

    conn = db.get_connection(db_path)
    try:
        db.mark_reviewed(
            conn, client, period, recon_type, fingerprint,
            note=note,
            reviewed_at=datetime.now(timezone.utc).isoformat(),
            reviewed_in_run_id=result.get("run_id") if result else None,
            classification=result.get("classification") if result else None,
            difference_type=result.get("difference_type") if result else None,
            confidence_band=result.get("confidence_band") if result else None,
        )
    finally:
        conn.close()


def clear_review(
    client: str, period: str, recon_type: str, fingerprint: str, *, db_path=None
) -> None:
    conn = db.get_connection(db_path)
    try:
        db.clear_review(conn, client, period, recon_type, fingerprint)
    finally:
        conn.close()


def get_review_state(
    client: str, period: str, recon_type: str, *, db_path=None
) -> dict[str, dict[str, Any]]:
    conn = db.get_connection(db_path)
    try:
        return db.get_review_state(conn, client, period, recon_type)
    finally:
        conn.close()


def get_run(run_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    """Return the run row as a dict, or None if not found."""
    conn = db.get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).description]
        result = dict(zip(cols, row))
        result["source_file_names"] = json.loads(result["source_file_names"] or "[]")
        # Caveats: what this run could not check. Absent on runs executed
        # before the column existed, and may be stored as JSON text.
        raw_caveats = result.get("caveats")
        if raw_caveats:
            try:
                result["caveats"] = json.loads(raw_caveats)
            except (TypeError, ValueError):
                result["caveats"] = []
        else:
            result["caveats"] = []
        return result
    finally:
        conn.close()


def list_runs(
    client: Optional[str] = None,
    period: Optional[str] = None,
    recon_type: Optional[str] = None,
    *,
    db_path=None,
) -> pd.DataFrame:
    """List runs, newest first, optionally filtered."""
    conn = db.get_connection(db_path)
    try:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if client is not None:
            sql += " AND client = ?"
            params.append(client)
        if period is not None:
            sql += " AND period = ?"
            params.append(period)
        if recon_type is not None:
            sql += " AND recon_type = ?"
            params.append(recon_type)
        sql += " ORDER BY run_id DESC"
        df = pd.read_sql_query(sql, conn, params=params)
        return df
    finally:
        conn.close()


def _row_to_result_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for json_field in ("books_record", "portal_record", "matched_record_ids"):
        if d.get(json_field):
            try:
                d[json_field] = json.loads(d[json_field])
            except (TypeError, ValueError):
                pass
    return d


def get_results(
    run_id: int,
    classification: Optional[str] = None,
    difference_type: Optional[str] = None,
    confidence_band: Optional[str] = None,
    reviewed: Optional[bool] = None,
    min_confidence: Optional[float] = None,
    *,
    db_path=None,
) -> pd.DataFrame:
    """Return results for a run as a DataFrame, with review state joined
    by fingerprint and a computed `review_stale` flag.

    review_stale is True when the fingerprint has a review record whose
    stored classification/difference_type/confidence_band differ from the
    result's current values in this run — the reviewer approved a verdict
    that has since changed.
    """
    conn = db.get_connection(db_path)
    try:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM match_results WHERE run_id = ?"
        params: list[Any] = [run_id]
        if classification is not None:
            sql += " AND classification = ?"
            params.append(classification)
        if difference_type is not None:
            sql += " AND difference_type = ?"
            params.append(difference_type)
        if confidence_band is not None:
            sql += " AND confidence_band = ?"
            params.append(confidence_band)
        if min_confidence is not None:
            sql += " AND confidence_score >= ?"
            params.append(min_confidence)
        sql += " ORDER BY result_id"

        rows = conn.execute(sql, params).fetchall()
        run_row = conn.execute(
            "SELECT client, period, recon_type FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise ValueError(f"Run {run_id} not found")
        client, period, recon_type = run_row

        review_map = db.get_review_state(conn, client, period, recon_type)
    finally:
        conn.close()

    records = []
    for row in rows:
        d = _row_to_result_dict(row)
        fp = d.get("fingerprint")
        rev = review_map.get(fp)

        is_reviewed = False
        is_stale = False
        review_note = None
        reviewed_at = None

        if rev is not None and rev["reviewed"]:
            review_note = rev["reviewer_note"]
            reviewed_at = rev["reviewed_at"]
            verdict_changed = (
                rev["reviewed_classification"] != d["classification"]
                or rev["reviewed_difference_type"] != d["difference_type"]
                or rev["reviewed_confidence_band"] != d["confidence_band"]
            )
            if verdict_changed:
                is_stale = True
                is_reviewed = False
            else:
                is_reviewed = True

        d["reviewed"] = is_reviewed
        d["review_stale"] = is_stale
        d["reviewer_note"] = review_note
        d["reviewed_at"] = reviewed_at
        records.append(d)

    df = pd.DataFrame.from_records(records, columns=_RESULT_COLUMNS + ["review_stale", "reviewed_at"])

    if reviewed is not None:
        df = df[df["reviewed"] == reviewed]

    return df.reset_index(drop=True)


def get_run_summary(run_id: int, *, db_path=None) -> dict[str, Any]:
    """Counts by classification, by band, by difference_type, plus total
    rupee value per classification bucket."""
    df = get_results(run_id, db_path=db_path)

    def _value_of(record: Any) -> float:
        if not isinstance(record, dict):
            return 0.0
        for key in ("invoice_value", "amount_paid_credited", "tax_deducted"):
            if key in record and record[key] is not None:
                try:
                    return abs(float(record[key]))
                except (TypeError, ValueError):
                    continue
        return 0.0

    df = df.copy()
    df["_value"] = df.apply(
        lambda r: _value_of(r["books_record"]) or _value_of(r["portal_record"]), axis=1
    )

    by_classification = df.groupby("classification").size().to_dict()
    by_band = df.groupby("confidence_band").size().to_dict()
    by_diff_type = (
        df[df["difference_type"].notna()].groupby("difference_type").size().to_dict()
    )
    value_by_classification = df.groupby("classification")["_value"].sum().round(2).to_dict()

    return {
        "run_id": run_id,
        "total_results": len(df),
        "by_classification": by_classification,
        "by_confidence_band": by_band,
        "by_difference_type": by_diff_type,
        "value_by_classification": value_by_classification,
    }


def compare_runs(run_id_a: int, run_id_b: int, *, db_path=None) -> dict[str, Any]:
    """Compare two runs on the same fingerprint identity. Intended for
    comparing runs of the same client/period with different tolerances —
    the primary tuning instrument.

    Returns a dict with:
      - changed: DataFrame of results whose classification differs between
        the two runs (joined on fingerprint), with both verdicts shown.
      - only_in_a / only_in_b: DataFrames of fingerprints present in one
        run but not the other.
      - movement: dict of {classification_a -> {classification_b: count}}
        showing aggregate movement between buckets.
    """
    df_a = get_results(run_id_a, db_path=db_path)
    df_b = get_results(run_id_b, db_path=db_path)

    a = df_a[["fingerprint", "classification", "difference_type", "confidence_band",
              "confidence_score", "match_reason"]].rename(
        columns={c: f"{c}_a" for c in
                 ["classification", "difference_type", "confidence_band", "confidence_score", "match_reason"]}
    )
    b = df_b[["fingerprint", "classification", "difference_type", "confidence_band",
              "confidence_score", "match_reason"]].rename(
        columns={c: f"{c}_b" for c in
                 ["classification", "difference_type", "confidence_band", "confidence_score", "match_reason"]}
    )

    merged = a.merge(b, on="fingerprint", how="outer", indicator=True)

    only_in_a = merged[merged["_merge"] == "left_only"].drop(columns=["_merge"]).reset_index(drop=True)
    only_in_b = merged[merged["_merge"] == "right_only"].drop(columns=["_merge"]).reset_index(drop=True)
    both = merged[merged["_merge"] == "both"].drop(columns=["_merge"])

    # Normalise NaN -> None before comparing, so "no difference_type on
    # either side" isn't reported as a change (NaN != NaN is True).
    diff_type_a = both["difference_type_a"].where(both["difference_type_a"].notna(), None)
    diff_type_b = both["difference_type_b"].where(both["difference_type_b"].notna(), None)

    changed = both[
        (both["classification_a"] != both["classification_b"])
        | (diff_type_a != diff_type_b)
    ].reset_index(drop=True)

    movement: dict[str, dict[str, int]] = {}
    for _, row in both.iterrows():
        ca, cb = row["classification_a"], row["classification_b"]
        movement.setdefault(ca, {}).setdefault(cb, 0)
        movement[ca][cb] += 1

    return {
        "run_id_a": run_id_a,
        "run_id_b": run_id_b,
        "changed": changed,
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "movement": movement,
        "unchanged_count": len(both) - len(changed),
        "changed_count": len(changed),
    }
