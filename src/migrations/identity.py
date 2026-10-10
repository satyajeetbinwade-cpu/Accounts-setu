"""Name → ``end_clients.client_id`` resolution for the client-id migration.

This is the ONE place that implements the exact → normalised-exact → unique
prefix matching ladder. It is used by the schema migration (to backfill
``client_id`` on every name-keyed table), by the folder mover (to decide what
``data/<name>/`` becomes) and by the migration report. Nothing else in the
codebase should match a client by name after this migration.

Matching rules (deliberately conservative — never guess):

1. **exact** — the name is byte-identical to an ``end_clients.legal_name``.
2. **normalised-exact** — equal after case/whitespace/punctuation strip.
3. **prefix** — only when exactly ONE existing client matches either
   direction (``f.startswith(n)`` or ``n.startswith(f)``). Longest overlap
   wins; a tie between two different clients is **ambiguous** and is NOT
   merged.

Anything with no match (or an ambiguous prefix) becomes a NEW migrated client
(``origin='migrated'``). Ambiguous names are registered as their own clients
and flagged in the report rather than merged into a wrong one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(name: Any) -> str:
    """Case/whitespace/punctuation-insensitive key for a client name."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# ---------------------------------------------------------------------------
# Where client names are read from (both the name column and its source label)
# ---------------------------------------------------------------------------

# (table, column, label). Read defensively — a table may not exist yet on a
# very old DB, and the ingestion tables may predate the ``client_id`` column.
_NAME_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("end_clients", "legal_name", "end_clients"),
    ("runs", "client", "runs"),
    ("review_state", "client", "review_state"),
    ("result_field_overrides", "client", "result_field_overrides"),
    ("per_client_team_assignments", "client", "per_client_team_assignments"),
    ("ingestion_results", "client_ref", "ingestion_results"),
    ("ingestion_shape_cache", "client_ref", "ingestion_shape_cache"),
    ("users", "end_client_ref", "users"),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.OperationalError:
        return set()


def collect_names(conn: sqlite3.Connection, data_root=None) -> dict[str, list[str]]:
    """Return ``{name: [source_label, ...]}`` for every client name in the DB
    and on disk under ``data/``.

    Numeric ``data/`` folders (already migrated to ``data/<client_id>/``) are
    ignored, so a second run of the migration finds no names on disk.
    """
    sources: dict[str, list[str]] = {}

    def add(name: Any, label: str) -> None:
        if name is None:
            return
        text = str(name).strip()
        if not text:
            return
        sources.setdefault(text, []).append(label)

    for table, column, label in _NAME_SOURCES:
        cols = _table_columns(conn, table)
        if column not in cols:
            continue
        try:
            rows = conn.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for (value,) in rows:
            add(value, label)

    # Folders on disk (the legacy data/<name>/ layout).
    try:
        from src.data_paths import DATA_ROOT

        root = data_root if data_root is not None else DATA_ROOT
        if Path(root).exists():
            for p in sorted(Path(root).iterdir()):
                if p.is_dir() and not p.name.isdigit() and p.name != "_migration_manifest.json":
                    add(p.name, "data_folder")
    except Exception:  # noqa: BLE001 — never let a fs error abort the mapping
        pass

    return sources


@dataclass
class Mapping:
    """The approved name → client_id decision, with per-kind provenance."""

    name_to_id: dict[str, int] = field(default_factory=dict)
    exact: list[str] = field(default_factory=list)
    normalized: list[str] = field(default_factory=list)
    fuzzy: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, list[int]]] = field(default_factory=list)
    created: list[tuple[str, int]] = field(default_factory=list)

    def id_for(self, name: Any) -> Optional[int]:
        if name is None:
            return None
        return self.name_to_id.get(str(name).strip())

    def as_report(self) -> dict[str, Any]:
        return {
            "exact": sorted(self.exact),
            "normalized": sorted(self.normalized),
            "fuzzy": sorted(self.fuzzy),
            "ambiguous": [{"name": n, "candidates": c} for n, c in sorted(self.ambiguous)],
            "created": [{"legal_name": n, "client_id": i} for n, i in sorted(self.created)],
            "name_to_id": dict(sorted(self.name_to_id.items())),
        }


def _create_migrated_client(conn: sqlite3.Connection, legal_name: str) -> int:
    """Insert a migrated client, bypassing the normal create-flow validation
    (which requires PAN-or-TAN). Idempotent: an existing migrated client with
    the same legal_name is reused."""
    existing = conn.execute(
        "SELECT client_id FROM end_clients WHERE legal_name = ? AND origin = 'migrated'",
        (legal_name,),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    cur = conn.execute(
        """
        INSERT INTO end_clients
            (legal_name, pan, tan, gstin, assigned_team,
             primary_contact_email, primary_contact_phone, primary_contact_address,
             is_active, created_at, created_by, origin)
        VALUES (?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, ?, 'migration', 'migrated')
        """,
        (legal_name, _now()),
    )
    return int(cur.lastrowid)


def resolve_mapping(
    conn: sqlite3.Connection, *, create: bool = True, data_root=None
) -> Mapping:
    """Build the approved name → client_id mapping.

    With ``create=True`` (the default) it may CREATE ``end_clients`` rows for
    unmatched / ambiguous names — that is the migration's job, and the call is
    safe to repeat. With ``create=False`` it is a pure planning pass (used by
    ``--dry-run``): no rows are written, and unmatched / ambiguous names are
    reported with ``client_id=None``.
    """
    existing = conn.execute(
        "SELECT client_id, legal_name FROM end_clients ORDER BY client_id"
    ).fetchall()

    exact_map: dict[str, int] = {}
    norm_map: dict[str, list[int]] = {}
    for client_id, legal_name in existing:
        if legal_name is None:
            continue
        exact_map.setdefault(str(legal_name), int(client_id))
        norm_map.setdefault(norm(legal_name), []).append(int(client_id))

    mapping = Mapping()
    names = collect_names(conn, data_root)

    def register_new(name: str) -> Optional[int]:
        """Create (or, in dry-run, defer) a migrated client for ``name``."""
        if not create:
            mapping.name_to_id[name] = 0
            return None
        cid = _create_migrated_client(conn, name)
        mapping.name_to_id[name] = cid
        return cid

    for name in sorted(names):
        n = norm(name)

        # 1. exact
        if name in exact_map:
            mapping.name_to_id[name] = exact_map[name]
            mapping.exact.append(name)
            continue

        # 2. normalised-exact (only when unambiguous)
        if n and n in norm_map:
            candidates = sorted(set(norm_map[n]))
            if len(candidates) == 1:
                mapping.name_to_id[name] = candidates[0]
                mapping.normalized.append(name)
                continue
            # two clients share a normalised name — never merge blindly
            mapping.ambiguous.append((name, candidates))
            mapping.created.append((name, register_new(name) or 0))
            continue

        # 3. unique prefix (longest overlap wins; a tie is ambiguous)
        best_overlap = -1
        best_ids: list[int] = []
        for client_id, legal_name in existing:
            f = norm(legal_name)
            if not (f and n):
                continue
            if f == n or f.startswith(n) or n.startswith(f):
                overlap = min(len(f), len(n))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_ids = [int(client_id)]
                elif overlap == best_overlap and int(client_id) not in best_ids:
                    best_ids.append(int(client_id))

        if len(best_ids) == 1:
            mapping.name_to_id[name] = best_ids[0]
            mapping.fuzzy.append(name)
            continue

        if len(best_ids) > 1:
            mapping.ambiguous.append((name, sorted(best_ids)))

        # No match (or ambiguous): register as a new migrated client.
        cid = register_new(name)
        mapping.created.append((name, cid or 0))
        if cid is not None:
            # make it a candidate for later names in this same pass
            existing = list(existing) + [(cid, name)]
            exact_map.setdefault(name, cid)
            norm_map.setdefault(norm(name), []).append(cid)

    return mapping

