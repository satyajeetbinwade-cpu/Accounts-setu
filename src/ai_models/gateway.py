"""The AI call gateway — touchpoint → legs → provider, WITH real failover.

Every AI call in the platform resolves through here rather than picking a
model itself. This is what makes a per-touchpoint Primary/Fallback assignment
real instead of cosmetic:

  * resolve the touchpoint's two legs (provider + model, independently set),
  * call the Primary leg through its provider adapter,
  * on a provider failure (auth / rate-limit / timeout / API error) — and only
    then — retry the Fallback leg, which MAY be a different provider,
  * record a fallback event naming the provider that actually resolved it,
  * only when BOTH legs fail, raise ``TouchpointUnavailableError`` so the
    caller degrades to "AI unavailable — proceed manually."

Provider isolation is structural: a failure is scoped to the one provider it
came from. One provider's bad key can never make the platform-wide AI layer
unavailable — at worst it degrades the touchpoints that named it.

Consumers (src/ingestion_ai/llm.py, src/ai_analysis.py,
src/invoice_extract/ai_extractor.py, src/module2/service.py) call
``call_touchpoint_text`` / ``call_touchpoint_json`` and never hardcode a
provider, URL, auth header, or response shape.
"""

from __future__ import annotations

from typing import Any, Optional

from src.ai_models import providers as prov
from src.ai_models import service as ai_models

_DEFAULT_TIMEOUT = 120


class TouchpointUnavailableError(RuntimeError):
    """Both legs of a touchpoint are unavailable — the caller must degrade to
    "AI unavailable — proceed manually." ``typed`` carries the last provider
    failure in the ``type|detail`` convention so callers can preserve its
    classification (e.g. ``auth_error|``)."""

    def __init__(self, message: str, *, typed: Optional[str] = None) -> None:
        super().__init__(message)
        self.typed = typed


def _legs_for(
    touchpoint_key: str, *, model_override: Optional[str] = None, db_path=None,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """Return (primary_leg, [(leg_name, target), ...]) where the candidate list
    is the primary first and the fallback second (only when it is genuinely a
    different (provider, model) pair).

    ``model_override`` pins the primary leg's model while keeping the
    touchpoint's configured provider — used only by legacy callers that pass an
    explicit model id.
    """
    tp = ai_models.effective_leg(touchpoint_key, db_path=db_path)
    if tp is None:
        raise TouchpointUnavailableError(f"Unknown touchpoint '{touchpoint_key}'.")
    if not tp["is_active"]:
        raise TouchpointUnavailableError(f"Touchpoint '{touchpoint_key}' is not active.")
    primary = dict(tp["primary"])
    if model_override:
        primary["model"] = model_override
    fallback = tp["fallback"]
    candidates: list[tuple[str, dict[str, Any]]] = [("primary", primary)]
    if fallback.get("model") and (fallback["provider"], fallback["model"]) != (
        primary["provider"], primary["model"]
    ):
        candidates.append(("fallback", fallback))
    return primary, candidates


def _call_leg(
    target: dict[str, Any],
    *,
    system_prompt: str,
    user_prompt: str,
    images: Optional[list[tuple[str, bytes]]],
    max_tokens: int,
    temperature: float,
    actor: Optional[str],
    purpose: str,
    timeout: int,
    db_path=None,
) -> tuple[str, int]:
    """Call one leg through its provider adapter. Raises ProviderError (typed)
    on failure, or AIModelsError when the leg has no usable credential."""
    provider_key = target["provider"]
    model = target["model"]
    spec = prov.get_spec(provider_key)  # ProviderError on an unknown provider
    api_key = ai_models.resolve_provider_secret(
        provider_key, actor=actor or "system:ai_gateway", purpose=purpose, db_path=db_path,
    )
    return prov.call_provider(
        spec, api_key, model=model, system=system_prompt, user=user_prompt,
        images=images, max_tokens=max_tokens, temperature=temperature, timeout=timeout,
    )


def call_touchpoint_text(
    touchpoint_key: str,
    system_prompt: str,
    user_prompt: str,
    *,
    images: Optional[list[tuple[str, bytes]]] = None,
    max_tokens: int = 2000,
    temperature: float = 0.0,
    actor: Optional[str] = None,
    client_id: Optional[int] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    model_override: Optional[str] = None,
    db_path=None,
) -> tuple[str, dict[str, Any]]:
    """Call a touchpoint, failing over from Primary to Fallback.

    Returns ``(text, meta)`` where meta carries the resolved provider + model,
    the latency, and whether a fallback was used. Raises
    ``TouchpointUnavailableError`` when both legs are unavailable.
    """
    primary, candidates = _legs_for(
        touchpoint_key, model_override=model_override, db_path=db_path,
    )
    errors: list[str] = []
    first_typed: Optional[str] = None

    for index, (leg_name, target) in enumerate(candidates):
        if not target.get("model"):
            errors.append(f"{leg_name}: no model assigned")
            continue
        try:
            text, latency_ms = _call_leg(
                target,
                system_prompt=system_prompt, user_prompt=user_prompt, images=images,
                max_tokens=max_tokens, temperature=temperature, actor=actor,
                purpose=f"{touchpoint_key} ({leg_name} leg)", timeout=timeout, db_path=db_path,
            )
        except (prov.ProviderError, ai_models.AIModelsError) as exc:
            typed = str(exc)
            if first_typed is None:
                first_typed = typed
            errors.append(f"{leg_name}:{typed}")
            continue

        fallback_used = index > 0
        meta: dict[str, Any] = {
            "touchpoint_key": touchpoint_key,
            "provider": target["provider"],
            "model": target["model"],
            "leg": leg_name,
            "latency_ms": latency_ms,
            "fallback_used": fallback_used,
            "primary_provider": primary["provider"],
            "primary_model": primary["model"],
        }
        if fallback_used:
            # Record WHICH provider the fallback actually resolved to.
            ai_models.record_fallback_event(
                touchpoint_key=touchpoint_key, leg="primary",
                primary_provider=primary["provider"], primary_model=primary["model"],
                resolved_provider=target["provider"], resolved_model=target["model"],
                reason=errors[0] if errors else None, latency_ms=latency_ms,
                actor=actor, client_id=client_id, db_path=db_path,
            )
        return text, meta

    raise TouchpointUnavailableError(
        f"AI unavailable — proceed manually. {touchpoint_key} could not be reached "
        f"on either leg ({'; '.join(errors) or 'no legs configured'}).",
        typed=first_typed,
    )


def call_touchpoint_json(
    touchpoint_key: str,
    system_prompt: str,
    user_prompt: str,
    *,
    images: Optional[list[tuple[str, bytes]]] = None,
    max_tokens: int = 2000,
    temperature: float = 0.0,
    actor: Optional[str] = None,
    client_id: Optional[int] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    model_override: Optional[str] = None,
    db_path=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call a touchpoint and parse a JSON object reply.

    Returns ``(parsed, meta)``. Raises ``TouchpointUnavailableError`` when both
    legs are unavailable, and the LLM layer's ``parse_error`` when the reply
    cannot be parsed (a parse failure is NOT a leg failure — it is not retried
    against the fallback, since the model was reached).
    """
    text, meta = call_touchpoint_text(
        touchpoint_key, system_prompt, user_prompt,
        images=images, max_tokens=max_tokens, temperature=temperature,
        actor=actor, client_id=client_id, timeout=timeout,
        model_override=model_override, db_path=db_path,
    )
    # Lazy import avoids a circular import (llm.py imports this module).
    from src.ingestion_ai import llm as _llm

    parsed = _llm.parse_json_response(text)
    meta = {**meta, "raw_text": text}
    return parsed, meta
