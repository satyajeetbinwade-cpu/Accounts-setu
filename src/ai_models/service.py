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
from src.ai_models.schema import init_ai_models_schema
from src.ai_models.seed import INGESTION_TOUCHPOINT, run_seed

# OpenRouter model ids are "<vendor>/<model>", optionally with a variant
# suffix (":free", ":nitro"). Deliberately permissive — this catches typos
# and pasted prose, not every possible future id shape.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._~-]+/[A-Za-z0-9._~:-]+$")

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


def validate_model_id(model_id: str) -> str:
    """Return the trimmed id, or raise AIModelsError explaining the problem."""
    cleaned = (model_id or "").strip()
    if not cleaned:
        raise AIModelsError("A model id is required.")
    if not _MODEL_ID_RE.match(cleaned):
        raise AIModelsError(
            f"'{cleaned}' doesn't look like an OpenRouter model id. "
            "They take the form vendor/model — for example anthropic/claude-opus-4.1."
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
    """Headline counts for the registry screen."""
    rows = list_assignments(db_path=db_path)
    by_category: dict[str, int] = {}
    unassigned = 0
    for row in rows:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        if not row.get("primary_model"):
            unassigned += 1
    return {
        "total": len(rows),
        "by_category": by_category,
        "unassigned": unassigned,
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
    """Fetch OpenRouter's live model list and cache it.

    Raises CatalogError when the fetch fails — the caller shows the reason
    and keeps using the cached/bundled list. Never silently pretends to
    have refreshed.
    """
    models = catalog_mod.fetch_catalog()
    conn = _connect(db_path)
    try:
        adb.replace_catalog(conn, models)
        adb.log_change(
            conn, touchpoint_key=None, action="catalog_refreshed",
            detail=f"{len(models)} model(s) fetched from OpenRouter", actor=actor,
        )
    finally:
        conn.close()
    return {"count": len(models)}


def list_catalog(*, db_path=None) -> list[dict[str, Any]]:
    """The cached catalogue, or the bundled fallback when nothing is cached."""
    conn = _connect(db_path)
    try:
        rows = adb.list_catalog(conn)
    finally:
        conn.close()
    return rows or catalog_mod.fallback_catalog()


def catalog_status(*, db_path=None) -> dict[str, Any]:
    """Whether the dropdown is backed by a live fetch or the bundled list."""
    conn = _connect(db_path)
    try:
        fetched_at = adb.catalog_fetched_at(conn)
        count = len(adb.list_catalog(conn))
    finally:
        conn.close()
    return {
        "live": bool(fetched_at),
        "fetched_at": fetched_at,
        "count": count,
        "source": "OpenRouter (live)" if fetched_at else "bundled fallback list",
    }


def model_options(*, db_path=None) -> list[dict[str, Any]]:
    """Catalogue entries shaped for a dropdown, sorted by display name."""
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
