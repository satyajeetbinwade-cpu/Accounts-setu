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

-----------------------------------------------------------------------------
TWO ENTRY POINTS, DELIBERATELY ASYMMETRIC
-----------------------------------------------------------------------------
``run_migrations(db_path=None)`` is the EXPLICIT one. It creates every
module's schema, seeds first-run defaults, then applies the client-id
migration (backing up ``poc.db`` and writing the folder-move manifest). Only
``main.py`` and ``src/migrations/__main__.py`` call it.

``ensure_schema_current(db_path=None)`` is the READ-ONLY gate. It opens the
database read-only and raises ``SchemaMigrationPending`` when the schema is
behind. It never creates a table, moves a folder or writes a byte.

Why: ``setu/setu.py`` used to call the (single) ``init_all()`` at import time,
so a stray ``python -c "import setu.setu"`` silently migrated the real
``db/poc.db`` *and* moved every folder under ``data/``. That actually happened
during development. The UI import path can no longer mutate anything — it
either finds a current schema or fails loudly with the command to run.
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
    ("Phase 2 communications", "src.communications.service", "init_communications"),
    ("Phase 2 GST/Tally", "src.gst_tally_integration.service", "init_gst_tally"),
    ("F3  documents", "src.documents.service", "init_documents"),
    ("F6  format registry", "src.f6.service", "init_f6"),
    ("F3-AI ingestion", "src.ingestion_ai.service", "init_ingestion_ai"),
    ("F3-B invoice extract", "src.invoice_extract.service", "init_invoice_extract"),
    # Marks any triage run left "running" by a previous process as
    # "interrupted", so a restart never leaves a run looking live forever.
    ("F3-B invoice triage", "src.invoice_extract.triage", "init_triage"),
    ("F5  data integrity", "src.f5.service", "init_f5"),
    ("C2  filing", "src.filing.service", "init_filing"),
    ("F4  edit history", "src.f4.service", "init_f4"),
    ("Module 2 reconciliation", "src.module2.service", "init_module2"),
    ("Module 8 action center", "src.action_center.service", "init_action_center"),
]

# The one instruction that resolves a pending migration. ``str()`` of
# ``SchemaMigrationPending`` is exactly this string.
MIGRATION_HINT = "Run python main.py (or python -m src.migrations --apply) first"

# Sentinel for "caller did not choose a data root": scope it to the DB.
_SCOPE_DATA_ROOT = object()


def _required_versions() -> list[int]:
    """The migration versions this code expects to be applied."""
    from src.migrations.migrate import MIGRATIONS

    return sorted(int(m.version) for m in MIGRATIONS)


class SchemaMigrationPending(RuntimeError):
    """The database is behind the latest schema and a human must migrate it.

    ``str(exc)`` is exactly :data:`MIGRATION_HINT` — the command to run. The
    diagnostics (which file, which versions are missing) travel as a PEP 678
    exception note, so they appear in the traceback without adding noise to
    the message.
    """

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(MIGRATION_HINT)
        if detail:
            self.add_note(detail)


def ensure_schema_current(db_path: Path | str | None = None) -> None:
    """READ-ONLY: raise unless every migration is already applied.

    Opens the database in SQLite read-only mode (``mode=ro``), so it cannot
    create the file, a table, a journal or a ``-wal``/``-shm`` sidecar, and it
    never looks at ``data/``. This is what the UI import path calls: a
    demigrated/unmigrated database must stop the app rather than be silently
    rewritten behind the operator's back.
    """
    import sqlite3

    from src import db

    path = Path(db_path) if db_path else Path(db.DB_PATH)
    required = _required_versions()
    where = f"schema check for {path}: required migrations {required}"

    if not path.exists():
        raise SchemaMigrationPending(f"{where}; the database file does not exist")

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # unreadable / not a database
        raise SchemaMigrationPending(
            f"{where}; cannot open it read-only: {exc}"
        ) from None
    try:
        try:
            applied = {
                int(row[0])
                for row in conn.execute("SELECT version FROM schema_migrations")
            }
        except sqlite3.Error:
            # No schema_migrations table at all — a pre-migration database.
            applied = set()
    finally:
        conn.close()

    missing = sorted(set(required) - applied)
    if missing:
        raise SchemaMigrationPending(
            f"{where}; applied={sorted(applied)} missing={missing}"
        )


def run_migrations(
    db_path: Path | str | None = None,
    *,
    data_root: Path | str | None | object = _SCOPE_DATA_ROOT,
    backup: bool = True,
    dry_run: bool = False,
) -> dict:
    """EXPLICIT entry point: create every schema, seed defaults, then migrate.

    This is the only function that may move client folders or apply the
    client-id migration. Called by ``main.py`` (every boot) and by
    ``python -m src.migrations --apply``.

    ``data_root`` defaults to :data:`_SCOPE_DATA_ROOT`, which means "scope to
    a sibling ``data/`` of an explicitly-passed DB, else the repo's ``data/``"
    — so a temp DB can never move the real client folders. Pass ``None``
    explicitly for the repo default, or a path to override it.

    Returns the migration report. Raises ``RuntimeError`` naming every module
    that failed, so a broken boot reports all of its problems at once.
    """
    from src import db

    db.init_db(db_path)

    import importlib

    failures: list[str] = []

    # The client-id migration runs FIRST — before any module's init_* — because
    # a pre-migration database still carries the old column names, and an
    # init_* that creates an index or constraint over a post-migration column
    # (e.g. auth's `idx_team_client ... (client_id)`) aborts the whole boot when
    # run against the legacy table. Migrate first, then let every module build
    # on the final shape. Every migration section is shape-guarded, so on a
    # fresh database this is a no-op and the init_* steps below create
    # everything in its born shape. It also backs up poc.db and writes the
    # folder-move manifest; a second run is a no-op.
    report: dict = {}
    try:
        from src.migrations import migrate as _migrate

        if data_root is _SCOPE_DATA_ROOT:
            # Scope the data/ root to the DB being migrated: the real app
            # (db_path=None) uses the repo's data/, while an explicit db_path
            # (tests, tooling) uses a sibling data/ so a temp DB can NEVER
            # move the real client folders.
            data_root = None if db_path is None else (Path(db_path).parent / "data")

        report = _migrate.run(
            db_path=db_path,
            data_root=data_root,
            dry_run=dry_run,
            backup=backup,
        )
        if report.get("mode") == "apply":
            # Report next to the DB that was migrated, never the repo default.
            resolved = Path(db_path) if db_path else Path(db.DB_PATH)
            report_path = _migrate.write_report(
                report, resolved.parent / "client_id_migration_report.json"
            )
            report["report_path"] = str(report_path)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"client-id migration: {type(exc).__name__}: {exc}")

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

    # Post-condition: the migration is what makes the DB current, so verify it
    # rather than trusting that run() succeeded.
    if not dry_run:
        ensure_schema_current(db_path)

    return report
