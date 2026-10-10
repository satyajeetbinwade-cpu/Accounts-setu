"""CLI for the client-id migration.

    python -m src.migrations --dry-run    # report only, nothing written
    python -m src.migrations --apply      # backup, migrate, move folders, report

Defaults to ``--dry-run`` when neither flag is given (safe default).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python -m src.migrations` from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.migrations import migrate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.migrations")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="report only (default)")
    group.add_argument("--apply", action="store_true", help="run the migration")
    parser.add_argument("--no-backup", action="store_true", help="skip the DB backup (not recommended)")
    parser.add_argument("--db", default=None, help="path to poc.db (default: db/poc.db)")
    parser.add_argument("--data-root", default=None, help="path to data/ (default: ./data)")
    args = parser.parse_args(argv)

    dry_run = not args.apply  # --dry-run or nothing => dry-run
    report = migrate.run(
        db_path=args.db,
        data_root=args.data_root,
        dry_run=dry_run,
        backup=not args.no_backup,
    )
    # Write the report NEXT TO the DB that was migrated, not next to the default
    # DB — otherwise a --db run would clobber the repo's report file.
    report_path = None
    if args.db:
        report_path = Path(args.db).resolve().parent / "client_id_migration_report.json"
    path = migrate.write_report(report, report_path)
    print(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {path}")
    if dry_run:
        print("\n(dry-run — nothing was changed; re-run with --apply to migrate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
