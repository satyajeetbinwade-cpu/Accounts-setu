"""Explicit boot entry point: create every schema, seed defaults, migrate.

This is one of only two places allowed to run the client-id migration (the
other is ``python -m src.migrations --apply``). ``setu/setu.py``—everything on
the UI import path—only *checks* that this has already happened, so importing
the app can never rewrite the database or move client folders.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import bootstrap
from src import db
from src.config_loader import load_config


def main() -> None:
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
    print("Tables present: runs =", "runs" in tables, "| match_results =", "match_results" in tables)
    print("Config loaded:", sorted(config.keys()))


if __name__ == "__main__":
    main()