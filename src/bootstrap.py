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

import sqlite3
from pathlib import Path
from typing import Optional

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
    # Phase 2: a document extraction left "running" by a stopped process goes
    # back to "queued" so resume_extraction can pick it up.
    ("F3-B doc extraction v2", "src.invoice_extract.extract", "init_extract"),
    # Phase 3: the deterministic rules layer (derived values, checks,
    # applicability, direction). Marks documents whose stored field set has
    # changed since the rules last ran, without recomputing them at boot.
    ("F3-B invoice rules v1", "src.invoice_extract.rules", "init_rules_v1"),
    ("F5  data integrity", "src.f5.service", "init_f5"),
    ("C2  filing", "src.filing.service", "init_filing"),
    ("F4  edit history", "src.f4.service", "init_f4"),
    ("Module 2 reconciliation", "src.module2.service", "init_module2"),
    ("Module 8 action center", "src.action_center.service", "init_action_center"),
]

# The one instruction that resolves a pending migration. ``str()`` of
# ``SchemaMigrationPending`` is exactly this string.
MIGRATION_HINT = "run: python main.py --migrate"

# Sentinel for "caller did not choose a data root": scope it to the DB.
_SCOPE_DATA_ROOT = object()


def _required_versions() -> list[int]:
    """The migration versions this code expects to be applied."""
    from src.migrations.migrate import MIGRATIONS

    return sorted(int(m.version) for m in MIGRATIONS)


# (label, table, column): columns an ``init_*`` step adds to an EXISTING table
# with ``ALTER TABLE``. A version number cannot express these — the step simply
# brings the old table forward on every boot — so a database can hold every
# migration version and still be missing one. Verified read-only below, because
# the failure mode is otherwise invisible until a request needs the column.
_REQUIRED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("F1  auth", "users", "end_client_id"),
    ("F1  auth", "users", "role"),
    ("F1  auth", "users", "must_change_password"),
    ("F1  auth", "users", "failed_login_count"),
    ("F1  auth", "users", "locked_until"),
)


def _missing_boot_columns(conn: sqlite3.Connection) -> list[str]:
    """READ-ONLY: the ``table.column`` pairs this database is missing.

    ``schema_migrations`` records the versioned migrations only, while some
    ``init_*`` steps also carry an old table forward with ``ALTER TABLE`` — F1's
    access-role and login-lockout columns on ``users`` are the ones that arrive
    that way. Those never bump a version, so a database can be "current" by
    version and still be missing a column: it boots, and the failure surfaces
    later as a raw ``sqlite3.OperationalError: no such column`` in the middle of
    a request — exactly how the lockout columns broke sign-in. Reporting them
    here turns that into the boot refusal the operator can act on.

    A table that is entirely absent counts as missing all of its columns.
    """
    try:
        columns: dict[str, set[str]] = {}
        for _label, table, _column in _REQUIRED_COLUMNS:
            if table not in columns:
                columns[table] = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
    except sqlite3.Error:
        # Unreadable or not a database: the caller's version read reports that
        # already, and a status/gate function must not raise from here.
        return []
    return [
        f"{table}.{column}"
        for _label, table, column in _REQUIRED_COLUMNS
        if column not in columns[table]
    ]


def expected_data_root(db_path: Path | str | None = None) -> Path:
    """The data root that BELONGS to this database.

    One rule, so the migration cannot be pointed at somebody else's folders:

    * a database under a ``db/`` folder owns the ``data/`` folder beside that
      one — the deployed layout, where ``/app/persist/db/poc.db`` is matched by
      ``/app/persist/data`` (and the repo's ``db/poc.db`` by its ``data/``);
    * any other database owns a ``data/`` folder next to itself — which is what
      a scratch database, a copy, or a test gets.
    """
    from src import db

    path = Path(db_path) if db_path else Path(db.DB_PATH)
    if path.parent.name == "db":
        return path.parent.parent / "data"
    return path.parent / "data"


def migration_status(db_path: Path | str | None = None) -> dict:
    """READ-ONLY: the database path, what is applied, and what is pending.

    Opens the file ``mode=ro`` — this never creates, migrates or seeds
    anything, and it is safe to run against the live database.
    """
    import sqlite3

    from src import db

    path = Path(db_path) if db_path else Path(db.DB_PATH)
    required = _required_versions()
    applied: list[int] = []
    missing_columns: list[str] = []
    error: Optional[str] = None
    if not path.exists():
        error = "the database file does not exist"
    else:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            error = f"cannot open it read-only: {exc}"
        else:
            try:
                try:
                    applied = sorted(
                        int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
                    )
                except sqlite3.Error:
                    # No schema_migrations table: a pre-migration database.
                    applied = []
                missing_columns = _missing_boot_columns(conn)
            finally:
                conn.close()
    pending = sorted(set(required) - set(applied))
    return {
        "db_path": str(path),
        "data_root": str(expected_data_root(path)),
        "applied": applied,
        "required": required,
        "pending": pending,
        "missing_columns": missing_columns,
        "current": not pending and not missing_columns and error is None,
        "error": error,
    }


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
        missing_columns = _missing_boot_columns(conn)
    finally:
        conn.close()

    missing = sorted(set(required) - applied)
    if missing or missing_columns:
        detail = f"{where}; applied={sorted(applied)} missing={missing}"
        if missing_columns:
            # Versions can be complete while an ALTER-carrying init_* has never
            # run: name the columns, or the operator sees "nothing pending".
            detail += f"; missing columns: {missing_columns}"
        raise SchemaMigrationPending(detail)


def run_migrations(
    db_path: Path | str | None = None,
    *,
    data_root: Path | str | None | object = _SCOPE_DATA_ROOT,
    backup: bool = True,
    dry_run: bool = False,
    allow_real_db: bool = False,
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
            allow_real_db=allow_real_db,
        )
        # A pipeline chain left RUNNING by a previous process is INTERRUPTED —
        # not complete (nothing was read) and not failed (nothing went wrong).
        # The review screen shows it as resumable instead of finished.
        try:
            from src.invoice_extract import pipeline as _pipeline

            _pipeline.recover_interrupted_chains(db_path=db_path)
        except Exception:  # noqa: BLE001 — recovery must never block a boot
            pass

        if report.get("mode") == "apply":
            # Report next to the DB that was migrated, never the repo default.
            resolved = Path(db_path) if db_path else Path(db.DB_PATH)
            report_path = _migrate.write_report(
                report, resolved.parent / "client_id_migration_report.json"
            )
            report["report_path"] = str(report_path)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"client-id migration: {type(exc).__name__}: {exc}")

    # A failed migration leaves the schema at its OLD version. Running the
    # init_* steps on it is exactly what the migrate-FIRST ordering above
    # exists to prevent (an init_* over a post-migration column shape aborts
    # on the legacy table), and a failed boot must leave the database as it
    # found it: report the migration failure and stop here.
    if not failures:
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
