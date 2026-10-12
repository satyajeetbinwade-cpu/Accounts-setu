"""Choosing the SEEDED default models for the page-reading touchpoints.

A fresh database used to start ``invoice_triage``, ``invoice_extraction_visual``
and ``invoice_extraction_verify`` on ``claude-sonnet-4`` with
``gemini-2.5-pro`` as the fallback: two of the most expensive models available,
chosen because they are good, not because anything justified the price. Reading
one multi-page bundle costs real money, and a default nobody chose is the worst
possible place to spend it.

So the seed now ASKS the catalogue. It keeps the models the catalogue says
accept IMAGE input, drops the ones it cannot price, drops Anthropic and the
"pro"/"opus"/"ultra" flagships, and takes the cheapest. Read A takes the
cheapest overall; read B takes the cheapest on a DIFFERENT PROVIDER, because two
reads from one vendor are not an independent second opinion.

When the catalogue cannot answer — which is the NORMAL case on a completely
fresh database, because a live provider's catalogue is only filled by an
explicit refresh — every leg is left EMPTY. The pre-flight then says "no vision
model assigned", which is honest and costs nothing. Quietly defaulting to an
expensive model so the field is not blank is exactly the behaviour this module
exists to stop.

Nothing here touches an EXISTING assignment: ``db.upsert_assignment`` never
overwrites a model on a row that is already there, so an admin's choice always
wins and re-seeding is harmless.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src.ai_models import providers as prov

#: The touchpoints whose default is a page-reading model. Only these are chosen
#: here; every other touchpoint keeps the hardcoded default in ``seed.py``.
VISION_SEED_TOUCHPOINTS: tuple[str, ...] = (
    "invoice_triage",
    "invoice_extraction_visual",
    "invoice_extraction_verify",
)

#: Read A's touchpoint. Read B (`invoice_extraction_verify`) must resolve to a
#: DIFFERENT provider from this one's primary leg.
READ_A_TOUCHPOINT = "invoice_extraction_visual"
READ_B_TOUCHPOINT = "invoice_extraction_verify"

#: Vendors whose models are never chosen as a SEEDED default. An admin can still
#: assign one deliberately.
EXCLUDED_VENDOR_PREFIXES: tuple[str, ...] = ("anthropic/",)

#: Flagship markers. A "pro"/"opus"/"ultra" model is a deliberate purchase, not
#: a sensible default.
FLAGSHIP_MARKERS: tuple[str, ...] = ("-pro", "/pro", "opus", "ultra")


def is_image_capable(modality: Optional[str]) -> bool:
    """True only when the catalogue SAYS the model takes images.

    An unknown modality is not good enough here: the whole point is to pick from
    what we know, and "unknown" would let anything in.
    """
    return prov.supports_images(modality) is True


def is_excluded_default(model_id: str) -> bool:
    """Anthropic, or a flagship — never a seeded default."""
    text = str(model_id or "").strip().lower()
    if not text:
        return True
    if any(text.startswith(prefix) for prefix in EXCLUDED_VENDOR_PREFIXES):
        return True
    return any(marker in text for marker in FLAGSHIP_MARKERS)


def _price(value: Any) -> float:
    """A missing price cannot be compared, so it sorts last."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("inf")
    return number if number >= 0 else float("inf")


def cheap_image_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every catalogued image-capable, priceable, non-flagship model — cheapest first.

    Ordered by prompt price, then completion price, then provider and id, so the
    choice is deterministic (two bootstraps of the same catalogue cannot
    disagree).
    """
    try:
        rows = conn.execute(
            "SELECT provider_key, model_id, prompt_price, completion_price, modality "
            "FROM ai_provider_models"
        ).fetchall()
    except sqlite3.OperationalError:       # no catalogue table yet
        return []

    candidates: list[dict[str, Any]] = []
    for provider_key, model_id, prompt, completion, modality in rows:
        if not is_image_capable(modality) or is_excluded_default(model_id):
            continue
        if prompt is None and completion is None:
            # Unpriced: it cannot be compared with the others, and "cheapest" is
            # the only thing being asked for. Left out rather than guessed at.
            continue
        candidates.append({
            "provider_key": str(provider_key),
            "model_id": str(model_id),
            "prompt_price": _price(prompt),
            "completion_price": _price(completion),
        })
    candidates.sort(key=lambda c: (
        c["prompt_price"], c["completion_price"], c["provider_key"], c["model_id"],
    ))
    return candidates


def _cheapest(
    candidates: list[dict[str, Any]], *, avoid_providers: tuple[str, ...] = (),
) -> Optional[dict[str, Any]]:
    for candidate in candidates:
        if candidate["provider_key"] in avoid_providers:
            continue
        return candidate
    return None


def _leg(candidate: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    if candidate is None:
        return None
    return {"provider": candidate["provider_key"], "model": candidate["model_id"]}


def choose_vision_defaults(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The (primary, fallback) legs for each vision touchpoint, from the catalogue.

    Each leg is ``None`` when the catalogue cannot supply one — which the seed
    turns into an EMPTY, unassigned leg rather than a silent expensive default.
    """
    candidates = cheap_image_candidates(conn)
    primary_a = _cheapest(candidates)
    avoid_a = (primary_a["provider_key"],) if primary_a else ()

    chosen: dict[str, dict[str, Any]] = {}
    for touchpoint in VISION_SEED_TOUCHPOINTS:
        if touchpoint == READ_B_TOUCHPOINT:
            primary = _cheapest(candidates, avoid_providers=avoid_a)
        else:
            primary = primary_a
        # The fallback must be on a DIFFERENT provider from its own primary,
        # or it provides no resilience when that provider is the thing failing.
        fallback = None
        if primary is not None:
            fallback = _cheapest(candidates, avoid_providers=(primary["provider_key"],))
        chosen[touchpoint] = {"primary": _leg(primary), "fallback": _leg(fallback)}
    return chosen


__all__ = [
    "VISION_SEED_TOUCHPOINTS",
    "READ_A_TOUCHPOINT",
    "READ_B_TOUCHPOINT",
    "EXCLUDED_VENDOR_PREFIXES",
    "FLAGSHIP_MARKERS",
    "is_image_capable",
    "is_excluded_default",
    "cheap_image_candidates",
    "choose_vision_defaults",
]
