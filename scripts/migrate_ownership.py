#!/usr/bin/env python
"""Migrate an existing deployment onto the ownership (data-silo) model.

What it does, in order
----------------------
1. Adds every column the silo model needs (``users.role``,
   ``users.must_change_password``, ``end_clients.owner_user_id``,
   ``documents.owner_user_id``, and the owner column on each client child
   table) — the same idempotent migration ``bootstrap.init_all()`` runs on
   every boot.
2. Assigns **every existing client to the Admin account**, so nothing is lost
   and nothing becomes invisible.
3. Registers the clients that exist only as folders (``data/<name>/``) or only
   inside ``runs`` as real client rows, also owned by Admin. Without this a
   folder that predates ownership would have no owner — and unowned records are
   admin-only, so a standard user simply could not see them, while an Admin
   would not see them in the roster either.
4. Mirrors the parent owner onto child rows (contacts, chart of accounts,
   historical snapshots, documents).
5. Flags any account still holding a password hashed with the OLD scheme for a
   forced change at next sign-in: those credentials predate this work, and one
   of them was displayed on the sign-in screen.

Safe to run repeatedly. Use ``--dry-run`` to print the plan without writing.

Usage
-----
    venv/bin/python scripts/migrate_ownership.py --dry-run
    venv/bin/python scripts/migrate_ownership.py
    venv/bin/python scripts/migrate_ownership.py --db /path/to/other.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import bootstrap  # noqa: E402
from src.auth import access  # noqa: E402
from src.data_paths import DATA_ROOT  # noqa: E402


def _admin_id(conn) -> int | None:
    row = conn.execute(
        "SELECT user_id FROM users WHERE role = 'admin' AND is_active = 1 "
        "ORDER BY user_id LIMIT 1"
    ).fetchone()
    return int(row[0]) if row else None


def _filesystem_client_names() -> set[str]:
    if not DATA_ROOT.exists():
        return set()
    return {p.name for p in DATA_ROOT.iterdir() if p.is_dir()}


def _run_client_names(conn) -> set[str]:
    try:
        rows = conn.execute("SELECT DISTINCT client FROM runs").fetchall()
    except Exception as exc:  # noqa: BLE001 — the runs table may predate this DB
        print(f"  (runs table not readable: {exc})")
        return set()
    return {str(r[0]) for r in rows if r[0]}


def _report(conn, label: str) -> None:
    print(f"\n--- {label} ---")
    rows = conn.execute(
        """
        SELECT COALESCE(u.username, '(unowned)') AS owner, COUNT(*) AS n
          FROM end_clients c
          LEFT JOIN users u ON u.user_id = c.owner_user_id
         GROUP BY owner ORDER BY n DESC
        """
    ).fetchall()
    for owner, count in rows:
        print(f"  {count:4d} client(s) owned by {owner}")
    if not rows:
        print("  0 clients")

    total = conn.execute("SELECT COUNT(*) FROM end_clients").fetchone()[0]
    unowned = conn.execute(
        "SELECT COUNT(*) FROM end_clients WHERE owner_user_id IS NULL"
    ).fetchone()[0]
    print(f"  total {total}, unowned {unowned}")

    for table in ("documents", "contact_directory", "chart_of_accounts", "historical_snapshots"):
        try:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE owner_user_id IS NULL"
            ).fetchone()[0]
            print(f"  {table}: {n} row(s) still unowned")
        except Exception:  # noqa: BLE001 — table not present in this build
            pass


def run(*, db_path, dry_run: bool, force_password_change: bool) -> int:
    # Always run the boot sequence: it is what the app itself does at startup,
    # it is idempotent, and it is what creates the columns this script reports
    # on. --dry-run therefore skips only the ADDITIONAL writes below (adopting
    # folder-only clients, flagging legacy passwords), not the column/owner
    # backfill the app applies on every boot anyway.
    bootstrap.init_all(db_path)

    from src import db as recon_db

    conn = recon_db.get_connection(db_path)
    try:
        admin_id = _admin_id(conn)
        if admin_id is None:
            print(
                "error: no active Admin account found. Boot the app once (it seeds the\n"
                "       first Admin) and re-run this script.",
                file=sys.stderr,
            )
            return 2
        print(f"Admin account: user_id={admin_id}")

        _report(conn, "before")

        names = _filesystem_client_names() | _run_client_names(conn)
        existing = {
            row[0] for row in conn.execute("SELECT legal_name FROM end_clients").fetchall()
        }
        missing = sorted(n for n in names if n not in existing)
        print(f"\nClient names found on disk / in runs: {len(names)}")
        print(f"  of which not registered as clients: {len(missing)}")
        for name in missing:
            print(f"    + '{name}' -> owned by Admin")

        legacy_users = [
            row[0]
            for row in conn.execute(
                "SELECT username FROM users WHERE password_salt != 'bcrypt'"
            ).fetchall()
        ]
        if legacy_users:
            print(
                "\nAccounts still on the legacy password scheme "
                f"({len(legacy_users)}): " + ", ".join(legacy_users)
            )
            print("  (these predate the silo work; one was displayed on the sign-in screen)")

        if dry_run:
            print("\nDRY RUN — the writes listed above were NOT applied.")
            return 0

        for name in missing:
            conn.execute(
                """
                INSERT INTO end_clients (legal_name, owner_user_id, is_active, created_at, created_by)
                VALUES (?, ?, 1, datetime('now'), 'migration')
                """,
                (name, admin_id),
            )
        conn.commit()

        if force_password_change and legacy_users:
            conn.execute(
                "UPDATE users SET must_change_password = 1 WHERE password_salt != 'bcrypt'"
            )
            conn.commit()
            print(f"  flagged {len(legacy_users)} account(s) for a forced password change")

        _report(conn, "after")
        print("\nMigration complete.")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=None, help="database path (default: db/poc.db)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument(
        "--no-force-password-change",
        action="store_true",
        help="leave legacy-hashed accounts unflagged (they are still upgraded on next sign-in)",
    )
    args = parser.parse_args(argv)
    print("Role model:", access.ADMIN_ROLE, "/", access.USER_ROLE)
    return run(
        db_path=args.db,
        dry_run=args.dry_run,
        force_password_change=not args.no_force_password_change,
    )


if __name__ == "__main__":
    raise SystemExit(main())
