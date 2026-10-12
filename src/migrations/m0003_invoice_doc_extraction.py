"""Migration 0003 — Invoice Extraction Phase 2: document extraction.

Purely additive. Creates the three tables the Phase-2 extraction pass writes.

It rebuilds NO existing table, changes NO existing row, adds NO column to an
existing table, and touches no folder: the legacy extraction path (which never
reads these tables) is unaffected, and the Phase-1 triage tables are untouched.

* ``invoice_doc_extractions`` — one row per document per extraction pass, with
  both reads' provider/model/tokens/cost and the ``is_active`` supersede flag.
* ``invoice_doc_fields``      — both reads' verbatim values, their agreement,
  the text-layer match, the handwriting flag and the page-normalised box.
* ``invoice_doc_annotations`` — handwritten notes / stamps, never used as a
  field value.

Idempotent: the DDL is ``CREATE ... IF NOT EXISTS``. The DDL is shared with the
module init (``src.invoice_extract.doc_extract_schema``), so the migration and
a fresh ``init_invoice_extract`` can never disagree about the shape.

Every statement runs inside the framework's ``BEGIN``/``COMMIT`` — nothing here
commits early or calls ``executescript``.
"""

from __future__ import annotations

import sqlite3

from src.invoice_extract.doc_extract_schema import execute_doc_extract_schema
from src.migrations.framework import Migration


def apply(conn: sqlite3.Connection) -> None:
    execute_doc_extract_schema(conn)


MIGRATION = Migration(
    version=3,
    name="invoice_doc_extraction",
    apply=apply,
)
