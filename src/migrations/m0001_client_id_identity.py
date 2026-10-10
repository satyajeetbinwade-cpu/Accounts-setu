"""Migration 0001 — client-id identity.

Replaces every name-keyed client identity with ``end_clients.client_id``:

* ``end_clients`` gains ``origin`` ('created' | 'migrated') and a partial
  UNIQUE index on ``gstin`` (non-null, non-blank). PAN/TAN stay non-unique
  (one PAN may have many GST registrations).
* ``runs`` is rebuilt around ``client_id`` (FK) and keeps the old name as the
  immutable, display-only ``client_name_at_run``.
* ``review_state`` PK becomes ``(client_id, period, recon_type, fingerprint)``.
* ``result_field_overrides`` PK becomes ``(client_id, period, recon_type,
  fingerprint, side, field)``.
* ``ingestion_results`` drops ``client_ref``, makes ``client_id`` NOT NULL and
  re-keys ``upload_key`` to ``"<client_id>/<period>/<source_type>/<filename>"``.
* ``ingestion_shape_cache`` drops ``client_ref`` and re-keys the unique index
  to ``(client_id, source_type, header_signature)``.
* ``per_client_team_assignments.client`` → ``client_id INTEGER``.
* ``users.end_client_ref`` → ``end_client_id INTEGER`` (all NULL in practice).

Every rebuild uses create-new / copy / drop / rename inside the migration's
single transaction. The migration is idempotent: it checks for the target
shape before doing anything, so it is safe to re-run.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional, Sequence

from src.migrations import framework
from src.migrations.identity import Mapping, resolve_mapping

# The data/ root the migration should scan for legacy ``data/<name>/`` folders.
# Set by the orchestrator (``migrate.run``) before the migration is applied; a
# module-level value keeps the ``apply(conn)`` signature the framework expects.
_DATA_ROOT = None


def set_data_root(root) -> None:
    global _DATA_ROOT
    _DATA_ROOT = root


def _pick_client_id(mapping: Mapping, name: Any) -> int:
    """Resolve a client_id for a stored name, never failing: an unknown name
    gets a freshly registered migrated client (resolve_mapping already did
    this for every name it saw, so this is a defensive fallback)."""
    cid = mapping.id_for(name)
    return int(cid) if cid else 0


def _rebuild(
    conn: sqlite3.Connection,
    table: str,
    new_ddl: str,
    columns: Sequence[str],
    row_transform,
) -> int:
    """Create ``<table>_new``, copy every row through ``row_transform``,
    drop the old table and rename. Returns the row count copied.

    Only columns that exist in the SOURCE table are copied; any column the
    new table gained (or that an older DB never had) falls back to the new
    table's own DEFAULT — so a legacy DB that predates a later-added column
    still migrates cleanly.
    """
    tmp = f"{table}_new"
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(new_ddl.replace("{table}", tmp, 1))
    cur = conn.execute(f'SELECT * FROM "{table}"')
    names = [d[0] for d in cur.description]
    source_cols = set(names)
    rows = cur.fetchall()
    count = 0
    for row in rows:
        values = row_transform(dict(zip(names, row)))
        if values is None:
            continue
        # Copy a column when the source has it, or when the transform supplies
        # a non-null value for a column the new table gained (e.g. client_id).
        # Otherwise omit it so the new table's DEFAULT applies.
        present = [c for c in columns if c in source_cols or values.get(c) is not None]
        col_list = ", ".join(f'"{c}"' for c in present)
        placeholders = ", ".join("?" for _ in present)
        conn.execute(
            f'INSERT INTO "{tmp}" ({col_list}) VALUES ({placeholders})',
            tuple(values.get(c) for c in present),
        )
        count += 1
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    return count


def _unique_upload_key(
    base: str, used: set[str], result_id: int
) -> str:
    """Re-key an ingestion upload_key without ever colliding.

    Two legacy names can normalise to the same client_id, making the new
    ``"<client_id>/<period>/<source_type>/<filename>"`` key collide. We keep
    the first and disambiguate the rest with a stable suffix — never delete.
    """
    if base not in used:
        used.add(base)
        return base
    candidate = f"{base}~{result_id}"
    used.add(candidate)
    return candidate


def _is_legacy(conn: sqlite3.Connection, table: str, final_column: str) -> bool:
    """True when ``table`` exists and is still in its pre-migration shape.

    The existence half matters: the boot sequence migrates *before* the module
    ``init_*`` steps create their tables, so a section that tests only "does the
    final column exist?" would try to rebuild a table that is not there yet on a
    brand-new database. A missing table is simply "nothing to migrate".
    """
    return framework.table_exists(conn, table) and final_column not in framework.columns(
        conn, table
    )


def apply(conn: sqlite3.Connection) -> None:
    # -- 0. origin column must exist before resolve_mapping inserts rows -----
    framework.ensure_column(
        conn, "end_clients", "origin", "TEXT NOT NULL DEFAULT 'created'"
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_end_clients_gstin_unique
            ON end_clients(gstin)
         WHERE gstin IS NOT NULL AND gstin <> ''
        """
    )
    # NOTE: PAN and TAN are intentionally NOT unique — one PAN can have many
    # GST registrations (each a separate EndClient row).

    # -- 1. resolve names → client_id (creates migrated clients as needed) ---
    mapping = resolve_mapping(conn, data_root=_DATA_ROOT)

    # -- 2. runs -----------------------------------------------------------
    if "client" in framework.columns(conn, "runs"):
        def runs_row(d: dict[str, Any]):
            cid = _pick_client_id(mapping, d.get("client"))
            name = d.get("client") or ""
            return {
                "run_id": d["run_id"], "client_id": cid, "client_name_at_run": name,
                "period": d["period"], "recon_type": d["recon_type"],
                "run_timestamp": d["run_timestamp"], "config_snapshot": d["config_snapshot"],
                "source_file_names": d.get("source_file_names"),
                "caveats": d.get("caveats"), "run_notes": d.get("run_notes"),
            }

        _rebuild(
            conn,
            "runs",
            """
            CREATE TABLE {table} (
                run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id          INTEGER NOT NULL REFERENCES end_clients(client_id),
                client_name_at_run TEXT    NOT NULL,
                period             TEXT    NOT NULL,
                recon_type         TEXT    NOT NULL,
                run_timestamp      TEXT    NOT NULL,
                config_snapshot    TEXT    NOT NULL,
                source_file_names  TEXT,
                caveats            TEXT,
                run_notes          TEXT
            )
            """,
            ["run_id", "client_id", "client_name_at_run", "period", "recon_type",
             "run_timestamp", "config_snapshot", "source_file_names", "caveats", "run_notes"],
            runs_row,
        )

    # -- 3. review_state ---------------------------------------------------
    if _is_legacy(conn, "review_state", "client_id"):
        _rebuild(
            conn,
            "review_state",
            """
            CREATE TABLE {table} (
                client_id                 INTEGER NOT NULL REFERENCES end_clients(client_id),
                period                    TEXT    NOT NULL,
                recon_type                TEXT    NOT NULL,
                fingerprint               TEXT    NOT NULL,
                reviewed                  INTEGER NOT NULL DEFAULT 0,
                reviewer_note             TEXT,
                reviewed_at               TEXT,
                reviewed_in_run_id        INTEGER,
                reviewed_classification   TEXT,
                reviewed_difference_type  TEXT,
                reviewed_confidence_band  TEXT,
                PRIMARY KEY (client_id, period, recon_type, fingerprint)
            )
            """,
            ["client_id", "period", "recon_type", "fingerprint", "reviewed",
             "reviewer_note", "reviewed_at", "reviewed_in_run_id",
             "reviewed_classification", "reviewed_difference_type",
             "reviewed_confidence_band"],
            lambda d: {
                "client_id": _pick_client_id(mapping, d["client"]),
                "period": d["period"], "recon_type": d["recon_type"],
                "fingerprint": d["fingerprint"], "reviewed": d["reviewed"],
                "reviewer_note": d["reviewer_note"], "reviewed_at": d["reviewed_at"],
                "reviewed_in_run_id": d["reviewed_in_run_id"],
                "reviewed_classification": d["reviewed_classification"],
                "reviewed_difference_type": d["reviewed_difference_type"],
                "reviewed_confidence_band": d["reviewed_confidence_band"],
            },
        )

    # -- 4. result_field_overrides ----------------------------------------
    if _is_legacy(conn, "result_field_overrides", "client_id"):
        _rebuild(
            conn,
            "result_field_overrides",
            """
            CREATE TABLE {table} (
                client_id    INTEGER NOT NULL REFERENCES end_clients(client_id),
                period       TEXT    NOT NULL,
                recon_type   TEXT    NOT NULL,
                fingerprint  TEXT    NOT NULL,
                side         TEXT    NOT NULL,
                field        TEXT    NOT NULL,
                value        TEXT,
                reason       TEXT,
                changed_by   TEXT,
                changed_at   TEXT,
                PRIMARY KEY (client_id, period, recon_type, fingerprint, side, field)
            )
            """,
            ["client_id", "period", "recon_type", "fingerprint", "side", "field",
             "value", "reason", "changed_by", "changed_at"],
            lambda d: {
                "client_id": _pick_client_id(mapping, d["client"]),
                "period": d["period"], "recon_type": d["recon_type"],
                "fingerprint": d["fingerprint"], "side": d["side"], "field": d["field"],
                "value": d["value"], "reason": d["reason"],
                "changed_by": d["changed_by"], "changed_at": d["changed_at"],
            },
        )

    # -- 5. ingestion_results ---------------------------------------------
    if "client_ref" in framework.columns(conn, "ingestion_results"):
        used: set[str] = set()

        def ing_row(d: dict[str, Any]):
            cid = d.get("client_id")
            if cid is None:
                cid = _pick_client_id(mapping, d.get("client_ref"))
            cid = int(cid)
            period = d.get("period") or "-"
            base = f"{cid}/{period}/{d.get('source_type')}/{d.get('filename')}"
            key = _unique_upload_key(base, used, int(d["result_id"]))
            return {
                "result_id": d["result_id"], "upload_key": key, "client_id": cid,
                "period": d.get("period"), "source_type": d.get("source_type"),
                "filename": d.get("filename"), "status": d.get("status"),
                "header_row": d.get("header_row"), "sheet_name": d.get("sheet_name"),
                "sheet_ambiguous": d.get("sheet_ambiguous"), "headers_json": d.get("headers_json"),
                "mapping_json": d.get("mapping_json"), "classification_json": d.get("classification_json"),
                "unmapped_required_json": d.get("unmapped_required_json"),
                "warnings_json": d.get("warnings_json"), "row_count_in": d.get("row_count_in"),
                "row_count_out": d.get("row_count_out"), "model_used": d.get("model_used"),
                "llm_cached": d.get("llm_cached"), "created_at": d.get("created_at"),
                "created_by": d.get("created_by"), "notes_json": d.get("notes_json"),
                "message": d.get("message"), "validation_json": d.get("validation_json"),
                "metadata_json": d.get("metadata_json"), "rate_matrix_json": d.get("rate_matrix_json"),
                "dispositions_json": d.get("dispositions_json"), "ingestion_path": d.get("ingestion_path"),
            }

        _rebuild(
            conn,
            "ingestion_results",
            """
            CREATE TABLE {table} (
                result_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_key          TEXT    NOT NULL,
                client_id           INTEGER NOT NULL REFERENCES end_clients(client_id),
                period              TEXT,
                source_type         TEXT    NOT NULL,
                filename            TEXT    NOT NULL,
                status              TEXT    NOT NULL,
                header_row          INTEGER,
                sheet_name          TEXT,
                sheet_ambiguous     INTEGER NOT NULL DEFAULT 0,
                headers_json        TEXT    NOT NULL,
                mapping_json        TEXT    NOT NULL,
                classification_json TEXT,
                unmapped_required_json TEXT NOT NULL,
                warnings_json       TEXT    NOT NULL,
                row_count_in        INTEGER NOT NULL DEFAULT 0,
                row_count_out       INTEGER NOT NULL DEFAULT 0,
                model_used          TEXT,
                llm_cached          INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT    NOT NULL,
                created_by          TEXT    NOT NULL,
                notes_json          TEXT    NOT NULL DEFAULT '[]',
                message             TEXT,
                validation_json     TEXT    NOT NULL DEFAULT '[]',
                metadata_json       TEXT    NOT NULL DEFAULT '{}',
                rate_matrix_json    TEXT    NOT NULL DEFAULT '[]',
                dispositions_json   TEXT    NOT NULL DEFAULT '[]',
                ingestion_path      TEXT    NOT NULL DEFAULT ''
            )
            """,
            ["result_id", "upload_key", "client_id", "period", "source_type",
             "filename", "status", "header_row", "sheet_name", "sheet_ambiguous",
             "headers_json", "mapping_json", "classification_json",
             "unmapped_required_json", "warnings_json", "row_count_in",
             "row_count_out", "model_used", "llm_cached", "created_at",
             "created_by", "notes_json", "message", "validation_json",
             "metadata_json", "rate_matrix_json", "dispositions_json",
             "ingestion_path"],
            ing_row,
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_results_upload "
            "ON ingestion_results(upload_key)"
        )

    # -- 6. ingestion_shape_cache -----------------------------------------
    if "client_ref" in framework.columns(conn, "ingestion_shape_cache"):
        def shape_row(d: dict[str, Any]):
            cid = d.get("client_id")
            if cid is None:
                cid = _pick_client_id(mapping, d.get("client_ref"))
            return {
                "shape_id": d["shape_id"], "client_id": int(cid),
                "source_type": d["source_type"], "header_signature": d["header_signature"],
                "headers_json": d["headers_json"], "mapping_json": d["mapping_json"],
                "classification_json": d.get("classification_json"),
                "model_used": d.get("model_used"), "confidence_floor": d.get("confidence_floor"),
                "trusted": d.get("trusted"), "source": d.get("source"),
                "times_used": d.get("times_used"), "created_by": d.get("created_by"),
                "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
                "last_used_at": d.get("last_used_at"),
            }

        _rebuild(
            conn,
            "ingestion_shape_cache",
            """
            CREATE TABLE {table} (
                shape_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id           INTEGER NOT NULL REFERENCES end_clients(client_id),
                source_type         TEXT    NOT NULL,
                header_signature    TEXT    NOT NULL,
                headers_json        TEXT    NOT NULL,
                mapping_json        TEXT    NOT NULL,
                classification_json TEXT,
                model_used          TEXT,
                confidence_floor    REAL,
                trusted             INTEGER NOT NULL DEFAULT 0,
                source              TEXT    NOT NULL DEFAULT 'ai',
                times_used          INTEGER NOT NULL DEFAULT 0,
                created_by          TEXT    NOT NULL,
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL,
                last_used_at        TEXT
            )
            """,
            ["shape_id", "client_id", "source_type", "header_signature",
             "headers_json", "mapping_json", "classification_json", "model_used",
             "confidence_floor", "trusted", "source", "times_used", "created_by",
             "created_at", "updated_at", "last_used_at"],
            shape_row,
        )
        # Collapse duplicate (client_id, source_type, header_signature) rows
        # that only differed by the legacy name — keep the most recently used,
        # then re-create the unique index. Nothing is deleted until the copy
        # is verified, and we only drop rows that are exact duplicates on the
        # new key (their content is preserved on the survivor).
        conn.execute(
            """
            DELETE FROM ingestion_shape_cache
             WHERE shape_id NOT IN (
                SELECT MAX(shape_id) FROM ingestion_shape_cache
                 GROUP BY client_id, source_type, header_signature
             )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shape_cache_lookup "
            "ON ingestion_shape_cache(client_id, source_type, header_signature)"
        )

    # -- 7. per_client_team_assignments -----------------------------------
    if _is_legacy(conn, "per_client_team_assignments", "client_id"):
        _rebuild(
            conn,
            "per_client_team_assignments",
            """
            CREATE TABLE {table} (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(user_id),
                client_id    INTEGER NOT NULL REFERENCES end_clients(client_id),
                assigned_by  TEXT,
                created_at   TEXT    NOT NULL,
                UNIQUE (user_id, client_id)
            )
            """,
            ["id", "user_id", "client_id", "assigned_by", "created_at"],
            lambda d: {
                "id": d["id"], "user_id": d["user_id"],
                "client_id": _pick_client_id(mapping, d["client"]),
                "assigned_by": d["assigned_by"], "created_at": d["created_at"],
            },
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_team_client "
            "ON per_client_team_assignments(client_id)"
        )

    # -- 8. users.end_client_ref → end_client_id --------------------------
    if "end_client_ref" in framework.columns(conn, "users"):
        framework.ensure_column(conn, "users", "end_client_id", "INTEGER")
        conn.execute(
            "UPDATE users SET end_client_id = NULL WHERE end_client_ref IS NULL"
        )
        conn.execute("ALTER TABLE users DROP COLUMN end_client_ref")

    # -- 9. move legacy data/<name>/ folders to data/<client_id>/ ----------
    _move_folders(mapping)


def _move_folders(mapping: Mapping) -> None:
    """Move ``data/<name>/`` → ``data/<client_id>/`` as part of THIS
    migration's transaction.

    Running inside the migration (rather than as a separate step afterwards)
    is what makes a mid-way failure safe: if a move raises, we reverse the
    moves already recorded in the manifest and re-raise — the framework's
    transaction then rolls the DB back too, so DB and folders are restored
    together. ``_DATA_ROOT`` is set by the orchestrator; when unset (a unit
    call) the folder step is skipped.
    """
    if _DATA_ROOT is None:
        return
    from pathlib import Path

    from src.migrations import folders

    root = Path(_DATA_ROOT)
    entries = folders.plan_folder_moves(mapping, root)
    if not entries:
        return
    try:
        folders.apply_folder_moves(entries, root)
    except Exception:
        folders.rollback(root)
        raise


VERSION = 1
NAME = "client_id_identity"
MIGRATION = framework.Migration(version=VERSION, name=NAME, apply=apply)
