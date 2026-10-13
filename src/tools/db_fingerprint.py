"""Print the repository database's fingerprint — the before/after comparator.

    python -m src.tools.db_fingerprint                 # the repository's db
    python -m src.tools.db_fingerprint --db /tmp/x.db  # any database
    python -m src.tools.db_fingerprint --json          # machine-readable

READ-ONLY, always: it opens the file with ``mode=ro`` and never writes, so it
can be run against the real ``db/poc.db`` before and after a suite, a tool or an
app session to prove nothing moved. ``python main.py --check`` answers the same
question about schema state; this one answers it about the FILE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from src import db as recon_db


def fingerprint(db_path=None) -> dict:
    """sha256 + table count + row totals for every table, read-only."""
    path = Path(db_path) if db_path else Path(recon_db.DB_PATH)
    path = path.expanduser()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    counts: dict[str, int] = {}
    tables: list[str] = []
    if path.exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = sorted(
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            )
            for table in tables:
                try:
                    counts[table] = int(
                        conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    )
                except sqlite3.Error:  # pragma: no cover - a view or a virtual table
                    counts[table] = -1
        finally:
            conn.close()
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_repo_db": recon_db.path_is_repo_db(path),
        "sha256": digest.hexdigest() if path.exists() else "missing",
        "tables": len(tables),
        "rows": sum(v for v in counts.values() if v > 0),
        "table_counts": counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.tools.db_fingerprint")
    parser.add_argument("--db", default=None, help="database (default: the app database)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    info = fingerprint(args.db)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(f"path:   {info['path']}")
    print(f"sha256: {info['sha256']}")
    print(f"tables: {info['tables']}  rows: {info['rows']}")
    print("row counts:")
    for table, count in info["table_counts"].items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
