"""Resolve duplicate GSTINs in the client master (the one-GSTIN-one-client rule).

Run:

    ./venv/bin/python resolve_duplicate_gstin.py --db db/poc.db              # report only
    ./venv/bin/python resolve_duplicate_gstin.py --db db/poc.db --apply       # back up, then fix

A GST registration is one `end_clients` row, so the roster must not hold the
same GSTIN twice. Nothing enforced that until the unique index was added, so a
database written by the older UI can contain the same company twice — and then
the index cannot be built, which is what blocks the client-id migration
(`DuplicateClientGstin`). This is the repair step for that.

It is deliberately NOT part of the migration. Deleting a client is a data
decision, and a deploy must not make one silently. This tool is the explicit
step, with a dry run, a backup, and a refusal path.

Who loses the registration: the row that OWNS DATA always keeps it. Ownership is
counted over EVERY table and column — both by `client_id` and by the client's
legal NAME, because a pre-migration database still keys several tables on the
name (`runs.client`, `review_state.client`, `ingestion_results.client_ref`, ...)
and a name-keyed row would be orphaned by deleting its client.

    exactly one row owns data   -> the other is deleted (it owns nothing)
    neither row owns data       -> the older row (lowest client_id) is kept
    both rows own data          -> REFUSED. Merging two clients that both hold
                                   data is a human decision; use
                                   --strategy blank-gstin to clear the GSTIN on
                                   the newer row instead, keeping both.

Stdlib only, so it runs on the Fly machine (`python3 /tmp/... --db
/app/persist/db/poc.db`) where this repo's other tools are not installed.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("db/poc.db")

# The only columns that hold a reference to a client. See find_references().
ID_REFERENCE_COLUMNS = ("client_id", "end_client_id")
NAME_REFERENCE_COLUMNS = ("client", "client_ref", "end_client_ref")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def duplicate_gstins(conn: sqlite3.Connection) -> list[tuple[str, list[int]]]:
    """Every GSTIN carried by more than one client, with those client ids."""
    rows = conn.execute(
        """
        SELECT gstin, GROUP_CONCAT(client_id)
          FROM end_clients
         WHERE gstin IS NOT NULL AND TRIM(gstin) <> ''
         GROUP BY gstin
        HAVING COUNT(*) > 1
         ORDER BY gstin
        """
    ).fetchall()
    return [(str(g), [int(i) for i in str(ids).split(",")]) for g, ids in rows]


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def find_references(
    conn: sqlite3.Connection, client_id: int, legal_name: str
) -> list[tuple[str, str, str, int]]:
    """Where this client is referred to: ``(table, column, how, rows)``.

    Only columns that MEAN "a client" are considered:

      * by id   — ``client_id`` / ``end_client_id``;
      * by name — ``client`` / ``client_ref`` / ``end_client_ref``, which is how
        a pre-migration database still keys several tables.

    Matching against every column instead was the first attempt and it is
    wrong: unrelated integer columns that happen to hold the same small number
    (`rule_id = 2`, `sort_order = 3`) read as references and make the tool
    refuse a perfectly safe delete. Deliberately excluded: ``client_name_at_run``
    (a historical snapshot, not a link — the identity is the run's ``client_id``),
    ``client_label`` and ``is_end_client`` (display/flags, not references).
    """
    hits: list[tuple[str, str, str, int]] = []
    for table in _tables(conn):
        if table == "end_clients":
            continue  # the row itself and its siblings are not references
        for column in _columns(conn, table):
            if column in ID_REFERENCE_COLUMNS:
                value, how = client_id, "client_id"
            elif column in NAME_REFERENCE_COLUMNS:
                value, how = legal_name, "legal_name"
            else:
                continue
            if not value:
                continue
            try:
                n = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?',
                    (value,),
                ).fetchone()[0]
            except sqlite3.Error:
                continue
            if n:
                hits.append((table, column, how, int(n)))
    return hits


def _client(conn: sqlite3.Connection, client_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM end_clients WHERE client_id = ?", (client_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"client_id {client_id} is not in end_clients")
    return row


def plan(conn: sqlite3.Connection) -> list[dict]:
    """What would be done for each duplicate GSTIN. No writes."""
    import_report: list[dict] = []
    for gstin, ids in duplicate_gstins(conn):
        members = []
        for client_id in sorted(ids):
            row = _client(conn, client_id)
            refs = find_references(conn, client_id, row["legal_name"] or "")
            members.append(
                {
                    "client_id": client_id,
                    "legal_name": row["legal_name"],
                    "pan": row["pan"],
                    "is_active": row["is_active"],
                    "references": refs,
                    "ref_total": sum(n for *_, n in refs),
                }
            )
        owners = [m for m in members if m["ref_total"]]
        if len(owners) == len(members):
            action, loser, reason = (
                "refuse",
                None,
                "every row already holds data — merging two clients that both "
                "own data is a human decision (use --strategy blank-gstin)",
            )
        elif owners:
            loser = next(m for m in members if not m["ref_total"])
            action, reason = (
                "delete",
                f"unreferenced; client_id={owners[0]['client_id']} owns the data",
            )
        else:
            loser = max(members, key=lambda m: m["client_id"])
            action, reason = (
                "delete",
                f"no row owns data; keeping the lowest client_id "
                f"({min(m['client_id'] for m in members)})",
            )
        import_report.append(
            {"gstin": gstin, "members": members, "action": action,
             "loser": loser, "reason": reason}
        )
    return import_report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(conn: sqlite3.Connection, plans: list[dict], *, applying: bool) -> None:
    if not plans:
        print("No duplicate GSTINs — the client master already satisfies "
              "one-registration-one-client.")
        return
    print(f"{len(plans)} duplicate GSTIN(s) found.\n")
    for p in plans:
        print(f"  GSTIN {p['gstin']}")
        for m in p["members"]:
            active = "active" if m["is_active"] else "inactive"
            owner = "OWNS DATA" if m["ref_total"] else "owns nothing"
            print(
                f"    client_id={m['client_id']:<4} {m['legal_name']!r} "
                f"pan={m['pan']!r} ({active}) — {owner}"
                f" ({m['ref_total']} reference(s))"
            )
            for table, column, how, n in m["references"]:
                print(f"        {table}.{column} by {how}: {n}")
        if p["action"] == "delete":
            loser = p["loser"]
            verb = "DELETE" if applying else "would delete"
            print(f"    -> {verb} client_id={loser['client_id']} "
                  f"({loser['legal_name']}) — {p['reason']}\n")
        else:
            print(f"    -> REFUSED: {p['reason']}\n")


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _backup(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = db_path.parent / "backups" / f"poc_pre_dupgstin_{stamp}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, dest)

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        a = src.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        b = dst.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        rows = src.execute("SELECT COUNT(*) FROM end_clients").fetchone()[0]
        rows_b = dst.execute("SELECT COUNT(*) FROM end_clients").fetchone()[0]
    finally:
        src.close()
        dst.close()
    if (a, rows) != (b, rows_b):
        raise SystemExit(
            f"Refusing to write: the backup does not match "
            f"(objects {a}/{b}, clients {rows}/{rows_b})"
        )
    return dest


def _enforce_unique_index(conn: sqlite3.Connection) -> str:
    """Build the one-GSTIN-one-client index — the thing that was blocked."""
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_end_clients_gstin_unique
            ON end_clients(gstin)
         WHERE gstin IS NOT NULL AND gstin <> ''
        """
    )
    return "idx_end_clients_gstin_unique present"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resolve_duplicate_gstin.py",
        description="Repair duplicate GSTINs in the F2 client master.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to poc.db")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write (backs the database up first). Default is report-only.",
    )
    parser.add_argument(
        "--strategy", choices=("delete", "blank-gstin"), default="delete",
        help=(
            "delete: remove the row that owns nothing (refuses when both own "
            "data). blank-gstin: clear the GSTIN on the newer row and keep it, "
            "for when deleting is not acceptable."
        ),
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"No database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        plans = plan(conn)
        print(f"database: {db_path}")
        print(f"mode:     {'APPLY' if args.apply else 'report only'}"
              f" (strategy: {args.strategy})\n")
        print_report(conn, plans, applying=args.apply)
        if not plans:
            return 0

        blocked = [p for p in plans if p["action"] != "delete"]
        if blocked and args.strategy == "delete":
            print("Nothing was written — resolve the REFUSED group(s) first.",
                  file=sys.stderr)
            return 2
        if not args.apply:
            print("Re-run with --apply to make these changes (a backup is "
                  "taken first).")
            return 0

        backup = _backup(db_path)
        print(f"backup: {backup}")

        changed = 0
        for p in plans:
            members = sorted(p["members"], key=lambda m: m["client_id"])
            if args.strategy == "blank-gstin":
                # Keep every client: the newest row gives up the registration.
                for m in members[:-1]:
                    conn.execute(
                        "UPDATE end_clients SET gstin = NULL WHERE client_id = ?",
                        (m["client_id"],),
                    )
                    print(f"  cleared GSTIN on client_id={m['client_id']} "
                          f"({m['legal_name']})")
                    changed += 1
                continue
            if p["action"] != "delete":
                continue  # already reported as refused; delete strategy exits above
            conn.execute(
                "UPDATE end_clients SET gstin = NULL WHERE client_id = ?",
                (p["loser"]["client_id"],),
            )
            conn.execute(
                "DELETE FROM end_clients WHERE client_id = ?",
                (p["loser"]["client_id"],),
            )
            print(f"  deleted client_id={p['loser']['client_id']} "
                  f"({p['loser']['legal_name']})")
            changed += 1

        conn.commit()
        print(f"\n{changed} client row(s) changed.")
        print(_enforce_unique_index(conn))
        conn.commit()

        remaining = duplicate_gstins(conn)
        if remaining:
            print(f"STILL DUPLICATED: {remaining}", file=sys.stderr)
            return 1
        print("No duplicate GSTINs remain — the client-id migration is "
              "unblocked.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
