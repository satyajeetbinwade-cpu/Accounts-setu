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
    "difference_type", "matched_record_ids", "difference", "itc_at_risk",
    "gross_value", "reviewed", "reviewer_note",
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


# ---------------------------------------------------------------------------
# Field-level overrides on a matched record (reviewer corrections).
# ---------------------------------------------------------------------------


def set_field_override(
    client: str,
    period: str,
    recon_type: str,
    fingerprint: str,
    side: str,
    field: str,
    value: Optional[str],
    *,
    reason: Optional[str] = None,
    actor: str = "system",
    db_path=None,
) -> None:
    """Record a reviewer's correction to one field of a matched record.

    Writes the override (carried forward by fingerprint, like review state)
    AND an F4 edit-history entry, so the correction is auditable. `side` is
    'books' or 'portal'.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_connection(db_path)
    try:
        db.set_field_override(
            conn, client, period, recon_type, fingerprint, side, field, value,
            reason=reason, changed_by=actor, changed_at=now,
        )
    finally:
        conn.close()

    # F4 audit — best-effort, never blocks the correction.
    try:
        from src.f4 import service as f4

        f4.record_edit(
            record_type="match_result",
            record_id=f"{fingerprint}:{side}",
            field=field,
            old_value=None,
            new_value=value,
            reason=reason,
            actor=actor,
            db_path=db_path,
        )
    except Exception:  # noqa: BLE001
        pass

    # Feed the correction back into Module 2 / Action Center: regenerate the
    # exceptions for every run containing this fingerprint so the flagged
    # items reflect the corrected value. Best-effort — a correction must
    # never fail because a downstream module is unavailable.
    try:
        from src.module2 import service as m2

        for run_id in run_ids_for_fingerprint(client, period, recon_type, fingerprint, db_path=db_path):
            m2.generate_exceptions_for_run(run_id, client_id=_client_id_for_run(run_id, db_path=db_path), actor=actor, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass


def _client_id_for_run(run_id: int, *, db_path=None) -> int:
    """Resolve the F2 client_id for a run via its client folder name."""
    conn = db.get_connection(db_path)
    try:
        row = conn.execute("SELECT client FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0
    folder = row[0]

    def norm(name: str) -> str:
        return "".join(ch for ch in str(name).lower() if ch.isalnum())

    try:
        from src.clients import service as clients

        target = norm(folder)
        for c in clients.list_clients(include_inactive=True):
            if norm(c["legal_name"]) == target:
                return int(c["client_id"])
        for c in clients.list_clients(include_inactive=True):
            n = norm(c["legal_name"])
            if target and (n.startswith(target) or target.startswith(n)):
                return int(c["client_id"])
    except Exception:  # noqa: BLE001
        pass
    return 0


def clear_field_override(
    client: str, period: str, recon_type: str, fingerprint: str, side: str, field: str,
    *, db_path=None,
) -> None:
    conn = db.get_connection(db_path)
    try:
        db.clear_field_override(conn, client, period, recon_type, fingerprint, side, field)
    finally:
        conn.close()


def get_field_overrides(
    client: str, period: str, recon_type: str, *, db_path=None
) -> dict[str, dict[str, dict[str, Any]]]:
    conn = db.get_connection(db_path)
    try:
        return db.get_field_overrides(conn, client, period, recon_type)
    finally:
        conn.close()


def _apply_field_overrides(
    records: list[dict[str, Any]], overrides: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """Apply stored field overrides onto the books/portal record dicts in
    place, and record which fields were overridden so the UI can mark them."""
    for d in records:
        fp = d.get("fingerprint")
        by_side = overrides.get(fp)
        if not by_side:
            continue
        overridden: list[str] = []
        for side, field_map in by_side.items():
            record = d.get(f"{side}_record")
            if not isinstance(record, dict):
                continue
            for field, meta in field_map.items():
                record[field] = meta.get("value")
                overridden.append(f"{side}.{field}")
        d["overridden_fields"] = overridden


def apply_record_overrides(
    client: str, period: str, recon_type: str, fingerprint: Optional[str],
    books: Optional[dict[str, Any]], portal: Optional[dict[str, Any]], *, db_path=None,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Apply a fingerprint's stored field overrides to a books/portal record
    pair, returning the corrected copies.

    Module 2 reads match_results directly (not through get_results()), so it
    calls this to see the reviewer's corrections — that is how an edit in the
    Review screen reaches the reconciliation exceptions and the Action Center.
    """
    if not fingerprint:
        return books, portal
    overrides = get_field_overrides(client, period, recon_type, db_path=db_path)
    by_side = overrides.get(fingerprint)
    if not by_side:
        return books, portal
    out_books = dict(books) if isinstance(books, dict) else books
    out_portal = dict(portal) if isinstance(portal, dict) else portal
    for side, field_map in by_side.items():
        target = out_books if side == "books" else out_portal
        if not isinstance(target, dict):
            continue
        for field, meta in field_map.items():
            target[field] = meta.get("value")
    return out_books, out_portal


def run_ids_for_fingerprint(
    client: str, period: str, recon_type: str, fingerprint: str, *, db_path=None,
) -> list[int]:
    """Every run that contains this fingerprint — used to regenerate the
    downstream exceptions after a reviewer corrects a record."""
    conn = db.get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT mr.run_id
            FROM match_results mr
            JOIN runs r ON r.run_id = mr.run_id
            WHERE mr.fingerprint = ? AND r.client = ? AND r.period = ? AND r.recon_type = ?
            """,
            (fingerprint, client, period, recon_type),
        ).fetchall()
        return [int(r[0]) for r in rows]
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
        # Run notes (D7): sources deliberately NOT reconciled, and coverage
        # declarations (e.g. IMS statuses). Distinct from caveats.
        raw_notes = result.get("run_notes")
        if raw_notes:
            try:
                result["run_notes"] = json.loads(raw_notes)
            except (TypeError, ValueError):
                result["run_notes"] = []
        else:
            result["run_notes"] = []
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
        override_map = db.get_field_overrides(conn, client, period, recon_type)
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
        d["overridden_fields"] = []
        records.append(d)

    # Reviewer corrections are applied on top of the engine's records, so the
    # UI (and any downstream reader) sees the corrected values.
    _apply_field_overrides(records, override_map)

    df = pd.DataFrame.from_records(
        records, columns=_RESULT_COLUMNS + ["review_stale", "reviewed_at", "overridden_fields"]
    )

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
