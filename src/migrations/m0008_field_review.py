"""Migration 0008 — the human review decision on a field row.

Purely additive. Adds ONE nullable column:

* ``invoice_doc_fields.review_json`` — the review screen's decision about a
  field ("You confirmed <value>" / "Not printed (you confirmed)"), stored as
  a small JSON on the field row itself, next to the status the decision also
  sets. Because it lives ON the row, a later rules or confidence re-run —
  neither of which writes ``invoice_doc_fields`` — can never overwrite or
  un-settle it, and every reader (the review model, the export, a report)
  sees the same answer without consulting the corrections log.

The shape is declared in ``src.invoice_extract.doc_extract_schema``
(``DOC_EXTRACT_COLUMNS``), which the module init and the migration share, so a
fresh ``init_invoice_extract`` and a migrated database can never disagree.
Idempotent: the ALTER is skipped when the column already exists, and the
statement runs inside the framework's own ``BEGIN``/``COMMIT``.

Note for readers of the column: ``review_json`` holds ONLY human decisions;
the handwriting guard's note never reaches the database (it is computed in
memory during extraction), so a JSON here is always a review decision.
"""

from __future__ import annotations

import sqlite3

from src.migrations.framework import Migration


def apply(conn: sqlite3.Connection) -> None:
    from src.invoice_extract.doc_extract_schema import execute_doc_extract_schema

    execute_doc_extract_schema(conn)


MIGRATION = Migration(
    version=8,
    name="invoice_field_review",
    apply=apply,
)
