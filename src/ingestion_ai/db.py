"""Low-level CRUD for the F3-AI ingestion-mapping tables. Mirrors
src/documents/db.py's style: functions take a connection, do one thing,
and commit individually.

src/ingestion_ai/service.py is the only module that should import this
one directly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from src import db as recon_db


def connect(db_path=None) -> sqlite3.Connection:
    """Open the shared PoC database. Exposed here so the normalizer (which
    sits below service.py and must not import it, to avoid a cycle) can
    reach the same connection factory."""
    return recon_db.get_connection(db_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_to_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _row_to_dict(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = _rows_to_dicts(conn, sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# RawUpload
# ---------------------------------------------------------------------------


def create_raw_upload(
    conn: sqlite3.Connection, *, document_id: int, client_id: int, source_type: str,
    filename: str, status: str, created_by: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO raw_uploads
            (document_id, client_id, source_type, filename, status, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, client_id, source_type, filename, status, _now(), created_by),
    )
    conn.commit()
    return cur.lastrowid


def get_raw_upload(conn: sqlite3.Connection, upload_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM raw_uploads WHERE upload_id = ?", (upload_id,))


def get_raw_upload_by_document(conn: sqlite3.Connection, document_id: int) -> Optional[dict[str, Any]]:
    return _row_to_dict(
        conn, "SELECT * FROM raw_uploads WHERE document_id = ? ORDER BY upload_id DESC LIMIT 1", (document_id,)
    )


def list_raw_uploads(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, status: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM raw_uploads WHERE 1=1"
    params: list[Any] = []
    if client_id is not None:
        sql += " AND client_id = ?"
        params.append(client_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def set_upload_status(conn: sqlite3.Connection, upload_id: int, status: str) -> None:
    conn.execute("UPDATE raw_uploads SET status = ? WHERE upload_id = ?", (status, upload_id))
    conn.commit()


def confirm_upload(
    conn: sqlite3.Connection, upload_id: int, *, confirmed_mapping: dict[str, Optional[str]],
    trusted_on_confirm: bool, confirmed_by: str,
) -> None:
    conn.execute(
        """
        UPDATE raw_uploads
        SET status = 'confirmed', confirmed_mapping = ?, trusted_on_confirm = ?,
            confirmed_at = ?, confirmed_by = ?
        WHERE upload_id = ?
        """,
        (json.dumps(confirmed_mapping), int(trusted_on_confirm), _now(), confirmed_by, upload_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# MappingConfidenceReport
# ---------------------------------------------------------------------------


def create_report(
    conn: sqlite3.Connection, *, upload_id: int, field_mapping: list[dict[str, Any]], c5_context_used: bool,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO mapping_confidence_reports
            (upload_id, field_mapping, c5_context_used, routing_stub, created_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        (upload_id, json.dumps(field_mapping), int(c5_context_used), _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_report_for_upload(conn: sqlite3.Connection, upload_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(
        conn, "SELECT * FROM mapping_confidence_reports WHERE upload_id = ? ORDER BY report_id DESC LIMIT 1",
        (upload_id,),
    )
    if row is not None:
        row["field_mapping"] = json.loads(row["field_mapping"])
    return row


# ---------------------------------------------------------------------------
# ColumnMappingProfile — keyed per client + source_type
# ---------------------------------------------------------------------------


def get_profile(conn: sqlite3.Connection, client_id: int, source_type: str) -> Optional[dict[str, Any]]:
    row = _row_to_dict(
        conn, "SELECT * FROM column_mapping_profiles WHERE client_id = ? AND source_type = ?",
        (client_id, source_type),
    )
    if row is not None:
        row["column_map"] = json.loads(row["column_map"])
    return row


def get_profile_by_id(conn: sqlite3.Connection, profile_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(
        conn, "SELECT * FROM column_mapping_profiles WHERE profile_id = ?", (profile_id,)
    )
    if row is not None:
        row["column_map"] = json.loads(row["column_map"])
    return row


def upsert_profile(
    conn: sqlite3.Connection, *, client_id: int, source_type: str, column_map: dict[str, Optional[str]],
    trusted: bool, actor: str,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO column_mapping_profiles
            (client_id, source_type, column_map, trusted, last_used_at, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_id, source_type) DO UPDATE SET
            column_map = excluded.column_map,
            trusted = excluded.trusted,
            last_used_at = excluded.last_used_at,
            updated_at = excluded.updated_at
        """,
        (client_id, source_type, json.dumps(column_map), int(trusted), now, actor, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT profile_id FROM column_mapping_profiles WHERE client_id = ? AND source_type = ?",
        (client_id, source_type),
    ).fetchone()
    return row[0]


def touch_profile_last_used(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute(
        "UPDATE column_mapping_profiles SET last_used_at = ? WHERE profile_id = ?", (_now(), profile_id)
    )
    conn.commit()


def set_profile_trust(conn: sqlite3.Connection, profile_id: int, trusted: bool) -> None:
    conn.execute(
        "UPDATE column_mapping_profiles SET trusted = ?, updated_at = ? WHERE profile_id = ?",
        (int(trusted), _now(), profile_id),
    )
    conn.commit()


def list_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows_to_dicts(conn, "SELECT * FROM column_mapping_profiles ORDER BY updated_at DESC")
    for r in rows:
        r["column_map"] = json.loads(r["column_map"])
    return rows


def list_profiles_with_usage(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Profiles joined to the REAL reuse store.

    ``column_mapping_profiles`` records that a mapping was confirmed for a
    client + source type, but the mapping that is actually reused on a
    future upload lives in ``ingestion_shape_cache`` (keyed by the file's
    header signature). An admin deciding whether to revoke needs the usage
    that actually happened, so aggregate it from the shape cache rather
    than reporting the profile's own (never-incremented) last_used_at.
    """
    rows = _rows_to_dicts(
        conn,
        """
        SELECT p.*,
               COALESCE(SUM(s.times_used), 0) AS usage_count,
               COUNT(s.shape_id)              AS shape_count,
               MAX(s.last_used_at)            AS shape_last_used_at
        FROM column_mapping_profiles p
        LEFT JOIN ingestion_shape_cache s
               ON s.client_id = p.client_id AND s.source_type = p.source_type
        GROUP BY p.profile_id
        ORDER BY p.updated_at DESC
        """,
    )
    for r in rows:
        r["column_map"] = json.loads(r["column_map"])
        # Prefer the shape cache's real last-used; fall back to the profile's.
        r["last_used_at"] = r.get("shape_last_used_at") or r.get("last_used_at")
    return rows


def untrust_shapes_for_profile(
    conn: sqlite3.Connection, *, client_id: int, source_type: str,
) -> int:
    """Revoke trust on every learned shape backing a profile.

    Revoking a profile must genuinely stop reuse. Flipping the profile's own
    ``trusted`` flag does nothing on its own — the runtime reads the shape
    cache — so the shapes are untrusted here too. Returns how many were
    affected.
    """
    cur = conn.execute(
        """
        UPDATE ingestion_shape_cache
        SET trusted = 0, updated_at = ?
        WHERE client_id = ? AND source_type = ? AND trusted = 1
        """,
        (_now(), client_id, source_type),
    )
    conn.commit()
    return cur.rowcount or 0


def delete_shape(
    conn: sqlite3.Connection, *, client_ref: str, source_type: str, header_signature: str,
) -> int:
    """Delete a cached shape so the next ingest of this file shape re-maps.

    Used by the "re-run through model" action: the cache is keyed on the
    header set, so leaving the row in place would replay the old mapping
    forever. Returns how many rows were deleted.
    """
    cur = conn.execute(
        """
        DELETE FROM ingestion_shape_cache
        WHERE client_ref = ? AND source_type = ? AND header_signature = ?
        """,
        (client_ref, source_type, header_signature),
    )
    conn.commit()
    return cur.rowcount or 0


def delete_ingestion_result(conn: sqlite3.Connection, upload_key: str) -> int:
    """Delete a persisted ingestion result so the next read re-derives it.

    The runner's `_canonical_frame_for()` reuses a stored result's mapping
    without re-calling the model, so a re-run must clear this row too —
    otherwise the fresh mapping would be ignored on the next run.
    """
    cur = conn.execute("DELETE FROM ingestion_results WHERE upload_key = ?", (upload_key,))
    conn.commit()
    return cur.rowcount or 0


def rekey_ingestion_result(
    conn: sqlite3.Connection, *, old_upload_key: str, new_upload_key: str, period: Optional[str],
) -> int:
    """Move a stored ingestion result to a new period.

    The upload key EMBEDS the period (``"<client>/<period>/<source_type>/
    <filename>"``), so re-filing a document under a different period must
    re-key its result row as well. Moving the bytes on disk alone would
    leave the Reconcile slot reading "not ingested yet", because the slot
    looks the result up by the NEW key.

    Returns how many rows were updated (0 when there was no stored result,
    which is fine — the file simply has not been ingested yet).
    """
    cur = conn.execute(
        "UPDATE ingestion_results SET upload_key = ?, period = ? WHERE upload_key = ?",
        (new_upload_key, period, old_upload_key),
    )
    conn.commit()
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# ingestion_shape_cache — the unified layer's shape-keyed cache + learned
# trusted mappings (see schema.py for why the key includes the header set).
# ---------------------------------------------------------------------------


def _decode_shape_row(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    for src, dst in (
        ("headers_json", "headers"),
        ("mapping_json", "mapping"),
        ("classification_json", "classification"),
    ):
        if row.get(src):
            try:
                row[dst] = json.loads(row[src])
            except (TypeError, ValueError):
                row[dst] = None if src == "classification_json" else []
    return row


def get_shape(
    conn: sqlite3.Connection, *, client_ref: str, source_type: str, header_signature: str,
) -> Optional[dict[str, Any]]:
    return _decode_shape_row(
        _row_to_dict(
            conn,
            """
            SELECT * FROM ingestion_shape_cache
            WHERE client_ref = ? AND source_type = ? AND header_signature = ?
            """,
            (client_ref, source_type, header_signature),
        )
    )


def upsert_shape(
    conn: sqlite3.Connection, *, client_ref: str, source_type: str, header_signature: str,
    headers: list[str], mapping: dict[str, Any], classification: Optional[dict[str, Any]],
    model_used: Optional[str], confidence_floor: Optional[float], trusted: bool, source: str,
    client_id: Optional[int], actor: str,
) -> int:
    """Insert or refresh a shape row. `times_used` is deliberately NOT
    reset on update — it accumulates across reuse."""
    now = _now()
    conn.execute(
        """
        INSERT INTO ingestion_shape_cache
            (client_id, client_ref, source_type, header_signature, headers_json, mapping_json,
             classification_json, model_used, confidence_floor, trusted, source,
             times_used, created_by, created_at, updated_at, last_used_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT(client_ref, source_type, header_signature) DO UPDATE SET
            client_id = excluded.client_id,
            headers_json = excluded.headers_json,
            mapping_json = excluded.mapping_json,
            classification_json = excluded.classification_json,
            model_used = excluded.model_used,
            confidence_floor = excluded.confidence_floor,
            trusted = excluded.trusted,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            client_id, client_ref, source_type, header_signature,
            json.dumps(headers), json.dumps(mapping),
            json.dumps(classification) if classification else None,
            model_used, confidence_floor, int(trusted), source, actor, now, now, now,
        ),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT shape_id FROM ingestion_shape_cache
        WHERE client_ref = ? AND source_type = ? AND header_signature = ?
        """,
        (client_ref, source_type, header_signature),
    ).fetchone()
    return row[0]


def touch_shape(conn: sqlite3.Connection, shape_id: int) -> None:
    """Record a cache hit / trusted reuse: bump usage + last_used_at."""
    conn.execute(
        """
        UPDATE ingestion_shape_cache
        SET times_used = times_used + 1, last_used_at = ?
        WHERE shape_id = ?
        """,
        (_now(), shape_id),
    )
    conn.commit()


def set_shape_trust(conn: sqlite3.Connection, shape_id: int, trusted: bool) -> None:
    conn.execute(
        "UPDATE ingestion_shape_cache SET trusted = ?, updated_at = ? WHERE shape_id = ?",
        (int(trusted), _now(), shape_id),
    )
    conn.commit()


def list_shapes(
    conn: sqlite3.Connection, *, client_ref: Optional[str] = None, source_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ingestion_shape_cache WHERE 1=1"
    params: list[Any] = []
    if client_ref:
        sql += " AND client_ref = ?"
        params.append(client_ref)
    if source_type:
        sql += " AND source_type = ?"
        params.append(source_type)
    sql += " ORDER BY updated_at DESC"
    return [_decode_shape_row(r) for r in _rows_to_dicts(conn, sql, tuple(params))]


# ---------------------------------------------------------------------------
# ingestion_results — one row per normalized upload (the shared result the
# Run screen and Smart Ingestion both read).
# ---------------------------------------------------------------------------


def upsert_ingestion_result(
    conn: sqlite3.Connection, *, upload_key: str, client_ref: str, client_id: Optional[int],
    period: Optional[str], source_type: str, filename: str, status: str, header_row: Optional[int],
    sheet_name: Optional[str], sheet_ambiguous: bool, headers: list[str], mapping: list[dict[str, Any]],
    classification: Optional[dict[str, Any]], unmapped_required: list[str], warnings: list[str],
    row_count_in: int, row_count_out: int, model_used: Optional[str], llm_cached: bool, actor: str,
    notes: Optional[list[str]] = None, message: Optional[str] = None,
    validation: Optional[list[dict[str, Any]]] = None,
    metadata: Optional[dict[str, Any]] = None,
    rate_matrix: Optional[list[dict[str, Any]]] = None,
    dispositions: Optional[list[dict[str, Any]]] = None,
    ingestion_path: str = "",
) -> int:
    now = _now()
    conn.execute(
        """
        INSERT INTO ingestion_results
            (upload_key, client_ref, client_id, period, source_type, filename, status, header_row,
             sheet_name, sheet_ambiguous, headers_json, mapping_json, classification_json,
             unmapped_required_json, warnings_json, notes_json, message, row_count_in, row_count_out,
             model_used, llm_cached, validation_json, metadata_json, rate_matrix_json,
             dispositions_json, ingestion_path, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(upload_key) DO UPDATE SET
            client_id = excluded.client_id,
            period = excluded.period,
            status = excluded.status,
            header_row = excluded.header_row,
            sheet_name = excluded.sheet_name,
            sheet_ambiguous = excluded.sheet_ambiguous,
            headers_json = excluded.headers_json,
            mapping_json = excluded.mapping_json,
            classification_json = excluded.classification_json,
            unmapped_required_json = excluded.unmapped_required_json,
            warnings_json = excluded.warnings_json,
            notes_json = excluded.notes_json,
            message = excluded.message,
            row_count_in = excluded.row_count_in,
            row_count_out = excluded.row_count_out,
            model_used = excluded.model_used,
            llm_cached = excluded.llm_cached,
            validation_json = excluded.validation_json,
            metadata_json = excluded.metadata_json,
            rate_matrix_json = excluded.rate_matrix_json,
            dispositions_json = excluded.dispositions_json,
            ingestion_path = excluded.ingestion_path,
            created_at = excluded.created_at,
            created_by = excluded.created_by
        """,
        (
            upload_key, client_ref, client_id, period, source_type, filename, status, header_row,
            sheet_name, int(sheet_ambiguous), json.dumps(headers), json.dumps(mapping),
            json.dumps(classification) if classification else None,
            json.dumps(unmapped_required), json.dumps(warnings), json.dumps(notes or []),
            message, row_count_in, row_count_out, model_used, int(llm_cached),
            json.dumps(validation or []), json.dumps(metadata or {}),
            json.dumps(rate_matrix or []), json.dumps(dispositions or []),
            ingestion_path, now, actor,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT result_id FROM ingestion_results WHERE upload_key = ?", (upload_key,)
    ).fetchone()
    return row[0]


def get_ingestion_result(conn: sqlite3.Connection, upload_key: str) -> Optional[dict[str, Any]]:
    row = _row_to_dict(conn, "SELECT * FROM ingestion_results WHERE upload_key = ?", (upload_key,))
    if row is None:
        return None
    for src, dst in (
        ("headers_json", "headers"),
        ("mapping_json", "mapping"),
        ("classification_json", "classification"),
        ("unmapped_required_json", "unmapped_required"),
        ("warnings_json", "warnings"),
        ("notes_json", "notes"),
        ("validation_json", "validation"),
        ("metadata_json", "metadata"),
        ("rate_matrix_json", "rate_matrix"),
        ("dispositions_json", "dispositions"),
    ):
        if row.get(src):
            try:
                row[dst] = json.loads(row[src])
            except (TypeError, ValueError):
                row[dst] = None
    return row


def find_ingestion_result(
    conn: sqlite3.Connection, *, client_id: int, source_type: str, filename: str,
) -> Optional[dict[str, Any]]:
    """Look up a persisted ingestion result WITHOUT composing the upload key.

    The key is ``"<client_ref>/<period>/<source_type>/<filename>"``, but
    ``raw_uploads`` stores neither the client_ref nor the period — so a
    caller holding only a raw_upload row cannot rebuild the key and would
    silently read back nothing (which is exactly how a file that genuinely
    read 37 rows came to be displayed as "0 row(s) read"). Matching on the
    columns that ARE stored is stable and needs no backfill.
    """
    row = _row_to_dict(
        conn,
        """
        SELECT * FROM ingestion_results
        WHERE client_id = ? AND source_type = ? AND filename = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (client_id, source_type, filename),
    )
    if row is None:
        return None
    for src, dst in (
        ("headers_json", "headers"),
        ("mapping_json", "mapping"),
        ("classification_json", "classification"),
        ("unmapped_required_json", "unmapped_required"),
        ("warnings_json", "warnings"),
        ("notes_json", "notes"),
        ("validation_json", "validation"),
        ("metadata_json", "metadata"),
        ("rate_matrix_json", "rate_matrix"),
        ("dispositions_json", "dispositions"),
    ):
        if row.get(src):
            try:
                row[dst] = json.loads(row[src])
            except (TypeError, ValueError):
                row[dst] = None
    return row


def list_raw_uploads_with_context(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Raw uploads joined to their F3 document's period.

    ``raw_uploads`` has no period column, but the document it files into
    does — so the Uploads list can surface the period the upload form
    collected without a schema migration.
    """
    sql = """
        SELECT u.*, d.period AS period, d.doc_type AS doc_type
        FROM raw_uploads u
        LEFT JOIN documents d ON d.document_id = u.document_id
        WHERE 1=1
    """
    params: list[Any] = []
    if client_id is not None:
        sql += " AND u.client_id = ?"
        params.append(client_id)
    if status:
        sql += " AND u.status = ?"
        params.append(status)
    sql += " ORDER BY u.created_at DESC"
    return _rows_to_dicts(conn, sql, tuple(params))


def list_ingestion_results(
    conn: sqlite3.Connection, *, client_ref: Optional[str] = None, source_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ingestion_results WHERE 1=1"
    params: list[Any] = []
    if client_ref:
        sql += " AND client_ref = ?"
        params.append(client_ref)
    if source_type:
        sql += " AND source_type = ?"
        params.append(source_type)
    sql += " ORDER BY created_at DESC"
    out = []
    for row in _rows_to_dicts(conn, sql, tuple(params)):
        key = row["upload_key"]
        full = get_ingestion_result(conn, key)
        if full:
            out.append(full)
    return out
