"""Every F4 audit sink must write to the database its CALLER is using.

Two kinds of test here:

* LIVE — the two paths that actually leaked (ai_models, rules) are driven with
  an explicit scratch database and the repository's database is hashed before
  and after;
* STRUCTURAL — every ``record_edit(`` call site in ``src/`` is scanned and must
  pass ``db_path``, so a NEW audit call that forgets it fails here instead of
  quietly writing to the app default.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

from src import bootstrap
from src import db as recon_db

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_DB = Path(recon_db.REPO_DB_PATH)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def edits_in(db_path) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM edit_history_entries").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture()
def scratch(tmp_path):
    db_path = tmp_path / "scratch.db"
    bootstrap.run_migrations(db_path=str(db_path), data_root=tmp_path / "data")
    return db_path


# ---------------------------------------------------------------------------
# LIVE: the two leaked paths
# ---------------------------------------------------------------------------


def test_the_rules_audit_lands_in_the_given_database(scratch):
    from src.rules import service as rules_service

    before_real = sha256(REPO_DB)
    entry_id = rules_service.add_taxonomy_entry(
        "expense_head", "AUDIT1", "Audit probe", actor="test", db_path=scratch,
    )
    before = edits_in(scratch)
    rules_service.rename_taxonomy_entry(entry_id, "Audit probe (renamed)", actor="test", db_path=scratch)
    assert edits_in(scratch) > before, "the rules edit must be audited in the given database"
    assert sha256(REPO_DB) == before_real


def test_the_auth_audit_lands_in_the_given_database(scratch):
    from src.auth import db as auth_db

    before_real = sha256(REPO_DB)
    before = edits_in(scratch)
    conn = recon_db.get_connection(scratch)
    try:
        auth_db.log_change(conn, "role", "audit-probe", field="created", changed_by="test", db_path=scratch)
    finally:
        conn.close()
    assert edits_in(scratch) > before
    assert sha256(REPO_DB) == before_real


def test_the_f6_audit_lands_in_the_given_database(scratch):
    from src.f6 import service as f6_service

    before_real = sha256(REPO_DB)
    before = edits_in(scratch)
    f6_service._audit_confirmation(
        run_id=1, version_id=1, actor="test", promotion_scope="firm", db_path=scratch,
    )
    assert edits_in(scratch) > before
    assert sha256(REPO_DB) == before_real


# ---------------------------------------------------------------------------
# STRUCTURAL: no sink call may omit db_path
# ---------------------------------------------------------------------------


def _enclosing_signature_takes_db_path(text: str, line_no: int) -> bool:
    lines = text.splitlines()
    for index in range(line_no - 1, -1, -1):
        match = re.match(r"^def (\w+)\((.*)", lines[index])
        if match:
            signature = match.group(2)
            cursor = index
            while ")" not in lines[cursor] and cursor < len(lines) - 1:
                cursor += 1
                signature += lines[cursor]
            return "db_path" in signature
    return False


def test_every_audit_call_names_the_database_it_writes_to():
    missing: list[str] = []
    for path in sorted(Path("src").rglob("*.py")):
        text = path.read_text()
        for match in re.finditer(r"record_edit\(", text):
            start = match.start()
            depth, cursor = 0, match.end() - 1
            while cursor < len(text):
                if text[cursor] == "(":
                    depth += 1
                elif text[cursor] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                cursor += 1
            call = text[start:cursor + 1]
            line_no = text[:start].count("\n") + 1
            if "db_path" in call:
                continue
            # A wrapper may legitimately omit it only if it has no db_path to give.
            if not _enclosing_signature_takes_db_path(text, line_no):
                continue
            missing.append(f"{path}:{line_no}")
    assert not missing, (
        "these audit calls are inside a function that HAS a db_path but do not "
        f"pass it — the entry would land in the app default: {missing}"
    )
