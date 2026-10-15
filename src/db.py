"""SQLite state persistence for the PoC. Minimal schema, disposable.

Runs are immutable: every execution creates a new run row and its own set
of match_results. Identity is `client_id` (end_clients.client_id) everywhere;
`runs.client_name_at_run` is an immutable display-only snapshot of the legal
name at run time and is NEVER used for lookup. Review status lives in a
separate `review_state` table keyed by
(client_id, period, recon_type, fingerprint) so it survives re-runs
(see src/runner.py and src/queries.py for the orchestration that relies on
this separation).
"""

from pathlib import Path
import os
import sqlite3
import json
from typing import Any, Optional

#: The repository's OWN database. Never changes: it is what the safety checks
#: compare against, so it must not follow an override.
REPO_DB_PATH = Path(__file__).resolve().parent.parent / "db" / "poc.db"


def path_is_repo_db(db_path=None) -> bool:
    """Is this path the repository's own database?"""
    target = Path(db_path) if db_path else Path(DB_PATH)
    try:
        return target.expanduser().resolve() == Path(REPO_DB_PATH).expanduser().resolve()
    except OSError:  # pragma: no cover - unresolvable path is not the repo db
        return False


def require_real_db_allowed(db_path=None, *, allow_real_db: bool = False) -> None:
    """Refuse to WRITE the repository's own database unless the caller says so.

    THE guard for this whole class of accident: a test, a tool or a stray CLI
    run must not write ``db/poc.db`` by simply not passing ``--db``. Callers that
    really mean it — the boot path, ``python main.py --migrate``, and a person on
    purpose — pass ``allow_real_db=True`` (CLI: ``--allow-real-db``).

    Writing means ANY row, including the migration planner's own edit-history
    rows: those were what silently landed in the real database once already.
    """
    if allow_real_db:
        return
    if path_is_repo_db(db_path):
        raise RuntimeError(
            f"{Path(db_path) if db_path else Path(DB_PATH)} is the repository's own "
            "database. Refusing to write it — pass an explicit database (--db) or "
            "allow_real_db=True / --allow-real-db if that is genuinely what you want."
        )

#: ``SETU_DB_PATH`` points the whole app at a COPY of the database. Used by a
#: local scratch run (and by the screenshot harness) so nothing can touch the
#: real db/poc.db. The DEFAULT is unchanged: with the variable unset.
DB_PATH = Path(os.environ.get("SETU_DB_PATH") or REPO_DB_PATH)

# Set to "delete" to opt out of WAL (e.g. on a filesystem without shared memory).
JOURNAL_MODE_ENV = "SETU_SQLITE_JOURNAL"
DEFAULT_JOURNAL_MODE = "WAL"
# Only modes SQLite defines as valid for a database file; anything else in the
# env var is ignored rather than interpolated into the pragma.
JOURNAL_MODES = frozenset({"wal", "delete", "truncate", "persist", "memory", "off"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id          INTEGER NOT NULL REFERENCES end_clients(client_id),
    -- Immutable snapshot of the client's legal name at run time. DISPLAY
    -- ONLY — never used for lookup or joins (that is client_id's job).
    client_name_at_run TEXT    NOT NULL,
    period             TEXT    NOT NULL,
    recon_type         TEXT    NOT NULL,  -- 'GST' | 'TDS' | 'OTHER'
    run_timestamp      TEXT    NOT NULL,
    config_snapshot    TEXT    NOT NULL,  -- YAML text as produced the config
    source_file_names  TEXT,              -- JSON array of source file names
    -- JSON array of caveats: what this run could NOT check because a field
    -- or whole source was unmapped. A run with caveats is a PARTIAL report —
    -- it still produces results, but the report says plainly what is missing.
    caveats            TEXT,
    run_notes          TEXT
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

-- Note-to-note reconciliation results. A SEPARATE table from match_results BY
-- DESIGN: notes are their own document class with their own ITC figure, so
-- "a note can never move an invoice count" is structural — no filter over the
-- invoice table is the thing that protects it. Deliberately NOT carrying
-- `reviewed`/`fingerprint`: notes have their own surface and no carry-forward
-- this build (see the note design doc).
CREATE TABLE IF NOT EXISTS note_match_results (
    note_result_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    classification   TEXT    NOT NULL,
    confidence_score REAL    NOT NULL,
    confidence_band  TEXT    NOT NULL,
    note_reference   TEXT,    -- the shared normalised join key
    note_reference_raw TEXT,  -- the cited string, as evidence (books side)
    note_kind        TEXT,    -- 'Credit Note' | 'Debit Note'
    note_date        TEXT,    -- the note's OWN date, not the booking date
    note_tax         REAL,    -- tax on the note — the note-level ITC input
    note_value       REAL,
    note_side        TEXT,    -- 'both' | 'books' | 'portal'
    difference_type  TEXT,
    books_record     TEXT,    -- JSON text
    portal_record    TEXT,    -- JSON text
    match_reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_note_match_results_run_id
    ON note_match_results (run_id);

-- Review status, kept OUTSIDE match_results so it survives re-runs.
-- Also stores the classification/difference_type/band that were in effect
-- at review time, so a later run whose verdict changed for the same
-- fingerprint can be detected as "review stale" rather than reviewed.
CREATE TABLE IF NOT EXISTS review_state (
    client_id                 INTEGER NOT NULL REFERENCES end_clients(client_id),
    period                    TEXT    NOT NULL,
    recon_type                TEXT    NOT NULL,
    fingerprint               TEXT    NOT NULL,
    reviewed                  INTEGER NOT NULL DEFAULT 0,
    reviewer_note             TEXT,
    reviewed_at               TEXT,
    reviewed_in_run_id        INTEGER,
    reviewed_classification   TEXT,
    reviewed_difference_type  TEXT,
    reviewed_confidence_band  TEXT,
    PRIMARY KEY (client_id, period, recon_type, fingerprint)
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
    client_id     INTEGER NOT NULL REFERENCES end_clients(client_id),
    period        TEXT    NOT NULL,
    recon_type    TEXT    NOT NULL,
    fingerprint   TEXT    NOT NULL,
    side          TEXT    NOT NULL,   -- 'books' | 'portal'
    field         TEXT    NOT NULL,
    value         TEXT,               -- the corrected value (NULL = cleared)
    reason        TEXT,
    changed_by    TEXT,
    changed_at    TEXT,
    PRIMARY KEY (client_id, period, recon_type, fingerprint, side, field)
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


def _configure_concurrency(conn: sqlite3.Connection) -> None:
    """Put the file in WAL journal mode. Readers then never wait on the single
    writer, and a reader can no longer make a writer fail once its busy timeout
    runs out — which is the behaviour we want now that several users can be
    signed in at once.

    WAL is recorded in the database header, so this is a one-off per file, and
    the pragma is a no-op read once it is set. Falls back silently to the
    default rollback journal where WAL is unavailable (read-only filesystem,
    no shared memory, network mounts)."""
    requested = os.environ.get(JOURNAL_MODE_ENV, DEFAULT_JOURNAL_MODE).strip().lower()
    if requested not in JOURNAL_MODES:
        return
    try:
        current = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(current).lower() == requested:
            return
        conn.execute(f"PRAGMA journal_mode = {requested}").fetchone()
    except sqlite3.Error:
        # Another process may hold the write lock while we switch, or the
        # filesystem may not support WAL. Either way the default mode is safe.
        pass


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return a connection, creating the db file and tables if missing."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    _configure_concurrency(conn)
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
    client_id: int,
    client_name_at_run: str,
    period: str,
    recon_type: str,
    run_timestamp: str,
    config_snapshot: str,
    source_file_names: list[str],
    caveats: Optional[list[dict[str, Any]]] = None,
    run_notes: Optional[list[dict[str, Any]]] = None,
) -> int:
    from src.shared.vocab import require_valid_recon_type

    require_valid_recon_type(recon_type)
    cur = conn.execute(
        """
        INSERT INTO runs
            (client_id, client_name_at_run, period, recon_type, run_timestamp,
             config_snapshot, source_file_names, caveats, run_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(client_id),
            client_name_at_run,
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


def insert_note_results(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Bulk-insert note-to-note results into their OWN table.

    Kept separate from `insert_match_results` so a note can never be written
    into the invoice table by a caller that forgot to filter one out.
    `books_record` / `portal_record` are already JSON strings (both are
    produced by `gst_matcher._row_json`).

    Does not commit — the caller controls the run's transaction.
    """
    if not rows:
        return

    def _as_json_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        return value if isinstance(value, str) else json.dumps(value)

    conn.executemany(
        """
        INSERT INTO note_match_results
            (run_id, classification, confidence_score, confidence_band,
             note_reference, note_reference_raw, note_kind, note_date, note_tax,
             note_value, note_side, difference_type, books_record, portal_record,
             match_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r.get("run_id"),
                r.get("classification"),
                r.get("confidence_score"),
                r.get("confidence_band"),
                r.get("note_reference"),
                r.get("note_reference_raw"),
                r.get("note_kind"),
                str(r.get("note_date")) if r.get("note_date") is not None else None,
                r.get("note_tax"),
                r.get("note_value"),
                r.get("note_side"),
                r.get("difference_type"),
                _as_json_text(r.get("books_record")),
                _as_json_text(r.get("portal_record")),
                r.get("match_reason"),
            )
            for r in rows
        ],
    )


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
    client_id: int,
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
            (client_id, period, recon_type, fingerprint, reviewed, reviewer_note,
             reviewed_at, reviewed_in_run_id,
             reviewed_classification, reviewed_difference_type, reviewed_confidence_band)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (client_id, period, recon_type, fingerprint) DO UPDATE SET
            reviewed = 1,
            reviewer_note = excluded.reviewer_note,
            reviewed_at = excluded.reviewed_at,
            reviewed_in_run_id = excluded.reviewed_in_run_id,
            reviewed_classification = excluded.reviewed_classification,
            reviewed_difference_type = excluded.reviewed_difference_type,
            reviewed_confidence_band = excluded.reviewed_confidence_band
        """,
        (
            int(client_id), period, recon_type, fingerprint, note,
            reviewed_at, reviewed_in_run_id,
            classification, difference_type, confidence_band,
        ),
    )
    conn.commit()


def clear_review(
    conn: sqlite3.Connection,
    client_id: int,
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
        WHERE client_id = ? AND period = ? AND recon_type = ? AND fingerprint = ?
        """,
        (int(client_id), period, recon_type, fingerprint),
    )
    conn.commit()


def get_review_state(
    conn: sqlite3.Connection,
    client_id: int,
    period: str,
    recon_type: str,
) -> dict[str, dict[str, Any]]:
    """Return a fingerprint-keyed map of review state for this client/period/recon_type."""
    rows = conn.execute(
        """
        SELECT fingerprint, reviewed, reviewer_note, reviewed_at, reviewed_in_run_id,
               reviewed_classification, reviewed_difference_type, reviewed_confidence_band
        FROM review_state
        WHERE client_id = ? AND period = ? AND recon_type = ?
        """,
        (int(client_id), period, recon_type),
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
    client_id: int,
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
            (client_id, period, recon_type, fingerprint, side, field, value, reason, changed_by, changed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (client_id, period, recon_type, fingerprint, side, field) DO UPDATE SET
            value = excluded.value,
            reason = excluded.reason,
            changed_by = excluded.changed_by,
            changed_at = excluded.changed_at
        """,
        (int(client_id), period, recon_type, fingerprint, side, field, value, reason, changed_by, changed_at),
    )
    conn.commit()


def clear_field_override(
    conn: sqlite3.Connection,
    client_id: int,
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
        WHERE client_id = ? AND period = ? AND recon_type = ? AND fingerprint = ?
          AND side = ? AND field = ?
        """,
        (int(client_id), period, recon_type, fingerprint, side, field),
    )
    conn.commit()


def get_field_overrides(
    conn: sqlite3.Connection,
    client_id: int,
    period: str,
    recon_type: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return {fingerprint: {side: {field: {value, reason, changed_by, changed_at}}}}."""
    rows = conn.execute(
        """
        SELECT fingerprint, side, field, value, reason, changed_by, changed_at
        FROM result_field_overrides
        WHERE client_id = ? AND period = ? AND recon_type = ?
        """,
        (int(client_id), period, recon_type),
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