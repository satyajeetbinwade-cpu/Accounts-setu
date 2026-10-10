"""Client-identity migration package.

Public surface:

* ``run`` / ``write_report`` — the migration orchestrator (also called at boot).
* ``framework`` — the versioned, idempotent schema-migration runner.
* ``identity`` — the single name → client_id resolver (do not add another).
"""

from __future__ import annotations

from src.migrations.framework import Migration, run_migrations
from src.migrations.migrate import already_migrated, run, write_report

__all__ = [
    "Migration",
    "run_migrations",
    "run",
    "write_report",
    "already_migrated",
]
