"""Password hashing and token generation — stdlib only, no extra deps.

PBKDF2-HMAC-SHA256 with a per-user random salt. Good enough for a PoC;
callers never see or store plaintext passwords once hash_password() runs.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ITERATIONS = 200_000
_ALGO = "sha256"


def hash_password(plain_password: str, *, salt: str | None = None) -> tuple[str, str]:
    """Return (salt_hex, hash_hex) for the given plaintext password.

    Generates a new random salt unless one is supplied (only useful for
    tests that need determinism).
    """
    salt_hex = salt if salt is not None else secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        _ALGO, plain_password.encode("utf-8"), bytes.fromhex(salt_hex), _ITERATIONS
    )
    return salt_hex, derived.hex()


def verify_password(plain_password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    """Constant-time check that plain_password matches the stored hash."""
    _, candidate_hash = hash_password(plain_password, salt=salt_hex)
    return hmac.compare_digest(candidate_hash, expected_hash_hex)


def new_session_token() -> str:
    """A high-entropy, URL-safe session token."""
    return secrets.token_urlsafe(32)


def validate_password_strength(plain_password: str) -> str | None:
    """Return an error message if the password is too weak, else None.

    Deliberately simple for a PoC: minimum length only.
    """
    if len(plain_password) < 8:
        return "Password must be at least 8 characters."
    return None
