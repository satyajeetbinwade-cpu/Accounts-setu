"""Password hashing and token generation.

Passwords are stored as **bcrypt** hashes (per-hash random salt, cost 12) —
a deliberately slow KDF, so a stolen ``db/poc.db`` cannot be turned back into
passwords at speed.

Legacy rows hashed with the original PBKDF2-HMAC-SHA256 scheme still verify
(``verify_password`` understands both) and ``needs_rehash`` tells the login
path to upgrade them on the next successful sign-in, so the scheme moves
forward without locking anyone out or ever storing a plaintext password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

import bcrypt

# --- bcrypt (current scheme) ------------------------------------------------
BCRYPT_ROUNDS = 12
# Written to users.password_salt for a bcrypt row: the salt itself lives inside
# the bcrypt hash, so the column carries the scheme marker instead. That keeps
# the upgrade free of any schema change.
BCRYPT_MARKER = "bcrypt"

# --- PBKDF2 (legacy scheme, verify-only) ------------------------------------
_ITERATIONS = 200_000
_ALGO = "sha256"

MAX_PASSWORD_BYTES = 72  # bcrypt ignores everything past this
MIN_PASSWORD_LENGTH = 8


def hash_password(plain_password: str, *, salt: str | None = None) -> tuple[str, str]:
    """Return ``(scheme_marker, hash)`` for the given plaintext password.

    ``salt`` is accepted for determinism in tests only; production callers let
    bcrypt generate its own per-hash random salt.
    """
    if salt is not None:
        # Legacy/deterministic path, retained so a test can pin a hash.
        derived = hashlib.pbkdf2_hmac(
            _ALGO, plain_password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
        )
        return salt, derived.hex()

    digest = bcrypt.hashpw(_bcrypt_bytes(plain_password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS))
    return BCRYPT_MARKER, digest.decode("utf-8")


def verify_password(plain_password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    """Constant-time check that plain_password matches the stored hash.

    Understands both current bcrypt rows and legacy PBKDF2 rows, so an
    upgraded app keeps working against an un-migrated database.
    """
    if not expected_hash_hex:
        return False

    if is_bcrypt_hash(salt_hex, expected_hash_hex):
        try:
            return bcrypt.checkpw(
                _bcrypt_bytes(plain_password), expected_hash_hex.encode("utf-8")
            )
        except (ValueError, TypeError):
            return False

    try:
        _, candidate_hash = hash_password(plain_password, salt=salt_hex)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate_hash, expected_hash_hex)


def is_bcrypt_hash(salt_hex: str, stored_hash: str) -> bool:
    """True when the row was hashed with the current scheme."""
    return (salt_hex or "").strip() == BCRYPT_MARKER or stored_hash.startswith(
        ("$2a$", "$2b$", "$2y$")
    )


def needs_rehash(salt_hex: str, stored_hash: str) -> bool:
    """True when this row should be re-hashed on the next successful sign-in:
    a legacy scheme, or a bcrypt cost below the current setting."""
    if not is_bcrypt_hash(salt_hex, stored_hash):
        return True
    return _cost_of(stored_hash) < BCRYPT_ROUNDS


def _cost_of(stored_hash: str) -> int:
    """The cost factor recorded in a ``$2b$NN$...`` hash (0 if unparseable)."""
    parts = (stored_hash or "").split("$")
    try:
        return int(parts[2])
    except (IndexError, ValueError):
        return 0


def _bcrypt_bytes(plain_password: str) -> bytes:
    """bcrypt input, capped at its 72-byte limit.

    ``validate_password_strength`` refuses anything longer, so this only
    guards the verify path against a hash written by another tool.
    """
    return (plain_password or "").encode("utf-8")[:MAX_PASSWORD_BYTES]


def new_session_token() -> str:
    """A high-entropy, URL-safe session token."""
    return secrets.token_urlsafe(32)


def generate_password() -> str:
    """A random, policy-compliant password.

    Used only where an operator explicitly needs a generated credential — the
    first-run admin seed. It is never a shared default and never hardcoded.
    """
    return secrets.token_urlsafe(18)


def validate_password_strength(plain_password: str) -> str | None:
    """Return an error message if the password is unacceptable, else None.

    Length is the only rule (still a PoC), but the upper bound is real:
    bcrypt ignores everything past 72 bytes, so accepting a longer password
    would silently make two different passwords interchangeable.
    """
    if len(plain_password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len((plain_password or "").encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"Password must be at most {MAX_PASSWORD_BYTES} characters."
    return None
