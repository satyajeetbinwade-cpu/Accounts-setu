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

import random
import threading
import time
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
    usage_sink: Optional[dict[str, Any]] = None,
) -> tuple[str, int]:
    """Call one leg through its provider adapter. Raises ProviderError (typed)
    on failure, or AIModelsError when the leg has no usable credential.

    A vision call (``images`` present) is paced by the global vision semaphore
    (``ai.vision.max_concurrency``) so a page-level thread pool cannot exceed
    the configured number of simultaneous model calls.
    """
    provider_key = target["provider"]
    model = target["model"]
    spec = prov.get_spec(provider_key)  # ProviderError on an unknown provider
    api_key = ai_models.resolve_provider_secret(
        provider_key, actor=actor or "system:ai_gateway", purpose=purpose, db_path=db_path,
    )
    # Install the usage sink for the duration of THIS call only. ``call_provider``'s
    # signature is unchanged (a test double that never fills the sink is fine).
    prov.push_usage_sink(usage_sink)
    try:
        if images:
            with _vision_semaphore(db_path):
                return prov.call_provider(
                    spec, api_key, model=model, system=system_prompt, user=user_prompt,
                    images=images, max_tokens=max_tokens, temperature=temperature, timeout=timeout,
                )
        return prov.call_provider(
            spec, api_key, model=model, system=system_prompt, user=user_prompt,
            images=images, max_tokens=max_tokens, temperature=temperature, timeout=timeout,
        )
    finally:
        prov.pop_usage_sink()


# ---------------------------------------------------------------------------
# Optional limits: global vision concurrency + per-leg retry policy
# ---------------------------------------------------------------------------

_VISION_CONCURRENCY_KEY = "ai.vision.max_concurrency"
_VISION_ALLOWLIST_KEY = "ai.vision.model_allowlist"
_DEFAULT_VISION_CONCURRENCY = 4

_vision_sem_lock = threading.Lock()
_vision_sem: Optional[threading.BoundedSemaphore] = None
_vision_sem_size: Optional[int] = None


def _setting_int(key: str, default: int, db_path) -> int:
    try:
        from src.settings import service as settings

        return int(settings.get_setting_int(key, default=default, db_path=db_path))
    except Exception:  # noqa: BLE001
        return default


def _setting_str(key: str, db_path) -> Optional[str]:
    try:
        from src.settings import service as settings

        return settings.get_setting_str(key, default=None, db_path=db_path)
    except Exception:  # noqa: BLE001
        return None


def _vision_semaphore(db_path) -> threading.BoundedSemaphore:
    """The process-wide vision-call semaphore, sized from settings (default 4).

    Rebuilt only when the configured size changes, so every caller shares ONE
    limiter regardless of the db_path it happens to pass.
    """
    global _vision_sem, _vision_sem_size
    size = max(1, _setting_int(_VISION_CONCURRENCY_KEY, _DEFAULT_VISION_CONCURRENCY, db_path))
    with _vision_sem_lock:
        if _vision_sem is None or _vision_sem_size != size:
            _vision_sem = threading.BoundedSemaphore(size)
            _vision_sem_size = size
        return _vision_sem


def _vision_allowed(provider: str, model: str, db_path) -> tuple[bool, str]:
    """Can this model receive images? (Step D — the vision capability guard.)

    The catalog DOES store modalities (``ai_provider_models.modality`` /
    ``ai_model_catalog.modality``), so the primary check reads them: a model
    the catalogue says is text-only is REFUSED. An UNKNOWN modality is allowed
    (we cannot prove it is text-only). A configurable allowlist
    (``ai.vision.model_allowlist``, comma-separated) is additionally enforced
    when set, as a belt-and-braces override.
    """
    allowlist = (_setting_str(_VISION_ALLOWLIST_KEY, db_path) or "").strip()
    if allowlist:
        allowed = {m.strip() for m in allowlist.split(",") if m.strip()}
        if model not in allowed:
            return False, (
                f"model '{model}' is not in the vision allowlist (ai.vision.model_allowlist)"
            )
    try:
        modality = ai_models.model_modality(provider, model, db_path=db_path)
    except Exception:  # noqa: BLE001
        modality = None
    if prov.supports_images(modality) is False:
        return False, (
            f"model '{model}' ({provider}) does not accept image input "
            f"(catalog modality {modality!r})"
        )
    return True, ""


def _normalize_policy(policy: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """None (the default) = today's behaviour: ONE attempt per leg."""
    if not policy:
        return None
    return {
        "attempts": max(1, int(policy.get("attempts", 3))),
        "base_delay": float(policy.get("base_delay", 1.0)),
        "max_delay": float(policy.get("max_delay", 20.0)),
        "jitter": float(policy.get("jitter", 0.5)),
    }


def _retryable(typed: Optional[str]) -> bool:
    """429 / timeout / 5xx are transient; auth & 4xx are not."""
    if not typed:
        return False
    if typed.startswith("rate_limit|") or typed.startswith("timeout|"):
        return True
    return typed.startswith("api_error|HTTP 5")


def _backoff_seconds(policy: dict[str, Any], attempt: int, retry_after: Optional[float]) -> float:
    if retry_after is not None:
        return max(0.0, min(float(retry_after), policy["max_delay"]))
    delay = min(policy["base_delay"] * (2 ** (attempt - 1)), policy["max_delay"])
    return delay + random.uniform(0.0, policy["jitter"])


def _parse_json_reply(text: str) -> dict[str, Any]:
    from src.ingestion_ai import llm as _llm   # lazy: avoids a circular import

    return _llm.parse_json_response(text)


def _log_call(
    *,
    touchpoint_key: str,
    upload_id: Optional[int],
    provider: Optional[str],
    model: Optional[str],
    usage: dict[str, Any],
    latency_ms: Optional[int],
    outcome: str,
    attempt: int,
    reference_type: Optional[str],
    reference_id: Optional[int],
    actor: Optional[str],
    db_path=None,
) -> None:
    """Best-effort side effect: one ``ai_call_log`` row for ONE attempt.

    Deliberately swallows EVERY error — token/cost accounting is an audit
    convenience, never a reason for a model call to fail or change its result.
    """
    try:
        ai_models.log_ai_call(
            touchpoint_key=touchpoint_key, provider=provider, model=model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            latency_ms=latency_ms, outcome=outcome, attempt=attempt,
            upload_id=upload_id, reference_type=reference_type,
            reference_id=reference_id, actor=actor, db_path=db_path,
        )
    except Exception:  # noqa: BLE001 — accounting must never break a call
        pass


def _call_with_policy(
    touchpoint_key: str,
    system_prompt: str,
    user_prompt: str,
    *,
    parser: Optional[Any],
    images: Optional[list[tuple[str, bytes]]],
    max_tokens: int,
    temperature: float,
    actor: Optional[str],
    client_id: Optional[int],
    timeout: int,
    model_override: Optional[str],
    reference_type: Optional[str],
    reference_id: Optional[int],
    upload_id: Optional[int],
    retry_policy: Optional[dict[str, Any]],
    db_path,
) -> tuple[Any, dict[str, Any]]:
    """Legs → (retry per leg) → provider → parse, logging EVERY attempt."""
    primary, candidates = _legs_for(
        touchpoint_key, model_override=model_override, db_path=db_path,
    )
    policy = _normalize_policy(retry_policy)
    vision = bool(images)
    errors: list[str] = []
    first_typed: Optional[str] = None

    for index, (leg_name, target) in enumerate(candidates):
        if not target.get("model"):
            errors.append(f"{leg_name}: no model assigned")
            continue
        provider = target["provider"]
        model = target["model"]

        # Vision capability guard — refuse, never silently drop the images.
        if vision:
            allowed, why = _vision_allowed(provider, model, db_path)
            if not allowed:
                _log_call(
                    touchpoint_key=touchpoint_key, upload_id=upload_id, provider=provider,
                    model=model, usage={}, latency_ms=None, outcome="rejected", attempt=1,
                    reference_type=reference_type, reference_id=reference_id,
                    actor=actor, db_path=db_path,
                )
                typed = f"config_error|{why}"
                if first_typed is None:
                    first_typed = typed
                errors.append(f"{leg_name}:{typed}")
                continue

        attempts = policy["attempts"] if policy else 1
        for attempt in range(1, attempts + 1):
            usage: dict[str, Any] = {}
            try:
                text, latency_ms = _call_leg(
                    target,
                    system_prompt=system_prompt, user_prompt=user_prompt, images=images,
                    max_tokens=max_tokens, temperature=temperature, actor=actor,
                    purpose=f"{touchpoint_key} ({leg_name} leg)", timeout=timeout,
                    db_path=db_path, usage_sink=usage,
                )
            except (prov.ProviderError, ai_models.AIModelsError) as exc:
                typed = str(exc)
                _log_call(
                    touchpoint_key=touchpoint_key, upload_id=upload_id, provider=provider,
                    model=model, usage=usage, latency_ms=None, outcome="transport_error",
                    attempt=attempt, reference_type=reference_type,
                    reference_id=reference_id, actor=actor, db_path=db_path,
                )
                if first_typed is None:
                    first_typed = typed
                errors.append(f"{leg_name}#{attempt}:{typed}")
                if policy and _retryable(typed) and attempt < attempts:
                    retry_after = prov.take_retry_after()
                    time.sleep(_backoff_seconds(policy, attempt, retry_after))
                    continue
                break

            # Provider returned. Parse if this call wants a JSON object —
            # a billed reply that will not parse is logged (with its tokens)
            # as parse_error, NOT silently dropped, and is NOT failed over.
            result: Any = text
            if parser is not None:
                try:
                    result = parser(text)
                except Exception as exc:  # noqa: BLE001
                    typed = str(exc)
                    if not typed.startswith("parse_error|"):
                        typed = f"parse_error|{exc}"
                    _log_call(
                        touchpoint_key=touchpoint_key, upload_id=upload_id, provider=provider,
                        model=model, usage=usage, latency_ms=latency_ms, outcome="parse_error",
                        attempt=attempt, reference_type=reference_type,
                        reference_id=reference_id, actor=actor, db_path=db_path,
                    )
                    if first_typed is None:
                        first_typed = typed
                    raise

            outcome = "fallback_used" if index > 0 else "success"
            _log_call(
                touchpoint_key=touchpoint_key, upload_id=upload_id, provider=provider,
                model=model, usage=usage, latency_ms=latency_ms, outcome=outcome,
                attempt=attempt, reference_type=reference_type,
                reference_id=reference_id, actor=actor, db_path=db_path,
            )
            meta: dict[str, Any] = {
                "touchpoint_key": touchpoint_key,
                "provider": provider,
                "model": model,
                "leg": leg_name,
                "latency_ms": latency_ms,
                "fallback_used": index > 0,
                "primary_provider": primary["provider"],
                "primary_model": primary["model"],
                "usage": dict(usage),
                "attempt": attempt,
                "outcome": outcome,
            }
            if parser is not None:
                meta["raw_text"] = text
            if index > 0:
                ai_models.record_fallback_event(
                    touchpoint_key=touchpoint_key, leg="primary",
                    primary_provider=primary["provider"], primary_model=primary["model"],
                    resolved_provider=provider, resolved_model=model,
                    reason=errors[0] if errors else None, latency_ms=latency_ms,
                    actor=actor, client_id=client_id, db_path=db_path,
                )
            return result, meta

    raise TouchpointUnavailableError(
        f"AI unavailable — proceed manually. {touchpoint_key} could not be reached "
        f"on either leg ({'; '.join(errors) or 'no legs configured'}).",
        typed=first_typed,
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
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
    upload_id: Optional[int] = None,
    retry_policy: Optional[dict[str, Any]] = None,
    db_path=None,
) -> tuple[str, dict[str, Any]]:
    """Call a touchpoint, failing over from Primary to Fallback.

    Returns ``(text, meta)`` where meta carries the resolved provider + model,
    the latency, whether a fallback was used, and — when the provider reported
    it — the token ``usage``.

    ``reference_type`` / ``reference_id`` / ``upload_id`` are optional, purely
    additive pointers at the calling record so the token/cost side effect can
    be attributed. ``retry_policy`` (None by default) turns on same-leg retries
    for 429 / timeout / 5xx with exponential backoff before failover; existing
    callers leave it None and keep today's one-attempt-per-leg behaviour.

    Raises ``TouchpointUnavailableError`` when both legs are unavailable.
    """
    return _call_with_policy(
        touchpoint_key, system_prompt, user_prompt, parser=None, images=images,
        max_tokens=max_tokens, temperature=temperature, actor=actor, client_id=client_id,
        timeout=timeout, model_override=model_override, reference_type=reference_type,
        reference_id=reference_id, upload_id=upload_id, retry_policy=retry_policy,
        db_path=db_path,
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
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
    upload_id: Optional[int] = None,
    retry_policy: Optional[dict[str, Any]] = None,
    db_path=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call a touchpoint and parse a JSON object reply.

    Returns ``(parsed, meta)``. Raises ``TouchpointUnavailableError`` when both
    legs are unavailable, and the LLM layer's ``parse_error`` when the reply
    cannot be parsed (a parse failure is NOT a leg failure — it is not retried
    against the fallback, since the model WAS reached and billed; it is logged
    with outcome ``parse_error`` and its tokens).
    """
    return _call_with_policy(
        touchpoint_key, system_prompt, user_prompt, parser=_parse_json_reply,
        images=images, max_tokens=max_tokens, temperature=temperature, actor=actor,
        client_id=client_id, timeout=timeout, model_override=model_override,
        reference_type=reference_type, reference_id=reference_id, upload_id=upload_id,
        retry_policy=retry_policy, db_path=db_path,
    )
