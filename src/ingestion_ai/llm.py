"""LLM client for the unified AI ingestion layer.

This is the ONE place the ingestion layer talks to a model. It exists so
that the model/provider choice is a *configuration* concern (editable from
the frontend) rather than something baked into the mapping logic.

Provider: OpenRouter, reusing the same urllib-based transport as
src/ai_analysis.py (no third-party HTTP client — `requests` is not a
dependency of this PoC).

Model tier: the extraction layer feeds the reconciliation engine directly,
so a wrong column mapping here silently corrupts every downstream match.
Per the build prompt's explicit note, this layer therefore runs at the
same tier as the engine itself (Opus-tier), NOT a cheaper model. The
default lives in config/ai_config.yaml (`models.extraction`) and is
overridable from the frontend.

API key resolution order (first hit wins):
  1. A C4 vault credential explicitly selected for ingestion
     (settings key `ingestion_ai.llm.credential_id`). The plaintext is
     decrypted through C4's sanctioned point-of-use path, which logs a
     SecurityEvent for every retrieval — never stored or displayed here.
  2. The environment variable named by `openrouter.api_key_env` in
     config/ai_config.yaml.

There is deliberately NO silent fallback to a heuristic mapper: per the
locked decision for this build, a missing key/model is a loud, actionable
error. Callers surface it; they never quietly degrade to a guess.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "ai_config.yaml"

# Settings keys (flat key/value store, C3's `settings` table).
KEY_MODEL = "ingestion_ai.llm.model"
KEY_BASE_URL = "ingestion_ai.llm.base_url"
KEY_TIMEOUT = "ingestion_ai.llm.timeout_seconds"
KEY_MAX_TOKENS = "ingestion_ai.llm.max_tokens"
KEY_TEMPERATURE = "ingestion_ai.llm.temperature"
KEY_CREDENTIAL_ID = "ingestion_ai.llm.credential_id"

# Fallbacks used only when config/ai_config.yaml itself is unreadable.
_FALLBACK_MODEL = "anthropic/claude-opus-4.1"
_FALLBACK_TIMEOUT = 120
_FALLBACK_MAX_TOKENS = 2000
_FALLBACK_TEMPERATURE = 0.0


class LLMError(RuntimeError):
    """Raised for expected LLM-layer failures. The message is prefixed
    with a machine-readable type, e.g. ``config_error|...``,
    ``auth_error|...``, ``rate_limit|...``, ``timeout|...``,
    ``api_error|...``, ``parse_error|...`` — same convention as
    src/ai_analysis.py so callers can classify without string matching on
    prose."""


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def _load_ai_config() -> dict[str, Any]:
    """Read config/ai_config.yaml. Returns {} if unreadable — the caller
    falls back to the module-level defaults rather than failing, so a
    malformed config file can't make the whole ingestion layer unusable."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a flat setting via C3's public service API. Best-effort: any
    settings-layer failure yields the default rather than breaking the
    ingestion path."""
    try:
        from src.settings import service as settings

        return settings.get_setting_str(key, default=default)
    except Exception:  # noqa: BLE001
        return default


def _setting_float(key: str, default: float) -> float:
    raw = _setting(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _setting_int(key: str, default: int) -> int:
    raw = _setting(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _default_model() -> str:
    cfg = _load_ai_config()
    models = cfg.get("models") or {}
    return models.get("extraction") or models.get("complex") or _FALLBACK_MODEL


def _registry_model() -> Optional[str]:
    """The ingestion touchpoint's model from the central AI model registry.

    The registry (Setup → AI Models) is the single place every AI
    touchpoint's model is set. This layer reads it first, so a change made
    there takes effect here without a rewrite. Best-effort: any registry
    failure falls through to the settings key / config file, so a registry
    problem can never make ingestion unusable.
    """
    try:
        from src.ai_models import service as ai_models

        return ai_models.effective_model("ingestion_mapping")
    except Exception:  # noqa: BLE001
        return None


def _default_base_url() -> str:
    """A legacy display value only — the gateway owns the real transport.

    Prefers the config file's value, then the platform provider registry, so
    no provider URL is hardcoded outside src/ai_models/providers.py.
    """
    cfg = _load_ai_config()
    configured = (cfg.get("openrouter") or {}).get("base_url")
    if configured:
        return configured
    from src.ai_models import providers as prov

    return prov.PROVIDER_SPECS["openrouter"].chat_url


def _api_key_env_name() -> str:
    """The env var consulted for the ingestion provider's key (display only).

    Falls back to the platform provider registry rather than a hardcoded
    provider name, so no provider reference lives outside providers.py.
    """
    cfg = _load_ai_config()
    from src.ai_models import providers as prov

    return (
        (cfg.get("openrouter") or {}).get("api_key_env")
        or prov.PROVIDER_SPECS["openrouter"].api_key_env
        or ""
    )


def _resolve_api_key(*, db_path=None) -> tuple[str, str, Optional[str]]:
    """Return (api_key, key_source, masked_ref).

    ``key_source`` is ``"vault"`` or ``"env"``. Raises LLMError with a
    config_error prefix when neither is available — never returns an
    empty key.
    """
    credential_id = _setting(KEY_CREDENTIAL_ID)
    if credential_id:
        try:
            from src.vault import service as vault

            secret = vault.get_credential_secret_for_use(
                int(credential_id), actor="system:ingestion", purpose="AI ingestion column mapping",
                db_path=db_path,
            )
            if secret:
                return secret, "vault", None
        except Exception as exc:  # noqa: BLE001
            raise LLMError(
                f"config_error|The vault credential selected for AI ingestion could not be read: {exc}"
            ) from exc

    env_name = _api_key_env_name()
    key = os.environ.get(env_name, "")
    if key:
        return key, "env", None

    raise LLMError(
        f"config_error|No API key available. Set the {env_name!r} environment variable, "
        "or select a stored credential under Settings → AI Ingestion."
    )


def resolve_llm_config(*, db_path=None) -> dict[str, Any]:
    """Resolve the effective LLM configuration (settings override → config
    file → module default). Raises LLMError if no API key is available."""
    api_key, key_source, masked_ref = _resolve_api_key(db_path=db_path)
    return {
        "api_key": api_key,
        "key_source": key_source,
        "masked_ref": masked_ref,
        "base_url": _setting(KEY_BASE_URL) or _default_base_url(),
        "model": _setting(KEY_MODEL) or _registry_model() or _default_model(),
        "timeout_seconds": _setting_int(KEY_TIMEOUT, _FALLBACK_TIMEOUT),
        "max_tokens": _setting_int(KEY_MAX_TOKENS, _FALLBACK_MAX_TOKENS),
        "temperature": _setting_float(KEY_TEMPERATURE, _FALLBACK_TEMPERATURE),
    }


def llm_status(*, db_path=None) -> dict[str, Any]:
    """Non-raising status descriptor for the settings screen and for
    callers that want to explain *why* ingestion is unavailable without
    triggering an exception. Never returns the key itself.

    The provider + model are read from the central registry (the ingestion
    touchpoint's primary leg), so this reflects the multi-provider
    configuration rather than a legacy base-URL setting.
    """
    provider_key = "openrouter"
    model: Optional[str] = None
    masked_ref: Optional[str] = None
    try:
        from src.ai_models import service as ai_models

        leg = ai_models.effective_leg("ingestion_mapping", db_path=db_path)
        primary = (leg or {}).get("primary") or {}
        provider_key = primary.get("provider") or "openrouter"
        model = primary.get("model")
        row = ai_models.get_provider_row(provider_key, db_path=db_path) or {}
        masked_ref = row.get("masked_ref")
    except Exception:  # noqa: BLE001
        provider_key = "openrouter"

    if not model:
        model = _setting(KEY_MODEL) or _default_model()

    base_url = _setting(KEY_BASE_URL) or _default_base_url()
    try:
        from src.ai_models import providers as prov

        base_url = prov.get_spec(provider_key).chat_url
    except Exception:  # noqa: BLE001
        pass

    configured = False
    key_source: Optional[str] = None
    reason: Optional[str] = None
    try:
        from src.ai_models import service as ai_models

        configured, key_source = ai_models.provider_key_configured(provider_key, db_path=db_path)
        if not configured:
            reason = f"No API key is stored for the {provider_key} provider."
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)

    return {
        "configured": configured,
        "model": model,
        "provider": provider_key,
        "base_url": base_url,
        "key_source": key_source,
        "credential_id": _setting(KEY_CREDENTIAL_ID),
        "masked_ref": masked_ref,
        "env_var": _api_key_env_name(),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Frontend-editable configuration (Admin)
# ---------------------------------------------------------------------------


def set_llm_model(model: str, *, actor: str, db_path=None) -> None:
    if not model or not model.strip():
        raise LLMError("A model id is required.")
    from src.settings import service as settings

    settings.set_setting_str(KEY_MODEL, model.strip(), actor=actor, db_path=db_path)


def set_llm_base_url(base_url: str, *, actor: str, db_path=None) -> None:
    if not base_url or not base_url.strip():
        raise LLMError("A base URL is required.")
    from src.settings import service as settings

    settings.set_setting_str(KEY_BASE_URL, base_url.strip(), actor=actor, db_path=db_path)


def set_llm_credential(credential_id: Optional[int], *, actor: str, db_path=None) -> None:
    """Point the ingestion layer at a stored C4 credential (or clear it
    with None, falling back to the environment variable)."""
    from src.settings import service as settings

    settings.set_setting_str(
        KEY_CREDENTIAL_ID, "" if credential_id is None else str(int(credential_id)),
        actor=actor, db_path=db_path,
    )


def set_llm_limits(
    *, timeout_seconds: Optional[int] = None, max_tokens: Optional[int] = None,
    temperature: Optional[float] = None, actor: str, db_path=None,
) -> None:
    from src.settings import service as settings

    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise LLMError("Timeout must be a positive number of seconds.")
        settings.set_setting_str(KEY_TIMEOUT, str(int(timeout_seconds)), actor=actor, db_path=db_path)
    if max_tokens is not None:
        if max_tokens <= 0:
            raise LLMError("Max tokens must be positive.")
        settings.set_setting_str(KEY_MAX_TOKENS, str(int(max_tokens)), actor=actor, db_path=db_path)
    if temperature is not None:
        if not (0.0 <= temperature <= 2.0):
            raise LLMError("Temperature must be between 0.0 and 2.0.")
        settings.set_setting_str(KEY_TEMPERATURE, str(float(temperature)), actor=actor, db_path=db_path)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def _gateway_call(
    touchpoint_key: str,
    system_prompt: str,
    user_prompt: str,
    *,
    images: Optional[list[tuple[str, bytes]]] = None,
    model_override: Optional[str] = None,
    db_path=None,
) -> tuple[str, int, str]:
    """Call a touchpoint through the AI gateway.

    The gateway owns provider resolution and Primary→Fallback failover; this
    layer no longer knows a provider URL or auth scheme. Returns
    ``(text, latency_ms, model_id)``.

    ``TouchpointUnavailableError`` is translated into the same typed-prefix
    ``LLMError`` contract callers already handle.
    """
    from src.ai_models import gateway

    try:
        text, meta = gateway.call_touchpoint_text(
            touchpoint_key, system_prompt, user_prompt,
            images=images,
            max_tokens=_setting_int(KEY_MAX_TOKENS, _FALLBACK_MAX_TOKENS),
            temperature=_setting_float(KEY_TEMPERATURE, _FALLBACK_TEMPERATURE),
            actor="system:ingestion",
            timeout=_setting_int(KEY_TIMEOUT, _FALLBACK_TIMEOUT),
            model_override=model_override,
            db_path=db_path,
        )
    except gateway.TouchpointUnavailableError as exc:
        raise LLMError(exc.typed or f"config_error|{exc}") from exc
    return text, int(meta.get("latency_ms") or 0), str(meta.get("model") or "")


def call_llm(
    system_prompt: str, user_prompt: str, *, db_path=None,
    model_override: Optional[str] = None, touchpoint_key: str = "ingestion_mapping",
) -> tuple[str, int, str]:
    """Call the configured model with a plain-text prompt. Returns
    (raw_text, latency_ms, model_id)."""
    return _gateway_call(
        touchpoint_key, system_prompt, user_prompt,
        model_override=model_override, db_path=db_path,
    )


def call_llm_vision(
    system_prompt: str,
    user_prompt: str,
    images: list[tuple[str, bytes]],
    *,
    db_path=None,
    model_override: Optional[str] = None,
    touchpoint_key: str = "ingestion_mapping",
) -> tuple[str, int, str]:
    """Call a VISION-capable model with one or more images attached.

    ``images`` is a list of ``(mime_type, raw_bytes)``. The gateway translates
    the images into whatever shape the resolved provider accepts, so callers
    never build a provider-specific payload.

    Returns (raw_text, latency_ms, model_id). Raises LLMError on failure.
    """
    if not images:
        raise LLMError("config_error|call_llm_vision requires at least one image.")
    return _gateway_call(
        touchpoint_key, system_prompt, user_prompt, images=images,
        model_override=model_override, db_path=db_path,
    )


def call_llm_vision_json(
    system_prompt: str,
    user_prompt: str,
    images: list[tuple[str, bytes]],
    *,
    db_path=None,
    model_override: Optional[str] = None,
    touchpoint_key: str = "ingestion_mapping",
) -> tuple[dict[str, Any], int, str, str]:
    """Vision call + JSON parse. Returns (parsed, latency_ms, model_id, raw)."""
    content, latency_ms, model_id = call_llm_vision(
        system_prompt, user_prompt, images, db_path=db_path,
        model_override=model_override, touchpoint_key=touchpoint_key,
    )
    parsed = parse_json_response(content)
    return parsed, latency_ms, model_id, content


def extract_json_object(text: str) -> str:
    """Strip markdown fences and return the outermost balanced {...} block.

    Same tolerant extraction as src/ai_analysis.py — models occasionally
    wrap JSON in prose or fences despite instructions.
    """
    if not text:
        raise LLMError("parse_error|empty response")
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        first_newline = s.find("\n")
        if first_newline != -1:
            head = s[:first_newline].strip().lower()
            if head in ("json", "javascript", ""):
                s = s[first_newline + 1 :]
    start = s.find("{")
    if start == -1:
        raise LLMError("parse_error|no JSON object found in response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    raise LLMError("parse_error|unbalanced JSON object in response")


def parse_json_response(raw_text: str) -> dict[str, Any]:
    """Extract + parse the model's JSON object. Raises LLMError on failure."""
    obj_str = extract_json_object(raw_text)
    try:
        obj = json.loads(obj_str)
    except (TypeError, ValueError) as e:
        raise LLMError(f"parse_error|{e}") from e
    if not isinstance(obj, dict):
        raise LLMError("parse_error|parsed JSON is not an object")
    return obj


def _extract_message_content(raw: str) -> str:
    """Pull the assistant message text out of an OpenRouter chat-completion
    response envelope."""
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise LLMError(f"parse_error|response envelope is not JSON: {e}") from e
    if isinstance(envelope, dict) and envelope.get("error"):
        raise LLMError(f"api_error|{envelope['error']}")
    try:
        return envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"parse_error|unexpected response shape: {e}") from e


def call_llm_json(
    system_prompt: str, user_prompt: str, *, db_path=None,
    model_override: Optional[str] = None, touchpoint_key: str = "ingestion_mapping",
) -> tuple[dict[str, Any], int, str, str]:
    """Call the model and parse its JSON reply.

    Returns (parsed_object, latency_ms, model_id, raw_text). Raises
    LLMError on transport, envelope, or JSON-parse failure.
    """
    raw, latency_ms, model_id = call_llm(
        system_prompt, user_prompt, db_path=db_path,
        model_override=model_override, touchpoint_key=touchpoint_key,
    )
    parsed = parse_json_response(raw)
    return parsed, latency_ms, model_id, raw