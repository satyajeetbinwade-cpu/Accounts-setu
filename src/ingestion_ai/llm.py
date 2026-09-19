"""LLM client for the unified AI ingestion layer.

This is the ONE place the ingestion layer talks to a model. It exists so
that the model/provider choice is a *configuration* concern (editable from
the frontend — see src/ui/ai_settings_tab.py) rather than something baked
into the mapping logic.

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
_FALLBACK_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
_FALLBACK_MODEL = "anthropic/claude-opus-4.1"
_FALLBACK_API_KEY_ENV = "OPENROUTER_API_KEY"
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
    cfg = _load_ai_config()
    return (cfg.get("openrouter") or {}).get("base_url") or _FALLBACK_BASE_URL


def _api_key_env_name() -> str:
    cfg = _load_ai_config()
    return (cfg.get("openrouter") or {}).get("api_key_env") or _FALLBACK_API_KEY_ENV


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
    triggering an exception. Never returns the key itself."""
    model = _setting(KEY_MODEL) or _registry_model() or _default_model()
    base_url = _setting(KEY_BASE_URL) or _default_base_url()
    credential_id = _setting(KEY_CREDENTIAL_ID)
    masked_ref: Optional[str] = None
    if credential_id:
        try:
            from src.vault import service as vault

            for row in vault.list_credentials(db_path=db_path):
                if str(row["credential_id"]) == str(credential_id):
                    masked_ref = row["masked_ref"]
                    break
        except Exception:  # noqa: BLE001
            masked_ref = None

    try:
        _api_key, key_source, _ = _resolve_api_key(db_path=db_path)
        configured = True
        reason = None
    except LLMError as exc:
        key_source = None
        configured = False
        reason = str(exc).split("|", 1)[-1]

    return {
        "configured": configured,
        "model": model,
        "base_url": base_url,
        "key_source": key_source,
        "credential_id": int(credential_id) if credential_id else None,
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


def _post_chat(payload: dict[str, Any], *, db_path=None) -> tuple[str, int, str]:
    """Shared transport for every chat-completion call (text or vision).

    Resolves config, POSTs the payload, and returns
    ``(raw_text, latency_ms, model_id)``. Raises LLMError (typed prefix)
    on any failure. The API key is never logged, stored, or returned.
    """
    cfg = resolve_llm_config(db_path=db_path)
    model_id = payload.get("model") or cfg["model"]
    payload = {**payload, "model": model_id}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"],
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        code = e.code
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        if code in (401, 403):
            raise LLMError(f"auth_error|HTTP {code} {body}") from e
        if code == 429:
            raise LLMError(f"rate_limit|HTTP {code} {body}") from e
        raise LLMError(f"api_error|HTTP {code} {body}") from e
    except (TimeoutError, socket.timeout) as e:
        raise LLMError(f"timeout|request exceeded {cfg['timeout_seconds']}s: {e}") from e
    except (urllib.error.URLError, OSError) as e:
        raise LLMError(f"api_error|{e.__class__.__name__}: {e}") from e

    latency_ms = int((time.monotonic() - start) * 1000)
    return raw, latency_ms, model_id


def call_llm(
    system_prompt: str, user_prompt: str, *, db_path=None, model_override: Optional[str] = None,
) -> tuple[str, int, str]:
    """Call the configured model with a plain-text prompt. Returns
    (raw_text, latency_ms, model_id)."""
    cfg = resolve_llm_config(db_path=db_path)
    payload = {
        "model": model_override or cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
    }
    return _post_chat(payload, db_path=db_path)


def call_llm_vision(
    system_prompt: str,
    user_prompt: str,
    images: list[tuple[str, bytes]],
    *,
    db_path=None,
    model_override: Optional[str] = None,
) -> tuple[str, int, str]:
    """Call a VISION-capable model with one or more images attached.

    ``images`` is a list of ``(mime_type, raw_bytes)`` — e.g.
    ``("image/png", png_bytes)``. Each image is sent as an OpenRouter
    ``image_url`` content part with a base64 data URI, which is the
    provider-agnostic shape every vision model on OpenRouter accepts.

    Returns (raw_text, latency_ms, model_id). Raises LLMError on failure.
    """
    if not images:
        raise LLMError("config_error|call_llm_vision requires at least one image.")

    cfg = resolve_llm_config(db_path=db_path)
    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for mime, raw in images:
        b64 = base64.b64encode(raw).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    payload = {
        "model": model_override or cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
    }
    return _post_chat(payload, db_path=db_path)


def call_llm_vision_json(
    system_prompt: str,
    user_prompt: str,
    images: list[tuple[str, bytes]],
    *,
    db_path=None,
    model_override: Optional[str] = None,
) -> tuple[dict[str, Any], int, str, str]:
    """Vision call + JSON parse. Returns (parsed, latency_ms, model_id, raw)."""
    raw, latency_ms, model_id = call_llm_vision(
        system_prompt, user_prompt, images, db_path=db_path, model_override=model_override
    )
    content = _extract_message_content(raw)
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
    system_prompt: str, user_prompt: str, *, db_path=None, model_override: Optional[str] = None,
) -> tuple[dict[str, Any], int, str, str]:
    """Call the model and parse its JSON reply.

    Returns (parsed_object, latency_ms, model_id, raw_text). Raises
    LLMError on transport, envelope, or JSON-parse failure.
    """
    raw, latency_ms, model_id = call_llm(
        system_prompt, user_prompt, db_path=db_path, model_override=model_override
    )
    content = _extract_message_content(raw)
    parsed = parse_json_response(content)
    return parsed, latency_ms, model_id, content