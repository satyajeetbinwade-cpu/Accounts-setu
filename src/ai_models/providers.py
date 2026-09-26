"""Platform-defined AI providers — the ONE place a provider's transport lives.

Fixed set for this build: **OpenRouter, Anthropic, Deepseek, Sarvam**. Admin
selects among these on the AI Models screen; registering an arbitrary fifth
provider is explicitly deferred, so this module owns the platform's provider
definitions rather than a config table an Admin edits.

Why this module exists
-----------------------
Before the multi-provider extension the whole platform talked to OpenRouter:
``src/ingestion_ai/llm.py`` hardcoded ``https://openrouter.ai/api/v1/chat/completions``
and a Bearer header, and ``src/ai_analysis.py`` had its own ``call_openrouter``.
A per-provider transport is now described once, here:

  * where to POST (``chat_url``),
  * how to authenticate (``auth_style``: ``bearer`` or ``x-api-key``),
  * how to build the request and parse the response (each provider's envelope
    differs — Anthropic uses a top-level ``system`` field and returns
    ``content[].text``, OpenAI-compatible providers use ``messages`` and return
    ``choices[0].message.content``),
  * whether a live model catalogue exists (``catalog_mode``).

No other module may hardcode a provider URL, auth header, or response shape.
``build_chat_request()`` / ``parse_chat_response()`` are the translation seam.

Errors use the same machine-readable ``type|detail`` prefix convention as
``src/ingestion_ai/llm.py`` (``auth_error|``, ``rate_limit|``, ``timeout|``,
``api_error|``, ``parse_error|``, ``config_error|``) so callers can classify a
failure without matching on prose — the gateway's failover decision depends on
this.
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

_CATALOG_TIMEOUT = 25
_PROBE_TIMEOUT = 20

# Anthropic requires an explicit API version header on every request.
_ANTHROPIC_VERSION = "2023-06-01"


class ProviderError(RuntimeError):
    """A provider transport failure, prefixed with a machine-readable type."""


@dataclass(frozen=True)
class ProviderSpec:
    """Everything needed to talk to one provider."""

    key: str
    label: str
    kind: str  # 'openai_compatible' | 'anthropic' | 'sarvam'
    chat_url: str
    auth_style: str  # 'bearer' | 'x-api-key'
    catalog_mode: str  # 'live' | 'static'
    catalog_url: Optional[str] = None
    static_models: list[dict[str, Any]] = field(default_factory=list)
    sort_order: int = 0
    api_key_env: Optional[str] = None

    @property
    def has_live_catalog(self) -> bool:
        return self.catalog_mode == "live" and bool(self.catalog_url)

    def to_row(self) -> dict[str, Any]:
        """The shape seeded into / read from the ``ai_providers`` table."""
        return {
            "provider_key": self.key,
            "label": self.label,
            "kind": self.kind,
            "chat_url": self.chat_url,
            "catalog_url": self.catalog_url,
            "catalog_mode": self.catalog_mode,
            "auth_style": self.auth_style,
            "static_models": json.dumps(self.static_models),
            "is_enabled": 1,
            "sort_order": self.sort_order,
        }


# ---------------------------------------------------------------------------
# The platform's fixed provider set
# ---------------------------------------------------------------------------

# Sarvam does not expose a discoverable model-list endpoint as of this build,
# so its catalogue is a short static starter list plus manual model-string
# entry on the AI Models screen (Admin-maintained). CONFIRM the live-endpoint
# state at build time and switch catalog_mode to 'live' if one exists.
#
# Sarvam retires model ids WITHOUT notice: 'sarvam-m' was deprecated and the
# API now rejects it with "Model 'sarvam-m' has been deprecated. Please use one
# of the available models instead: sarvam-105b, sarvam-105b-conversations."
# That arrives as a typed api_error which the gateway (correctly) does NOT
# retry — a retired id is a request problem, not a provider outage. Keep this
# list to ids the API currently accepts, because a stale id here becomes the
# DEFAULT the dropdown offers AND the id the provider-switch handler auto-selects
# — so one retired entry breaks every Sarvam leg at once.
_SARVAM_STATIC_MODELS: list[dict[str, Any]] = [
    {"model_id": "sarvam-105b", "name": "Sarvam 105B", "modality": "text->text"},
    {"model_id": "sarvam-105b-conversations", "name": "Sarvam 105B (conversations)", "modality": "text->text"},
]

PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        key="openrouter",
        label="OpenRouter",
        kind="openai_compatible",
        chat_url="https://openrouter.ai/api/v1/chat/completions",
        auth_style="bearer",
        catalog_mode="live",
        catalog_url="https://openrouter.ai/api/v1/models",
        sort_order=1,
        api_key_env="OPENROUTER_API_KEY",
    ),
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic",
        kind="anthropic",
        chat_url="https://api.anthropic.com/v1/messages",
        auth_style="x-api-key",
        catalog_mode="live",
        catalog_url="https://api.anthropic.com/v1/models",
        sort_order=2,
        api_key_env="ANTHROPIC_API_KEY",
    ),
    "deepseek": ProviderSpec(
        key="deepseek",
        label="Deepseek",
        kind="openai_compatible",
        chat_url="https://api.deepseek.com/chat/completions",
        auth_style="bearer",
        catalog_mode="live",
        catalog_url="https://api.deepseek.com/models",
        sort_order=3,
        api_key_env="DEEPSEEK_API_KEY",
    ),
    "sarvam": ProviderSpec(
        key="sarvam",
        label="Sarvam",
        kind="openai_compatible",
        chat_url="https://api.sarvam.ai/v1/chat/completions",
        auth_style="bearer",
        catalog_mode="static",
        catalog_url=None,
        static_models=_SARVAM_STATIC_MODELS,
        sort_order=4,
        api_key_env="SARVAM_API_KEY",
    ),
}

PROVIDER_KEYS: list[str] = ["openrouter", "anthropic", "deepseek", "sarvam"]


def get_spec(provider_key: str) -> ProviderSpec:
    spec = PROVIDER_SPECS.get((provider_key or "").strip().lower())
    if spec is None:
        raise ProviderError(f"config_error|Unknown provider '{provider_key}'.")
    return spec


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — `requests` is not a dependency of this PoC)
# ---------------------------------------------------------------------------


def _auth_headers(spec: ProviderSpec, api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if spec.auth_style == "x-api-key":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = _ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _http_get_json(url: str, headers: dict[str, str], *, timeout: int) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        if exc.code in (401, 403):
            raise ProviderError(f"auth_error|HTTP {exc.code} {detail}") from exc
        if exc.code == 429:
            raise ProviderError(f"rate_limit|HTTP {exc.code} {detail}") from exc
        raise ProviderError(f"api_error|HTTP {exc.code} {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderError(f"timeout|request exceeded {timeout}s: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError(f"api_error|{exc.__class__.__name__}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError(f"parse_error|response was not valid JSON: {exc}") from exc


def _http_post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], *, timeout: int,
) -> tuple[str, int]:
    """POST a JSON payload. Returns ``(raw_text, latency_ms)``."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc)
        if exc.code in (401, 403):
            raise ProviderError(f"auth_error|HTTP {exc.code} {detail}") from exc
        if exc.code == 429:
            raise ProviderError(f"rate_limit|HTTP {exc.code} {detail}") from exc
        raise ProviderError(f"api_error|HTTP {exc.code} {detail}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderError(f"timeout|request exceeded {timeout}s: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError(f"api_error|{exc.__class__.__name__}: {exc}") from exc
    latency_ms = int((time.monotonic() - start) * 1000)
    return raw, latency_ms


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def _per_million(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def _normalize_openai(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """OpenRouter + OpenAI-compatible (Deepseek, Sarvam) catalogue entries."""
    model_id = entry.get("id")
    if not model_id:
        return None
    pricing = entry.get("pricing") or {}
    architecture = entry.get("architecture") or {}
    return {
        "model_id": str(model_id),
        "name": entry.get("name") or str(model_id),
        "context_length": entry.get("context_length"),
        "prompt_price": _per_million(pricing.get("prompt")),
        "completion_price": _per_million(pricing.get("completion")),
        "modality": architecture.get("modality"),
    }


def _normalize_anthropic(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Anthropic's /v1/models shape: {id, display_name}."""
    model_id = entry.get("id")
    if not model_id:
        return None
    return {
        "model_id": str(model_id),
        "name": entry.get("display_name") or str(model_id),
        "context_length": None,
        "prompt_price": None,
        "completion_price": None,
        "modality": "text+image->text",
    }


def static_models(spec: ProviderSpec) -> list[dict[str, Any]]:
    """The seeded static catalogue, in the same shape as a live fetch."""
    return [dict(m) for m in spec.static_models]


def fetch_catalog(spec: ProviderSpec, api_key: Optional[str], *, timeout: int = _CATALOG_TIMEOUT) -> list[dict[str, Any]]:
    """Fetch + normalize a provider's live model list.

    Raises ProviderError on any failure. Providers with no catalogue endpoint
    (Sarvam) return their static list instead — that is not a failure.
    """
    if not spec.has_live_catalog:
        return static_models(spec)
    if not api_key:
        raise ProviderError(f"config_error|No API key stored for {spec.label}.")

    payload = _http_get_json(spec.catalog_url or "", _auth_headers(spec, api_key), timeout=timeout)
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ProviderError(f"parse_error|{spec.label}'s model list had an unexpected shape.")

    normalize = _normalize_anthropic if spec.kind == "anthropic" else _normalize_openai
    models = [m for m in (normalize(e) for e in entries if isinstance(e, dict)) if m]
    if not models:
        raise ProviderError(f"parse_error|{spec.label} returned an empty model list.")
    return models


def probe(spec: ProviderSpec, api_key: Optional[str], *, timeout: int = _PROBE_TIMEOUT) -> tuple[str, str]:
    """Check whether a provider is reachable with the stored key.

    Returns ``(status, detail)`` where status is one of
    ``live`` / ``invalid`` / ``unreachable``. Uses the model-list endpoint
    where one exists (cheap, no tokens); otherwise a minimal chat call.
    One provider's failure never touches another's status.
    """
    if not api_key:
        return "unconfigured", f"No API key stored for {spec.label}."
    try:
        if spec.has_live_catalog:
            fetch_catalog(spec, api_key, timeout=timeout)
            return "live", f"{spec.label} responded to the model-list request."
        # No catalogue endpoint — probe with the smallest possible chat call.
        url, headers, payload = build_chat_request(
            spec, api_key, model=_default_probe_model(spec), system="ping",
            user="Reply with the single word: ok", max_tokens=8, temperature=0.0,
        )
        _http_post_json(url, headers, payload, timeout=timeout)
        return "live", f"{spec.label} accepted a minimal test call."
    except ProviderError as exc:
        kind = str(exc).split("|", 1)[0]
        if kind in ("auth_error",):
            return "invalid", str(exc).split("|", 1)[-1]
        if kind == "rate_limit":
            # Reachable and authenticated — a rate limit is not a bad key.
            return "live", "Reachable (rate-limited on the probe)."
        return "unreachable", str(exc).split("|", 1)[-1]


def _default_probe_model(spec: ProviderSpec) -> str:
    models = static_models(spec)
    return models[0]["model_id"] if models else "default"


# ---------------------------------------------------------------------------
# Chat request / response translation
# ---------------------------------------------------------------------------


def build_chat_request(
    spec: ProviderSpec,
    api_key: str,
    *,
    model: str,
    system: str,
    user: str,
    images: Optional[list[tuple[str, bytes]]] = None,
    max_tokens: int = 2000,
    temperature: float = 0.0,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Build ``(url, headers, payload)`` for one provider.

    ``images`` is ``[(mime, raw_bytes), ...]``; an empty list means a text-only
    call. Each provider's envelope is handled here so callers send a uniform
    (system, user, images) shape.
    """
    headers = _auth_headers(spec, api_key)

    if spec.kind == "anthropic":
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for mime, raw in images or []:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            })
        payload = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        return spec.chat_url, headers, payload

    # OpenAI-compatible (OpenRouter / Deepseek / Sarvam).
    user_content: Any
    if images:
        user_content = [{"type": "text", "text": user}]
        for mime, raw in images:
            b64 = base64.b64encode(raw).decode("ascii")
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
    else:
        user_content = user
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    return spec.chat_url, headers, payload


def parse_chat_response(spec: ProviderSpec, raw: str) -> str:
    """Extract the assistant text from a provider's response envelope."""
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"parse_error|response envelope is not JSON: {exc}") from exc
    if isinstance(envelope, dict) and envelope.get("error"):
        raise ProviderError(f"api_error|{envelope['error']}")

    if spec.kind == "anthropic":
        blocks = envelope.get("content") if isinstance(envelope, dict) else None
        if isinstance(blocks, list):
            text = "".join(
                b.get("text", "") for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                return text
        raise ProviderError("parse_error|unexpected Anthropic response shape.")

    try:
        return envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"parse_error|unexpected response shape: {exc}") from exc


def call_provider(
    spec: ProviderSpec,
    api_key: str,
    *,
    model: str,
    system: str,
    user: str,
    images: Optional[list[tuple[str, bytes]]] = None,
    max_tokens: int = 2000,
    temperature: float = 0.0,
    timeout: int = 120,
) -> tuple[str, int]:
    """One-shot call to a specific provider. Returns ``(text, latency_ms)``.

    Raises ProviderError (typed) on any transport/parse failure.
    """
    url, headers, payload = build_chat_request(
        spec, api_key, model=model, system=system, user=user, images=images,
        max_tokens=max_tokens, temperature=temperature,
    )
    raw, latency_ms = _http_post_json(url, headers, payload, timeout=timeout)
    return parse_chat_response(spec, raw), latency_ms
