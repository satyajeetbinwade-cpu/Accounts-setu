"""Migration 0004 — Invoice Extraction Phase 3: the deterministic rules layer.

Purely additive. Creates the five tables the Phase-3 layer writes:

* ``invoice_doc_derived``       — values calculated from printed ones.
* ``invoice_doc_checks``        — pass / fail / not_applicable per check.
* ``invoice_doc_applicability`` — required / optional / not applicable per field.
* ``invoice_doc_direction``     — sales / purchase / likely_* / undetermined.
* ``client_aliases``            — reviewer-confirmed client name aliases.

Plus the C1 side of Phase 3, both of which are data rather than schema: the GST
state-code table (``gst_state_codes``) that the state checks resolve against.
It is created here so a migrated database has it, and seeded by the normal C1
``init_rules`` boot step (the same path a fresh database takes).

Nothing here touches an existing table, an existing row, or an extracted value:
the deterministic layer only READS ``invoice_doc_fields``.

Idempotent: every statement is ``CREATE ... IF NOT EXISTS``, and the C1 seeding
is the additive-upsert seed the app already runs on every boot.
"""

from __future__ import annotations

import sqlite3

from src.invoice_extract.rules_schema import execute_rules_schema
from src.migrations.framework import Migration
from src.rules.gst_state_codes import execute_state_code_schema


def apply(conn: sqlite3.Connection) -> None:
    execute_rules_schema(conn)
    execute_state_code_schema(conn)


MIGRATION = Migration(
    version=4,
    name="invoice_rules_v1",
    apply=apply,
)
