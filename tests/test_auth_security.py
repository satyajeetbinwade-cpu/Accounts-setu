"""Task 1 — the sign-in path's security properties.

Pins the things that must stay true for credentials:

* the sign-in screen renders NO credential material, in the markup or in the
  page source;
* no credential is hardcoded anywhere in the repository;
* passwords are stored as bcrypt hashes (and legacy PBKDF2 rows still verify,
  then upgrade on the next successful sign-in);
* failures are answered with ONE generic message, and the account locks after
  five consecutive failures;
* a password somebody else issued cannot be used to enter the app;
* sessions expire on inactivity, server-side.

Run:

    venv/bin/python -m pytest tests/test_auth_security.py -v
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import bootstrap  # noqa: E402
from src.auth import security  # noqa: E402
from src.auth import service as auth  # noqa: E402


@pytest.fixture(scope="session")
def _seeded_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("auth_seed") / "poc.db"
    bootstrap.run_migrations(path)
    return path


@pytest.fixture()
def db_path(_seeded_db, tmp_path):
    dest = tmp_path / "poc.db"
    shutil.copy(_seeded_db, dest)
    return str(dest)


def _role_id(name: str, db_path) -> int:
    return int(next(r for r in auth.list_roles(db_path=db_path) if r["name"] == name)["role_id"])


def _make_user(username: str, password: str, *, role: str = "Senior Accountant", db_path) -> int:
    return auth.create_user(
        username=username, display_name=username, email=None, password=password,
        role_id=_role_id(role, db_path), actor="test", db_path=db_path,
    )


# ---------------------------------------------------------------------------
# (a) No credentials on the login screen, or anywhere in the repo
# ---------------------------------------------------------------------------

# Assembled at runtime so this file does not itself contain the literal it
# searches for (which would make the repository scan match its own source).
_LEAKED_BOOTSTRAP_PASSWORD = "ChangeMe" + "123"


def test_login_page_shows_no_credentials():
    from setu.views.login import login_page

    source = json.dumps(login_page().render(), default=str)

    assert _LEAKED_BOOTSTRAP_PASSWORD not in source
    assert "First run" not in source  # the old hint box is gone
    assert "your.username" not in source  # no example ID in a placeholder
    # The only occurrence of "password" must be the field's own wiring.
    assert source.count("password") <= 3, source


def test_login_password_field_is_masked_and_not_autocompleted():
    from setu.views.login import login_page

    source = json.dumps(login_page().render(), default=str)

    assert '"type:\\"password\\""' in source
    assert "autoComplete" in source and "off" in source


def test_no_credential_is_hardcoded_in_the_repository():
    """The bootstrap password that used to be printed on the sign-in screen —
    and hardcoded in the seed — must not reappear anywhere."""
    skip_dirs = {".git", "venv", ".web", "__pycache__", "node_modules", ".pytest_cache"}
    offenders: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix not in {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".html", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _LEAKED_BOOTSTRAP_PASSWORD in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == [], f"hardcoded bootstrap credential found in: {offenders}"


def test_seed_has_no_default_password_constant():
    from src.auth import seed

    assert not hasattr(seed, "DEFAULT_ADMIN_PASSWORD")
    # A generated fallback exists, and it is random per call.
    assert security.generate_password() != security.generate_password()


# ---------------------------------------------------------------------------
# Password storage
# ---------------------------------------------------------------------------


def test_passwords_are_stored_as_bcrypt_hashes(db_path):
    user_id = _make_user("Hashed", "correct-horse-battery", db_path=db_path)
    row = sqlite3.connect(db_path).execute(
        "SELECT password_salt, password_hash FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()

    assert row[0] == security.BCRYPT_MARKER
    assert row[1].startswith("$2b$")
    assert "correct-horse-battery" not in row[1]  # never the plaintext


def test_legacy_pbkdf2_hashes_still_verify_and_upgrade_on_login(db_path):
    """An un-migrated database keeps working, and the sign-in that proves the
    password is also the moment the hash is upgraded."""
    user_id = _make_user("LegacyUser", "temporary-pass-1", db_path=db_path)
    salt, legacy_hash = security.hash_password("temporary-pass-1", salt="ab" * 16)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE users SET password_salt = ?, password_hash = ? WHERE user_id = ?",
        (salt, legacy_hash, user_id),
    )
    conn.commit()

    assert security.verify_password("temporary-pass-1", salt, legacy_hash)
    assert security.needs_rehash(salt, legacy_hash)

    auth.login("LegacyUser", "temporary-pass-1", db_path=db_path)

    new_salt, new_hash = sqlite3.connect(db_path).execute(
        "SELECT password_salt, password_hash FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    assert new_salt == security.BCRYPT_MARKER
    assert new_hash.startswith("$2b$")


# ---------------------------------------------------------------------------
# Login failures: one generic message, then a lockout
# ---------------------------------------------------------------------------


def test_failure_message_is_generic_and_identical_for_unknown_user(db_path):
    _make_user("Known", "right-password-1", db_path=db_path)

    with pytest.raises(auth.AuthError) as wrong_password:
        auth.login("Known", "wrong-password", db_path=db_path)
    with pytest.raises(auth.AuthError) as unknown_user:
        auth.login("NoSuchPerson", "whatever", db_path=db_path)

    assert str(wrong_password.value) == auth.GENERIC_LOGIN_FAILURE
    assert str(unknown_user.value) == auth.GENERIC_LOGIN_FAILURE
    assert "password" not in str(wrong_password.value).lower() or "id" in str(
        wrong_password.value
    ).lower()
    # It must not reveal WHICH part was wrong, nor that the account exists.
    assert "unknown" not in str(unknown_user.value).lower()
    assert "user" not in str(unknown_user.value).lower()


def test_account_locks_after_five_failed_attempts(db_path):
    _make_user("LockTarget", "right-password-1", db_path=db_path)

    for attempt in range(auth.MAX_FAILED_LOGIN_ATTEMPTS - 1):
        with pytest.raises(auth.AuthError) as exc:
            auth.login("LockTarget", "nope", db_path=db_path)
        assert str(exc.value) == auth.GENERIC_LOGIN_FAILURE, f"attempt {attempt + 1}"

    # The fifth failure locks it...
    with pytest.raises(auth.AuthError):
        auth.login("LockTarget", "nope", db_path=db_path)

    # ...and from then on even the CORRECT password is refused.
    with pytest.raises(auth.AuthError) as locked:
        auth.login("LockTarget", "right-password-1", db_path=db_path)
    assert "Too many failed attempts" in str(locked.value)

    events = [
        e["event_type"] for e in auth.list_security_events(limit=50, db_path=db_path)
    ]
    assert "account_locked" in events
    assert "login_locked" in events


def test_successful_login_clears_the_failure_counter(db_path):
    _make_user("Recovers", "right-password-1", db_path=db_path)
    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.login("Recovers", "wrong", db_path=db_path)

    auth.login("Recovers", "right-password-1", db_path=db_path)

    count = sqlite3.connect(db_path).execute(
        "SELECT failed_login_count FROM users WHERE username = 'Recovers'"
    ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Issued credentials must be replaced before the app opens
# ---------------------------------------------------------------------------


def test_created_account_must_change_password_and_can_do_so(db_path):
    _make_user("Issued", "issued-password-1", db_path=db_path)

    token = auth.login("Issued", "issued-password-1", db_path=db_path)
    user = auth.current_user(token, db_path=db_path)
    assert user is not None
    assert auth.must_change_password(user) is True

    auth.change_own_password(user, "issued-password-1", "chosen-by-me-9", db_path=db_path)

    user = auth.current_user(token, db_path=db_path)
    assert auth.must_change_password(user) is False
    assert auth.login("Issued", "chosen-by-me-9", db_path=db_path)


def test_change_own_password_requires_the_current_one(db_path):
    user_id = _make_user("Careful", "right-password-1", db_path=db_path)
    auth.set_must_change_password(user_id, False, db_path=db_path)
    token = auth.login("Careful", "right-password-1", db_path=db_path)
    user = auth.current_user(token, db_path=db_path)

    with pytest.raises(auth.AuthError, match="current password"):
        auth.change_own_password(user, "not-my-password", "brand-new-password-7", db_path=db_path)


def test_admin_reset_revokes_sessions_and_forces_a_change(db_path):
    user_id = _make_user("ResetMe", "original-password-1", db_path=db_path)
    auth.set_must_change_password(user_id, False, db_path=db_path)
    victim_token = auth.login("ResetMe", "original-password-1", db_path=db_path)
    assert auth.current_user(victim_token, db_path=db_path) is not None

    auth.reset_user_password(user_id, "new-password-2", actor="admin", db_path=db_path)

    # The old session is dead, the old password is dead, and a change is forced.
    assert auth.current_user(victim_token, db_path=db_path) is None
    with pytest.raises(auth.AuthError):
        auth.login("ResetMe", "original-password-1", db_path=db_path)
    user = auth.current_user(
        auth.login("ResetMe", "new-password-2", db_path=db_path), db_path=db_path
    )
    assert auth.must_change_password(user) is True


def test_deactivated_user_loses_access_and_sessions(db_path):
    user_id = _make_user("Gone", "some-password-1", db_path=db_path)
    token = auth.login("Gone", "some-password-1", db_path=db_path)

    auth.set_user_active(user_id, False, actor="admin", db_path=db_path)

    assert auth.current_user(token, db_path=db_path) is None
    with pytest.raises(auth.AuthError):
        auth.login("Gone", "some-password-1", db_path=db_path)


# ---------------------------------------------------------------------------
# Session lifetime
# ---------------------------------------------------------------------------


def test_session_dies_after_inactivity(monkeypatch, db_path):
    """Idle expiry is enforced by the SERVER on every request, independent of
    whatever max-age the browser was handed."""
    _make_user("Idler", "some-password-1", db_path=db_path)
    token = auth.login("Idler", "some-password-1", db_path=db_path)
    assert auth.current_user(token, db_path=db_path) is not None

    # Simulate two hours of inactivity (backdating is more faithful than
    # shrinking the window to microseconds, which a same-instant call clears).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE session_token = ?",
        ("2020-01-01T00:00:00+00:00", token),
    )
    conn.commit()

    monkeypatch.setenv("SETU_SESSION_IDLE_MINUTES", "60")
    assert auth.current_user(token, db_path=db_path) is None

    # A window wide enough to cover the gap keeps it valid — proving the
    # refusal above came from the idle rule, not from the token being wrong.
    token2 = auth.login("Idler", "some-password-1", db_path=db_path)
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE session_token = ?",
        ("2020-01-01T00:00:00+00:00", token2),
    )
    conn.commit()
    monkeypatch.setenv("SETU_SESSION_IDLE_MINUTES", "999999999")
    assert auth.current_user(token2, db_path=db_path) is not None

    monkeypatch.delenv("SETU_SESSION_IDLE_MINUTES", raising=False)


def test_logout_revokes_the_session(db_path):
    _make_user("Leaver", "some-password-1", db_path=db_path)
    token = auth.login("Leaver", "some-password-1", db_path=db_path)

    auth.logout(token, db_path=db_path)

    assert auth.current_user(token, db_path=db_path) is None


# ---------------------------------------------------------------------------
# Access role derivation
# ---------------------------------------------------------------------------


def test_access_role_is_derived_from_the_f1_role(db_path):
    admin_id = _make_user("Boss", "some-password-1", role="Admin", db_path=db_path)
    staff_id = _make_user("Staff", "some-password-1", role="Senior Accountant", db_path=db_path)

    assert auth.get_user(admin_id, db_path=db_path)["role"] == "admin"
    assert auth.get_user(staff_id, db_path=db_path)["role"] == "user"


def test_demotion_changes_the_access_role_immediately(db_path):
    user_id = _make_user("Promoted", "some-password-1", db_path=db_path)
    assert auth.get_user(user_id, db_path=db_path)["role"] == "user"

    auth.update_user_role(user_id, _role_id("Admin", db_path), actor="admin", db_path=db_path)
    assert auth.get_user(user_id, db_path=db_path)["role"] == "admin"

    auth.update_user_role(
        user_id, _role_id("Senior Accountant", db_path), actor="admin", db_path=db_path
    )
    assert auth.get_user(user_id, db_path=db_path)["role"] == "user"
