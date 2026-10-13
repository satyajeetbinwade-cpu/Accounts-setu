"""Explicit boot entry point: create every schema, seed defaults, migrate.

SAFETY FIRST — this file used to run the whole boot sequence before argparse saw
argv, so ``python main.py --help`` migrated the live database and MOVED the real
client folders. It now parses argv at the very top, imports the app only inside
the handlers, and refuses to migrate unless it is asked to:

    python main.py --check      READ-ONLY. Prints the database path, the data
                                root that belongs to it, and which migrations
                                are applied and pending. Changes nothing.
    python main.py --migrate    Run the boot sequence: schema, seeds, and the
                                client-id migration (which renames data
                                folders). The ONLY command-line way to migrate
                                from here.
    python main.py --help       Usage. Nothing is imported, nothing is touched.
    python main.py              Refused — with the two flags above in the
                                message — UNLESS ``SETU_ALLOW_BOOT_MIGRATE=1``
                                is set. The container CMD sets it, so a Fly boot
                                still migrates, and still fails closed.

The app itself (``setu/setu.py``, ``reflex run``) never migrates: it only checks
the schema version and raises if it is behind.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: The environment variable that lets a BARE ``python main.py`` migrate. Set by
#: the Dockerfile CMD. Anything else must pass --migrate explicitly.
ALLOW_ENV = "SETU_ALLOW_BOOT_MIGRATE"

#: The exit code for "refused, and nothing was changed".
EXIT_REFUSED = 2

REFUSAL = f"""Refusing to start: a bare `python main.py` is not allowed to run migrations.

The boot sequence creates every schema, seeds defaults, and runs the client-id
migration — which RENAMES data folders under the data root. That must be asked
for, not inherited from a typo: `python main.py --help` used to do it.

  python main.py --check      read-only: database path, applied and pending migrations
  python main.py --migrate    run the boot sequence (schema, seeds, migrations)

A container start is allowed to migrate: the CMD sets {ALLOW_ENV}=1, so
deployments keep working and still fail closed.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=(
            "Boot the database: schema, seeds, and the client-id migration. "
            "Requires an explicit flag unless " + ALLOW_ENV + "=1 is set."
        ),
        epilog=(
            "The app itself (reflex run) never migrates — it only checks that the "
            "schema is current and says which command to run if it is not."
        ),
    )
    parser.add_argument(
        "--migrate", action="store_true",
        help="run the boot sequence: schema, seeds, and the client-id folder migration",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="read-only: print the database path and the applied / pending migration numbers",
    )
    return parser


# ---------------------------------------------------------------------------
# Handlers — the app is imported HERE, never at module import time
# ---------------------------------------------------------------------------


def _check() -> int:
    """Read-only report. Opens the database ``mode=ro``; changes nothing."""
    from src import bootstrap

    status = bootstrap.migration_status()
    print(f"database:  {status['db_path']}")
    print(f"data root: {status['data_root']}")
    if status["error"]:
        print(f"state:     {status['error']}")
    else:
        print(f"applied:   {status['applied']}")
        print(f"pending:   {status['pending']}")
    if status["current"]:
        print("schema is current.")
        return 0
    print(f"schema is NOT current — {bootstrap.MIGRATION_HINT}")
    return 1


def _boot() -> int:
    """The original behaviour: migrate, then verify what is there."""
    from src import bootstrap
    from src import db
    from src.config_loader import load_config

    bootstrap.run_migrations()
    config = load_config()

    # Verify both tables exist.
    conn = db.get_connection()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn.close()

    print("Database initialized at:", db.DB_PATH)
    print(
        "Tables present: runs =",
        "runs" in tables,
        "| match_results =",
        "match_results" in tables,
    )
    print("Config loaded:", sorted(config.keys()))
    return 0


def main(argv: list[str] | None = None) -> int:
    # ARGV FIRST. Nothing may run before the parse: --help must be free, and an
    # unknown flag must fail without touching anything.
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.check and args.migrate:
        print(
            "error: --check is read-only and --migrate writes; pass one of them.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if args.check:
        return _check()
    if args.migrate:
        return _boot()

    # A bare start. A container is allowed to migrate (its CMD sets the
    # variable); a human is told what to type instead.
    if os.environ.get(ALLOW_ENV) == "1":
        return _boot()

    print(REFUSAL, file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
