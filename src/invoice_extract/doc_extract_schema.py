"""Schema for the Phase-2 Invoice Extraction **document extraction** pass.

Phase 1 (``triage``) found documents as page regions. Phase 2 reads each
document from its OWN region crops into a new field set, takes a SECOND
independent read from a different vendor, and records where on the page each
value came from.

Three tables, all keyed on ``client_id``-scoped records that already exist:

* ``invoice_doc_extractions`` — one row per document per extraction pass.
  ``is_active`` implements "a re-extraction supersedes the old one; history is
  kept": a re-run inserts NEW rows and deactivates the previous ones. Nothing is
  ever deleted.
* ``invoice_doc_fields``      — one row per canonical field per extraction.
  Carries BOTH reads' verbatim values and how they agree. There is deliberately
  NO confidence column — confidence is Phase 4.
* ``invoice_doc_annotations`` — handwritten notes and stamps, captured so the
  reviewer can see them, with ``used_as_value`` always 0: an annotation is
  NEVER a field value.

Plus nothing on existing tables: this phase changes no existing table and no
existing row. A model call is attributed to its document through
``ai_call_log.reference_type`` ('doc_extraction') plus ``reference_id`` and
``upload_id``, which Phase 1 already records.

WHY A STATEMENT LIST, NOT ONE ``executescript`` STRING
-----------------------------------------------------
Same reason as ``triage_schema``: the migration framework runs each migration
inside an explicit ``BEGIN``/``COMMIT`` with ``isolation_level = None``, and a
``conn.executescript(...)`` (or any ``conn.commit()``) would end that
transaction early. The numbered migration iterates ``DOC_EXTRACT_STATEMENTS``
with plain ``conn.execute`` calls. The DDL lives here — not only inside the
migration — so a fresh DB created by ``init_invoice_extract`` has exactly the
same shape the migration produces.

NO CONFIDENCE, NO DERIVATION, NO DIRECTION
-----------------------------------------
This phase records what was PRINTED. A rate the invoice does not print stays
``not_printed`` with a NULL value (Phase 3 derives it). A value the model would
have had to compute is never stored. ``reads_agree`` is a comparison of two
independent reads, not a score.
"""

from __future__ import annotations

import sqlite3

# Extraction status machine. ``skipped`` is a first-class outcome: a document
# whose category is not extracted gets a row saying so, rather than silently
# disappearing from the run.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

EXTRACTION_TERMINAL = frozenset({STATUS_COMPLETE, STATUS_PARTIAL, STATUS_FAILED, STATUS_SKIPPED})
EXTRACTION_UNFINISHED = frozenset({STATUS_QUEUED, STATUS_RUNNING})

# Field statuses (STEP 2 + STEP 4).
FIELD_READ = "read"                    # printed and read
FIELD_NOT_PRINTED = "not_printed"      # the model says it is not on the document
FIELD_UNREADABLE = "unreadable"        # printed but the model could not read it
FIELD_OUT_OF_REGION = "out_of_region"  # box lands outside every region of the doc
FIELD_NEEDS_PRINTED = "needs_printed"  # a handwritten value where a printed one is required

# read-A vs read-B verdicts (STEP 5). ``missing_b`` = read B returned nothing for
# a field read A found; ``null`` = neither read found the field.
AGREE_EXACT = "exact"
AGREE_NEAR = "near"
AGREE_DISAGREE = "disagree"
AGREE_MISSING_B = "missing_b"
AGREE_NULL = "null"

DOC_EXTRACT_STATEMENTS: tuple[str, ...] = (
    # One row per document per extraction pass. A re-extraction inserts a NEW
    # row and deactivates the previous one — history is never deleted.
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_extractions (
        extraction_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id    INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        -- queued | running | complete | partial | failed | skipped
        --   skipped = deliberately not extracted (category or excluded); the
        --   reason is in skipped_reason so it is never a silent omission.
        status          TEXT    NOT NULL DEFAULT 'queued',
        skipped_reason  TEXT,
        provider_a      TEXT,
        model_a         TEXT,
        provider_b      TEXT,
        model_b         TEXT,
        attempts        INTEGER NOT NULL DEFAULT 0,
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        cost_usd        REAL,
        error           TEXT,
        started_at      TEXT,
        finished_at     TEXT,
        is_active       INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_extractions_doc
        ON invoice_doc_extractions(document_id, is_active)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_extractions_status
        ON invoice_doc_extractions(status, is_active)
    """,
    # One row per canonical field per extraction. value_a / value_b are the
    # VERBATIM text each read produced (never a normalised or computed value).
    # Boxes are page-normalised 0-1, top-left origin, AFTER that page's rotation.
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_fields (
        field_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id  INTEGER NOT NULL REFERENCES invoice_doc_extractions(extraction_id),
        document_id    INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        field_key      TEXT    NOT NULL,
        -- read | not_printed | unreadable | out_of_region | needs_printed
        status          TEXT    NOT NULL DEFAULT 'read',
        value_a         TEXT,
        value_b         TEXT,
        -- exact | near | disagree | missing_b | null
        reads_agree     TEXT,
        -- did the PDF's own text layer contain this value inside the box?
        -- NULL when the source has no text layer at all.
        text_layer_match INTEGER,
        is_handwritten  INTEGER NOT NULL DEFAULT 0,
        page            INTEGER,
        x               REAL,
        y               REAL,
        w               REAL,
        h               REAL,
        in_region       INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_fields_extraction
        ON invoice_doc_fields(extraction_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_fields_doc
        ON invoice_doc_fields(document_id, field_key)
    """,
    # Handwritten notes, stamps and scribbles. Captured for the reviewer,
    # NEVER used as a field value (used_as_value is a constant 0 and is kept as
    # a column so a later phase can record an explicit promotion if the product
    # ever wants one — this phase never writes anything but 0).
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_annotations (
        annotation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id  INTEGER NOT NULL REFERENCES invoice_doc_extractions(extraction_id),
        document_id    INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        page           INTEGER,
        x              REAL,
        y              REAL,
        w              REAL,
        h              REAL,
        text_verbatim  TEXT    NOT NULL,
        -- tds | payment | cheque | other | unknown
        kind_guess      TEXT    NOT NULL DEFAULT 'unknown',
        used_as_value   INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_annotations_extraction
        ON invoice_doc_annotations(extraction_id)
    """,
)

# NOTE: existing tables are deliberately left UNCHANGED by this phase. A model
# call is attributed to its document through ``ai_call_log.reference_type``
# ('doc_extraction') plus ``reference_id`` / ``upload_id``, which Phase 1
# already records, so no new column is needed on an existing table.
DOC_EXTRACT_COLUMNS: tuple[tuple[str, str, str], ...] = ()


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


def execute_doc_extract_schema(conn: sqlite3.Connection) -> None:
    """Execute the Phase-2 DDL + additive columns WITHOUT committing.

    Used by the numbered migration, which runs inside the framework's own
    transaction. An ALTER is skipped when its target table does not exist yet
    (a fresh DB is migrated BEFORE ``init_*`` creates ``ai_call_log``); the
    module init then adds the column on the table it just created.
    """
    for statement in DOC_EXTRACT_STATEMENTS:
        conn.execute(statement)
    for table, column, decl in DOC_EXTRACT_COLUMNS:
        if not _table_exists(conn, table):
            continue
        if column not in _columns(conn, table):
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')


def ensure_doc_extract_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase-2 tables and add the additive column. Idempotent.

    Used by ``init_invoice_extract`` (tests / fresh DBs); commits.
    """
    execute_doc_extract_schema(conn)
    conn.commit()
