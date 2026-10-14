"""CLI for the client-id migration.

    python -m src.migrations --dry-run    # report only, nothing written
    python -m src.migrations --apply      # backup, migrate, move folders, report

Defaults to ``--dry-run`` when neither flag is given (safe default).

The work is delegated to ``src.bootstrap.run_migrations`` — the same explicit
entry point ``main.py`` uses — so the CLI and the app boot can never drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python -m src.migrations` from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import bootstrap  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.migrations")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="report only (default)")
    group.add_argument("--apply", action="store_true", help="run the migration")
    parser.add_argument("--no-backup", action="store_true", help="skip the DB backup (not recommended)")
    parser.add_argument("--db", default=None, help="path to poc.db (default: db/poc.db)")
    parser.add_argument("--allow-real-db", action="store_true", help="allow writing the repository's own db/poc.db")
    parser.add_argument(
        "--data-root", default=None,
        help="path to data/ — must be the database's OWN data folder (its sibling). "
             "Default: ./data, or the --db database's sibling under --db.",
    )
    args = parser.parse_args(argv)
    from src import db as _db
    _db.require_real_db_allowed(
        getattr(args, "db", None), allow_real_db=getattr(args, "allow_real_db", False),
    )

    dry_run = not args.apply  # --dry-run or nothing => dry-run

    # Delegate to the one explicit boot entry point, so the CLI and main.py
    # cannot drift. `data_root` is passed ONLY when the operator chose one:
    # omitting it keeps bootstrap's safe scoping (explicit --db ⇒ that DB's
    # sibling data/), which is what stops a --db run from moving the repo's
    # real client folders.
    kwargs: dict = {}
    if args.data_root:
        kwargs["data_root"] = args.data_root
    report = bootstrap.run_migrations(
        args.db,
        backup=not args.no_backup,
        dry_run=dry_run,
        **kwargs,
    )

    print(json.dumps(report, indent=2, default=str))
    path = report.get("report_path")
    if path:
        print(f"\nReport written to {path}")
    if dry_run:
        print("\n(dry-run — nothing was changed; re-run with --apply to migrate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
