"""Versioned, idempotent schema migration framework.

One numbered migration per change. Each migration is applied once, inside a
single transaction, and recorded in ``schema_migrations``. Running the
migrations again is a no-op, so this is safe to call on every boot
(``python main.py`` runs before Reflex on Fly).

Migrations MUST be written to be idempotent themselves as well — a
half-applied migration that was rolled back, or a table rebuilt by hand,
must not break the next run. ``apply`` receives an open connection with
foreign-key enforcement disabled; the runner re-enables it and runs
``PRAGMA foreign_key_check`` after all migrations.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT    NOT NULL,
            applied_at TEXT    NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    ensure_migrations_table(conn)
    return {int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations")}


def run_migrations(
    conn: sqlite3.Connection, migrations: Iterable[Migration], *, verbose: bool = False
) -> list[int]:
    """Apply every not-yet-applied migration, each in its own transaction.

    Returns the list of versions actually applied this call. Raises on the
    first failure (that migration's transaction is rolled back; earlier ones
    stay applied — the framework is re-entrant).
    """
    ordered = sorted(migrations, key=lambda m: m.version)
    already = applied_versions(conn)
    ran: list[int] = []

    # Explicit transaction control: the migrations themselves issue DDL that
    # sqlite refuses inside an implicit transaction in some paths, so drive
    # BEGIN/COMMIT by hand and keep foreign keys off for the rebuilds.
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        for m in ordered:
            if m.version in already:
                continue
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            try:
                m.apply(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (m.version, m.name, _now()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            ran.append(m.version)
            if verbose:
                print(f"  applied migration {m.version}: {m.name}")

        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"foreign_key_check reported {len(violations)} violation(s) after "
                f"migrations: {violations[:10]}"
            )
    finally:
        conn.isolation_level = prev_isolation

    return ran


# ---------------------------------------------------------------------------
# Small idempotency helpers shared by migrations.
# ---------------------------------------------------------------------------


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.OperationalError:
        return set()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def ensure_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> bool:
    """Add ``column`` to ``table`` if missing. Returns True if it was added."""
    if column in columns(conn, table):
        return False
    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')
    return True
