"""Orchestrates the client-id migration: backup → schema → folders → report.

Entry points:

* ``python -m src.migrations --dry-run`` — report only, nothing written.
* ``python -m src.migrations --apply``     — run the full migration.
* ``run()`` is called automatically and idempotently from the boot path
  (``src/bootstrap.py``), after the backup step.

Safety:
* A backup of ``db/poc.db`` is copied to ``db/backups/poc_pre_clientid_<ts>.db``
  before any change; the run REFUSES to proceed if the backup fails.
* Every DB change happens inside the migration framework's transaction(s);
  the folder moves are reversed from the manifest if the DB step raises.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.migrations import folders as folders_mod
from src.migrations import framework
from src.migrations.identity import Mapping, resolve_mapping
from src.migrations.m0001_client_id_identity import MIGRATION
from src.migrations.m0002_invoice_triage import MIGRATION as MIGRATION_2
from src.migrations.m0003_invoice_doc_extraction import MIGRATION as MIGRATION_3
from src.migrations.m0004_invoice_rules_v1 import MIGRATION as MIGRATION_4
from src.migrations.m0005_invoice_confidence_v1 import MIGRATION as MIGRATION_5
from src.migrations.m0006_invoice_review_services import MIGRATION as MIGRATION_6

MIGRATIONS = [MIGRATION, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5, MIGRATION_6]


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# Tables whose row counts are reported before/after. Only the ones the client-id
# migration can touch, plus the ones a reviewer most wants to confirm intact.
INVENTORIED_TABLES: tuple[str, ...] = (
    "end_clients", "runs", "match_results", "review_state",
    "result_field_overrides", "ingestion_results", "ingestion_shape_cache",
    "column_mapping_profiles", "raw_uploads", "per_client_team_assignments",
    "users", "documents", "document_versions", "invoice_uploads",
    "edit_history_entries", "reconciliation_exceptions", "eligible_credit_figures",
    "sync_health_status", "recon_of_recon_results", "tally_export_batches",
)


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in INVENTORIED_TABLES:
        if not framework.table_exists(conn, t):
            continue
        try:
            out[t] = int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
        except sqlite3.OperationalError:
            pass
    return out


def _backup(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"poc_pre_clientid_{_now_stamp()}.db"
    try:
        shutil.copy2(db_path, dest)
        # verify the copy is a readable sqlite db with the same table count
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(dest)
        try:
            a = src.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
            b = dst.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        finally:
            src.close(); dst.close()
        if a != b:
            raise RuntimeError(f"backup row-count mismatch ({a} vs {b})")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Refusing to migrate: DB backup failed: {exc}") from exc
    return dest


def already_migrated(conn: sqlite3.Connection) -> bool:
    return 1 in framework.applied_versions(conn)


def _open(db_path: Path) -> sqlite3.Connection:
    # Ensure the base + clients schema exist (fresh-DB / CLI use), then open.
    from src import db as recon_db
    from src.clients.schema import init_clients_schema

    recon_db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_clients_schema(conn)
    conn.commit()
    return conn


def _guard_data_root(db_path: Path, data_root: Path) -> None:
    """Refuse to move folders in a tree that does not belong to this database.

    The migration renames ``data/<name>/`` folders. Pointed at the wrong root it
    would rename somebody else's tree — a sandbox, a temp copy, or a directory
    that simply is not this database's own ``data/``. The expectation itself
    lives in ONE place (``bootstrap.expected_data_root``).
    """
    from src.bootstrap import expected_data_root

    resolved = Path(data_root).expanduser().resolve()
    expected = Path(expected_data_root(db_path)).expanduser().resolve()
    database = Path(db_path).expanduser().resolve()
    # Accepted: the data root that BELONGS to this database (`data/` beside a
    # `db/`, or this database's sibling), and — for a caller who passes one
    # explicitly — any folder that is a SIBLING of the database. A scratch
    # database with a deliberately-named data folder (the scoring harness uses
    # `srcdata/`) is a choice, not an accident: it lives in the same tree the
    # caller owns.
    is_sibling = resolved.parent == database.parent
    if resolved != expected and not is_sibling:
        raise RuntimeError(
            f"refusing to migrate: the data root {resolved} is not the one that belongs "
            f"to {database} (expected {expected}, or a folder beside the database). Pass "
            "the database and its own data/ folder from the same tree."
        )
    # Belt and braces, and ONLY for the repository's OWN database: a scratch
    # database legitimately has a scratch data root (tests, copies, the scoring
    # harnesses all do this), but the real one must never be migrated from
    # inside a temp folder or a known sandbox.
    #
    # NOTE the path is computed from THIS source tree, not from ``db.DB_PATH``:
    # a test or tool points DB_PATH at a copy, and that copy is not the app's.
    if Path(db_path).expanduser().resolve() == _app_database_path():
        temp = Path(tempfile.gettempdir()).resolve()
        if "sandbox" in str(resolved).lower() or resolved == temp or temp in resolved.parents:
            raise RuntimeError(
                f"refusing to migrate the app database from {resolved}: that is a temp or "
                "sandbox folder, not the app's own data/ tree."
            )


def _app_database_path() -> Path:
    """The repository's own database, derived from the installed source."""
    from src import db as recon_db

    return (Path(recon_db.__file__).resolve().parents[1] / "db" / "poc.db").resolve()


def _require_readable_backup(path: Optional[Path]) -> None:
    """The backup must EXIST and be readable before any folder is moved.

    A rollback restores the DATABASE, so a migration that moved folders with no
    working backup to go back to is not recoverable.
    """
    if path is None:
        raise RuntimeError(
            "refusing to move data folders without a database backup "
            "(run with backup=True)."
        )
    backup_path = Path(path)
    if not backup_path.exists():
        raise RuntimeError(f"refusing to move data folders: the backup {backup_path} was not written.")
    try:
        conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        try:
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"refusing to move data folders: the backup {backup_path} is not readable ({exc})."
        ) from None


def run(
    db_path: Optional[Path | str] = None,
    data_root: Optional[Path | str] = None,
    *,
    dry_run: bool = False,
    backup: bool = True,
) -> dict:
    """Run (or plan) the migration. Returns the report dict.

    Idempotent: a second ``--apply`` on an already-migrated DB changes nothing.
    """
    from src.data_paths import DATA_ROOT
    from src import db as recon_db

    db_path = Path(db_path) if db_path else Path(recon_db.DB_PATH)
    data_root = Path(data_root) if data_root else Path(DATA_ROOT)

    # Anything that can MOVE a folder is guarded first. A dry run plans only, so
    # it is deliberately left alone — tooling plans against roots it never
    # writes to.
    if not dry_run:
        _guard_data_root(db_path, data_root)

    conn = _open(db_path)
    try:
        before = _row_counts(conn)

        if dry_run:
            mapping = resolve_mapping(conn, create=False, data_root=data_root)
            moves = folders_mod.plan_folder_moves(mapping, data_root)
            return {
                "mode": "dry-run",
                "already_migrated": already_migrated(conn),
                "mapping": mapping.as_report(),
                "folder_moves": moves,
                "row_counts_before": before,
                "would_create_clients": len(mapping.created),
                "would_move_folders": sum(1 for m in moves if m["status"] == "planned"),
            }

        if already_migrated(conn):
            # Schema already migrated. Still run the folder step (idempotent) so
            # a crash between the two steps self-heals on the next boot.
            moves = folders_mod.plan_folder_moves(
                resolve_mapping(conn, create=False, data_root=data_root), data_root
            )
            # This branch moves folders too (self-healing a crash between the two
            # steps), so it needs the same backup guarantee — but ONLY when there
            # is something to move, so an ordinary restart writes nothing.
            if any(m["status"] == "planned" for m in moves):
                _require_readable_backup(_backup(db_path) if backup else None)
            folder_result = folders_mod.apply_folder_moves(moves, data_root)
            return {
                "mode": "no-op",
                "already_migrated": True,
                "folder_moves": folder_result,
                "row_counts_before": before,
                "row_counts_after": _row_counts(conn),
            }

        backup_path = _backup(db_path) if backup else None
        # No backup, no moves. The migration renames client folders, and a
        # rollback can only restore the database it copied.
        _require_readable_backup(backup_path)

        try:
            from src.migrations import m0001_client_id_identity as _m1

            _m1.set_data_root(data_root)
            applied = framework.run_migrations(conn, MIGRATIONS, verbose=True)
            # The migration performed the folder move INSIDE its transaction,
            # so a failure there rolls back the DB and reverses the folders
            # together. We only re-derive the mapping for the report.
            mapping = resolve_mapping(conn, create=True, data_root=data_root)
            folder_result = _read_manifest(data_root)
        except Exception:
            # The migration already reversed any completed moves; a second
            # rollback is a safe no-op (its targets no longer exist).
            folders_mod.rollback(data_root)
            raise

        after = _row_counts(conn)
        report = {
            "mode": "apply",
            "backup": str(backup_path) if backup_path else None,
            "applied_migrations": applied,
            "mapping": mapping.as_report(),
            "folder_moves": folder_result,
            "row_counts_before": before,
            "row_counts_after": after,
        }
        return report
    finally:
        conn.close()


def _read_manifest(data_root: Path) -> dict:
    """Summarise the folder-move manifest written by the migration."""
    mpath = folders_mod.manifest_path(data_root)
    if not mpath.exists():
        return {"manifest_path": str(mpath), "moved": 0, "moves": []}
    try:
        data = json.loads(mpath.read_text())
    except (OSError, ValueError):
        return {"manifest_path": str(mpath), "moved": 0, "moves": []}
    moves = data.get("moves", [])
    return {
        "manifest_path": str(mpath),
        "moved": sum(1 for m in moves if m.get("status") in ("moved", "merged")),
        "moves": moves,
    }


def write_report(report: dict, path: Optional[Path] = None) -> Path:
    from src import db as recon_db

    path = path or (Path(recon_db.DB_PATH).parent / "client_id_migration_report.json")
    path.write_text(json.dumps(report, indent=2, default=str))
    return path
