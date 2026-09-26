"""Public API for the central AI model registry.

Every other module (and every UI file) should import from here, not from
src.ai_models.db or src.ai_models.catalog directly. Mirrors
src/ingestion_ai/service.py's role.

What this module is for
-----------------------
One place to answer "which model does this touchpoint use, and how do I
change it?" — across every AI-touching module, with a dropdown populated
from OpenRouter's real catalogue.

Business rules enforced HERE (not merely in the UI):
- A model id must be non-empty and look like an OpenRouter id
  (``vendor/model``). A typo'd id would otherwise fail at call time, deep
  inside a run, with an opaque provider error.
- Primary and fallback must differ. A "fallback" identical to the primary
  is not a fallback — it's a silent single point of failure.
- The catalogue is a CONVENIENCE, never a constraint: any valid-looking id
  is accepted even if it isn't in the cached list, because OpenRouter adds
  models continuously and a stale cache must not block a legitimate choice.
- Changing a model is logged (locally, retrofitting to F4 later).

Runtime wiring
--------------
``effective_model(touchpoint_key)`` is the read path modules use. The
ingestion layer's own settings key (``ingestion_ai.llm.model``) is kept in
sync by ``set_assignment_models()`` so the existing ingestion code path
keeps working unchanged — this registry becomes the source of truth without
requiring a rewrite of src/ingestion_ai/llm.py.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

from src import db as recon_db
from src.ai_models import catalog as catalog_mod
from src.ai_models import db as adb
from src.ai_models import providers as prov
from src.ai_models.schema import init_ai_models_schema
from src.ai_models.seed import INGESTION_TOUCHPOINT, run_seed

# OpenRouter model ids are "<vendor>/<model>", optionally with a variant
# suffix (":free", ":nitro"). Deliberately permissive — this catches typos
# and pasted prose, not every possible future id shape.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._~-]+/[A-Za-z0-9._~:-]+$")

# Providers called directly (Anthropic / Deepseek / Sarvam) use a bare model
# id with no vendor namespace — e.g. "claude-sonnet-4", "deepseek-chat".
_BARE_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._~:/-]+$")

CATEGORIES = ["Ingestion", "Extraction", "Analysis"]


class AIModelsError(RuntimeError):
    """Raised for expected registry failures (bad model id, unknown
    touchpoint). The message is user-facing."""


def init_ai_models(db_path=None) -> None:
    """Create the registry tables and seed the touchpoint list. Idempotent."""
    conn = recon_db.get_connection(db_path)
    try:
        init_ai_models_schema(conn)
        run_seed(conn)
    finally:
        conn.close()


def _connect(db_path=None) -> sqlite3.Connection:
    return recon_db.get_connection(db_path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_model_id(model_id: str, *, provider_key: str = "openrouter") -> str:
    """Return the trimmed id, or raise AIModelsError explaining the problem.

    Validation is provider-aware: OpenRouter ids are ``vendor/model`` (they
    namespace across vendors), while providers called directly (Anthropic,
    Deepseek, Sarvam) use a bare model id (``claude-sonnet-4``,
    ``deepseek-chat``).
    """
    cleaned = (model_id or "").strip()
    if not cleaned:
        raise AIModelsError("A model id is required.")
    if provider_key == "openrouter":
        if not _MODEL_ID_RE.match(cleaned):
            raise AIModelsError(
                f"'{cleaned}' doesn't look like an OpenRouter model id. "
                "They take the form vendor/model — for example anthropic/claude-opus-4.1."
            )
        return cleaned
    spec = prov.get_spec(provider_key)
    if not _BARE_MODEL_ID_RE.match(cleaned):
        raise AIModelsError(
            f"'{cleaned}' doesn't look like a valid {spec.label} model id."
        )
    return cleaned


# ---------------------------------------------------------------------------
# Assignments — read
# ---------------------------------------------------------------------------


def list_assignments(*, category: Optional[str] = None, db_path=None) -> list[dict[str, Any]]:
    """Every AI touchpoint, with its current model assignment."""
    conn = _connect(db_path)
    try:
        rows = adb.list_assignments(conn)
    finally:
        conn.close()
    if category:
        rows = [r for r in rows if r["category"] == category]
    return rows


def get_assignment(touchpoint_key: str, *, db_path=None) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.get_assignment(conn, touchpoint_key)
    finally:
        conn.close()


def effective_model(touchpoint_key: str, *, db_path=None) -> Optional[str]:
    """The model id a module should actually call for this touchpoint.

    This is the read path modules use. Returns None when the touchpoint is
    unknown or has no primary model — callers fall back to their own
    configured default rather than guessing.
    """
    row = get_assignment(touchpoint_key, db_path=db_path)
    if not row:
        return None
    return row.get("primary_model") or None


def assignment_summary(*, db_path=None) -> dict[str, Any]:
    """Headline counts for the registry screen.

    Only ACTIVE touchpoints can be "unassigned" — the inactive placeholders
    (Coming-with-[Module] rows) legitimately carry no model and must not make
    the screen report a problem.
    """
    rows = list_assignments(db_path=db_path)
    by_category: dict[str, int] = {}
    unassigned = 0
    inactive = 0
    for row in rows:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        if not row.get("is_active"):
            inactive += 1
            continue
        if not row.get("primary_model"):
            unassigned += 1
    return {
        "total": len(rows),
        "by_category": by_category,
        "unassigned": unassigned,
        "inactive": inactive,
    }


# ---------------------------------------------------------------------------
# Assignments — write
# ---------------------------------------------------------------------------


def set_assignment_models(
    touchpoint_key: str,
    *,
    primary_model: str,
    fallback_model: str,
    actor: str,
    db_path=None,
) -> dict[str, Any]:
    """Set a touchpoint's primary and fallback model.

    Validates both ids and rejects a fallback identical to the primary —
    a "fallback" that is the same model provides no resilience and would
    hide a real outage.
    """
    primary = validate_model_id(primary_model)
    fallback = validate_model_id(fallback_model)
    if primary == fallback:
        raise AIModelsError(
            "The fallback model must differ from the primary — otherwise it provides "
            "no resilience if the primary is unavailable."
        )

    conn = _connect(db_path)
    try:
        existing = adb.get_assignment(conn, touchpoint_key)
        if existing is None:
            raise AIModelsError(f"Unknown touchpoint '{touchpoint_key}'.")

        adb.set_assignment_models(
            conn, touchpoint_key,
            primary_model=primary, fallback_model=fallback, actor=actor,
        )
        adb.log_change(
            conn, touchpoint_key=touchpoint_key, action="model_assignment_changed",
            detail=(
                f"primary: {existing.get('primary_model')} -> {primary}; "
                f"fallback: {existing.get('fallback_model')} -> {fallback}"
            ),
            actor=actor,
        )
    finally:
        conn.close()

    # Keep the ingestion layer's own settings key in sync so its existing
    # code path (src/ingestion_ai/llm.py) picks up the change with no
    # rewrite. Best-effort: a settings failure must not undo the registry
    # write that already succeeded.
    if touchpoint_key == INGESTION_TOUCHPOINT:
        try:
            from src.settings import service as settings

            settings.set_setting_str(
                "ingestion_ai.llm.model", primary, actor=actor, db_path=db_path,
            )
        except Exception:  # noqa: BLE001
            pass

    return {"touchpoint_key": touchpoint_key, "primary_model": primary, "fallback_model": fallback}


def set_assignment_active(touchpoint_key: str, *, is_active: bool, actor: str, db_path=None) -> None:
    conn = _connect(db_path)
    try:
        if adb.get_assignment(conn, touchpoint_key) is None:
            raise AIModelsError(f"Unknown touchpoint '{touchpoint_key}'.")
        conn.execute(
            "UPDATE ai_model_assignments SET is_active = ? WHERE touchpoint_key = ?",
            (1 if is_active else 0, touchpoint_key),
        )
        conn.commit()
        adb.log_change(
            conn, touchpoint_key=touchpoint_key,
            action="touchpoint_activated" if is_active else "touchpoint_deactivated",
            detail=f"is_active -> {bool(is_active)}", actor=actor,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def refresh_catalog(*, actor: str, db_path=None) -> dict[str, Any]:
    """Refresh the OpenRouter catalogue (legacy entry point).

    Delegates to the provider-scoped refresh so there is ONE fetch path.
    Raises AIModelsError when the fetch fails — the caller shows the reason
    and keeps using the cached/bundled list. Never silently pretends to have
    refreshed.
    """
    result = refresh_provider_catalog("openrouter", actor=actor, db_path=db_path)
    return {"count": result["count"]}


def list_catalog(*, db_path=None) -> list[dict[str, Any]]:
    """OpenRouter's cached catalogue, or the bundled fallback when empty."""
    rows = models_for_provider("openrouter", db_path=db_path)
    return rows or catalog_mod.fallback_catalog()


def catalog_status(*, db_path=None) -> dict[str, Any]:
    """Whether the OpenRouter dropdown is backed by a live fetch or the
    bundled list."""
    conn = _connect(db_path)
    try:
        rows = adb.list_provider_models(conn, "openrouter")
    finally:
        conn.close()
    fetched_at = max((r.get("fetched_at") or "" for r in rows), default="")
    return {
        "live": bool(rows),
        "fetched_at": fetched_at or None,
        "count": len(rows),
        "source": "OpenRouter (live)" if rows else "bundled fallback list",
    }


def model_options(*, db_path=None) -> list[dict[str, Any]]:
    """OpenRouter catalogue entries shaped for a dropdown, sorted by name."""
    rows = list_catalog(db_path=db_path)
    return sorted(rows, key=lambda r: (r.get("name") or r.get("model_id") or "").lower())


def model_label(entry: dict[str, Any]) -> str:
    """One-line dropdown label: name, id, context window, and price when known."""
    name = entry.get("name") or entry.get("model_id") or "unknown"
    model_id = entry.get("model_id") or ""
    parts = [f"{name}  ·  {model_id}"]
    context = entry.get("context_length")
    if context:
        parts.append(f"{int(context) // 1000}k ctx")
    price = entry.get("prompt_price")
    if price is not None:
        parts.append(f"${price:.2f}/M in")
    return "  ·  ".join(parts)


def list_change_log(*, limit: int = 200, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.list_change_log(conn, limit=limit)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Providers — the fixed platform set, each with its own credential + status
# ---------------------------------------------------------------------------


def _credential_masked_map(db_path=None) -> dict[int, str]:
    """vault credential_id -> masked_ref, best-effort."""
    try:
        from src.vault import service as vault

        return {
            int(r["credential_id"]): (r.get("masked_ref") or "")
            for r in vault.list_credentials(db_path=db_path)
        }
    except Exception:  # noqa: BLE001
        return {}


def list_providers(*, db_path=None) -> list[dict[str, Any]]:
    """Every platform provider, joined to its credential binding + status."""
    conn = _connect(db_path)
    try:
        providers = adb.list_providers(conn)
        creds = {p["provider_key"]: adb.get_provider_credential(conn, p["provider_key"]) for p in providers}
    finally:
        conn.close()

    masked = _credential_masked_map(db_path)
    out: list[dict[str, Any]] = []
    for p in providers:
        cred = creds.get(p["provider_key"]) or {}
        credential_id = cred.get("credential_id")
        out.append({
            **p,
            "credential_id": credential_id,
            "status": cred.get("status") or "unconfigured",
            "status_detail": cred.get("status_detail") or "",
            "masked_ref": masked.get(int(credential_id)) if credential_id else None,
            "has_key": bool(credential_id),
        })
    return out


def get_provider_row(provider_key: str, *, db_path=None) -> Optional[dict[str, Any]]:
    for p in list_providers(db_path=db_path):
        if p["provider_key"] == provider_key:
            return p
    return None


def set_provider_key(provider_key: str, secret: str, *, actor: str, db_path=None) -> dict[str, Any]:
    """Store a provider's API key in C4's vault and bind the provider to it.

    The secret is handed to the vault (the only place it is ever persisted,
    encrypted) and is never stored or returned here — only the vault's masked
    reference is displayed downstream.
    """
    spec = prov.get_spec(provider_key)
    if not secret or not secret.strip():
        raise AIModelsError(f"An API key is required for {spec.label}.")

    from src.vault import service as vault

    credential_id = vault.add_credential(
        spec.label, secret.strip(), label=f"AI provider: {spec.label}",
        actor=actor, db_path=db_path,
    )
    conn = _connect(db_path)
    try:
        adb.set_provider_credential(
            conn, spec.key, credential_id=credential_id, status="live",
            status_detail="Key stored — not yet verified.", updated_by=actor,
        )
        adb.log_change(
            conn, touchpoint_key=None, action="provider_key_set",
            detail=f"An API key was stored for {spec.label}", actor=actor,
        )
    finally:
        conn.close()
    return {"provider_key": spec.key, "credential_id": credential_id, "status": "live"}


def replace_provider_key(provider_key: str, secret: str, *, actor: str, db_path=None) -> dict[str, Any]:
    """Replace a provider's key (rotate). Never reveals the old or new value."""
    spec = prov.get_spec(provider_key)
    if not secret or not secret.strip():
        raise AIModelsError(f"An API key is required for {spec.label}.")

    conn = _connect(db_path)
    try:
        cred = adb.get_provider_credential(conn, spec.key)
    finally:
        conn.close()
    credential_id = (cred or {}).get("credential_id")
    if not credential_id:
        # No existing key — this is a first store, not a replace.
        return set_provider_key(spec.key, secret, actor=actor, db_path=db_path)

    from src.vault import service as vault

    vault.rotate_credential(int(credential_id), secret.strip(), actor=actor, db_path=db_path)
    conn = _connect(db_path)
    try:
        adb.set_provider_credential(
            conn, spec.key, credential_id=int(credential_id), status="live",
            status_detail="Key replaced — not yet verified.", updated_by=actor,
        )
        adb.log_change(
            conn, touchpoint_key=None, action="provider_key_replaced",
            detail=f"The API key for {spec.label} was replaced", actor=actor,
        )
    finally:
        conn.close()
    return {"provider_key": spec.key, "credential_id": int(credential_id), "status": "live"}


def provider_key_configured(provider_key: str, *, db_path=None) -> tuple[bool, Optional[str]]:
    """Whether a provider has a usable key, WITHOUT decrypting it.

    Returns ``(configured, source)`` where source is ``'vault'`` or ``'env'``.
    Used by status/introspection screens so they never trigger a vault
    retrieval (and its SecurityEvent) just to render.
    """
    spec = prov.get_spec(provider_key)
    conn = _connect(db_path)
    try:
        cred = adb.get_provider_credential(conn, spec.key)
    finally:
        conn.close()
    if (cred or {}).get("credential_id"):
        return True, "vault"
    if spec.api_key_env:
        import os

        if os.environ.get(spec.api_key_env):
            return True, "env"
    return False, None


def resolve_provider_secret(
    provider_key: str, *, actor: str, purpose: str, db_path=None,
) -> str:
    """Resolve a provider's API key.

    A key bound to the provider in the vault is decrypted through C4's
    sanctioned point-of-use path (which logs a SecurityEvent on every
    retrieval). When no vault credential is bound, the provider's own
    environment variable is used — so an existing env-based deployment keeps
    working without a stored credential.
    """
    spec = prov.get_spec(provider_key)
    conn = _connect(db_path)
    try:
        cred = adb.get_provider_credential(conn, spec.key)
    finally:
        conn.close()
    credential_id = (cred or {}).get("credential_id")
    if credential_id:
        from src.vault import service as vault

        return vault.get_credential_secret_for_use(
            int(credential_id), actor=actor, purpose=purpose, db_path=db_path,
        )

    if spec.api_key_env:
        import os

        env_key = os.environ.get(spec.api_key_env, "")
        if env_key:
            return env_key

    raise AIModelsError(
        f"No API key is stored for {spec.label}. Add one on the AI Models screen."
    )


def models_for_provider(provider_key: str, *, db_path=None) -> list[dict[str, Any]]:
    """A provider's cached catalogue. Never returns an empty list for a
    provider that has a usable fallback: the static list for a static
    provider, or the bundled list for OpenRouter (whose ids are
    OpenRouter-shaped). Providers called directly with no cache return an
    empty list rather than a list of ids that would not resolve — the UI
    offers manual entry and a catalogue refresh for those."""
    spec = prov.get_spec(provider_key)
    conn = _connect(db_path)
    try:
        rows = adb.list_provider_models(conn, spec.key)
    finally:
        conn.close()
    if rows:
        return rows
    static = prov.static_models(spec)
    if static:
        return static
    if spec.key == "openrouter":
        return catalog_mod.fallback_catalog()
    return []


def refresh_provider_catalog(provider_key: str, *, actor: str, db_path=None) -> dict[str, Any]:
    """Fetch + cache ONE provider's model list, and update its live status.

    Raises AIModelsError (with the provider's reason) on failure, after
    recording invalid/unreachable status. Other providers are untouched —
    one provider's failure never changes another's status.
    """
    spec = prov.get_spec(provider_key)
    try:
        api_key: Optional[str] = resolve_provider_secret(
            spec.key, actor=actor, purpose=f"refresh {spec.label} catalogue", db_path=db_path,
        )
    except AIModelsError:
        api_key = None  # static providers need no key; live ones will report it

    try:
        models = prov.fetch_catalog(spec, api_key)
    except prov.ProviderError as exc:
        kind = str(exc).split("|", 1)[0]
        status = "invalid" if kind == "auth_error" else "unreachable"
        conn = _connect(db_path)
        try:
            adb.set_provider_status(
                conn, spec.key, status=status, status_detail=str(exc).split("|", 1)[-1],
            )
        finally:
            conn.close()
        raise AIModelsError(str(exc).split("|", 1)[-1]) from exc

    source = "live" if spec.has_live_catalog else "static"
    conn = _connect(db_path)
    try:
        adb.replace_provider_models(conn, spec.key, models, source=source)
        adb.set_provider_status(
            conn, spec.key, status="live",
            status_detail=f"{len(models)} model(s) available.",
        )
        adb.log_change(
            conn, touchpoint_key=None, action="provider_catalog_refreshed",
            detail=f"{len(models)} model(s) loaded for {spec.label}", actor=actor,
        )
    finally:
        conn.close()
    return {"provider_key": spec.key, "count": len(models), "source": source}


def verify_provider(provider_key: str, *, actor: str, db_path=None) -> dict[str, Any]:
    """Probe a provider's connectivity and record its status."""
    spec = prov.get_spec(provider_key)
    try:
        api_key: Optional[str] = resolve_provider_secret(
            spec.key, actor=actor, purpose=f"verify {spec.label} connection", db_path=db_path,
        )
    except AIModelsError:
        api_key = None
    status, detail = prov.probe(spec, api_key)
    conn = _connect(db_path)
    try:
        adb.set_provider_status(conn, spec.key, status=status, status_detail=detail)
    finally:
        conn.close()
    return {"provider_key": spec.key, "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# Per-touchpoint legs (provider + model, independently selectable)
# ---------------------------------------------------------------------------


def effective_leg(touchpoint_key: str, *, db_path=None) -> Optional[dict[str, Any]]:
    """The primary and fallback (provider, model) legs for a touchpoint."""
    row = get_assignment(touchpoint_key, db_path=db_path)
    if not row:
        return None
    return {
        "is_active": bool(row.get("is_active")),
        "primary": {
            "provider": row.get("primary_provider") or "openrouter",
            "model": row.get("primary_model"),
        },
        "fallback": {
            "provider": row.get("fallback_provider") or "openrouter",
            "model": row.get("fallback_model"),
        },
    }


def set_assignment_legs(
    touchpoint_key: str,
    *,
    primary_provider: str,
    primary_model: str,
    fallback_provider: str,
    fallback_model: str,
    actor: str,
    db_path=None,
) -> dict[str, Any]:
    """Set both legs, each an independent (provider, model) pair.

    The fallback provider MAY differ from the primary — that cross-provider
    fallback is the point of this extension. A fallback identical to the
    primary provides no resilience and is rejected.
    """
    pp = (primary_provider or "").strip().lower()
    fp = (fallback_provider or "").strip().lower()
    prov.get_spec(pp)
    prov.get_spec(fp)
    pm = validate_model_id(primary_model, provider_key=pp)
    fm = validate_model_id(fallback_model, provider_key=fp)
    if (pp, pm) == (fp, fm):
        raise AIModelsError(
            "The fallback leg must differ from the primary — otherwise it provides "
            "no resilience if the primary is unavailable."
        )

    conn = _connect(db_path)
    try:
        existing = adb.get_assignment(conn, touchpoint_key)
        if existing is None:
            raise AIModelsError(f"Unknown touchpoint '{touchpoint_key}'.")
        adb.set_assignment_legs(
            conn, touchpoint_key,
            primary_provider=pp, primary_model=pm,
            fallback_provider=fp, fallback_model=fm, actor=actor,
        )
        adb.log_change(
            conn, touchpoint_key=touchpoint_key, action="model_assignment_changed",
            detail=f"primary: {pp}/{pm}; fallback: {fp}/{fm}", actor=actor,
        )
    finally:
        conn.close()

    # Keep the ingestion layer's legacy settings key in sync when its primary
    # is OpenRouter, so the existing code path keeps working. Best-effort.
    if touchpoint_key == INGESTION_TOUCHPOINT and pp == "openrouter":
        try:
            from src.settings import service as settings

            settings.set_setting_str(
                "ingestion_ai.llm.model", pm, actor=actor, db_path=db_path,
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "touchpoint_key": touchpoint_key,
        "primary_provider": pp, "primary_model": pm,
        "fallback_provider": fp, "fallback_model": fm,
    }


# ---------------------------------------------------------------------------
# Live per-leg test call + fallback events
# ---------------------------------------------------------------------------


def test_leg(touchpoint_key: str, leg: str, *, actor: str, db_path=None) -> dict[str, Any]:
    """Run a live test call against ONE leg (primary|fallback).

    Returns latency + a truncated sample output for the leg tested, not merely
    pass/fail. Never raises for an expected provider failure — the reason is
    returned in ``error`` so the UI can show it.
    """
    leg = (leg or "").strip().lower()
    if leg not in ("primary", "fallback"):
        raise AIModelsError("A leg must be 'primary' or 'fallback'.")
    row = get_assignment(touchpoint_key, db_path=db_path)
    if row is None:
        raise AIModelsError(f"Unknown touchpoint '{touchpoint_key}'.")

    provider_key = (row.get(f"{leg}_provider") or "openrouter")
    model = row.get(f"{leg}_model") or ""
    result_base = {"touchpoint_key": touchpoint_key, "leg": leg, "provider": provider_key, "model": model}
    if not model:
        return {**result_base, "ok": False, "latency_ms": None, "sample": "",
                "error": "No model is assigned to this leg yet."}

    spec = prov.get_spec(provider_key)
    try:
        api_key = resolve_provider_secret(
            provider_key, actor=actor, purpose=f"test call {touchpoint_key}/{leg}", db_path=db_path,
        )
    except AIModelsError as exc:
        return {**result_base, "ok": False, "latency_ms": None, "sample": "", "error": str(exc)}

    try:
        text, latency_ms = prov.call_provider(
            spec, api_key, model=model,
            system="You are a connectivity test for the SETU platform.",
            user="Reply with the single word: ok",
            max_tokens=16, temperature=0.0, timeout=30,
        )
        return {**result_base, "ok": True, "latency_ms": latency_ms,
                "sample": (text or "").strip()[:300], "error": None}
    except prov.ProviderError as exc:
        return {**result_base, "ok": False, "latency_ms": None, "sample": "", "error": str(exc)}


def record_fallback_event(
    *,
    touchpoint_key: str,
    leg: str,
    primary_provider: Optional[str],
    primary_model: Optional[str],
    resolved_provider: str,
    resolved_model: str,
    reason: Optional[str],
    latency_ms: Optional[int],
    actor: Optional[str],
    client_id: Optional[int] = None,
    db_path=None,
) -> int:
    """Record that a touchpoint fell back, including WHICH provider resolved it.

    Written locally (the Recent Fallbacks panel reads it) AND mirrored to C4's
    canonical security-event sink. F4 deliberately excludes AI fallbacks, so
    C4 — not Edit History — is the retrofit target here.
    """
    conn = _connect(db_path)
    try:
        event_id = adb.insert_fallback_event(
            conn,
            touchpoint_key=touchpoint_key, leg=leg,
            primary_provider=primary_provider, primary_model=primary_model,
            resolved_provider=resolved_provider, resolved_model=resolved_model,
            reason=reason, latency_ms=latency_ms, actor=actor, client_id=client_id,
        )
    finally:
        conn.close()

    try:
        from src.vault import service as vault

        vault.log_security_event(
            "ai_fallback", actor=actor or "system", client_id=client_id,
            detail=(f"{touchpoint_key}: {leg} leg failed ({reason}); resolved via "
                    f"{resolved_provider}/{resolved_model}"),
            db_path=db_path,
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the call path
        pass
    return event_id


def list_fallback_events(*, limit: int = 50, db_path=None) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return adb.list_fallback_events(conn, limit=limit)
    finally:
        conn.close()
