"""Migration 0006 — the review screen's backend services.

Purely additive. Creates the two tables the review services write:

* ``invoice_document_log``    — the audit trail for a human action on a
  document (category change, exclusion, inclusion, review mark, and the
  automatic un-mark that follows a re-extraction or a new correction). The
  REASON is a column, not a JSON blob, because a reason that cannot be queried
  cannot be audited.
* ``invoice_document_review`` — "this document has been looked over", with the
  formula version and threshold that were in force when a human signed it off.

Why a new log table and not ``document_region_log``: that one is the REGION
audit trail (it has no ``reason`` column, no ``upload_id``, and triage already
writes region creates and ``page_unassigned`` rows into it).

Nothing here touches an existing table, an existing row, or an extracted value.
Idempotent: every statement is ``CREATE ... IF NOT EXISTS``.

We deliberately reuse the framework's transaction: every statement runs inside
the framework's ``BEGIN``/``COMMIT``, so a failure rolls the whole migration
back and leaves the recorded version untouched. Nothing here commits early.
"""

from __future__ import annotations

import sqlite3

from src.invoice_extract.review_schema import execute_review_schema
from src.migrations.framework import Migration


def apply(conn: sqlite3.Connection) -> None:
    execute_review_schema(conn)


MIGRATION = Migration(
    version=6,
    name="invoice_review_services",
    apply=apply,
)
