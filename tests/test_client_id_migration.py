"""Tests for the client-id identity migration (Steps 1-2).

Builds a synthetic LEGACY-shaped DB (client names, ``data/<name>/`` folders),
runs the migration, and asserts the guarantees the task calls out:
row counts unchanged, no orphans, review state still linked, a run found by
client_id, idempotency, path safety, and that nothing outside
``src/data_paths.py`` builds a data path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
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
        "INSERT INTO review_state (client, period, recon_type, fingerprint, reviewed, reviewed_in_run_id, reviewed_classification) "
        "VALUES (?,?,?,?,1,(SELECT MIN(run_id) FROM runs),?)",
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


# ---------------------------------------------------------------------------
# Step-4 scenario coverage
# ---------------------------------------------------------------------------


def test_run_match_results_counts_and_review_link_after_migration(tmp_path):
    """Every run keeps its OWN match_results, and review state stays linked."""
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    before = _counts(db, ["match_results"])
    migrate.run(db_path=db, data_root=data)
    assert _counts(db, ["match_results"]) == before
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # each of the 3 runs still owns its 2 results
    rows = con.execute("SELECT run_id, COUNT(*) c FROM match_results GROUP BY run_id").fetchall()
    assert sorted(r["c"] for r in rows) == [2, 2, 2]
    # the review row still points at a run that exists
    rv = con.execute(
        "SELECT client_id, reviewed_in_run_id FROM review_state"
    ).fetchone()
    assert rv["client_id"] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM runs WHERE run_id = ?", (rv["reviewed_in_run_id"],)
    ).fetchone()[0] == 1
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_duplicate_active_gstin_is_rejected(tmp_path):
    from src import db as recon_db
    from src.clients import service as clients

    db = tmp_path / "clients.db"
    recon_db.init_db(db)
    clients.init_clients(db)
    clients.create_client(
        legal_name="A Co", pan="AAAPL1234C", gstin="07AAACP9472A1ZJ", actor="t", db_path=db,
    )
    with pytest.raises(clients.ClientError) as exc:
        clients.create_client(
            legal_name="B Co", pan="AABCM1234K", gstin="07AAACP9472A1ZJ", actor="t", db_path=db,
        )
    assert "GSTIN 07AAACP9472A1ZJ" in str(exc.value)


def test_inactive_clients_excluded_from_picker_and_labelled(tmp_path):
    """Context pickers show active clients only; history screens keep inactive
    ones and label them "(inactive)"."""
    import src.data_paths as dp
    from src import db as recon_db
    from src.clients import service as clients
    from src.shared import discovery

    db = tmp_path / "clients.db"
    recon_db.init_db(db)
    clients.init_clients(db)
    clients.create_client(legal_name="Active Co", pan="AAAPL1234C", actor="t", db_path=db)
    gone = clients.create_client(legal_name="Gone Co", pan="AABCM1234K", actor="t", db_path=db)
    clients.set_client_active(gone, False, actor="t", db_path=db)

    active = discovery.list_clients(db_path=db)
    assert [o["legal_name"] for o in active] == ["Active Co"], "inactive must be hidden by default"

    all_rows = discovery.list_clients(include_inactive=True, db_path=db)
    labels = {o["legal_name"]: o["display_name"] for o in all_rows}
    assert labels["Active Co"] == "Active Co"
    assert labels["Gone Co"] == "Gone Co (inactive)"


def test_migration_failure_midway_restores_db_and_folders(tmp_path, monkeypatch):
    """A failure during the folder move must leave the DB un-migrated AND the
    folders back in their original names — not a half-applied state."""
    import os

    from src.migrations import folders as fmod

    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client", "Meridian Fabrics"])

    def boom(entries, root, **kwargs):
        moved = []
        for e in entries:
            if e["status"] != "planned" or not e["new_path"]:
                continue
            old, new = Path(e["old_path"]), Path(e["new_path"])
            if new.exists():
                continue
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(old), str(new))
            moved.append({**e, "status": "moved"})
            break  # move ONE, then fail
        fmod.manifest_path(root).write_text(json.dumps({"moves": moved}))
        raise RuntimeError("simulated mid-way failure")

    monkeypatch.setattr(fmod, "apply_folder_moves", boom)

    with pytest.raises(RuntimeError):
        migrate.run(db_path=db, data_root=data)

    # DB rolled back: runs still name-keyed, migration not recorded
    con = sqlite3.connect(db)
    assert "client" in {c[1] for c in con.execute("PRAGMA table_info(runs)")}
    assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    con.close()

    # Folders restored to their original names, nothing left under data/<id>/
    assert (data / "Test Client").is_dir()
    assert (data / "Meridian Fabrics").is_dir()
    for numeric in (data / "1", data / "2"):
        assert not numeric.exists() or not any(numeric.iterdir())


def test_bootstrap_run_migrations_never_touches_the_global_data_root(tmp_path, monkeypatch):
    """`bootstrap.run_migrations(explicit_db)` must scope its data root to that
    DB, never the process-wide DATA_ROOT — the guard against the accident where
    a temp-DB boot moved the repo's real client folders."""
    sentinel = tmp_path / "global_data"
    legacy = sentinel / "Legacy Client" / "2026-08" / "tally"
    legacy.mkdir(parents=True)
    (legacy / "x.xlsx").write_bytes(b"x")
    monkeypatch.setattr("src.data_paths.DATA_ROOT", sentinel)
    monkeypatch.setattr("src.shared.discovery.DATA_ROOT", sentinel, raising=False)

    from src import bootstrap

    bootstrap.run_migrations(tmp_path / "app.db")

    assert (sentinel / "Legacy Client").is_dir(), "the global data root was moved!"
    assert not (sentinel / "Legacy Client").is_symlink()


# ---------------------------------------------------------------------------
# The UI import path is READ-ONLY
#
# These run in a SUBPROCESS because module import is cached per interpreter:
# the only way to observe "what does `import setu.setu` do" is to start a fresh
# Python that has not imported it yet. Each child redirects db.DB_PATH and both
# DATA_ROOT globals to throwaway paths BEFORE importing, so it can never touch
# the real db/ and data/ even if the guarantee regressed.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CHILD_PREAMBLE = """
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
import src.db as db, src.data_paths as dp, src.shared.discovery as disc
db.DB_PATH = Path({db!r})
dp.DATA_ROOT = Path({data!r})
disc.DATA_ROOT = dp.DATA_ROOT
"""

_IMPORT_APP_CHILD = _CHILD_PREAMBLE + """
import setu.setu
print("APP_IMPORT_OK")
"""

_RUN_MAIN_CHILD = _CHILD_PREAMBLE + """
import runpy
runpy.run_path({main!r}, run_name="__main__")
"""

# Build EVERY page component. `reflex run` does exactly this before it serves
# anything, and a page that cannot build is a page that cannot render — but
# pytest never compiles a page, so this is the only place the failure shows up.
# NOTE: no braces in this snippet — it goes through `str.format()`.
_BUILD_PAGES_CHILD = _CHILD_PREAMBLE + """
from setu.views import pages as p
from setu.views.login import login_page

builders = [("login", login_page), ("dashboard", p.dashboard_page)] + list(p.all_routes())
failed = []
for name, builder in builders:
    try:
        builder()
    except Exception as exc:
        failed.append(name + ": " + type(exc).__name__ + ": " + str(exc))
if failed:
    raise SystemExit("PAGE COMPONENT BUILD FAILED\\n  - " + "\\n  - ".join(failed))
print("BUILT " + str(len(builders)) + " PAGE COMPONENTS")
"""


def _run_child(body: str, tmp_path, db: Path, data: Path) -> "subprocess.CompletedProcess":
    src = body.format(repo=str(_REPO_ROOT), db=str(db), data=str(data),
                      main=str(_REPO_ROOT / "main.py"))
    return subprocess.run(
        [sys.executable, "-c", src],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _fingerprint(root: Path) -> dict:
    """Content hash of every file under ``root`` (path → sha256, or 'dir')."""
    import hashlib

    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_importing_the_app_before_migration_raises_and_changes_nothing(tmp_path):
    """`import setu.setu` against a pre-migration DB must fail with the hint to
    run main.py — and leave the database file and every folder byte-identical.

    This is the regression guard for the accident that motivated the split: a
    bare import used to migrate db/poc.db and move data/<name>/ → data/<id>/.
    """
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client", "Meridian Fabrics"])
    db_before, data_before = _file_hash(db), _fingerprint(data)

    proc = _run_child(_IMPORT_APP_CHILD, tmp_path, db, data)

    assert proc.returncode != 0, "importing the app against a stale DB must fail"
    assert "SchemaMigrationPending" in proc.stderr
    assert "Run python main.py (or python -m src.migrations --apply) first" in proc.stderr
    assert "APP_IMPORT_OK" not in proc.stdout

    assert _file_hash(db) == db_before, "the import altered the database file"
    assert _fingerprint(data) == data_before, "the import moved files under data/"
    # And specifically: no data/<client_id>/ folder appeared.
    assert not (data / "1").exists() and not (data / "2").exists()


def test_main_migrates_then_importing_the_app_works(tmp_path):
    """main.py migrates; the same import that failed before now succeeds."""
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client", "Meridian Fabrics"])

    assert _run_child(_IMPORT_APP_CHILD, tmp_path, db, data).returncode != 0

    boot = _run_child(_RUN_MAIN_CHILD, tmp_path, db, data)
    assert boot.returncode == 0, boot.stderr
    assert "Database initialized at:" in boot.stdout

    # main.py migrated the DB and moved the folders.
    con = sqlite3.connect(db)
    assert "client_id" in {c[1] for c in con.execute("PRAGMA table_info(runs)")}
    con.close()
    assert (data / "1" / "2026-08" / "tally" / "books.xlsx").exists()
    assert not (data / "Test Client").exists()

    after = _run_child(_IMPORT_APP_CHILD, tmp_path, db, data)
    assert after.returncode == 0, after.stderr
    assert "APP_IMPORT_OK" in after.stdout


def test_ensure_schema_current_is_read_only(tmp_path):
    """The gate itself never writes: same DB bytes, no data/<id>/ folders, and
    a fresh empty DB is refused rather than created."""
    from src import bootstrap

    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    db_before, data_before = _file_hash(db), _fingerprint(data)

    with pytest.raises(bootstrap.SchemaMigrationPending) as exc:
        bootstrap.ensure_schema_current(db)
    assert str(exc.value) == "Run python main.py (or python -m src.migrations --apply) first"
    assert exc.value.detail, "the diagnostic detail should be attached as a note"

    assert _file_hash(db) == db_before
    assert _fingerprint(data) == data_before
    assert not (data / "1").exists()

    # A brand-new empty DB is "behind" too — and stays non-existent.
    fresh = tmp_path / "fresh" / "poc.db"
    fresh.parent.mkdir()
    with pytest.raises(bootstrap.SchemaMigrationPending):
        bootstrap.ensure_schema_current(fresh)
    assert not fresh.exists(), "the gate must not create the database"

    # No import-time alias can bypass the split.
    assert not hasattr(bootstrap, "init_all")


def test_sandboxed_data_root_seeds_sources_and_isolates_exports(tmp_path, monkeypatch):
    """A verification harness can read the real uploads but its exports land in
    a throwaway root — so a run can never add files to the real data/ tree.

    ``monkeypatch.setattr`` on the current values is what restores the real
    data root for the other tests in this process.
    """
    from src import data_paths
    from src.shared import discovery

    monkeypatch.setattr(data_paths, "DATA_ROOT", data_paths.DATA_ROOT)
    monkeypatch.setattr(discovery, "DATA_ROOT", discovery.DATA_ROOT)

    real = tmp_path / "real"
    src_slot = real / "1" / "2026-08" / "tally"
    src_slot.mkdir(parents=True)
    (src_slot / "books.xlsx").write_bytes(b"source bytes")
    old_export = real / "1" / "2026-08" / "exports"
    old_export.mkdir(parents=True)
    (old_export / "old.html").write_text("stale")
    before = _fingerprint(real)

    sandbox = data_paths.use_sandboxed_data_root(real)

    assert sandbox != real
    # Sources are readable through the sandboxed root...
    assert (sandbox / "1" / "2026-08" / "tally" / "books.xlsx").read_bytes() == b"source bytes"
    # ...but stale exports are not carried over, they are OUTPUT.
    assert not (sandbox / "1" / "2026-08" / "exports").exists()

    # A new export goes to the sandbox only.
    written = data_paths.exports_dir(1, "2026-08") / "new.html"
    written.write_text("fresh")
    assert written.is_file()
    assert sandbox in written.parents

    assert _fingerprint(real) == before, "the real data root was modified"


def test_duplicate_gstin_in_the_roster_blocks_the_migration_with_a_named_error(tmp_path):
    """A legacy roster holding one GSTIN twice cannot satisfy
    "one GST registration = one client". The migration must stop, and the error
    must NAME the offending rows — a raw
    `IntegrityError: UNIQUE constraint failed: end_clients.gstin` tells the
    operator nothing about what to fix.

    This mirrors the live production roster, where "KMAP Capital Limited" exists
    twice with the same PAN and GSTIN.
    """
    from src import bootstrap
    from src.clients import schema as clients_schema

    db = _legacy_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE end_clients SET legal_name=?, pan=?, gstin=? WHERE client_id=1",
        ("KMAP Capital Limited", "AAECK9054D", "07AAECK9054D1ZK"),
    )
    con.execute(
        "INSERT INTO end_clients (legal_name, pan, gstin, is_active, created_at) "
        "VALUES (?,?,?,1,?)",
        ("KMAP Capital Limited", "AAECK9054D", "07AAECK9054D1ZK", "2026-01-01"),
    )
    con.commit()
    con.row_factory = sqlite3.Row
    dupes = clients_schema.duplicate_gstins(con)
    con.close()
    assert len(dupes) == 1
    assert dupes[0][0] == "07AAECK9054D1ZK"
    assert sorted(dupes[0][1]) == [1, 2]

    with pytest.raises(RuntimeError) as exc:
        bootstrap.run_migrations(db, data_root=tmp_path / "data")

    message = str(exc.value)
    assert "the same GST registration more than once" in message
    assert "07AAECK9054D1ZK" in message
    assert "client_id=1" in message and "client_id=2" in message
    assert "UNIQUE constraint failed" not in message


def test_every_page_component_builds(tmp_path):
    """Every route's component tree must construct.

    `reflex run` builds them before serving, so a failure here is a page that
    cannot render at all — and it is invisible to pytest otherwise. The
    client-id refactor broke this three ways, all found only by running the app:
      * `client_picker(value=<int var>)` — `rx.select.root.value` is
        string-typed, so every call site needs `.to_string()`;
      * `on_change=<handler annotated client_id: int>` — the picker hands the id
        back as a STRING, so Reflex rejects the handler signature;
      * a picker called without `value=` at all.
    """
    db = _legacy_db(tmp_path)
    data = _legacy_data(tmp_path, ["Test Client"])
    migrate.run(db_path=db, data_root=data)

    proc = _run_child(_BUILD_PAGES_CHILD, tmp_path, db, data)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BUILT " in proc.stdout and "PAGE COMPONENTS" in proc.stdout

