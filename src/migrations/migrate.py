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


def run(
    db_path: Optional[Path | str] = None,
    data_root: Optional[Path | str] = None,
    *,
    dry_run: bool = False,
    backup: bool = True,
    allow_real_db: bool = False,
) -> dict:
    """Run (or plan) the migration. Returns the report dict.

    Idempotent: a second ``--apply`` on an already-migrated DB changes nothing.

    Refuses to touch the repository's own ``db/poc.db`` unless the caller says
    ``allow_real_db=True``. Every path through here writes something — including
    the planner's edit-history rows, which is exactly how 52 rows once landed in
    the real database from a test run.
    """
    from src import db as _db

    _db.require_real_db_allowed(db_path, allow_real_db=allow_real_db)
    from src.data_paths import DATA_ROOT
    from src import db as recon_db

    db_path = Path(db_path) if db_path else Path(recon_db.DB_PATH)
    data_root = Path(data_root) if data_root else Path(DATA_ROOT)

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
            folder_result = folders_mod.apply_folder_moves(moves, data_root)
            return {
                "mode": "no-op",
                "already_migrated": True,
                "folder_moves": folder_result,
                "row_counts_before": before,
                "row_counts_after": _row_counts(conn),
            }

        backup_path = _backup(db_path) if backup else None

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
