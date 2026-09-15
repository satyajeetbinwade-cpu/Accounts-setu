"""App-level key management and real encryption-at-rest for C4.

Uses ``cryptography``'s Fernet (AES-128-CBC + HMAC-SHA256, authenticated
encryption) — a real cipher, not a UI-level concealment layer. The key
itself is never stored in the database (db/poc.db) — it lives in a
separate key file outside the DB, or an environment variable, so that a
copy of the database file alone never contains enough to decrypt secrets.

Key resolution order (first match wins):
1. ``SETU_VAULT_KEY`` environment variable (base64 Fernet key) — the
   production path once real hosting/secrets-management exists.
2. ``db/vault.key`` — auto-generated on first run for this PoC, written
   with owner-only permissions (0600). This is the PoC's stand-in for a
   real KMS; the build prompt's "Backup/DR ... pending hosting decision"
   placeholder covers the fact that key custody itself is not yet a
   hosted, production-grade solution.

Nothing in this module ever logs or returns a plaintext secret except
via ``decrypt()``'s direct return value, which callers must not persist
or print — see src/vault/service.py's `get_credential_secret()` for the
single sanctioned point-of-use decrypt path.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_KEY_ENV_VAR = "SETU_VAULT_KEY"
_KEY_FILE = Path(__file__).resolve().parent.parent.parent / "db" / "vault.key"

__all__ = ["encrypt", "decrypt", "mask_secret", "InvalidToken"]


def _load_or_create_key() -> bytes:
    env_key = os.environ.get(_KEY_ENV_VAR)
    if env_key:
        return env_key.encode("utf-8")

    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()

    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        os.chmod(_KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
    except OSError:
        pass  # best-effort on platforms that don't support chmod semantics
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` and return a base64 ciphertext string safe to
    store in a TEXT column. Never returns or logs the plaintext."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt ``ciphertext`` back to the plaintext secret.

    This is the ONLY function in the vault that reconstitutes a plaintext
    secret. Callers (src/vault/service.py) must decrypt only at the exact
    moment of use, log a SecurityEvent for the retrieval, and never store
    or print the result.
    """
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def mask_secret(secret: str) -> str:
    """Mask a secret to a short, non-reversible display reference — the
    only representation of a credential ever shown in the UI."""
    s = secret.strip()
    if len(s) <= 4:
        return "\u2022\u2022\u2022\u2022"
    return f"{s[:3]}\u2022\u2022\u2022\u2022{s[-4:]}"
