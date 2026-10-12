"""Migration 0005 — Invoice Extraction Phase 4: evidence-based confidence.

Purely additive. Two tables:

* ``invoice_doc_field_scores``   — the score for each stored field, with the
  signals and caps that produced it.
* ``invoice_field_corrections``  — every human decision about a field, including
  "confirmed as is", which is the record that makes calibration possible.

Nothing here changes an existing table, an existing row or an extracted value:
the confidence layer READS the Phase-2 field rows and the Phase-3 check rows and
writes only its own two tables.

Idempotent: both statements are ``CREATE ... IF NOT EXISTS``. The DDL is shared
with ``src.invoice_extract.confidence_schema``, so a fresh database built by
``init_invoice_extract`` has the same shape a migrated one does.
"""

from __future__ import annotations

import sqlite3

from src.invoice_extract.confidence_schema import execute_confidence_schema
from src.migrations.framework import Migration


def apply(conn: sqlite3.Connection) -> None:
    execute_confidence_schema(conn)


MIGRATION = Migration(
    version=5,
    name="invoice_confidence_v1",
    apply=apply,
)
