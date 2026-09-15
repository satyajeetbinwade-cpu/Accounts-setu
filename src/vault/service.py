"""Public API for the C4 Security & Credential Vault module \u2014 every
other module (and every UI file) should import from here, not from
src.vault.db or src.vault.crypto directly.

Covers: the encrypted secrets vault (real encryption-at-rest, credential
rotation), a real SecurityEvent sink (every credential retrieval logged,
not just changes), the DPDP Act deletion-request workflow (Partner-only
approval), and a structural Backup/DR placeholder (never fabricated
status values).

Cross-module retrofit contract:
- C3 (src/settings/service.py)'s `_handoff_to_c4()` stub is replaced by
  calling `vault.add_credential()` here once a module wires it up — see
  the C4 build prompt's "RETROFIT: wire C3-extended's masked API-key
  display to actually delegate storage to this vault" item. C3-ext
  doesn't exist yet in this repo, so nothing to retrofit there this
  build; C3's own stub is left as-is (it never persisted secrets, so
  there is nothing unsafe about it remaining a no-op until C3-ext ships
  and calls this module directly).
- F1's Recent Security Events panel (`auth.list_security_events`) and
  this module's own event log are DISTINCT tables in this build
  (`security_events` vs `vault_security_events`). Per the build prompt's
  "Documentation Conflict" note, this module is the new permanent home
  for security events going forward; `merged_security_events()` below
  gives the Security Overview screen a single, time-ordered view across
  both without requiring F1's existing call sites to change today.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src import db as recon_db
from src.vault import crypto
from src.vault import db as vdb
from src.vault.schema import init_vault_schema


class VaultError(Exception):
    """Raised for expected vault-module failures (validation, blocked
    action, etc.)."""


# ---------------------------------------------------------------------------
# Init + connection
# ---------------------------------------------------------------------------


def init_vault(db_path=None) -> None:
    """Create C4 tables. Call once at app start, alongside the other
    modules' init_*() calls."""
    conn = _connect(db_path)
    try:
        init_vault_schema(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Encrypted credentials \u2014 REAL encryption-at-rest, never plaintext
# ---------------------------------------------------------------------------
# Business rule: Admin manages vault-level configuration but NEVER sees
# raw secret values, even as Admin. Enforced structurally here: no
# function in this module returns a decrypted secret to a caller that
# isn't explicitly the point-of-use decrypt path
# (get_credential_secret_for_use), and that path always logs a
# SecurityEvent for the retrieval.


def list_credentials(*, client_id: Optional[int] = None, db_path=None) -> list[dict[str, Any]]:
    """Rows with masked_ref + status only \u2014 ciphertext is never
    selected by this call, let alone decrypted."""
    conn = _connect(db_path)
    try:
        return vdb.list_credentials(conn, client_id=client_id)
    finally:
        conn.close()


def add_credential(
    service: str, secret: str, *, label: Optional[str] = None, client_id: Optional[int] = None,
    actor: str, db_path=None,
) -> int:
    """Store a new credential, REAL encryption-at-rest. The plaintext
    ``secret`` is encrypted immediately and never persisted or logged in
    cleartext anywhere \u2014 only ``masked_ref`` is ever displayed."""
    if not service:
        raise VaultError("Service name is required.")
    if not secret:
        raise VaultError("A secret/API key is required.")
    ciphertext = crypto.encrypt(secret)
    masked_ref = crypto.mask_secret(secret)
    conn = _connect(db_path)
    try:
        credential_id = vdb.add_credential(
            conn, service, label, masked_ref, ciphertext,
            client_id=client_id, created_by=actor,
        )
        vdb.log_event(
            conn, "credential_created", actor=actor, client_id=client_id,
            detail=f"{service} credential added (id {credential_id})",
        )
        return credential_id
    finally:
        conn.close()


def get_credential_secret_for_use(credential_id: int, *, actor: str, purpose: str, db_path=None) -> str:
    """The ONLY sanctioned point-of-use decrypt path.

    Every call is logged as a SecurityEvent \u2014 per the build prompt,
    every credential RETRIEVAL/USE, not just every change, is logged.
    Callers must use the returned plaintext immediately and must never
    store, print, or hand it back to any UI layer.
    """
    conn = _connect(db_path)
    try:
        row = vdb.get_credential(conn, credential_id)
        if row is None or not row["is_active"]:
            raise VaultError("No such credential.")
        secret = crypto.decrypt(row["ciphertext"])
        vdb.log_event(
            conn, "credential_retrieved", actor=actor, client_id=row["client_id"],
            detail=f"{row['service']} credential retrieved for: {purpose}",
        )
        return secret
    finally:
        conn.close()


def rotate_credential(credential_id: int, new_secret: str, *, actor: str, db_path=None) -> None:
    """Write-only rotation: the old value is never shown, the new value is
    never redisplayed after save. Only the masked reference + status
    update afterward; a rotation-history row records timestamp + who, no
    value column ever."""
    if not new_secret:
        raise VaultError("A new secret/API key is required.")
    conn = _connect(db_path)
    try:
        row = vdb.get_credential(conn, credential_id)
        if row is None or not row["is_active"]:
            raise VaultError("No such credential.")
        new_ciphertext = crypto.encrypt(new_secret)
        new_masked = crypto.mask_secret(new_secret)
        vdb.rotate_credential(conn, credential_id, new_masked, new_ciphertext)
        vdb.record_rotation(conn, credential_id, actor)
        vdb.log_event(
            conn, "credential_rotated", actor=actor, client_id=row["client_id"],
            detail=f"{row['service']} credential rotated",
        )
    finally:
        conn.close()


def set_credential_status(connection_id: int, status: str, *, actor: str, db_path=None) -> None:
    if status not in ("connected", "expired", "needs_reauth"):
        raise VaultError(f"Unknown connection status: {status}")
    conn = _connect(db_path)
    try:
        vdb.set_credential_status(conn, connection_id, status)
    finally:
        conn.close()


def deactivate_credential(credential_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        row = vdb.get_credential(conn, credential_id)
        vdb.deactivate_credential(conn, credential_id)
        vdb.log_event(
            conn, "credential_removed",
            actor=actor, client_id=row["client_id"] if row else None,
            detail=f"{row['service'] if row else credential_id} credential deactivated",
        )
    finally:
        conn.close()


def list_rotation_history(credential_id: Optional[int] = None, limit: int = 200, *, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return vdb.list_rotation_history(conn, credential_id, limit=limit)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Security events \u2014 the real C4 sink
# ---------------------------------------------------------------------------


def log_security_event(
    event_type: str, *, actor: Optional[str] = None, client_id: Optional[int] = None,
    detail: Optional[str] = None, db_path=None,
) -> None:
    """Generic entry point for other modules to log a security event once
    they retrofit to C4 (validation-block overrides, filing sends, etc.)."""
    conn = _connect(db_path)
    try:
        vdb.log_event(conn, event_type, actor=actor, client_id=client_id, detail=detail)
    finally:
        conn.close()


def list_security_events(*, client_id: Optional[int] = None, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return vdb.list_events(conn, client_id=client_id, limit=limit)
    finally:
        conn.close()


def merged_security_events(limit: int = 200, *, db_path=None) -> list[dict[str, Any]]:
    """Time-ordered union of this module's own event log and F1's existing
    security_events table, normalized to a common shape, for the Security
    Overview screen's "Recent activity panel" (3.8). This is additive-only
    plumbing \u2014 F1's original table and call sites are untouched; see
    the module docstring's retrofit-contract note."""
    vault_events = list_security_events(limit=limit, db_path=db_path)
    normalized = [
        {
            "event_type": e["event_type"],
            "actor": e.get("actor") or "?",
            "detail": e.get("detail") or "",
            "created_at": e["created_at"],
            "source": "vault",
        }
        for e in vault_events
    ]
    try:
        from src.auth import service as auth  # local import avoids a hard circular dep

        for e in auth.list_security_events(limit=limit, db_path=db_path):
            normalized.append(
                {
                    "event_type": e["event_type"],
                    "actor": e.get("username") or "?",
                    "detail": e.get("detail") or "",
                    "created_at": e["created_at"],
                    "source": "auth",
                }
            )
    except Exception:  # noqa: BLE001 \u2014 F1 events are a best-effort merge
        pass
    normalized.sort(key=lambda e: e["created_at"], reverse=True)
    return normalized[:limit]


def event_type_breakdown_last_30_days(*, db_path=None) -> dict[str, int]:
    """Grouped breakdown for the Security Overview headline\u2192grouped\u2192
    detail layout (3.6): events by type, last 30 days, this module's own
    log only (the merged view is for the raw activity panel; the summary
    counts are scoped to C4's real sink per the build prompt)."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn = _connect(db_path)
    try:
        return vdb.event_counts_by_type_since(conn, since)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DPDP Act deletion-request workflow \u2014 Partner-only approval
# ---------------------------------------------------------------------------


def submit_dpdp_request(
    *, client_id: Optional[int], client_label: str, scope: str, legal_basis: str,
    deletable_items: list[str], exempt_items: list[str], actor: str, db_path=None,
) -> int:
    if not client_label:
        raise VaultError("A client is required.")
    if not scope:
        raise VaultError("A scope description is required.")
    if not legal_basis:
        raise VaultError("A legal basis is required.")
    conn = _connect(db_path)
    try:
        request_id = vdb.create_dpdp_request(
            conn, client_id=client_id, client_label=client_label, scope=scope,
            legal_basis=legal_basis, deletable_items=deletable_items,
            exempt_items=exempt_items, submitted_by=actor,
        )
        vdb.log_event(
            conn, "dpdp_request_submitted", actor=actor, client_id=client_id,
            detail=f"DPDP deletion request #{request_id} for {client_label}: {scope}",
        )
        return request_id
    finally:
        conn.close()


def list_dpdp_requests(*, status: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return vdb.list_dpdp_requests(conn, status=status)
    finally:
        conn.close()


def get_dpdp_request(request_id: int, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return vdb.get_dpdp_request(conn, request_id)
    finally:
        conn.close()


def decide_dpdp_request(
    request_id: int, *, approve: bool, reason: str, actor: str, db_path=None,
) -> None:
    """Approve or reject a pending DPDP deletion request. BOTH actions
    require a non-empty reason (component 3.3, reason-capture-before-save)
    \u2014 reject leaves the underlying data untouched but is still fully
    logged. Approval is the one real decision point in this module."""
    if not reason or not reason.strip():
        raise VaultError("A reason is required before this decision can be saved.")
    conn = _connect(db_path)
    try:
        req = vdb.get_dpdp_request(conn, request_id)
        if req is None:
            raise VaultError("No such request.")
        if req["status"] != "pending":
            raise VaultError("This request has already been decided.")
        status = "approved" if approve else "rejected"
        vdb.decide_dpdp_request(conn, request_id, status=status, decided_by=actor, reason=reason)
        vdb.log_event(
            conn,
            "dpdp_request_approved" if approve else "dpdp_request_rejected",
            actor=actor, client_id=req["client_id"],
            detail=f"DPDP deletion request #{request_id} ({req['client_label']}): {reason}",
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backup/DR \u2014 structural placeholder only, never a fabricated value
# ---------------------------------------------------------------------------


def backup_dr_status() -> dict[str, str]:
    """Returns the fixed placeholder text only. There is no backing table
    and no real backup/DR mechanism in this build \u2014 the build prompt is
    explicit that no status value should be fabricated. RTO/RPO targets
    are deferred until the hosting decision is made."""
    return {
        "label": "Not yet configured \u2014 pending hosting decision",
        "rto": "Deferred until the hosting decision is made.",
        "rpo": "Deferred until the hosting decision is made.",
    }
