"""Turn the new invoice pipeline on (or off) WITHOUT touching code.

    python -m src.invoice_extract.config show
    python -m src.invoice_extract.config set invoice_extract.v2.enabled true
    python -m src.invoice_extract.config unset invoice_extract.v2.enabled

WHY THIS EXISTS: on the deployed app there is no screen that writes settings, so
switching the v2 pipeline on used to need database access. This is the honest
path a person can use, including over ``fly ssh``.

Rules, deliberately narrow:

* only keys starting with ``invoice_extract.`` or ``ai.vision.`` can be touched
  — this tool is not a general settings editor;
* the repository's own ``db/poc.db`` is refused unless ``--allow-real-db`` is
  passed explicitly (same guard as every other tool here); ``SETU_DB_PATH`` and
  ``--db`` are honoured;
* every change is printed with its OLD and NEW value, and written to the audit
  trail through the settings service (which stamps the actor).
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from src import db as recon_db

ALLOWED_PREFIXES = ("invoice_extract.", "ai.vision.")
DEFAULT_ACTOR = "cli:config"


def _check_key(key: str) -> None:
    if not key.startswith(ALLOWED_PREFIXES):
        raise SystemExit(
            f"refusing '{key}': this tool only touches keys starting with "
            + " or ".join(ALLOWED_PREFIXES)
        )


def _all_settings(db_path=None) -> dict[str, str]:
    conn = recon_db.get_connection(db_path)
    try:
        try:
            rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        except Exception:  # noqa: BLE001 — a database without the table yet
            return {}
        return {str(k): str(v) for k, v in rows}
    finally:
        conn.close()


def _show(db_path=None, *, only_managed: bool = True) -> int:

    values = _all_settings(db_path)
    keys = sorted(
        k for k in values if (k.startswith(ALLOWED_PREFIXES) if only_managed else True)
    )
    print(f"database: {recon_db.DB_PATH if db_path is None else db_path}")
    if not keys:
        print("(no managed settings set)")
    for key in keys:
        print(f"{key} = {values[key]}")
    # The flags the pipeline watches, spelled out even when unset.
    from src.invoice_extract import extract as extract_mod
    from src.invoice_extract import triage as triage_mod

    for key in (extract_mod.KEY_ENABLED, triage_mod.KEY_ENABLED):
        if key not in values:
            print(f"{key} = (unset — default false)")
    return 0


def _set(key: str, value: str, *, actor: str, db_path=None) -> int:
    from src.settings import service as settings


    _check_key(key)
    before = _all_settings(db_path).get(key)
    settings.set_setting_str(key, value, actor=actor, db_path=db_path)
    after = _all_settings(db_path).get(key)
    print(f"{key}: {before!r} -> {after!r} (by {actor})")
    return 0


def _unset(key: str, *, actor: str, db_path=None) -> int:
    _check_key(key)
    before = _all_settings(db_path).get(key)
    conn = recon_db.get_connection(db_path)
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
    print(f"{key}: {before!r} -> (unset) (by {actor})")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.invoice_extract.config")
    parser.add_argument("--db", default=None, help="database (default: SETU_DB_PATH or the app DB)")
    parser.add_argument("--allow-real-db", action="store_true",
                        help="allow writing the repository's own db/poc.db")
    parser.add_argument("--actor", default=DEFAULT_ACTOR)
    sub = parser.add_subparsers(dest="cmd")

    show_p = sub.add_parser("show", help="print the managed settings")
    show_p.add_argument("--all", action="store_true", help="print EVERY setting, not just the managed ones")

    set_p = sub.add_parser("set", help="set a key")
    set_p.add_argument("key")
    set_p.add_argument("value")

    unset_p = sub.add_parser("unset", help="remove a key")
    unset_p.add_argument("key")

    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    recon_db.require_real_db_allowed(args.db, allow_real_db=args.allow_real_db)

    if args.cmd == "set":
        return _set(args.key, args.value, actor=args.actor, db_path=args.db)
    if args.cmd == "unset":
        return _unset(args.key, actor=args.actor, db_path=args.db)
    return _show(args.db, only_managed=not getattr(args, "all", False))


if __name__ == "__main__":
    raise SystemExit(main())
