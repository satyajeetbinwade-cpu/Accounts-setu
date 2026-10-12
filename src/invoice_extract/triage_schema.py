"""Schema for the Phase-1 Invoice Extraction **triage** foundation.

This is the "one file, many documents" document model:

* ``invoice_triage_runs``    — one triage pass over one uploaded file.
* ``invoice_triage_pages``   — one row per page of that file (per run).
* ``invoice_documents``      — the documents triage found across the file.
* ``document_regions``       — the box(es) each document occupies, page by page.
* ``document_region_log``    — audit trail for the (later) region-editing UI.

Plus two additive columns on existing tables:

* ``invoice_uploads.pages_examined / pages_total / text_truncated`` — the
  honesty fields recorded by the LEGACY extraction path (Step 3).
* ``extracted_invoice_fields.document_id`` — populated in Phase 2, unused now.

WHY A STATEMENT LIST, NOT ONE ``executescript`` STRING
-----------------------------------------------------
The migration framework (``src/migrations/framework.py``) runs each migration
inside an explicit ``BEGIN``/``COMMIT`` with ``isolation_level = None``. A
``conn.executescript(...)`` call — and any ``conn.commit()`` — would end that
transaction early and break the all-or-nothing guarantee. Migration 0001
therefore issues plain ``conn.execute`` calls; this module follows the same
rule by exposing ``TRIAGE_STATEMENTS`` for the migration to iterate.

The DDL lives here (not only inside the numbered migration) so that
``init_invoice_extract`` — which tests and a fresh DB use — creates the same
tables the migration does. Both paths are ``CREATE TABLE IF NOT EXISTS`` /
guarded ALTER, so they are idempotent and cannot fight each other.

One DESIGN NOTE (deviation from the task's column list, additive only):
``invoice_triage_pages.result_json`` caches the per-page model reply. Without
it, ``retry_failed_pages`` could re-run a failed page but could not RE-STITCH
without re-calling the pages that already succeeded. Caching the reply makes
the retry exact, idempotent, and free of extra model calls.
"""

from __future__ import annotations

import sqlite3

TRIAGE_STATEMENTS: tuple[str, ...] = (
    # One triage pass over one uploaded file.
    """
    CREATE TABLE IF NOT EXISTS invoice_triage_runs (
        run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        upload_id     INTEGER NOT NULL REFERENCES invoice_uploads(upload_id),
        -- status: queued | running | complete | partial | failed | interrupted
        --   (queued    = waiting for the single background worker,
        --    interrupted = was 'running' when the process last stopped)
        status        TEXT    NOT NULL DEFAULT 'running',
        pages_total   INTEGER NOT NULL DEFAULT 0,
        pages_ok      INTEGER NOT NULL DEFAULT 0,
        pages_failed  INTEGER NOT NULL DEFAULT 0,
        provider      TEXT,
        model         TEXT,
        settings_json TEXT,
        input_tokens  INTEGER,
        output_tokens INTEGER,
        cost_usd      REAL,
        -- how many pages were served by the FALLBACK leg (visibility of failover)
        fallback_pages INTEGER,
        error         TEXT,
        started_at    TEXT    NOT NULL,
        finished_at   TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_triage_runs_upload
        ON invoice_triage_runs(upload_id)
    """,
    # One row per page of the file, per run. ``page`` is 1-based.
    """
    CREATE TABLE IF NOT EXISTS invoice_triage_pages (
        run_id            INTEGER NOT NULL REFERENCES invoice_triage_runs(run_id),
        page              INTEGER NOT NULL,
        status            TEXT    NOT NULL DEFAULT 'ok',    -- ok|failed|skipped
        rotation_deg      INTEGER NOT NULL DEFAULT 0,       -- clockwise degrees to upright
        is_blank          INTEGER NOT NULL DEFAULT 0,
        duplicate_of_page INTEGER,                          -- nullable
        content_hash      TEXT,
        text_layer_chars  INTEGER,
        attempts          INTEGER NOT NULL DEFAULT 0,
        error             TEXT,
        result_json       TEXT,                             -- cached per-page model reply
        -- which model actually served this page, and whether the fallback leg did
        model             TEXT,
        fallback_used     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, page)
    )
    """,
    # One row per document triage found across the file. ``ordinal`` is stable
    # page order and INCLUDES excluded documents.
    """
    CREATE TABLE IF NOT EXISTS invoice_documents (
        document_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        upload_id       INTEGER NOT NULL REFERENCES invoice_uploads(upload_id),
        run_id          INTEGER NOT NULL REFERENCES invoice_triage_runs(run_id),
        ordinal         INTEGER NOT NULL,
        category        TEXT    NOT NULL,                   -- tax_invoice|no_gst_expense|supporting|ticket|not_a_voucher
        category_source TEXT    NOT NULL DEFAULT 'triage',  -- triage|user
        label           TEXT,
        excluded        INTEGER NOT NULL DEFAULT 0,
        excluded_reason TEXT,
        flags_json      TEXT,
        is_active       INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_documents_upload
        ON invoice_documents(upload_id, is_active)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_documents_run
        ON invoice_documents(run_id)
    """,
    # The box(es) a document occupies. x/y/w/h are normalised 0-1, top-left
    # origin, measured on the page AFTER applying that page's rotation_deg.
    """
    CREATE TABLE IF NOT EXISTS document_regions (
        region_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id  INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        page         INTEGER NOT NULL,
        x            REAL NOT NULL,
        y            REAL NOT NULL,
        w            REAL NOT NULL,
        h            REAL NOT NULL,
        order_in_doc INTEGER NOT NULL DEFAULT 0,
        source       TEXT NOT NULL DEFAULT 'triage',        -- triage|user
        is_active    INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_document_regions_doc
        ON document_regions(document_id)
    """,
    # Audit trail for the later region-editing UI (and for triage itself).
    """
    CREATE TABLE IF NOT EXISTS document_region_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER,
        region_id   INTEGER,                                -- nullable
        action      TEXT    NOT NULL,
        before_json TEXT,
        after_json  TEXT,
        actor       TEXT    NOT NULL,
        at          TEXT    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_document_region_log_doc
        ON document_region_log(document_id)
    """,
)

# (table, column, declaration) — additive columns on existing tables.
TRIAGE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # Step 3 — the legacy path's honesty fields.
    ("invoice_uploads", "pages_examined", "INTEGER"),
    ("invoice_uploads", "pages_total", "INTEGER"),
    ("invoice_uploads", "text_truncated", "INTEGER"),
    # Phase 2 seam — unused in Phase 1.
    ("extracted_invoice_fields", "document_id", "INTEGER"),
    # Amendments: visibility of fallback use during triage, and which model
    # served each page.
    ("invoice_triage_runs", "fallback_pages", "INTEGER"),
    ("invoice_triage_pages", "model", "TEXT"),
    ("invoice_triage_pages", "fallback_used", "INTEGER"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.OperationalError:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def execute_triage_schema(conn: sqlite3.Connection) -> None:
    """Execute the triage DDL + additive columns WITHOUT committing.

    Used by the numbered migration, which runs inside the framework's own
    transaction — it must not commit early. An ALTER is skipped when its
    target table does not exist yet (a fresh DB is migrated BEFORE the module
    ``init_*`` steps create ``invoice_uploads``); the module init then adds the
    columns on the table it just created.
    """
    for statement in TRIAGE_STATEMENTS:
        conn.execute(statement)
    for table, column, decl in TRIAGE_COLUMNS:
        if not _table_exists(conn, table):
            continue
        if column not in _columns(conn, table):
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')


def ensure_triage_schema(conn: sqlite3.Connection) -> None:
    """Create the triage tables and add the additive columns. Idempotent.

    Used by ``init_invoice_extract`` (tests / fresh DBs); commits.
    """
    execute_triage_schema(conn)
    conn.commit()
