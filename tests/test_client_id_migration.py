"""Tests for the client-id identity migration (Steps 1-2).

Builds a synthetic LEGACY-shaped DB (client names, ``data/<name>/`` folders),
runs the migration, and asserts the guarantees the task calls out:
row counts unchanged, no orphans, review state still linked, a run found by
client_id, idempotency, path safety, and that nothing outside
``src/data_paths.py`` builds a data path.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.migrations import migrate  # noqa: E402
from src.migrations.identity import resolve_mapping  # noqa: E402

# --- legacy DDL (the shape the migration must upgrade FROM) -----------------

LEGACY_DDL = """
CREATE TABLE end_clients (
    client_id INTEGER PRIMARY KEY AUTOINCREMENT, legal_name TEXT NOT NULL,
    pan TEXT, tan TEXT, gstin TEXT, assigned_team TEXT,
    primary_contact_email TEXT, primary_contact_phone TEXT, primary_contact_address TEXT,
    is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, created_by TEXT
);
CREATE TABLE runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL, period TEXT NOT NULL,
    recon_type TEXT NOT NULL, run_timestamp TEXT NOT NULL, config_snapshot TEXT NOT NULL,
    source_file_names TEXT, caveats TEXT, run_notes TEXT
);
CREATE TABLE match_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs(run_id),
    fingerprint TEXT, classification TEXT NOT NULL, confidence_score REAL NOT NULL,
    confidence_band TEXT NOT NULL, books_record TEXT, portal_record TEXT,
    match_reason TEXT, difference_type TEXT, matched_record_ids TEXT,
    reviewed INTEGER NOT NULL DEFAULT 0, reviewer_note TEXT, difference REAL,
    itc_at_risk REAL, gross_value REAL
);
CREATE TABLE review_state (
    client TEXT NOT NULL, period TEXT NOT NULL, recon_type TEXT NOT NULL, fingerprint TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0, reviewer_note TEXT, reviewed_at TEXT,
    reviewed_in_run_id INTEGER, reviewed_classification TEXT, reviewed_difference_type TEXT,
    reviewed_confidence_band TEXT, PRIMARY KEY (client, period, recon_type, fingerprint)
);
CREATE TABLE result_field_overrides (
    client TEXT NOT NULL, period TEXT NOT NULL, recon_type TEXT NOT NULL, fingerprint TEXT NOT NULL,
    side TEXT NOT NULL, field TEXT NOT NULL, value TEXT, reason TEXT, changed_by TEXT, changed_at TEXT,
    PRIMARY KEY (client, period, recon_type, fingerprint, side, field)
);
CREATE TABLE ingestion_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT, upload_key TEXT NOT NULL, client_ref TEXT NOT NULL,
    client_id INTEGER, period TEXT, source_type TEXT NOT NULL, filename TEXT NOT NULL,
    status TEXT NOT NULL, header_row INTEGER, sheet_name TEXT, sheet_ambiguous INTEGER NOT NULL DEFAULT 0,
    headers_json TEXT NOT NULL, mapping_json TEXT NOT NULL, classification_json TEXT,
    unmapped_required_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
    row_count_in INTEGER NOT NULL DEFAULT 0, row_count_out INTEGER NOT NULL DEFAULT 0,
    model_used TEXT, llm_cached INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, created_by TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_ingestion_results_upload ON ingestion_results(upload_key);
CREATE TABLE ingestion_shape_cache (
    shape_id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, client_ref TEXT NOT NULL,
    source_type TEXT NOT NULL, header_signature TEXT NOT NULL, headers_json TEXT NOT NULL,
    mapping_json TEXT NOT NULL, classification_json TEXT, model_used TEXT, confidence_floor REAL,
    trusted INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'ai',
    times_used INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, last_used_at TEXT
);
CREATE UNIQUE INDEX idx_shape_cache_lookup ON ingestion_shape_cache(client_ref, source_type, header_signature);
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
    email TEXT, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL, role_id INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1, is_end_client INTEGER NOT NULL DEFAULT 0,
    end_client_ref TEXT, created_at TEXT NOT NULL, last_login_at TEXT
);
CREATE TABLE per_client_team_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(user_id),
    client TEXT NOT NULL, assigned_by TEXT, created_at TEXT NOT NULL, UNIQUE (user_id, client)
);
"""


def _legacy_db(tmp_path: Path, *, active_client: str = "Test Client") -> Path:
    db_path = tmp_path / "poc.db"
    con = sqlite3.connect(db_path)
    con.executescript(LEGACY_DDL)
    con.execute(
        "INSERT INTO end_clients (legal_name, pan, gstin, is_active, created_at) VALUES (?,?,?,?,?)",
        (active_client, "AABCM1234K", "27AABCM1234K1Z9", 1, "2026-01-01"),
    )
    # 3 runs for that client, each with 2 match results
    for i in range(3):
        rid = con.execute(
            "INSERT INTO runs (client, period, recon_type, run_timestamp, config_snapshot, source_file_names) "
            "VALUES (?,?,?,?,?,?)",
            (active_client, "2026-08", "GST", "2026-09-0%d" % (i + 1), "cfg", "[]"),
        ).lastrowid
        for j in range(2):
            con.execute(
                "INSERT INTO match_results (run_id, fingerprint, classification, confidence_score, confidence_band) "
                "VALUES (?,?,?,?,?)",
                (rid, f"fp{i}{j}", "Matched", 0.9, "High"),
            )
    con.execute(
        "INSERT INTO review_state (client, period, recon_type, fingerprint, reviewed, reviewed_classification) "
        "VALUES (?,?,?,?,1,?)",
        (active_client, "2026-08", "GST", "fp00", "Matched"),
    )
    con.execute(
        "INSERT INTO ingestion_results (upload_key, client_ref, client_id, period, source_type, filename, status, headers_json, mapping_json, unmapped_required_json, warnings_json, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{active_client}/2026-08/tally/books.xlsx", active_client, 1, "2026-08", "tally", "books.xlsx",
         "ok", "[]", "{}", "[]", "[]", "2026-01-01", "tester"),
    )
    con.commit()
    con.close()
    return db_path


def _legacy_data(tmp_path: Path, names) -> Path:
    data = tmp_path / "data"
    for n in names:
        (data / n / "2026-08" / "tally").mkdir(parents=True)
        (data / n / "2026-08" / "tally" / "books.xlsx").write_bytes(b"x")
    return data


def _counts(db: Path, tables) -> dict:
    con = sqlite3.connect(db)
    out = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    con.close()
    return out


def test_migration_moves_names_to_ids_and_preserves_rows(tmp_path):
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    tables = ["end_clients", "runs", "match_results", "review_state", "result_field_overrides", "ingestion_results"]
    before = _counts(db, tables)

    report = migrate.run(db_path=db, data_root=data)
    assert report["mode"] == "apply"
    assert Path(report["backup"]).exists()

    after = _counts(db, tables)
    assert after == before, "row counts must be unchanged"

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # no orphans
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    # runs rebuilt around client_id, name kept as snapshot
    cols = {c[1] for c in con.execute("PRAGMA table_info(runs)")}
    assert "client_id" in cols and "client" not in cols and "client_name_at_run" in cols
    assert con.execute("SELECT COUNT(*) FROM runs WHERE client_id=1").fetchone()[0] == 3
    assert con.execute("SELECT client_name_at_run FROM runs WHERE run_id=1").fetchone()[0] == "Test Client"
    # review state linked by client_id, still one row, still reviewed
    rs = con.execute("SELECT client_id, fingerprint, reviewed FROM review_state").fetchone()
    assert rs["client_id"] == 1 and rs["fingerprint"] == "fp00" and rs["reviewed"] == 1
    # ingestion_results re-keyed by client_id, client_ref gone
    cols = {c[1] for c in con.execute("PRAGMA table_info(ingestion_results)")}
    assert "client_ref" not in cols
    assert con.execute("SELECT upload_key FROM ingestion_results").fetchone()[0].startswith("1/2026-08/")
    con.close()

    # folders moved to data/<id>/ and a manifest written
    assert (data / "1" / "2026-08" / "tally" / "books.xlsx").exists()
    assert not (data / "Test Client").exists()
    assert (data / "_migration_manifest.json").exists()


def test_migration_is_idempotent(tmp_path):
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    migrate.run(db_path=db, data_root=data)
    snap = _counts(db, ["runs", "match_results", "review_state", "ingestion_results"])
    second = migrate.run(db_path=db, data_root=data)
    assert second["mode"] == "no-op"
    assert _counts(db, ["runs", "match_results", "review_state", "ingestion_results"]) == snap


def test_dry_run_writes_nothing(tmp_path):
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    report = migrate.run(db_path=db, data_root=data, dry_run=True)
    assert report["mode"] == "dry-run"
    con = sqlite3.connect(db)
    assert "client" in {c[1] for c in con.execute("PRAGMA table_info(runs)")}
    assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    con.close()
    assert (data / "Test Client").exists()  # not moved


def test_unmatched_name_becomes_migrated_client(tmp_path):
    db = _legacy_db(tmp_path, active_client="Existing Co")
    data = _legacy_data(tmp_path, ["Ghost Client"])
    migrate.run(db_path=db, data_root=data)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT client_id, origin FROM end_clients WHERE legal_name='Ghost Client'").fetchone()
    assert row is not None and row["origin"] == "migrated"
    assert (data / str(row["client_id"])).exists()
    con.close()


def test_rename_legal_name_does_not_break_lookups(tmp_path):
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    migrate.run(db_path=db, data_root=data)
    con = sqlite3.connect(db)
    con.execute("UPDATE end_clients SET legal_name='Test Client Renamed' WHERE client_id=1")
    con.commit()
    # identity is client_id, so the run is still found and the display name is current
    assert con.execute("SELECT COUNT(*) FROM runs WHERE client_id=1").fetchone()[0] == 3
    assert con.execute("SELECT legal_name FROM end_clients WHERE client_id=1").fetchone()[0] == "Test Client Renamed"
    con.close()
    from src.shared import discovery
    opts = discovery.list_clients(include_inactive=True, db_path=db)
    assert any(o["display_name"] == "Test Client Renamed" for o in opts)


def test_two_clients_same_display_name_are_disambiguated(tmp_path):
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO end_clients (legal_name, pan, gstin, is_active, created_at) VALUES (?,?,?,?,?)",
        ("Test Client", "ZZZZZ9999Z", "29ZZZZZ9999Z1Z5", 1, "2026-01-01"),
    )
    con.commit()
    con.close()
    from src.shared import discovery
    opts = [o for o in discovery.list_clients(include_inactive=True, db_path=db) if o["legal_name"] == "Test Client"]
    assert len(opts) == 2
    assert len({o["display_name"] for o in opts}) == 2, "same name must be disambiguated"


def test_soft_delete_then_new_client_gets_new_id(tmp_path):
    db = _legacy_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE end_clients SET is_active=0 WHERE client_id=1")
    new_id = con.execute(
        "INSERT INTO end_clients (legal_name, pan, created_at) VALUES (?,?,?)",
        ("Another Co", "AAAAA1111A", "2026-01-02"),
    ).lastrowid
    con.commit()
    con.close()
    assert new_id != 1  # ids are never reused


def test_source_data_path_rejects_bad_input():
    from src.data_paths import source_data_path
    for bad in [("../etc", "2026-08", "tally"), (1, "../x", "tally"), (1, "2026-13", "tally"),
                (1, "2026-08", "evil"), (0, "2026-08", "tally"), ("abc", "2026-08", "tally")]:
        with pytest.raises(ValueError):
            source_data_path(*bad)


def test_no_data_path_built_outside_data_paths():
    """Fail if ANY module other than src/data_paths.py builds a data/ path.

    The Step-3 refactor removed every one, so the allowlist is now empty: a
    NEW offender anywhere fails immediately."""
    import re
    root = Path(__file__).resolve().parents[1] / "src"
    known_remaining: set[str] = set()
    offenders = []
    for py in root.rglob("*.py"):
        if py.name == "data_paths.py":
            continue
        text = py.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if "DATA_ROOT /" in line or re.search(r'DATA_ROOT\s*/\s*f?["\']', line):
                rel = str(py.relative_to(root))
                if rel not in known_remaining:
                    offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "data paths built outside src/data_paths.py:\n" + "\n".join(offenders)
