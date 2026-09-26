"""SQLite state persistence for the PoC. Minimal schema, disposable.

Runs are immutable: every execution creates a new run row and its own set
of match_results. Review status lives in a separate `review_state` table
keyed by (client, period, recon_type, fingerprint) so it survives re-runs
(see src/runner.py and src/queries.py for the orchestration that relies on
this separation).
"""

from pathlib import Path
import sqlite3
import json
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "poc.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client            TEXT    NOT NULL,
    period            TEXT    NOT NULL,
    recon_type        TEXT    NOT NULL,  -- 'GST' or 'TDS'
    run_timestamp     TEXT    NOT NULL,
    config_snapshot   TEXT    NOT NULL,  -- YAML text as produced the config
    source_file_names TEXT,              -- JSON array of source file names
    -- JSON array of caveats: what this run could NOT check because a field
    -- or whole source was unmapped. A run with caveats is a PARTIAL report —
    -- it still produces results, but the report says plainly what is missing.
    caveats           TEXT
);

CREATE TABLE IF NOT EXISTS match_results (
    result_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    fingerprint       TEXT,    -- stable identity of the underlying record(s);
                                -- used to carry review status across runs
    classification   TEXT    NOT NULL,
    confidence_score REAL    NOT NULL,
    confidence_band  TEXT    NOT NULL,
    books_record     TEXT,    -- JSON text
    portal_record    TEXT,    -- JSON text
    match_reason     TEXT,    -- plain-language explanation of classification
    difference_type  TEXT,    -- nullable; e.g. Short Deduction, Rounding, etc.
    matched_record_ids TEXT,  -- nullable JSON text; group members for aggregated matches
    -- Value-at-risk semantics (D6). Stored so the Review UI reads them
    -- rather than re-deriving them, and so run-level aggregates are exact.
    difference       REAL,    -- signed (portal − books) on the differing field
    itc_at_risk      REAL,    -- tax at risk (Not in Books/Portal) or |tax diff|
    gross_value      REAL,    -- invoice value, kept for Not in Books/Portal only
    reviewed         INTEGER NOT NULL DEFAULT 0,
    reviewer_note    TEXT
);

CREATE INDEX IF NOT EXISTS idx_match_results_run_id
    ON match_results (run_id);

CREATE INDEX IF NOT EXISTS idx_match_results_fingerprint
    ON match_results (fingerprint);

-- Review status, kept OUTSIDE match_results so it survives re-runs.
-- Also stores the classification/difference_type/band that were in effect
-- at review time, so a later run whose verdict changed for the same
-- fingerprint can be detected as "review stale" rather than reviewed.
CREATE TABLE IF NOT EXISTS review_state (
    client                  TEXT    NOT NULL,
    period                  TEXT    NOT NULL,
    recon_type              TEXT    NOT NULL,
    fingerprint             TEXT    NOT NULL,
    reviewed                INTEGER NOT NULL DEFAULT 0,
    reviewer_note           TEXT,
    reviewed_at             TEXT,
    reviewed_in_run_id      INTEGER,
    reviewed_classification TEXT,
    reviewed_difference_type TEXT,
    reviewed_confidence_band TEXT,
    PRIMARY KEY (client, period, recon_type, fingerprint)
);

-- AI analysis layer (post-matching). Immutable like runs: never UPDATE or
-- DELETE a row — re-analysis inserts. Cache lookup takes the most recent
-- successful row per fingerprint. Keyed off the SAME fingerprint used for
-- review carry-forward (src/fingerprint.py); no second fingerprint.
CREATE TABLE IF NOT EXISTS ai_analysis (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT    NOT NULL,
    result_id         INTEGER NOT NULL,
    fingerprint       TEXT    NOT NULL,
    recon_type        TEXT    NOT NULL,
    classification    TEXT    NOT NULL,
    difference_type   TEXT,
    model_used        TEXT    NOT NULL,
    provider_used     TEXT,               -- the provider that resolved the call (multi-provider)
    routing_reason    TEXT    NOT NULL,
    status            TEXT    NOT NULL,   -- 'success' | 'failed'
    issue_summary     TEXT,
    probable_causes   TEXT,               -- JSON array
    suggested_fix     TEXT,
    reasoning         TEXT,
    confidence        TEXT,
    data_sufficient   INTEGER,
    raw_response      TEXT,
    error_type        TEXT,
    error_detail      TEXT,
    latency_ms        INTEGER,
    created_at        TEXT    NOT NULL,
    FOREIGN KEY (result_id) REFERENCES match_results(result_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_fingerprint ON ai_analysis(fingerprint, status);
CREATE INDEX IF NOT EXISTS idx_ai_result      ON ai_analysis(result_id);

-- Field-level corrections a reviewer makes to a matched record's books/portal
-- values. Kept OUTSIDE match_results (which is immutable by design) and keyed
-- by fingerprint, exactly like review_state — so a correction survives a
-- re-run and is re-applied to the same underlying record. Every write also
-- goes to F4's edit history (see src/queries.py).
CREATE TABLE IF NOT EXISTS result_field_overrides (
    client        TEXT    NOT NULL,
    period        TEXT    NOT NULL,
    recon_type    TEXT    NOT NULL,
    fingerprint   TEXT    NOT NULL,
    side          TEXT    NOT NULL,   -- 'books' | 'portal'
    field         TEXT    NOT NULL,
    value         TEXT,               -- the corrected value (NULL = cleared)
    reason        TEXT,
    changed_by    TEXT,
    changed_at    TEXT,
    PRIMARY KEY (client, period, recon_type, fingerprint, side, field)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created.

    ``CREATE TABLE IF NOT EXISTS`` won't add a column to a table that already
    exists, so new columns need an explicit, additive migration here. Safe to
    call on every connection: each column is only added when missing, and
    existing rows get the column's default.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "caveats" not in existing:
        conn.execute("ALTER TABLE runs ADD COLUMN caveats TEXT")
        conn.commit()
    # Run notes (D7): unreconciled sources and coverage declarations. Kept
    # separate from `caveats` (which describe checks that could not run) —
    # a note DECLARES something that was deliberately not reconciled.
    if "run_notes" not in existing:
        conn.execute("ALTER TABLE runs ADD COLUMN run_notes TEXT")
        conn.commit()

    result_cols = {row[1] for row in conn.execute("PRAGMA table_info(match_results)")}
    for col in ("difference", "itc_at_risk", "gross_value"):
        if col not in result_cols:
            conn.execute(f"ALTER TABLE match_results ADD COLUMN {col} REAL")
    conn.commit()

    # AI attribution: which provider resolved the call (multi-provider). The
    # model id alone no longer implies the provider once Anthropic/Deepseek/
    # Sarvam can be selected directly.
    analysis_cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_analysis)")}
    if "provider_used" not in analysis_cols:
        conn.execute("ALTER TABLE ai_analysis ADD COLUMN provider_used TEXT")
    conn.commit()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return a connection, creating the db file and tables if missing."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = get_connection(db_path)
    conn.close()


# ---------------------------------------------------------------------------
# Runs / results — transaction-controlled by the caller (see src/runner.py).
# These functions do NOT commit; the caller commits or rolls back the whole
# run as a single transaction so a partial run can never persist.
# ---------------------------------------------------------------------------


def insert_run(
    conn: sqlite3.Connection,
    client: str,
    period: str,
    recon_type: str,
    run_timestamp: str,
    config_snapshot: str,
    source_file_names: list[str],
    caveats: Optional[list[dict[str, Any]]] = None,
    run_notes: Optional[list[dict[str, Any]]] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO runs
            (client, period, recon_type, run_timestamp, config_snapshot, source_file_names, caveats, run_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client,
            period,
            recon_type,
            run_timestamp,
            config_snapshot,
            json.dumps(source_file_names),
            json.dumps(caveats or []),
            json.dumps(run_notes or []),
        ),
    )
    return cur.lastrowid


def insert_match_results(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Bulk-insert match results. Each row dict may include:
    run_id (required), fingerprint, classification, confidence_score,
    confidence_band, books_record, portal_record, match_reason,
    difference_type, matched_record_ids, reviewed, reviewer_note.

    Does not commit — caller controls the transaction.

    books_record / portal_record are stored as-is if already a JSON string
    (both matchers produce them pre-serialised); otherwise json.dumps()'d.
    matched_record_ids is always a Python list/None from the matchers, so
    it is always json.dumps()'d here.
    """

    def _as_json_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        return value if isinstance(value, str) else json.dumps(value)

    for r in rows:
        conn.execute(
            """
            INSERT INTO match_results
                (run_id, fingerprint, classification, confidence_score, confidence_band,
                 books_record, portal_record, match_reason,
                 difference_type, matched_record_ids,
                 difference, itc_at_risk, gross_value,
                 reviewed, reviewer_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["run_id"],
                r.get("fingerprint"),
                r["classification"],
                r["confidence_score"],
                r["confidence_band"],
                _as_json_text(r.get("books_record")),
                _as_json_text(r.get("portal_record")),
                r.get("match_reason"),
                r.get("difference_type"),
                _as_json_text(r.get("matched_record_ids")),
                r.get("difference"),
                r.get("itc_at_risk"),
                r.get("gross_value"),
                int(r.get("reviewed", False)),
                r.get("reviewer_note"),
            ),
        )


def query_results(
    conn: sqlite3.Connection,
    run_id: int,
    classification: Optional[str] = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM match_results WHERE run_id = ?"
    params: list[Any] = [run_id]
    if classification is not None:
        sql += " AND classification = ?"
        params.append(classification)
    sql += " ORDER BY result_id"
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Review status — carried forward across runs, keyed by fingerprint.
# These DO commit individually; review actions are independent of any run.
# ---------------------------------------------------------------------------


def mark_reviewed(
    conn: sqlite3.Connection,
    client: str,
    period: str,
    recon_type: str,
    fingerprint: str,
    *,
    note: Optional[str] = None,
    reviewed_at: Optional[str] = None,
    reviewed_in_run_id: Optional[int] = None,
    classification: Optional[str] = None,
    difference_type: Optional[str] = None,
    confidence_band: Optional[str] = None,
) -> None:
    """Upsert a review record for this fingerprint.

    Stores the verdict (classification/difference_type/confidence_band) in
    effect at review time so a later run with a different verdict for the
    same fingerprint can be detected as "review stale" on read.
    """
    conn.execute(
        """
        INSERT INTO review_state
            (client, period, recon_type, fingerprint, reviewed, reviewer_note,
             reviewed_at, reviewed_in_run_id,
             reviewed_classification, reviewed_difference_type, reviewed_confidence_band)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (client, period, recon_type, fingerprint) DO UPDATE SET
            reviewed = 1,
            reviewer_note = excluded.reviewer_note,
            reviewed_at = excluded.reviewed_at,
            reviewed_in_run_id = excluded.reviewed_in_run_id,
            reviewed_classification = excluded.reviewed_classification,
            reviewed_difference_type = excluded.reviewed_difference_type,
            reviewed_confidence_band = excluded.reviewed_confidence_band
        """,
        (
            client, period, recon_type, fingerprint, note,
            reviewed_at, reviewed_in_run_id,
            classification, difference_type, confidence_band,
        ),
    )
    conn.commit()


def clear_review(
    conn: sqlite3.Connection,
    client: str,
    period: str,
    recon_type: str,
    fingerprint: str,
) -> None:
    """Clear a review record (correcting a mistaken review)."""
    conn.execute(
        """
        UPDATE review_state
        SET reviewed = 0, reviewer_note = NULL, reviewed_at = NULL,
            reviewed_in_run_id = NULL, reviewed_classification = NULL,
            reviewed_difference_type = NULL, reviewed_confidence_band = NULL
        WHERE client = ? AND period = ? AND recon_type = ? AND fingerprint = ?
        """,
        (client, period, recon_type, fingerprint),
    )
    conn.commit()


def get_review_state(
    conn: sqlite3.Connection,
    client: str,
    period: str,
    recon_type: str,
) -> dict[str, dict[str, Any]]:
    """Return a fingerprint-keyed map of review state for this client/period/recon_type."""
    rows = conn.execute(
        """
        SELECT fingerprint, reviewed, reviewer_note, reviewed_at, reviewed_in_run_id,
               reviewed_classification, reviewed_difference_type, reviewed_confidence_band
        FROM review_state
        WHERE client = ? AND period = ? AND recon_type = ?
        """,
        (client, period, recon_type),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result[row[0]] = {
            "reviewed": bool(row[1]),
            "reviewer_note": row[2],
            "reviewed_at": row[3],
            "reviewed_in_run_id": row[4],
            "reviewed_classification": row[5],
            "reviewed_difference_type": row[6],
            "reviewed_confidence_band": row[7],
        }
    return result


# ---------------------------------------------------------------------------
# Field-level overrides on a matched record — carried forward across runs,
# keyed by fingerprint (same carry-forward contract as review_state).
# ---------------------------------------------------------------------------


def set_field_override(
    conn: sqlite3.Connection,
    client: str,
    period: str,
    recon_type: str,
    fingerprint: str,
    side: str,
    field: str,
    value: Optional[str],
    *,
    reason: Optional[str] = None,
    changed_by: Optional[str] = None,
    changed_at: Optional[str] = None,
) -> None:
    """Upsert a reviewer's correction to one field of a matched record.

    `side` is 'books' or 'portal'. A NULL `value` clears the field. Keyed by
    fingerprint so the correction is re-applied to the same record on a
    later run.
    """
    conn.execute(
        """
        INSERT INTO result_field_overrides
            (client, period, recon_type, fingerprint, side, field, value, reason, changed_by, changed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (client, period, recon_type, fingerprint, side, field) DO UPDATE SET
            value = excluded.value,
            reason = excluded.reason,
            changed_by = excluded.changed_by,
            changed_at = excluded.changed_at
        """,
        (client, period, recon_type, fingerprint, side, field, value, reason, changed_by, changed_at),
    )
    conn.commit()


def clear_field_override(
    conn: sqlite3.Connection,
    client: str,
    period: str,
    recon_type: str,
    fingerprint: str,
    side: str,
    field: str,
) -> None:
    """Remove a field override (revert to the engine's original value)."""
    conn.execute(
        """
        DELETE FROM result_field_overrides
        WHERE client = ? AND period = ? AND recon_type = ? AND fingerprint = ?
          AND side = ? AND field = ?
        """,
        (client, period, recon_type, fingerprint, side, field),
    )
    conn.commit()


def get_field_overrides(
    conn: sqlite3.Connection,
    client: str,
    period: str,
    recon_type: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return {fingerprint: {side: {field: {value, reason, changed_by, changed_at}}}}."""
    rows = conn.execute(
        """
        SELECT fingerprint, side, field, value, reason, changed_by, changed_at
        FROM result_field_overrides
        WHERE client = ? AND period = ? AND recon_type = ?
        """,
        (client, period, recon_type),
    ).fetchall()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for fp, side, field, value, reason, changed_by, changed_at in rows:
        out.setdefault(fp, {}).setdefault(side, {})[field] = {
            "value": value,
            "reason": reason,
            "changed_by": changed_by,
            "changed_at": changed_at,
        }
    return out