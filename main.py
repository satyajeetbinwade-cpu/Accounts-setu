"""PoC smoke test: init DB + load config, print confirmation, exit."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import bootstrap
from src import db
from src.config_loader import load_config


def main() -> None:
    bootstrap.init_all()
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