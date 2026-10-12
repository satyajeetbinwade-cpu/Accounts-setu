"""Schema for the Phase-3 Invoice Extraction **deterministic rules** layer.

Phase 2 stores what each document PRINTED. Phase 3 adds the layer on top that
can be computed from those stored values with no model in the loop:

* ``invoice_doc_derived``       — values CALCULATED from printed ones (round-off,
  a rate implied by tax over taxable, the issuer's state code, the expected tax
  type). A derived value never overwrites a printed one and never takes a score.
* ``invoice_doc_checks``        — one row per check, with ``pass`` / ``fail`` /
  ``not_applicable``. A check that cannot fail is recorded as not_applicable with
  a reason, never as a pass.
* ``invoice_doc_applicability`` — is a field required, optional or not applicable
  for THIS document, and why. Rule rows are recomputed; ``source = 'user'`` rows
  are a reviewer's override and are preserved across re-runs.
* ``invoice_doc_direction``     — sales / purchase / likely_* / undetermined,
  with the basis and the evidence. Direction never defaults.
* ``client_aliases``            — names a client's paperwork prints, remembered
  ONLY when a reviewer confirms one. Revocable, never created automatically.

WHY A STATEMENT LIST, NOT ONE ``executescript`` STRING
-----------------------------------------------------
Same reason as ``triage_schema`` / ``doc_extract_schema``: the migration
framework runs each migration inside an explicit ``BEGIN``/``COMMIT`` with
``isolation_level = None``, and ``executescript`` (or any ``commit``) would end
that transaction early. The numbered migration iterates
``RULES_STATEMENTS`` with plain ``conn.execute`` calls, and the DDL lives here
so a fresh DB built by ``init_invoice_extract`` is identical to a migrated one.

NOTHING HERE CAN CHANGE AN EXTRACTED VALUE
------------------------------------------
There is no UPDATE path to ``invoice_doc_fields`` anywhere in this module: the
deterministic layer only ever READS it. A re-run replaces its OWN rows
(``invoice_doc_derived`` / ``invoice_doc_checks`` / the ``source = 'rule'``
applicability rows) and leaves the extracted values alone.
"""

from __future__ import annotations

import sqlite3

# Check statuses.
CHECK_PASS = "pass"
CHECK_FAIL = "fail"
CHECK_NOT_APPLICABLE = "not_applicable"
CHECK_STATUSES = (CHECK_PASS, CHECK_FAIL, CHECK_NOT_APPLICABLE)

# Applicability levels.
LEVEL_REQUIRED = "required"
LEVEL_OPTIONAL = "optional"
LEVEL_NOT_APPLICABLE = "not_applicable"
LEVELS = (LEVEL_REQUIRED, LEVEL_OPTIONAL, LEVEL_NOT_APPLICABLE)

# Direction values. ``likely_*`` and ``undetermined`` are QUESTIONS for a
# human; only sales / purchase are settled.
DIRECTION_SALES = "sales"
DIRECTION_PURCHASE = "purchase"
DIRECTION_LIKELY_SALES = "likely_sales"
DIRECTION_LIKELY_PURCHASE = "likely_purchase"
DIRECTION_UNDETERMINED = "undetermined"
DIRECTIONS = (
    DIRECTION_SALES, DIRECTION_PURCHASE, DIRECTION_LIKELY_SALES,
    DIRECTION_LIKELY_PURCHASE, DIRECTION_UNDETERMINED,
)
#: Only these are asked of the user.
DIRECTIONS_NEEDING_A_HUMAN = (DIRECTION_LIKELY_SALES, DIRECTION_LIKELY_PURCHASE, DIRECTION_UNDETERMINED)

RULES_STATEMENTS: tuple[str, ...] = (
    # Values CALCULATED from printed ones. `is_derived` is implicit: everything
    # in this table is derived by definition, which is why nothing here may be
    # written back into invoice_doc_fields.
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_derived (
        derived_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id INTEGER NOT NULL REFERENCES invoice_doc_extractions(extraction_id),
        document_id   INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        key           TEXT    NOT NULL,
        value         TEXT,
        -- How it was computed, in words a reviewer can check by hand:
        -- 'total 12,917.00 - taxable 10,947.00 - tax 1,970.46'.
        formula       TEXT    NOT NULL,
        inputs_json   TEXT,
        computed_at   TEXT    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_derived_doc
        ON invoice_doc_derived(document_id, key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_derived_extraction
        ON invoice_doc_derived(extraction_id)
    """,
    # One row per check per extraction.
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_checks (
        check_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        extraction_id  INTEGER NOT NULL REFERENCES invoice_doc_extractions(extraction_id),
        document_id    INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        check_key      TEXT    NOT NULL,
        status         TEXT    NOT NULL,     -- pass | fail | not_applicable
        detail         TEXT    NOT NULL,
        inputs_json    TEXT,
        -- the field_keys the failure points at, so the review screen can jump
        fields_implicated_json TEXT,
        computed_at    TEXT    NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_checks_doc
        ON invoice_doc_checks(document_id, check_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_checks_extraction
        ON invoice_doc_checks(extraction_id)
    """,
    # Applicability: one row per field per document. A `user` row is a reviewer's
    # override — it survives a re-run (only `rule` rows are recomputed).
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_applicability (
        document_id     INTEGER NOT NULL REFERENCES invoice_documents(document_id),
        field_key       TEXT    NOT NULL,
        level           TEXT    NOT NULL,     -- required | optional | not_applicable
        reason          TEXT    NOT NULL,
        source          TEXT    NOT NULL DEFAULT 'rule',   -- rule | user
        overridden_by   TEXT,
        override_reason TEXT,
        at              TEXT    NOT NULL,
        PRIMARY KEY (document_id, field_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_invoice_doc_applicability_doc
        ON invoice_doc_applicability(document_id, level)
    """,
    # Direction: ONE row per document. Never defaulted; a `likely_*` or
    # `undetermined` row is a question for a person, and until that person
    # answers, the document is excluded from every register.
    """
    CREATE TABLE IF NOT EXISTS invoice_doc_direction (
        document_id   INTEGER PRIMARY KEY REFERENCES invoice_documents(document_id),
        direction     TEXT    NOT NULL,
        basis         TEXT    NOT NULL,
        match_json    TEXT,
        confirmed_by  TEXT,
        confirmed_at  TEXT,
        scope         TEXT
    )
    """,
    # Names a client's paperwork prints. Created ONLY by a reviewer confirming
    # one (see rules.remember_alias) and revocable (rules.revoke_alias).
    """
    CREATE TABLE IF NOT EXISTS client_aliases (
        alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id       INTEGER NOT NULL,
        alias_text      TEXT    NOT NULL,
        normalised_text TEXT    NOT NULL,
        created_by      TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        revoked_by      TEXT,
        revoked_at      TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_client_aliases_client
        ON client_aliases(client_id)
    """,
    # One live alias per client per spelling; a revoked one stays as history.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_client_aliases_live
        ON client_aliases(client_id, normalised_text) WHERE revoked_at IS NULL
    """,
)


def execute_rules_schema(conn: sqlite3.Connection) -> None:
    """Execute the Phase-3 DDL WITHOUT committing (used by the migration)."""
    for statement in RULES_STATEMENTS:
        conn.execute(statement)


def ensure_rules_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase-3 tables. Idempotent; commits."""
    execute_rules_schema(conn)
    conn.commit()
