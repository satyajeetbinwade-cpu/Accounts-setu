"""Schema for the Phase-4 Invoice Extraction **confidence** layer.

Phase 3 grades a document (does it add up, are its tax heads right, is the
direction settled). Phase 4 grades EACH VALUE: how much the app trusts one
stored field, from evidence it already holds — never from what a model says
about itself.

Two tables:

* ``invoice_doc_field_scores``   — one row per stored field, with the score, the
  SIGNALS that produced it and any CAPS that bounded it. The signals and caps
  are stored as JSON so a screen can explain the number rather than assert it.
  A field whose score is NULL is one that has no score BY RULE (a value that was
  never printed, or a derived value) — the reason travels in ``signals_json``.
* ``invoice_field_corrections``  — the human record: every decision a reviewer
  makes about a field, INCLUDING "confirmed as is". That last one is what makes
  calibration possible at all: without it the only visible human decisions are
  the disagreements, and a score can never be shown to be right.

KEYED ON THE FIELD ROW, NOT THE DOCUMENT
----------------------------------------
``formula_version`` is stored on every row because the formula WILL change and a
score is only meaningful alongside the version that produced it. A re-extraction
inserts new ``invoice_doc_fields`` rows, so it produces new score rows and the
old ones stay exactly as they were — nothing is overwritten, and the
corrections that reference the old field rows keep pointing at the values a
human actually saw.
"""

from __future__ import annotations

import sqlite3

#: The correction actions. ``confirmed_as_is`` is a first-class action, not an
#: absence of one.
ACTION_CONFIRMED = "confirmed_as_is"
ACTION_EDITED = "edited"
ACTION_MARKED_NOT_PRINTED = "marked_not_printed"
ACTION_MARKED_NOT_APPLICABLE = "marked_not_applicable"
ACTIONS = (
    ACTION_CONFIRMED, ACTION_EDITED, ACTION_MARKED_NOT_PRINTED, ACTION_MARKED_NOT_APPLICABLE,
)

#: The reason codes a reviewer picks from. Free text is allowed alongside.
REASON_CODES = (
    "correct",
    "ocr_error",
    "wrong_field",
    "wrong_reader",
    "not_on_document",
    "not_applicable_here",
    "illegible",
    "handwritten_source",
    "other",
)

CONFIDENCE_STATEMENTS: tuple[str, ...] = (
    # One row per stored field. field_id is the PRIMARY KEY: a field has exactly
    # one score under one formula version, and a re-extraction makes NEW field
    # rows rather than updating these.
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_field_scores (
        field_id        INTEGER PRIMARY KEY REFERENCES invoice_doc_fields(field_id),
        -- NULL = this field has no score BY RULE (never printed, or derived).
        -- 0-100 otherwise. Never NULL merely because scoring failed.
        score           REAL,
        -- [{"name": "...", "points": 30, "reason": "..."}, ...] — every signal
        -- that contributed, including the ones worth 0, so the number can be
        -- explained rather than asserted.
        signals_json    TEXT    NOT NULL,
        -- [{"name": "...", "cap": 60, "reason": "..."}, ...] — every cap that
        -- applied and why, in the order they were applied.
        caps_json       TEXT    NOT NULL,
        formula_version TEXT    NOT NULL,
        threshold_used  REAL    NOT NULL,
        computed_at     TEXT    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_field_scores_score
        ON invoice_doc_field_scores(score)
    """,
    # Every human decision about a field, including "it was right".
    """
    CREATE TABLE IF NOT EXISTS invoice_field_corrections (
        correction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        field_id         INTEGER REFERENCES invoice_doc_fields(field_id),
        document_id      INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        client_id        INTEGER,
        field_key        TEXT    NOT NULL,
        value_before     TEXT,
        value_after      TEXT,
        action           TEXT    NOT NULL,
        reason_code      TEXT,
        reason_text      TEXT,
        actor            TEXT    NOT NULL,
        at               TEXT    NOT NULL,
        -- The score the field carried WHEN the human decided, kept as history:
        -- re-scoring must never rewrite what a person was looking at.
        score_at_time    REAL,
        was_flagged      INTEGER NOT NULL DEFAULT 0,
        formula_version  TEXT,
        document_category TEXT,
        extraction_models TEXT,
        is_handwritten   INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_field_corrections_doc
        ON invoice_field_corrections(document_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_field_corrections_field
        ON invoice_field_corrections(field_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_field_corrections_client
        ON invoice_field_corrections(client_id, at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_field_corrections_actor
        ON invoice_field_corrections(actor, at)
    """,
)


def execute_confidence_schema(conn: sqlite3.Connection) -> None:
    """Execute the Phase-4 DDL WITHOUT committing (used by the migration)."""
    for statement in CONFIDENCE_STATEMENTS:
        conn.execute(statement)


def ensure_confidence_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase-4 tables. Idempotent; commits."""
    execute_confidence_schema(conn)
    conn.commit()
