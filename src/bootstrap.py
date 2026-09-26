"""Single, idempotent boot sequence for the whole application.

Every module exposes an ``init_*()`` that creates its schema and seeds its
first-run defaults. Historically each entry point called a *hand-maintained
subset* of them (``main.py`` called only ``db.init_db()``; the Reflex entry
point called four more), so a genuinely fresh database was left without whole
families of tables. The UI then read those missing tables at page-load time
and the failure surfaced as a client-side crash rather than a clear error —
e.g. ``AdminState`` built its users, then the per-user edit-history lookup hit
a missing ``edit_history_entries`` table and aborted mid-load, leaving
``user_history`` empty and the ``rx.foreach`` renderer reading
``user_history[username].length`` on ``undefined``.

Having ONE list of modules here means a new module cannot be forgotten at one
entry point and remembered at another. Every ``init_*`` is additive and
idempotent, so this is safe to run on every boot (including against an
existing, already-populated ``db/poc.db``).

Ordering follows the build dependency order: identity first, then the
configuration each later module seeds from (rules/taxonomy before the modules
that read them, etc.). Imports are performed lazily inside the function so
this module stays free of import-order surprises between services.
"""

from __future__ import annotations

from pathlib import Path

# (label, module path, init function name) in boot order. The label is only
# used in the aggregated error message below.
_BOOT_STEPS: list[tuple[str, str, str]] = [
    ("F1  auth", "src.auth.service", "init_auth"),
    ("C3  settings", "src.settings.service", "init_settings"),
    ("F2  clients", "src.clients.service", "init_clients"),
    ("C1  rules", "src.rules.service", "init_rules"),
    ("C3x ai_models", "src.ai_models.service", "init_ai_models"),
    ("C5  instruction library", "src.c5.service", "init_c5"),
    ("C4  vault", "src.vault.service", "init_vault"),
    ("F3  documents", "src.documents.service", "init_documents"),
    ("F6  format registry", "src.f6.service", "init_f6"),
    ("F3-AI ingestion", "src.ingestion_ai.service", "init_ingestion_ai"),
    ("F3-B invoice extract", "src.invoice_extract.service", "init_invoice_extract"),
    ("F5  data integrity", "src.f5.service", "init_f5"),
    ("C2  filing", "src.filing.service", "init_filing"),
    ("F4  edit history", "src.f4.service", "init_f4"),
    ("Module 2 reconciliation", "src.module2.service", "init_module2"),
    ("Module 8 action center", "src.action_center.service", "init_action_center"),
]


def init_all(db_path: Path | str | None = None) -> None:
    """Create every module's schema and seed first-run defaults.

    Raises ``RuntimeError`` listing *every* module that failed, so a broken
    boot names all of the problems at once instead of stopping at the first.
    """
    from src import db

    db.init_db(db_path)

    import importlib

    failures: list[str] = []
    for label, module_path, func_name in _BOOT_STEPS:
        try:
            module = importlib.import_module(module_path)
            getattr(module, func_name)(db_path)
        except Exception as exc:  # noqa: BLE001 — aggregate, then fail loudly
            failures.append(f"{label}: {type(exc).__name__}: {exc}")

    if failures:
        raise RuntimeError(
            "Database initialisation failed for "
            f"{len(failures)} module(s):\n  - " + "\n  - ".join(failures)
        )
