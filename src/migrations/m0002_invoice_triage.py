"""Migration 0002 — Invoice Extraction triage foundation (Phase 1).

Purely additive. Creates the "one file, many documents" tables and the token
accounting table, and adds a handful of nullable columns to two existing
tables. It rebuilds NOTHING: every existing row and every existing upload keeps
working unchanged.

* ``invoice_triage_runs`` / ``invoice_triage_pages`` — one triage pass, its
  pages, and their per-page outcome.
* ``invoice_documents`` / ``document_regions`` / ``document_region_log`` — the
  document model and its audit trail.
* ``invoice_uploads`` gains ``pages_examined`` / ``pages_total`` /
  ``text_truncated`` (the legacy path's honesty fields).
* ``extracted_invoice_fields`` gains a nullable ``document_id`` (Phase 2 seam,
  unused now — deliberately NOT a foreign key so Phase 2 can decide the
  relationship without a second migration).
* ``ai_call_log`` — per-call token/cost accounting.

Idempotent: both the DDL (``CREATE ... IF NOT EXISTS``) and the column adds
(guarded by ``PRAGMA table_info``) are safe to re-run. The DDL itself is shared
with the module inits (``src.invoice_extract.triage_schema`` and
``src.ai_models.schema``), so the migration and a fresh ``init_*`` can never
disagree about the shape.

We deliberately reuse the framework's transaction: every statement runs inside
the framework's ``BEGIN``/``COMMIT``, so a failure rolls the whole migration
back and leaves the recorded version untouched. Nothing here commits early.
"""

from __future__ import annotations

import sqlite3

from src.migrations.framework import Migration
from src.ai_models.schema import execute_ai_call_log_schema
from src.invoice_extract.triage_schema import execute_triage_schema


def apply(conn: sqlite3.Connection) -> None:
    execute_triage_schema(conn)
    execute_ai_call_log_schema(conn)


MIGRATION = Migration(
    version=2,
    name="invoice_triage",
    apply=apply,
)
