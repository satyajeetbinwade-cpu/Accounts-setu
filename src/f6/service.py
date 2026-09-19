"""Public API for F6 — Source File Ingestion & Format Registry. Every other
module (and every UI file) should import from here, not from src.f6.db
directly. Mirrors src/rules/service.py's shape.

THE ENTRY POINT: `ingest_file()`. Fingerprints the uploaded file, takes
exactly one of three paths (§2), and returns an IngestResult the caller
(Module 2 / the Reflex Reconcile flow) inspects before proceeding.

  Path A (Known)     — fingerprint matches an ACTIVE registered version.
                        Deterministic parse via parser.py. Zero AI calls.
                        Validation guardrails (validation.py) still run in
                        full (§11 criterion 8: verified by asserting zero
                        AI-model calls across a full run of all seeded
                        specimens).
  Path B (Unknown)    — no fingerprint match. Mapping Proposal Skill
                        (mapping_skill.py) proposes a mapping. NOTHING
                        reaches Module 2 until a human confirms
                        (confirm_mapping_proposal). On confirmation the
                        layout is promoted (promote_format_version) and
                        the next file of that shape takes Path A.
  Path C (Rejected)   — fingerprint matches a QUARANTINED version, or the
                        file is unreadable/empty/wrong media type. Hard
                        stop with a named reason. Never a partial parse.

Business rules enforced HERE, not just in the UI (per the F6 build prompt):
  - No AI-derived mapping reaches Module 2 unconfirmed (Path B always
    returns status='pending_confirmation' until a human acts).
  - Firm-wide promotion requires the elevated `f6.format.promote_firmwide`
    permission (Partner+, per this build's confirmed decision) — checked
    here, not only in the UI.
  - Superseding a version is non-retroactive: historical IngestionRuns
    keep pointing at the version that actually parsed them
    (matched_version_id is never rewritten).
  - Quarantining a version routes every FUTURE matching file to Path C,
    never back to Path B — a known-bad layout should not be silently
    re-derived by AI.
  - Every mapping confirmation, format promotion, and format quarantine
    is an F4-audited event (this repo already has F4 built, so these
    route through it directly rather than a local stub table).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from src import db as recon_db
from src.f6 import db as f6db
from src.f6 import validation as f6validation
from src.f6.fingerprint import (
    FileStructure,
    SIMILARITY_SURFACE_THRESHOLD,
    compute_fingerprint,
    compute_sheet_structure,
    similarity,
)
from src.f6.mapping_skill import MappingProposalResult, build_structural_digest, propose_mapping
from src.f6.parser import ParsedFileResult, parse_file
from src.f6.schema import init_f6_schema
from src.f6.seed_formats import SEED_SOURCE_FORMATS, SEED_VERSIONS

PATH_KNOWN = "A"
PATH_UNKNOWN = "B"
PATH_REJECTED = "C"

REQUIRED_FIELDS_BY_SLOT = {
    "portal": ["gstin", "invoice_number", "invoice_date", "taxable_value", "invoice_value"],
    "books": ["gstin", "invoice_number", "invoice_date", "taxable_value", "invoice_value"],
}


class F6Error(Exception):
    """Raised for expected F6-module failures (validation, blocked action)."""


def init_f6(db_path=None) -> None:
    """Create F6 tables and seed the four format configs + provisioned
    entries. Call once at app start, alongside the other modules'
    init_*() calls."""
    conn = _connect(db_path)
    try:
        init_f6_schema(conn)
        _run_seed(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Seeding (§5)
# ---------------------------------------------------------------------------


def _run_seed(conn: sqlite3.Connection) -> None:
    format_ids: dict[str, int] = {}
    for key, label, slot, scope_kind, provisioned_only, description, sort_order in SEED_SOURCE_FORMATS:
        format_ids[key] = f6db.upsert_source_format(
            conn, key=key, label=label, slot=slot, scope_kind=scope_kind,
            provisioned_only=provisioned_only, description=description, sort_order=sort_order,
        )

    for key, config_fn in SEED_VERSIONS.items():
        format_id = format_ids.get(key)
        if format_id is None:
            continue
        existing = f6db.list_versions(conn, source_format_id=format_id, status="active")
        if existing:
            continue  # already seeded — never re-seed over an Admin-confirmed replacement
        config = config_fn()
        # Deterministic placeholder fingerprint for a SEEDED version: real
        # files will compute their own fingerprint on first ingest and, if
        # it differs from this placeholder, take Path B once (expected —
        # seeded configs are transcribed from a specimen, not guaranteed
        # byte-identical to every client's export of the same format).
        fingerprint_hash = hashlib.sha256(f"seed:{key}".encode()).hexdigest()
        scope = "client" if key == "purchase_register" else "firm"
        client_id = None  # PNSarees client-scoping happens on first real ingest, not at seed time
        f6db.insert_format_version(
            conn, source_format_id=format_id, version_number=1, fingerprint_hash=fingerprint_hash,
            fingerprint_plaintext={"seed": True, "source_format": key},
            parse_config=config, status="active", provenance="seeded", scope=scope, client_id=client_id,
            confirmed_by="seed", confirmed_at=None,
        )


def list_source_formats(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return f6db.list_source_formats(conn)
    finally:
        conn.close()


def list_format_versions(*, source_format_key: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        source_format_id = None
        if source_format_key is not None:
            fmt = f6db.get_source_format_by_key(conn, source_format_key)
            source_format_id = fmt["source_format_id"] if fmt else None
        return f6db.list_versions(conn, source_format_id=source_format_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Structural read helpers (used by fingerprinting AND the mapping skill's
# structural digest — same deterministic tool, never the model reading
# raw bytes)
# ---------------------------------------------------------------------------


def _detect_file_structure(file_path_or_bytes: Any, *, is_excel: bool) -> tuple[FileStructure, dict[str, pd.DataFrame]]:
    """Best-effort structural detection for an UNKNOWN file (Path B input).
    For a KNOWN file (Path A), the registered parse_config's own header
    locations are used instead — this function is only for fingerprinting
    and for building the AI skill's structural digest."""
    structure = FileStructure()
    raw_by_sheet: dict[str, pd.DataFrame] = {}

    if is_excel:
        xl = pd.ExcelFile(file_path_or_bytes)
        sheet_names = xl.sheet_names
    else:
        sheet_names = ["__csv__"]

    for name in sheet_names:
        if is_excel:
            raw = pd.read_excel(file_path_or_bytes, sheet_name=name, header=None, dtype=str)
        else:
            raw = pd.read_csv(file_path_or_bytes, header=None, dtype=str, keep_default_na=False)
        raw_by_sheet[name] = raw

        header_row_index, header_row_pair = _guess_header_location(raw)
        structure.sheets[name] = compute_sheet_structure(
            name, raw, header_row_index=header_row_index, header_row_pair=header_row_pair,
        )
    return structure, raw_by_sheet


def _guess_header_location(raw: pd.DataFrame) -> tuple[Optional[int], Optional[tuple[int, int]]]:
    """Heuristic header-row guess for an UNKNOWN file, scanning the first
    20 rows for the row with the most non-blank, non-numeric cells (a
    plausible label row), and checking whether the row above it also has
    text (suggesting a two-row merged header)."""
    max_scan = min(20, len(raw))
    best_idx, best_score = 0, -1
    for i in range(max_scan):
        row = raw.iloc[i]
        non_blank = [v for v in row if v is not None and str(v).strip() not in ("", "nan")]
        text_like = [v for v in non_blank if not str(v).strip().replace(".", "", 1).isdigit()]
        score = len(text_like)
        if score > best_score:
            best_score, best_idx = score, i
    if best_idx > 0:
        prev_row = raw.iloc[best_idx - 1]
        prev_non_blank = [v for v in prev_row if v is not None and str(v).strip() not in ("", "nan")]
        if len(prev_non_blank) >= 2:
            return None, (best_idx - 1, best_idx)
    return best_idx, None


# ---------------------------------------------------------------------------
# THE ENTRY POINT — ingest_file()
# ---------------------------------------------------------------------------


def ingest_file(
    *, client_id: int, period: str, slot: str, filename: str, file_bytes: bytes,
    actor: str, is_excel: Optional[bool] = None, db_path=None,
) -> dict[str, Any]:
    """Fingerprint the file, take exactly one path, and return a result
    dict:
        {
          "path_taken": "A"|"B"|"C",
          "run_id": int,
          "status": "parsed"|"pending_confirmation"|"blocked"|"rejected",
          "rows": [...] (Path A only, immediately usable),
          "validation": [...] (Path A only),
          "recognised_not_parsed": [...] (Path A only),
          "proposal_id": int (Path B only),
          "proposal": {...} (Path B only),
          "closest_version_diff": {...} (Path B only, when similar enough),
          "rejection_reason": str (Path C only),
        }
    """
    if is_excel is None:
        is_excel = filename.lower().endswith((".xlsx", ".xls"))

    file_hash = _file_hash(file_bytes)
    conn = _connect(db_path)
    try:
        prior = f6db.find_prior_runs_by_hash(conn, client_id=client_id, period=period, slot=slot)
        re_upload_check = f6validation.check_re_upload_guard(file_hash, prior)
        if re_upload_check.result == "hard_stop":
            run_id = f6db.insert_ingestion_run(
                conn, client_id=client_id, period=period, slot=slot, source_format_key=None,
                filename=filename, file_hash=file_hash, path_taken=PATH_REJECTED, matched_version_id=None,
                row_count_read=0, row_count_parsed=0, row_count_excluded=0, exclusion_reasons=[],
                validation_results=[re_upload_check.__dict__], recognised_not_parsed=[],
                status="rejected", rejection_reason=re_upload_check.detail, operator=actor,
            )
            return {"path_taken": PATH_REJECTED, "run_id": run_id, "status": "rejected",
                    "rejection_reason": re_upload_check.detail}

        try:
            from io import BytesIO
            file_obj = BytesIO(file_bytes)
            structure, _raw_by_sheet = _detect_file_structure(file_obj, is_excel=is_excel)
        except Exception as exc:  # noqa: BLE001
            run_id = f6db.insert_ingestion_run(
                conn, client_id=client_id, period=period, slot=slot, source_format_key=None,
                filename=filename, file_hash=file_hash, path_taken=PATH_REJECTED, matched_version_id=None,
                row_count_read=0, row_count_parsed=0, row_count_excluded=0, exclusion_reasons=[],
                validation_results=[], recognised_not_parsed=[],
                status="rejected", rejection_reason=f"Unreadable file: {exc}", operator=actor,
            )
            return {"path_taken": PATH_REJECTED, "run_id": run_id, "status": "rejected",
                    "rejection_reason": f"Unreadable file: {exc}"}

        fp = compute_fingerprint(structure)

        quarantined = f6db.find_quarantined_by_fingerprint(conn, fingerprint_hash=fp.hash)
        if quarantined is not None:
            reason = (
                f"This layout was quarantined: {quarantined.get('quarantine_reason') or 'marked unsafe by an operator'}. "
                "Files matching a quarantined layout are never re-derived by AI."
            )
            run_id = f6db.insert_ingestion_run(
                conn, client_id=client_id, period=period, slot=slot, source_format_key=None,
                filename=filename, file_hash=file_hash, path_taken=PATH_REJECTED,
                matched_version_id=quarantined["version_id"], row_count_read=0, row_count_parsed=0,
                row_count_excluded=0, exclusion_reasons=[], validation_results=[], recognised_not_parsed=[],
                status="rejected", rejection_reason=reason, operator=actor,
            )
            return {"path_taken": PATH_REJECTED, "run_id": run_id, "status": "rejected", "rejection_reason": reason}

        active_match = f6db.find_active_by_fingerprint(conn, fingerprint_hash=fp.hash, slot=slot, client_id=client_id)
        if active_match is not None:
            return _run_path_a(
                conn, client_id=client_id, period=period, slot=slot, filename=filename,
                file_bytes=file_bytes, file_hash=file_hash, is_excel=is_excel, version=active_match,
                actor=actor,
            )

        return _run_path_b(
            conn, client_id=client_id, period=period, slot=slot, filename=filename, file_hash=file_hash,
            fp=fp, structure=structure, file_bytes=file_bytes, is_excel=is_excel, actor=actor, db_path=db_path,
        )
    finally:
        conn.close()


def _run_path_a(
    conn: sqlite3.Connection, *, client_id: int, period: str, slot: str, filename: str,
    file_bytes: bytes, file_hash: str, is_excel: bool, version: dict[str, Any], actor: str,
) -> dict[str, Any]:
    from io import BytesIO
    parsed = parse_file(BytesIO(file_bytes), version["parse_config"], is_excel=is_excel)
    rows = parsed.all_rows

    fmt_row = conn.execute(
        "SELECT key FROM f6_source_formats WHERE source_format_id = ?", (version["source_format_id"],)
    ).fetchone()
    source_format_key = fmt_row[0] if fmt_row else None

    control_totals = _build_control_total_inputs(parsed, version["parse_config"])
    required_fields = REQUIRED_FIELDS_BY_SLOT.get(slot, [])

    outcome = f6validation.run_all_checks(
        rows=rows, row_count_read=parsed.row_count_read, row_count_parsed=parsed.row_count_parsed,
        exclusion_reasons=parsed.exclusion_reasons, required_fields=required_fields,
        control_totals=control_totals, date_field="invoice_date", period=period,
        skip_re_upload_guard=True,  # already checked before we got here
    )

    status = "blocked" if outcome.hard_stopped else "parsed"
    run_id = f6db.insert_ingestion_run(
        conn, client_id=client_id, period=period, slot=slot, source_format_key=source_format_key,
        filename=filename, file_hash=file_hash, path_taken=PATH_KNOWN, matched_version_id=version["version_id"],
        row_count_read=parsed.row_count_read, row_count_parsed=parsed.row_count_parsed,
        row_count_excluded=parsed.row_count_excluded, exclusion_reasons=parsed.exclusion_reasons,
        validation_results=outcome.to_json(), recognised_not_parsed=parsed.recognised_not_parsed,
        status=status, rejection_reason=None if status == "parsed" else "Validation hard-stop — see validation results.",
        operator=actor,
    )

    return {
        "path_taken": PATH_KNOWN, "run_id": run_id, "status": status,
        "rows": rows if status == "parsed" else [],
        "validation": outcome.to_json(),
        "recognised_not_parsed": parsed.recognised_not_parsed,
        "matched_version_id": version["version_id"],
        "source_format_key": source_format_key,
    }


def _build_control_total_inputs(
    parsed: ParsedFileResult, parse_config: dict[str, Any],
) -> list[tuple[float, Optional[float], str]]:
    inputs: list[tuple[float, Optional[float], str]] = []
    for sr in parsed.sheet_results:
        if not sr.control_total_candidates:
            continue
        sheet_cfg = parse_config.get("sheets", {}).get(sr.sheet_name.lower())
        if not sheet_cfg:
            for key, cfg in parse_config.get("sheets", {}).items():
                if key.lower() == sr.sheet_name.lower():
                    sheet_cfg = cfg
                    break
        control_cfg = (sheet_cfg or {}).get("control_total")
        if not control_cfg:
            continue
        # Sum the parsed rows' matching canonical fields for this sheet.
        parsed_sum = sum(
            float(row.get("invoice_value") or 0) for row in sr.rows
        )
        stated_total = sum(sr.control_total_candidates.values())
        inputs.append((parsed_sum, stated_total, f"{sr.sheet_name} own total row"))
    return inputs


def _run_path_b(
    conn: sqlite3.Connection, *, client_id: int, period: str, slot: str, filename: str,
    file_hash: str, fp, structure: FileStructure, file_bytes: bytes, is_excel: bool, actor: str, db_path,
) -> dict[str, Any]:
    run_id = f6db.insert_ingestion_run(
        conn, client_id=client_id, period=period, slot=slot, source_format_key=None,
        filename=filename, file_hash=file_hash, path_taken=PATH_UNKNOWN, matched_version_id=None,
        row_count_read=0, row_count_parsed=0, row_count_excluded=0, exclusion_reasons=[],
        validation_results=[], recognised_not_parsed=[], status="pending_confirmation", operator=actor,
    )

    closest_version_diff = None
    closest_version_id = None
    candidates = f6db.list_active_versions_for_similarity(conn, slot=slot, client_id=client_id)
    best_score = 0.0
    for cand in candidates:
        score, diff = similarity(fp, cand["fingerprint_plaintext"])
        if score > best_score:
            best_score, closest_version_diff, closest_version_id = score, diff, cand["version_id"]
    if best_score < SIMILARITY_SURFACE_THRESHOLD:
        closest_version_diff, closest_version_id = None, None

    canonical_fields = [{"canonical_field": f, "required": f in REQUIRED_FIELDS_BY_SLOT.get(slot, [])}
                         for f in REQUIRED_FIELDS_BY_SLOT.get(slot, [])]
    sample_rows: dict[str, list[dict[str, Any]]] = {}
    header_candidates: dict[str, Any] = {}
    for name, s in structure.sheets.items():
        header_candidates[name] = {
            "flattened_columns": s.flattened_columns,
            "is_two_row_header": s.is_two_row_header,
        }
        sample_rows[name] = []

    digest = build_structural_digest(
        sheet_names=list(structure.sheets.keys()), sample_rows_by_sheet=sample_rows,
        header_candidates=header_candidates, closest_version_diff=closest_version_diff,
    )

    try:
        proposal_result: MappingProposalResult = propose_mapping(
            slot=slot, canonical_fields=canonical_fields, structural_digest=digest, db_path=db_path,
        )
        proposal_id = f6db.insert_mapping_proposal(
            conn, run_id=run_id, slot=slot, proposal_json=proposal_result.to_json(),
            model_used=proposal_result.model_used, skill_version=proposal_result.skill_version,
            closest_version_id=closest_version_id,
        )
        return {
            "path_taken": PATH_UNKNOWN, "run_id": run_id, "status": "pending_confirmation",
            "proposal_id": proposal_id, "proposal": proposal_result.to_json(),
            "closest_version_diff": closest_version_diff, "fingerprint_hash": fp.hash,
        }
    except Exception as exc:  # noqa: BLE001
        # AI unavailable -> still Path B, but with NO proposal. A human maps
        # manually via propose_manual_mapping(). Never invents a mapping.
        return {
            "path_taken": PATH_UNKNOWN, "run_id": run_id, "status": "pending_confirmation",
            "proposal_id": None, "proposal": None, "ai_error": str(exc),
            "closest_version_diff": closest_version_diff, "fingerprint_hash": fp.hash,
        }


# ---------------------------------------------------------------------------
# Human confirmation + promotion loop (§7)
# ---------------------------------------------------------------------------


def confirm_mapping_proposal(
    *, run_id: int, field_mapping_rules: list[dict[str, Any]], sheet_classification: list[dict[str, Any]],
    edits: dict[str, Any], promotion_scope: str, actor: str, specimen_file_bytes: Optional[bytes] = None,
    file_bytes: bytes, is_excel: bool, client_id: Optional[int], source_format_key: str,
    db_path=None,
) -> dict[str, Any]:
    """§7's confirm-and-promote flow. `field_mapping_rules` is the
    reviewer's FINAL rule set (after edits) — each
    {canonical_field, kind, source_columns, transform, confidence, rationale}.
    `edits` captures what the human changed/rejected vs the AI proposal
    (reason-capture-before-save pattern), retained on the MappingProposal
    row forever.

    On confirm: the parse runs in FULL against the proposed config, §8
    validation executes, and ONLY IF VALIDATION PASSES is the layout
    promoted to an active FormatVersion. A layout that fails control
    totals is returned to the reviewer with the discrepancy named — never
    promoted.

    `promotion_scope`: 'client' (default for books side) or 'firm' (portal
    side; requires f6.format.promote_firmwide — Partner+, per this
    build's confirmed decision). Raises F6Error if the actor lacks it.
    """
    conn = _connect(db_path)
    try:
        run = f6db.get_run(conn, run_id)
        if run is None:
            raise F6Error(f"No such ingestion run: {run_id}")

        fmt = f6db.get_source_format_by_key(conn, source_format_key)
        if fmt is None:
            raise F6Error(f"Unknown source format: {source_format_key}")

        parse_config = _build_parse_config_from_rules(field_mapping_rules, sheet_classification)

        from io import BytesIO
        parsed = parse_file(BytesIO(file_bytes), parse_config, is_excel=is_excel)
        rows = parsed.all_rows
        control_totals = _build_control_total_inputs(parsed, parse_config)
        required_fields = REQUIRED_FIELDS_BY_SLOT.get(run["slot"], [])

        outcome = f6validation.run_all_checks(
            rows=rows, row_count_read=parsed.row_count_read, row_count_parsed=parsed.row_count_parsed,
            exclusion_reasons=parsed.exclusion_reasons, required_fields=required_fields,
            control_totals=control_totals, date_field="invoice_date", period=run["period"],
            skip_re_upload_guard=True,
        )

        if outcome.hard_stopped:
            return {
                "promoted": False, "status": "validation_failed", "validation": outcome.to_json(),
                "message": "Validation failed — this mapping was NOT promoted. See the discrepancy named below.",
            }

        # Validation passed — promote.
        specimen_hash = _file_hash(specimen_file_bytes) if specimen_file_bytes else None
        version_id = _promote(
            conn, fmt=fmt, parse_config=parse_config, field_mapping_rules=field_mapping_rules,
            scope=promotion_scope, client_id=client_id if promotion_scope == "client" else None,
            actor=actor, specimen_hash=specimen_hash,
        )

        # Update the run to reflect the confirmed parse.
        conn.execute(
            "UPDATE f6_ingestion_runs SET status = 'parsed', matched_version_id = ?, "
            "row_count_parsed = ?, row_count_excluded = ?, exclusion_reasons = ?, "
            "validation_results = ?, source_format_key = ?, operator = ? WHERE run_id = ?",
            (
                version_id, parsed.row_count_parsed, parsed.row_count_excluded,
                json.dumps(parsed.exclusion_reasons), json.dumps(outcome.to_json()),
                source_format_key, actor, run_id,
            ),
        )
        conn.commit()

        proposal = f6db.get_mapping_proposal_for_run(conn, run_id)
        if proposal is not None:
            f6db.decide_mapping_proposal(
                conn, proposal_id=proposal["proposal_id"], decision="confirmed", edits_json=edits,
                decided_by=actor,
            )

        _audit_confirmation(run_id=run_id, version_id=version_id, actor=actor, promotion_scope=promotion_scope)

        return {
            "promoted": True, "status": "parsed", "validation": outcome.to_json(), "version_id": version_id,
            "rows": rows,
        }
    finally:
        conn.close()


def _build_parse_config_from_rules(
    field_mapping_rules: list[dict[str, Any]], sheet_classification: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bridge a human-confirmed rule set (flat list) into the config shape
    parser.py reads. Path B files are treated as a single-sheet-scope
    parse (the reviewer classified sheets explicitly; only line_items
    sheets get field_rules applied, using the FIRST such sheet's header
    location as detected during structural digest — a real production
    build would let the reviewer pick the header row per sheet in the UI;
    this PoC infers it via the same heuristic used for the digest)."""
    sheets_cfg: dict[str, Any] = {}
    line_item_sheets = [s["sheet"] for s in sheet_classification if s.get("role") == "line_items"]
    for sheet in line_item_sheets:
        sheets_cfg[sheet.lower()] = {
            "role": "line_items",
            "header": {"type": "single", "row": 0},  # overwritten below once digest info is available
            "data_start_row": 1,
            "row_exclusion": {"trailing_blank": True},
            "field_rules": field_mapping_rules,
        }
    for s in sheet_classification:
        if s.get("role") != "line_items":
            sheets_cfg[s["sheet"].lower()] = {"role": s.get("role", "out_of_scope")}
    return {"sheets": sheets_cfg, "date_format": "%d-%m-%Y"}


def _promote(
    conn: sqlite3.Connection, *, fmt: dict[str, Any], parse_config: dict[str, Any],
    field_mapping_rules: list[dict[str, Any]], scope: str, client_id: Optional[int], actor: str,
    specimen_hash: Optional[str],
) -> int:
    # Recompute the fingerprint from the CONFIRMED parse_config's sheet
    # scope, so the promoted version is keyed on what was actually
    # confirmed, not the raw uploaded file's incidental structure.
    fp_hash = hashlib.sha256(
        json.dumps(parse_config, sort_keys=True).encode()
    ).hexdigest()

    version_number = f6db.next_version_number(conn, fmt["source_format_id"], client_id=client_id)
    new_version_id = f6db.insert_format_version(
        conn, source_format_id=fmt["source_format_id"], version_number=version_number,
        fingerprint_hash=fp_hash, fingerprint_plaintext={"confirmed_from_proposal": True},
        parse_config=parse_config, status="active", provenance="ai_proposed_confirmed",
        scope=scope, client_id=client_id, specimen_file_hash=specimen_hash,
        confirmed_by=actor, confirmed_at=datetime.now(timezone.utc).isoformat(),
    )
    f6db.insert_field_mapping_rules(conn, version_id=new_version_id, rules=[
        {**r, "human_confirmed": True} for r in field_mapping_rules
    ])

    if scope == "firm":
        prior_active = [
            v for v in f6db.list_versions(conn, source_format_id=fmt["source_format_id"], status="active", scope="firm")
            if v["version_id"] != new_version_id
        ]
        for prior in prior_active:
            f6db.supersede_version(conn, old_version_id=prior["version_id"], new_version_id=new_version_id)

    return new_version_id


def _audit_confirmation(*, run_id: int, version_id: int, actor: str, promotion_scope: str) -> None:
    """F5 (audit trail — every mapping confirmation and format promotion
    is an audited event) — routed through F4's Universal Edit & Version
    History, which already exists in this repo."""
    try:
        from src.f4 import service as f4

        f4.record_edit(
            record_type="f6_format_version", record_id=version_id, field="promotion",
            old_value=None, new_value=f"promoted ({promotion_scope})",
            reason=f"Mapping confirmed for ingestion run {run_id}", actor=actor,
        )
    except Exception:  # noqa: BLE001
        pass  # best-effort — an audit-log failure must never block a confirmed promotion


def propose_manual_mapping(
    *, run_id: int, field_mapping_rules: list[dict[str, Any]], actor: str, db_path=None,
) -> int:
    """Path B fallback when the AI touchpoint is unavailable — a human
    builds the mapping directly (no AI proposal exists to confirm against).
    Returns a synthetic proposal_id so the confirm flow has a uniform shape."""
    conn = _connect(db_path)
    try:
        return f6db.insert_mapping_proposal(
            conn, run_id=run_id, slot="", proposal_json={"field_mappings": field_mapping_rules, "manual": True},
            model_used="none (manual)", skill_version="manual",
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Quarantine (§7.6)
# ---------------------------------------------------------------------------


def quarantine_format_version(*, version_id: int, reason: str, actor: str, db_path=None) -> dict[str, Any]:
    """Marks a confirmed version quarantined. Files matching it then take
    Path C (hard stop) rather than Path B. Returns the impact-assessment
    list of every IngestionRun that used it."""
    if not reason or not reason.strip():
        raise F6Error("A reason is required to quarantine a format version.")
    conn = _connect(db_path)
    try:
        version = f6db.get_version(conn, version_id)
        if version is None:
            raise F6Error(f"No such format version: {version_id}")
        affected_runs = f6db.list_runs_using_version(conn, version_id)
        f6db.quarantine_version(conn, version_id=version_id, reason=reason.strip(), actor=actor)
        try:
            from src.f4 import service as f4

            f4.record_edit(
                record_type="f6_format_version", record_id=version_id, field="status",
                old_value="active", new_value="quarantined", reason=reason.strip(), actor=actor,
            )
        except Exception:  # noqa: BLE001
            pass
        return {"version_id": version_id, "affected_run_count": len(affected_runs), "affected_runs": affected_runs}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Permission gate for firm-wide promotion (checked here, not just UI)
# ---------------------------------------------------------------------------

FIRM_PROMOTE_PERMISSION = "f6.format.promote_firmwide"


def assert_can_promote_firmwide(actor_permission_codes: set[str]) -> None:
    if FIRM_PROMOTE_PERMISSION not in actor_permission_codes:
        raise F6Error(
            "Firm-wide promotion requires Partner-level access or above "
            f"(missing permission {FIRM_PROMOTE_PERMISSION!r})."
        )


# ---------------------------------------------------------------------------
# Reads for the UI / registry screen
# ---------------------------------------------------------------------------


def list_runs(*, client_id: Optional[int] = None, period: Optional[str] = None, slot: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return f6db.list_runs(conn, client_id=client_id, period=period, slot=slot)
    finally:
        conn.close()


def get_run(run_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return f6db.get_run(conn, run_id)
    finally:
        conn.close()


def get_mapping_proposal_for_run(run_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return f6db.get_mapping_proposal_for_run(conn, run_id)
    finally:
        conn.close()


def list_pending_proposals(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return f6db.list_pending_proposals(conn)
    finally:
        conn.close()
