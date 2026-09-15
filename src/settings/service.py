"""Public API for the C3 System Settings (base) module — every other
module (and every UI file) should import from here, not from
src.settings.db directly. Mirrors src/clients/service.py's shape.

Covers: firm profile/branding, notification preferences (platform default
+ per-user override, firm-mandatory flag blocks override), credential/API
connection status VIEW (secret never stored here — handed to C4, a stub
until C4 exists), onboarding defaults, retention settings, locale
(hardcoded India), and the F1 retrofit: session timeout reads LIVE from
C3 instead of F1's hardcoded 8h default.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.settings import db as sdb
from src.settings.schema import init_settings_schema


class SettingsError(Exception):
    """Raised for expected settings-module failures (validation, etc.)."""


# ---------------------------------------------------------------------------
# Init + connection
# ---------------------------------------------------------------------------


def init_settings(db_path=None) -> None:
    """Create C3 tables (and seed the default notification preferences as
    additive-only), then return. Call once at app start."""
    conn = _connect(db_path)
    try:
        init_settings_schema(conn)
        _seed_notification_prefs(conn)
    finally:
        conn.close()


# Seed notification preferences: the platform-default list with one
# firm-mandatory (blocked-override) example. Additive-only — never resets
# an Admin's edits (mirrors auth.seed._sync_new_permissions).
_SEED_NOTIFICATION_PREFS: list[tuple[str, str, str, bool, bool]] = [
    # (pref_key, label, kind, firm_default, firm_mandatory)
    ("recon_run_complete", "Reconciliation run completed", "recon", True, False),
    ("recon_exceptions_found", "Exceptions found in a run", "recon", True, False),
    ("client_onboarded", "New client onboarded", "admin", True, False),
    ("security_alert", "Security / escalation alert", "security", True, True),
]


def _seed_notification_prefs(conn: sqlite3.Connection) -> None:
    for pref_key, label, kind, firm_default, firm_mandatory in _SEED_NOTIFICATION_PREFS:
        sdb.upsert_notification_pref(
            conn, pref_key, label, kind, firm_default=firm_default, firm_mandatory=firm_mandatory
        )


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# Locale (hardcoded India — not configurable in v1)
# ---------------------------------------------------------------------------

LOCALE = {
    "country": "India",
    "currency": "INR",
    "date_format": "DD-MM-YYYY",
    "configurable": False,
}


# ---------------------------------------------------------------------------
# Session timeout (retrofit point for F1)
# ---------------------------------------------------------------------------

SESSION_TIMEOUT_KEY = "session_timeout_hours"
DEFAULT_SESSION_TIMEOUT_HOURS = 8.0  # matches F1's original hardcoded default


def get_session_timeout_hours(*, db_path=None) -> float:
    """Read the configured session-timeout (hours) LIVE from C3. Falls back
    to DEFAULT_SESSION_TIMEOUT_HOURS (F1's original default) if unset."""
    conn = _connect(db_path)
    try:
        raw = sdb.get_setting(conn, SESSION_TIMEOUT_KEY)
    finally:
        conn.close()
    if raw is None:
        return DEFAULT_SESSION_TIMEOUT_HOURS
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SESSION_TIMEOUT_HOURS


def set_session_timeout_hours(value: float, *, actor: str, db_path=None) -> None:
    if value <= 0:
        raise SettingsError("Session timeout must be a positive number of hours.")
    conn = _connect(db_path)
    try:
        sdb.set_setting(conn, SESSION_TIMEOUT_KEY, str(value), actor)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Generic integer setting helpers — reused by later modules (e.g. F3-AI's
# admin-configurable confidence thresholds) that just need a flat,
# editable numeric knob without their own settings table.
# ---------------------------------------------------------------------------


def get_setting_int(key: str, *, default: int, db_path=None) -> int:
    conn = _connect(db_path)
    try:
        raw = sdb.get_setting(conn, key)
    finally:
        conn.close()
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def set_setting_int(key: str, value: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        sdb.set_setting(conn, key, str(int(value)), actor)
    finally:
        conn.close()


def get_setting_str(key: str, *, default: Optional[str] = None, db_path=None) -> Optional[str]:
    """Read a flat string setting. The string sibling of
    get_setting_int — used by later modules that need an editable text
    value (e.g. the AI ingestion layer's model id / base URL / selected
    vault credential) without their own settings table."""
    conn = _connect(db_path)
    try:
        return sdb.get_setting(conn, key, default)
    finally:
        conn.close()


def set_setting_str(key: str, value: str, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        sdb.set_setting(conn, key, str(value), actor)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Firm profile / branding
# ---------------------------------------------------------------------------

FIRM_NAME_KEY = "firm_name"
FIRM_PAN_KEY = "firm_pan"


def get_firm_profile(*, db_path=None) -> dict[str, Optional[str]]:
    conn = _connect(db_path)
    try:
        return {
            "firm_name": sdb.get_setting(conn, FIRM_NAME_KEY),
            "firm_pan": sdb.get_setting(conn, FIRM_PAN_KEY),
        }
    finally:
        conn.close()


def update_firm_profile(firm_name: str, firm_pan: Optional[str], *, actor: str, db_path=None) -> None:
    if not firm_name:
        raise SettingsError("Firm name is required.")
    conn = _connect(db_path)
    try:
        sdb.set_setting(conn, FIRM_NAME_KEY, firm_name, actor)
        sdb.set_setting(conn, FIRM_PAN_KEY, firm_pan or "", actor)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------


def list_notification_prefs(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return sdb.list_notification_prefs(conn)
    finally:
        conn.close()


def set_firm_default(pref_key: str, enabled: bool, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        sdb.set_firm_default(conn, pref_key, enabled)
    finally:
        conn.close()


def set_firm_mandatory(pref_key: str, mandatory: bool, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        sdb.set_firm_mandatory(conn, pref_key, mandatory)
    finally:
        conn.close()


def user_notification_state(user_id: int, *, db_path=None) -> dict[str, Any]:
    """Return {pref_key: {'effective': bool, 'mandatory': bool, 'override': bool|None}}
    — the effective on/off for each pref for a given user, resolved against
    platform defaults and the firm-mandatory flag. Override is None when the
    user follows the platform default."""
    conn = _connect(db_path)
    try:
        prefs = sdb.list_notification_prefs(conn)
        overrides = sdb.list_user_overrides(conn, user_id)
    finally:
        conn.close()

    out: dict[str, Any] = {}
    for p in prefs:
        key = p["pref_key"]
        mandatory = bool(p["firm_mandatory"])
        override = overrides.get(key)
        effective = bool(p["firm_default"]) if override is None else override
        if mandatory:
            override = None  # blocked; follow firm state
            effective = bool(p["firm_default"])
        out[key] = {
            "label": p["label"],
            "kind": p["kind"],
            "effective": effective,
            "mandatory": mandatory,
            "override": override,
        }
    return out


def set_user_notification_override(user_id: int, pref_key: str, enabled: bool, *, db_path=None) -> None:
    """Set a per-user override. Refused for firm-mandatory prefs (the toggle
    is locked by design; this is a service-level guard)."""
    conn = _connect(db_path)
    try:
        pref = sdb.get_notification_pref(conn, pref_key)
        if pref is None:
            raise SettingsError(f"Unknown notification preference: {pref_key}")
        if pref["firm_mandatory"]:
            raise SettingsError("This notification is firm-mandatory and can't be overridden.")
        sdb.set_user_override(conn, user_id, pref_key, enabled)
    finally:
        conn.close()


def clear_user_notification_override(user_id: int, pref_key: str, *, db_path=None) -> None:
    """Return this pref to the platform default for the user."""
    conn = _connect(db_path)
    try:
        sdb.clear_user_override(conn, user_id, pref_key)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Credential / API connections (masked reference + live status; never the
# secret itself — handed to C4, a stub until C4 exists)
# ---------------------------------------------------------------------------


def mask_secret(secret: str) -> str:
    """Mask a secret so only a short, non-reversible reference remains.
    This is the ONLY representation C3 ever persists or displays."""
    s = secret.strip()
    if len(s) <= 4:
        return "••••"
    return f"{s[:3]}••••{s[-4:]}"


def list_connections(*, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return sdb.list_connections(conn)
    finally:
        conn.close()


def add_connection(service: str, secret: str, *, actor: str, db_path=None) -> int:
    """Enter a credential. The secret is masked and immediately handed off
    to C4 for REAL encrypted storage (RETROFIT: the C4 handoff is no longer
    a stub — C4 now exists and takes real custody of the secret; nothing
    plaintext is ever persisted here). C3 keeps only the masked reference
    and an initial 'connected' status, purely for display in this screen."""
    if not service:
        raise SettingsError("Service name is required.")
    if not secret:
        raise SettingsError("A secret/API key is required.")
    _handoff_to_c4(service, secret, actor=actor)
    conn = _connect(db_path)
    try:
        return sdb.add_connection(conn, service, mask_secret(secret))
    finally:
        conn.close()


def set_connection_status(connection_id: int, status: str, *, db_path=None) -> None:
    if status not in ("connected", "expired", "needs_reauth"):
        raise SettingsError(f"Unknown connection status: {status}")
    conn = _connect(db_path)
    try:
        sdb.set_connection_status(conn, connection_id, status)
    finally:
        conn.close()


def remove_connection(connection_id: int, *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        sdb.remove_connection(conn, connection_id)
    finally:
        conn.close()


def _handoff_to_c4(service: str, secret: str, *, actor: str) -> None:
    """RETROFIT (was a stub): delegates real storage to C4 (Security &
    Credential Vault) — the secret is encrypted at rest there and NEVER
    persisted by C3 itself. C3's own behavior (masked reference display)
    is unchanged; this is purely a handoff. If C4 is ever unavailable
    (e.g. during a future migration), this fails loudly rather than
    silently dropping the secret."""
    from src.vault import service as vault  # local import avoids a hard circular dep

    vault.add_credential(service, secret, actor=actor)


# ---------------------------------------------------------------------------
# Onboarding defaults (applied when F2 creates a new client)
# ---------------------------------------------------------------------------

ONBOARDING_DEFAULT_KEYS = {
    "onboarding_default_status": "Default branch status for new clients",
    "onboarding_default_assigned_team": "Default assigned team for new clients",
}


def get_onboarding_defaults(*, db_path=None) -> dict[str, Optional[str]]:
    conn = _connect(db_path)
    try:
        return {k: sdb.get_setting(conn, k) for k in ONBOARDING_DEFAULT_KEYS}
    finally:
        conn.close()


def update_onboarding_defaults(values: dict[str, Optional[str]], *, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        for k in ONBOARDING_DEFAULT_KEYS:
            sdb.set_setting(conn, k, values.get(k) or "", actor)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Retention settings (platform-wide default; F3 infinite-retention is the
# documented exception)
# ---------------------------------------------------------------------------

RETENTION_KEY = "retention_years"


def get_retention_years(*, db_path=None) -> Optional[str]:
    conn = _connect(db_path)
    try:
        return sdb.get_setting(conn, RETENTION_KEY)
    finally:
        conn.close()


def set_retention_years(value: str, *, actor: str, db_path=None) -> None:
    if value:
        try:
            years = int(value)
        except ValueError:
            raise SettingsError("Retention must be a whole number of years.")
        if years < 0:
            raise SettingsError("Retention can't be negative.")
    conn = _connect(db_path)
    try:
        sdb.set_setting(conn, RETENTION_KEY, value, actor)
    finally:
        conn.close()