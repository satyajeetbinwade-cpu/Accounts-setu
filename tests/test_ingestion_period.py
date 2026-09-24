"""Regression tests for the Smart Ingestion period fix.

WHY THESE EXIST
---------------
The reconciliation engine reads source files from
``data/<client>/<period>/<source_type>/`` and the Run/Reconcile pickers list
the sub-directories of ``data/<client>/`` as the available periods. When the
upload form's Period field was optional, a blank period wrote the file to a
literal ``"-"`` folder — on disk, but unreachable by every reconciliation.

These tests lock in the three things that fix it:

1. ``periods.suggest_period`` derives a period from real evidence and
   honestly returns ``None`` when there is none (never a guess).
2. ``service.set_upload_period`` performs ALL FOUR steps of a re-file —
   document period, re-keyed ingestion result, moved bytes, audit entry —
   because doing only some leaves the file half-visible.
3. The unfiled sentinel is labelled, not hidden, and is excluded from the
   periods a run may target.

Run:

    venv/bin/python -m pytest tests/test_ingestion_period.py -v
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import db as recon_db
from src.documents import db as ddb
from src.documents import service as documents
from src.ingestion_ai import db as idb
from src.ingestion_ai import normalizer
from src.ingestion_ai import periods as p
from src.ingestion_ai import service as ingestion_ai
from src.ingestion_ai.schema import init_ingestion_ai_schema

PERIOD = "2026-08"
UNFILED = "-"


# ---------------------------------------------------------------------------
# 1. Period derivation — real evidence, and honest None
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        # GSTN GSTR-2B download convention: MMYYYY.
        ("082026_07AAACP9472A1ZJ_GSTR2B_15092026 (1).xlsx", "2026-08"),
        # ISO in the name.
        ("gstr2b_2026-08_export.csv", "2026-08"),
        # YYYYMM.
        ("purchase_register_202608.xlsx", "2026-08"),
        # Mon-YY.
        ("PurchaseRegister Aug'26.xlsx", "2026-08"),
        ("PurchaseRegister Aug-2026.xlsx", "2026-08"),
    ],
)
def test_period_from_filename(filename, expected):
    assert p.period_from_filename(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        # A download date is NOT a return period — must not be guessed.
        "IMS_07AAACP9472A1ZJ_14092026_1 (1).xlsx",
        "PurchaseRegister(Bill-wise) (1).xlsx",
        "invoice.xlsx",
        "",
    ],
)
def test_period_from_filename_returns_none_without_signal(filename):
    assert p.period_from_filename(filename) is None


def test_period_from_metadata_prefers_the_files_own_statement():
    # A books register's header period.
    assert p.period_from_metadata({"period_start": "2026-08-01", "period_end": "2026-08-31"}) == "2026-08"
    # A portal export's return period (already normalised by the F6 transform).
    assert p.period_from_metadata({"gstr1_period": "2026-08"}) == "2026-08"
    # Nothing stated.
    assert p.period_from_metadata({}) is None
    assert p.period_from_metadata(None) is None


def test_suggest_period_reports_its_evidence():
    period, source = p.suggest_period("x.xlsx", {"metadata": {"period_start": "2026-08-01"}})
    assert (period, source) == ("2026-08", "the file's stated period")

    period, source = p.suggest_period("082026_export.xlsx", None)
    assert (period, source) == ("2026-08", "the filename")

    # No signal at all — the caller must ask a human, not invent one.
    period, source = p.suggest_period("IMS_14092026.xlsx", None)
    assert (period, source) == (None, "")


def test_metadata_beats_filename():
    """A file that states its own period must not be overridden by a
    download date in its name."""
    period, source = p.suggest_period(
        "082026_export.xlsx", {"metadata": {"period_start": "2026-07-01"}}
    )
    assert period == "2026-07"
    assert source == "the file's stated period"


def test_period_validity_and_labels():
    assert p.is_valid_period("2026-08")
    assert not p.is_valid_period("-")
    assert not p.is_valid_period("")
    assert not p.is_valid_period("2026-13")
    assert not p.is_valid_period("Aug 2026")

    assert p.is_unfiled("-")
    assert p.is_unfiled(None)
    assert not p.is_unfiled("2026-08")

    assert p.period_label("-") == p.UNFILED_LABEL
    assert p.period_label("2026-08") == "2026-08"


# ---------------------------------------------------------------------------
# 2. Re-filing — all four steps, on a scratch DB + scratch data root
# ---------------------------------------------------------------------------


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    """A scratch DB and a scratch data/ root, so the test never touches the
    real PoC database or the real client folders."""
    db_path = tmp_path / "poc.db"
    data_root = tmp_path / "data"
    data_root.mkdir()

    monkeypatch.setattr("src.data_paths.DATA_ROOT", data_root)
    monkeypatch.setattr("src.shared.discovery.DATA_ROOT", data_root)

    conn = recon_db.get_connection(str(db_path))
    try:
        from src.documents.schema import init_documents_schema
        from src.f4.schema import init_f4_schema

        init_documents_schema(conn)
        init_ingestion_ai_schema(conn)
        init_f4_schema(conn)
    finally:
        conn.close()

    return {"db_path": str(db_path), "data_root": data_root}


def _seed_upload(scratch, *, period, filename="082026_export.xlsx", source_type="gstr2b", client_ref="TestClient"):
    """Create a document + raw_upload + stored ingestion result, and write
    the bytes to disk exactly as the real upload path would."""
    from src.data_paths import source_data_path

    db_path = scratch["db_path"]
    conn = recon_db.get_connection(db_path)
    try:
        document_id = ddb.create_document_with_version(
            conn, client_id=1, doc_type="GSTR-2B", period=period, filename=filename,
            file_ext="xlsx", file_size=4, file_bytes=b"data",
            classification_source="ai", classification_pct=None,
            review_status="pending_review", created_by="tester",
        )
        upload_id = idb.create_raw_upload(
            conn, document_id=document_id, client_id=1, source_type=source_type,
            filename=filename, status="confirmed", created_by="tester",
        )
        idb.upsert_ingestion_result(
            conn,
            upload_key=normalizer.upload_key(client_ref, period, source_type, filename),
            client_ref=client_ref, client_id=1, period=period, source_type=source_type,
            filename=filename, status="ok", header_row=0, sheet_name=None, sheet_ambiguous=False,
            headers=["gstin"], mapping=[], classification=None, unmapped_required=[],
            warnings=[], row_count_in=1, row_count_out=1, model_used=None, llm_cached=False,
            actor="tester",
        )
    finally:
        conn.close()

    directory = source_data_path(client_ref, period or UNFILED, source_type)
    (directory / filename).write_bytes(b"data")
    return upload_id


def test_set_upload_period_moves_rekeys_and_audits(scratch):
    upload_id = _seed_upload(scratch, period=None)
    db_path = scratch["db_path"]
    data_root = scratch["data_root"]

    old_dir = data_root / "TestClient" / UNFILED / "gstr2b"
    assert (old_dir / "082026_export.xlsx").exists(), "the file starts unfiled on disk"

    out = ingestion_ai.set_upload_period(
        upload_id, PERIOD, actor="tester", client_ref="TestClient", db_path=db_path,
    )
    assert out["period"] == PERIOD
    assert out["moved"] is True
    assert out["rekeyed"] == 1

    # 1. the document's period
    conn = recon_db.get_connection(db_path)
    try:
        upload = idb.get_raw_upload(conn, upload_id)
        doc = ddb.get_document(conn, upload["document_id"])
        assert doc["period"] == PERIOD

        # 2. the stored result is reachable under the NEW key
        new_key = normalizer.upload_key("TestClient", PERIOD, "gstr2b", "082026_export.xlsx")
        assert idb.get_ingestion_result(conn, new_key) is not None
        old_key = normalizer.upload_key("TestClient", None, "gstr2b", "082026_export.xlsx")
        assert idb.get_ingestion_result(conn, old_key) is None
    finally:
        conn.close()

    # 3. the bytes moved, and the unfiled folder is gone
    assert (data_root / "TestClient" / PERIOD / "gstr2b" / "082026_export.xlsx").exists()
    assert not (old_dir / "082026_export.xlsx").exists()
    assert not old_dir.exists(), "the empty unfiled folder is tidied away"

    # 4. an F4 audit entry records the change
    from src.f4 import service as f4

    history = f4.history_for_record("ingestion_upload", "gstr2b:082026_export.xlsx", db_path=db_path)
    assert any(h["field"] == "period" and h["new_value"] == PERIOD for h in history)


def test_set_upload_period_rejects_an_invalid_period(scratch):
    upload_id = _seed_upload(scratch, period=None)
    with pytest.raises(ingestion_ai.IngestionAIError):
        ingestion_ai.set_upload_period(
            upload_id, "-", actor="tester", client_ref="TestClient", db_path=scratch["db_path"],
        )
    with pytest.raises(ingestion_ai.IngestionAIError):
        ingestion_ai.set_upload_period(
            upload_id, "Aug 2026", actor="tester", client_ref="TestClient", db_path=scratch["db_path"],
        )


def test_unfiled_uploads_lists_only_unfiled(scratch):
    unfiled_id = _seed_upload(scratch, period=None, filename="unfiled.xlsx")
    _seed_upload(scratch, period=PERIOD, filename="filed.xlsx")

    rows = ingestion_ai.unfiled_uploads(db_path=scratch["db_path"])
    assert [r["upload_id"] for r in rows] == [unfiled_id]


def test_materialize_refuses_to_write_the_unfiled_sentinel(scratch):
    """The defensive backstop: even if a caller passes no period, the file
    must never be written to the unreachable `-` folder."""
    upload_id = _seed_upload(scratch, period=None)
    # Remove the seeded file so we can prove nothing new is written.
    (scratch["data_root"] / "TestClient" / UNFILED / "gstr2b" / "082026_export.xlsx").unlink()

    result = ingestion_ai.materialize_upload_to_disk(
        upload_id, client_ref="TestClient", db_path=scratch["db_path"],
    )
    assert result is None
    assert not (scratch["data_root"] / "TestClient" / UNFILED / "gstr2b" / "082026_export.xlsx").exists()


# ---------------------------------------------------------------------------
# 3. Discovery — the sentinel is labelled, and excluded from runnable periods
# ---------------------------------------------------------------------------


def test_discovery_labels_and_excludes_the_unfiled_period(scratch):
    from src.shared import discovery

    _seed_upload(scratch, period=None, filename="unfiled.xlsx")
    _seed_upload(scratch, period=PERIOD, filename="filed.xlsx")

    all_periods = discovery.list_periods("TestClient")
    assert UNFILED in all_periods, "an unfiled file must never be silently invisible"
    assert PERIOD in all_periods

    reconcilable = discovery.reconcilable_periods("TestClient")
    assert UNFILED not in reconcilable
    assert PERIOD in reconcilable


def test_source_file_status_still_finds_unfiled_files(scratch):
    """The unfiled folder is still discoverable — the fix is to LABEL it,
    not to hide the files."""
    from src.shared import discovery

    _seed_upload(scratch, period=None, filename="unfiled.xlsx")
    status = discovery.source_file_status("TestClient", UNFILED, "GST")
    assert "unfiled.xlsx" in status["gstr2b"]["files"]