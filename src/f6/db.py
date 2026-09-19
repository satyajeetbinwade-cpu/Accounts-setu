"""Low-level CRUD for F6. Everything else should import from
src.f6.service, not this module directly."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


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
# SourceFormat
# ---------------------------------------------------------------------------


def upsert_source_format(
    conn: sqlite3.Connection, *, key: str, label: str, slot: str, scope_kind: str,
    provisioned_only: bool, description: str, sort_order: int,
) -> int:
    conn.execute(
        """
        INSERT INTO f6_source_formats (key, label, slot, scope_kind, provisioned_only, description, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            label = excluded.label, slot = excluded.slot, scope_kind = excluded.scope_kind,
            provisioned_only = excluded.provisioned_only, description = excluded.description,
            sort_order = excluded.sort_order
        """,
        (key, label, slot, scope_kind, 1 if provisioned_only else 0, description, sort_order),
    )
    conn.commit()
    row = conn.execute("SELECT source_format_id FROM f6_source_formats WHERE key = ?", (key,)).fetchone()
    return row[0]


def list_source_formats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows_to_dicts(conn, "SELECT * FROM f6_source_formats ORDER BY sort_order")


def get_source_format_by_key(conn: sqlite3.Connection, key: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn, "SELECT * FROM f6_source_formats WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# FormatVersion
# ---------------------------------------------------------------------------


def insert_format_version(
    conn: sqlite3.Connection, *, source_format_id: int, version_number: int,
    fingerprint_hash: str, fingerprint_plaintext: dict[str, Any], parse_config: dict[str, Any],
    status: str, provenance: str, scope: str, client_id: Optional[int],
    specimen_file_hash: Optional[str] = None, confirmed_by: Optional[str] = None,
    confirmed_at: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO f6_format_versions
            (source_format_id, version_number, fingerprint_hash, fingerprint_plaintext, parse_config,
             status, provenance, scope, client_id, specimen_file_hash, confirmed_by, confirmed_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_format_id, version_number, fingerprint_hash, json.dumps(fingerprint_plaintext),
            json.dumps(parse_config), status, provenance, scope, client_id, specimen_file_hash,
            confirmed_by, confirmed_at, _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _transform_version(d: dict[str, Any]) -> dict[str, Any]:
    d["fingerprint_plaintext"] = json.loads(d["fingerprint_plaintext"]) if d.get("fingerprint_plaintext") else {}
    d["parse_config"] = json.loads(d["parse_config"]) if d.get("parse_config") else {}
    return d


def get_version(conn: sqlite3.Connection, version_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(conn, "SELECT * FROM f6_format_versions WHERE version_id = ?", (version_id,))
    return _transform_version(row) if row else None


def list_versions(
    conn: sqlite3.Connection, *, source_format_id: Optional[int] = None,
    status: Optional[str] = None, scope: Optional[str] = None, client_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM f6_format_versions WHERE 1=1"
    params: list[Any] = []
    if source_format_id is not None:
        query += " AND source_format_id = ?"
        params.append(source_format_id)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    if scope is not None:
        query += " AND scope = ?"
        params.append(scope)
    if client_id is not None:
        query += " AND (client_id = ? OR client_id IS NULL)"
        params.append(client_id)
    query += " ORDER BY version_number DESC"
    rows = _rows_to_dicts(conn, query, tuple(params))
    return [_transform_version(r) for r in rows]


def find_active_by_fingerprint(
    conn: sqlite3.Connection, *, fingerprint_hash: str, slot: str, client_id: Optional[int],
) -> Optional[dict[str, Any]]:
    """Exact-hash match against ACTIVE versions visible to this client:
    firm-wide versions of the same slot, plus this client's own
    client-scoped versions."""
    rows = _rows_to_dicts(
        conn,
        """
        SELECT v.* FROM f6_format_versions v
        JOIN f6_source_formats f ON f.source_format_id = v.source_format_id
        WHERE v.status = 'active' AND v.fingerprint_hash = ? AND f.slot = ?
          AND (v.scope = 'firm' OR (v.scope = 'client' AND v.client_id = ?))
        ORDER BY v.scope DESC, v.version_number DESC
        """,
        (fingerprint_hash, slot, client_id),
    )
    return _transform_version(rows[0]) if rows else None


def find_quarantined_by_fingerprint(
    conn: sqlite3.Connection, *, fingerprint_hash: str,
) -> Optional[dict[str, Any]]:
    row = _row_to_dict(
        conn,
        "SELECT * FROM f6_format_versions WHERE fingerprint_hash = ? AND status = 'quarantined' LIMIT 1",
        (fingerprint_hash,),
    )
    return _transform_version(row) if row else None


def list_active_versions_for_similarity(
    conn: sqlite3.Connection, *, slot: str, client_id: Optional[int],
) -> list[dict[str, Any]]:
    rows = _rows_to_dicts(
        conn,
        """
        SELECT v.* FROM f6_format_versions v
        JOIN f6_source_formats f ON f.source_format_id = v.source_format_id
        WHERE v.status = 'active' AND f.slot = ?
          AND (v.scope = 'firm' OR (v.scope = 'client' AND v.client_id = ?))
        """,
        (slot, client_id),
    )
    return [_transform_version(r) for r in rows]


def next_version_number(conn: sqlite3.Connection, source_format_id: int, *, client_id: Optional[int]) -> int:
    row = conn.execute(
        "SELECT MAX(version_number) FROM f6_format_versions WHERE source_format_id = ? "
        "AND (client_id = ? OR (client_id IS NULL AND ? IS NULL))",
        (source_format_id, client_id, client_id),
    ).fetchone()
    return (row[0] or 0) + 1


def supersede_version(conn: sqlite3.Connection, *, old_version_id: int, new_version_id: int) -> None:
    conn.execute(
        "UPDATE f6_format_versions SET status = 'superseded', superseded_by_version_id = ? WHERE version_id = ?",
        (new_version_id, old_version_id),
    )
    conn.commit()


def quarantine_version(conn: sqlite3.Connection, *, version_id: int, reason: str, actor: str) -> None:
    conn.execute(
        "UPDATE f6_format_versions SET status = 'quarantined', quarantine_reason = ?, "
        "quarantined_by = ?, quarantined_at = ? WHERE version_id = ?",
        (reason, actor, _now(), version_id),
    )
    conn.commit()


def list_runs_using_version(conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        conn,
        "SELECT * FROM f6_ingestion_runs WHERE matched_version_id = ? ORDER BY created_at DESC",
        (version_id,),
    )


# ---------------------------------------------------------------------------
# FieldMappingRule
# ---------------------------------------------------------------------------


def insert_field_mapping_rules(conn: sqlite3.Connection, *, version_id: int, rules: list[dict[str, Any]]) -> None:
    for r in rules:
        conn.execute(
            """
            INSERT INTO f6_field_mapping_rules
                (version_id, canonical_field, kind, source_columns, transform, confidence, human_confirmed, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id, r["canonical_field"], r["kind"], json.dumps(r.get("source_columns") or []),
                r.get("transform"), r.get("confidence"), 1 if r.get("human_confirmed") else 0,
                r.get("rationale"),
            ),
        )
    conn.commit()


def list_field_mapping_rules(conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
    rows = _rows_to_dicts(conn, "SELECT * FROM f6_field_mapping_rules WHERE version_id = ?", (version_id,))
    for r in rows:
        r["source_columns"] = json.loads(r["source_columns"]) if r.get("source_columns") else []
    return rows


# ---------------------------------------------------------------------------
# IngestionRun
# ---------------------------------------------------------------------------


def insert_ingestion_run(
    conn: sqlite3.Connection, *, client_id: int, period: str, slot: str,
    source_format_key: Optional[str], filename: str, file_hash: str, path_taken: str,
    matched_version_id: Optional[int], row_count_read: int, row_count_parsed: int,
    row_count_excluded: int, exclusion_reasons: list[dict[str, Any]],
    validation_results: list[dict[str, Any]], recognised_not_parsed: list[str],
    status: str, rejection_reason: Optional[str] = None, operator: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO f6_ingestion_runs
            (client_id, period, slot, source_format_key, filename, file_hash, path_taken,
             matched_version_id, row_count_read, row_count_parsed, row_count_excluded,
             exclusion_reasons, validation_results, recognised_not_parsed, status,
             rejection_reason, operator, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_id, period, slot, source_format_key, filename, file_hash, path_taken,
            matched_version_id, row_count_read, row_count_parsed, row_count_excluded,
            json.dumps(exclusion_reasons), json.dumps(validation_results), json.dumps(recognised_not_parsed),
            status, rejection_reason, operator, _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _transform_run(d: dict[str, Any]) -> dict[str, Any]:
    for key in ("exclusion_reasons", "validation_results", "recognised_not_parsed"):
        d[key] = json.loads(d[key]) if d.get(key) else []
    return d


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(conn, "SELECT * FROM f6_ingestion_runs WHERE run_id = ?", (run_id,))
    return _transform_run(row) if row else None


def list_runs(
    conn: sqlite3.Connection, *, client_id: Optional[int] = None, period: Optional[str] = None,
    slot: Optional[str] = None, limit: int = 200,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM f6_ingestion_runs WHERE 1=1"
    params: list[Any] = []
    if client_id is not None:
        query += " AND client_id = ?"
        params.append(client_id)
    if period is not None:
        query += " AND period = ?"
        params.append(period)
    if slot is not None:
        query += " AND slot = ?"
        params.append(slot)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = _rows_to_dicts(conn, query, tuple(params))
    return [_transform_run(r) for r in rows]


def find_prior_runs_by_hash(
    conn: sqlite3.Connection, *, client_id: int, period: str, slot: str,
) -> list[dict[str, Any]]:
    rows = _rows_to_dicts(
        conn,
        "SELECT * FROM f6_ingestion_runs WHERE client_id = ? AND period = ? AND slot = ? "
        "AND status IN ('parsed') ORDER BY created_at DESC",
        (client_id, period, slot),
    )
    return [_transform_run(r) for r in rows]


# ---------------------------------------------------------------------------
# MappingProposal
# ---------------------------------------------------------------------------


def insert_mapping_proposal(
    conn: sqlite3.Connection, *, run_id: int, slot: str, proposal_json: dict[str, Any],
    model_used: str, skill_version: str, closest_version_id: Optional[int] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO f6_mapping_proposals
            (run_id, slot, proposal_json, model_used, skill_version, closest_version_id, decision, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (run_id, slot, json.dumps(proposal_json), model_used, skill_version, closest_version_id, _now()),
    )
    conn.commit()
    return cur.lastrowid


def _transform_proposal(d: dict[str, Any]) -> dict[str, Any]:
    d["proposal_json"] = json.loads(d["proposal_json"]) if d.get("proposal_json") else {}
    d["edits_json"] = json.loads(d["edits_json"]) if d.get("edits_json") else None
    return d


def get_mapping_proposal(conn: sqlite3.Connection, proposal_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(conn, "SELECT * FROM f6_mapping_proposals WHERE proposal_id = ?", (proposal_id,))
    return _transform_proposal(row) if row else None


def get_mapping_proposal_for_run(conn: sqlite3.Connection, run_id: int) -> Optional[dict[str, Any]]:
    row = _row_to_dict(
        conn,
        "SELECT * FROM f6_mapping_proposals WHERE run_id = ? ORDER BY proposal_id DESC LIMIT 1", (run_id,)
    )
    return _transform_proposal(row) if row else None


def decide_mapping_proposal(
    conn: sqlite3.Connection, *, proposal_id: int, decision: str, edits_json: Optional[dict[str, Any]],
    decided_by: str,
) -> None:
    conn.execute(
        "UPDATE f6_mapping_proposals SET decision = ?, edits_json = ?, decided_by = ?, decided_at = ? "
        "WHERE proposal_id = ?",
        (decision, json.dumps(edits_json) if edits_json is not None else None, decided_by, _now(), proposal_id),
    )
    conn.commit()


def list_pending_proposals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows_to_dicts(
        conn, "SELECT * FROM f6_mapping_proposals WHERE decision = 'pending' ORDER BY created_at DESC"
    )
    return [_transform_proposal(r) for r in rows]
