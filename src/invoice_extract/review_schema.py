"""Schema for the review screen's backend services (migration 0006).

Two additive tables. Nothing here changes an existing table, an existing row or
an extracted value; the document actions and the reviewed flag are new facts
about a document, and until now the app had nowhere to record them.

Why not ``document_region_log``. That table exists and it does hold an actor and
a before/after pair, but it is the REGION audit trail: it has no ``reason``
column, no ``upload_id``, and triage already writes region creates and
``page_unassigned`` rows into it. A reviewer's reason is the whole point of
logging a category change or an exclusion, and a reason buried inside a JSON
blob cannot be queried, shown in a column, or audited. So:

* ``invoice_document_log``    — one row per human action on a DOCUMENT, with the
                               reason as its own column. The audit trail for
                               category changes, exclusions, review marks, and
                               the automatic un-mark that follows a change.
* ``invoice_document_review`` — "this document has been looked over". One row per
                               document, and it exists ONLY while that review
                               stands: un-marking deletes it, and the log keeps
                               the history of when it stood.

Both are created by ``execute_review_schema`` without committing, so the
numbered migration can run them inside the framework's own transaction, and by
``ensure_review_schema`` (which commits) for a fresh DB or a test.
"""

from __future__ import annotations

import sqlite3

#: Actions recorded in ``invoice_document_log.action``.
ACTION_SET_CATEGORY = "set_category"
ACTION_EXCLUDED = "excluded"
ACTION_INCLUDED = "included"
ACTION_REVIEWED = "reviewed"
ACTION_REVIEW_CLEARED = "review_cleared"

ACTIONS: tuple[str, ...] = (
    ACTION_SET_CATEGORY,
    ACTION_EXCLUDED,
    ACTION_INCLUDED,
    ACTION_REVIEWED,
    ACTION_REVIEW_CLEARED,
)

#: Why a standing review was cleared. Kept as data so a report can count them.
CLEARED_BY_RUN = "re_extraction"
CLEARED_BY_CORRECTION = "field_correction"
CLEARED_BY_REVIEWER = "reviewer"


REVIEW_STATEMENTS: tuple[str, ...] = (
    # One row per human action on a document. Append-only.
    """
    CREATE TABLE IF NOT EXISTS invoice_document_log (
        log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER,                  -- NULL for an upload-level action
        upload_id   INTEGER,
        -- set_category | excluded | included | reviewed | review_cleared
        action      TEXT    NOT NULL,
        field_key   TEXT,                     -- set only for a correction-driven clear
        -- The human's reason. REQUIRED by the API for every action that changes
        -- something; never invented on the user's behalf.
        reason      TEXT,
        before_json TEXT,
        after_json  TEXT,
        actor       TEXT    NOT NULL,
        at          TEXT    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_document_log_doc
        ON invoice_document_log(document_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_document_log_upload
        ON invoice_document_log(upload_id)
    """,
    # "This document has been looked over." Absent = not reviewed. The snapshot
    # columns record WHAT was believed when a human signed it off, so a later
    # formula or threshold change cannot silently re-sign it.
    """
    CREATE TABLE IF NOT EXISTS invoice_document_review (
        document_id              INTEGER PRIMARY KEY
                                 REFERENCES invoice_documents(document_id),
        reviewed_by              TEXT    NOT NULL,
        reviewed_at              TEXT    NOT NULL,
        note                     TEXT,
        reviewed_formula_version TEXT,
        reviewed_threshold       REAL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_document_review_at
        ON invoice_document_review(reviewed_at)
    """,
)


def execute_review_schema(conn: sqlite3.Connection) -> None:
    """Create the review tables WITHOUT committing.

    Used by the numbered migration, which runs inside the framework's own
    transaction — it must not commit early. Every statement is
    ``CREATE ... IF NOT EXISTS``, so it is safe to re-run.
    """
    for statement in REVIEW_STATEMENTS:
        conn.execute(statement)


def ensure_review_schema(conn: sqlite3.Connection) -> None:
    """Create the review tables and commit. Used by a fresh DB / a test."""
    execute_review_schema(conn)
    conn.commit()
