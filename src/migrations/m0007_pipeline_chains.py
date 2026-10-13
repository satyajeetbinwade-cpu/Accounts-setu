"""Migration 0007 — the pipeline's persisted chain state.

Purely additive. Creates one table:

* ``invoice_pipeline_chains`` — where the chained triage → extraction pass keeps
  its state (``queued``, ``triage_running``, ``triage_done_extraction_pending``,
  ``extraction_running``, ``complete``, ``failed``, and ``interrupted`` after a
  restart). ``triage_done_extraction_pending`` is the row that stops a poller
  mistaking "pages read, values not yet extracted" for a finished upload.

Nothing here touches an existing table, row or value. Idempotent: the statement
is ``CREATE TABLE IF NOT EXISTS``, and it runs inside the framework's own
``BEGIN``/``COMMIT``, so a failure rolls back and leaves the recorded version
untouched.

``src/invoice_extract/pipeline.py::ensure_schema()`` remains as a harmless
safety net for a database that never ran the boot: it executes these same
statements on its own connection before any chain write.
"""

from __future__ import annotations

import sqlite3

from src.migrations.framework import Migration


def apply(conn: sqlite3.Connection) -> None:
    from src.invoice_extract.pipeline import chain_schema_statements

    for statement in chain_schema_statements():
        conn.execute(statement)


MIGRATION = Migration(
    version=7,
    name="invoice_pipeline_chains",
    apply=apply,
)