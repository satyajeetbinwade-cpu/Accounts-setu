"""OpenRouter model catalogue — fetch, normalize, and cache.

OpenRouter publishes its full model list at ``/api/v1/models`` with no
authentication required. This module is the ONE place that endpoint is
called, so the rest of the app never depends on a third-party network call
succeeding.

Two deliberate design points:

  * The catalogue is a CONVENIENCE, never a constraint. The registry works
    with any model id string; this only powers the dropdown. If the fetch
    fails, the UI falls back to the bundled list below and says so.
  * Pricing is normalized to USD per 1M tokens at fetch time. OpenRouter
    returns per-token price strings (e.g. ``"0.000003"``), which are
    unreadable in a UI and easy to mis-scale later.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

CATALOG_URL = "https://openrouter.ai/api/v1/models"
_FETCH_TIMEOUT = 25

# Bundled fallback — used only when the live catalogue has never been
# fetched AND the network call fails. Deliberately short: it exists so the
# dropdown is never empty, not to mirror OpenRouter's full list.
FALLBACK_MODELS: list[dict[str, Any]] = [
    {"model_id": "anthropic/claude-opus-4.1", "name": "Anthropic: Claude Opus 4.1", "context_length": 200000, "modality": "text+image->text"},
    {"model_id": "anthropic/claude-sonnet-4", "name": "Anthropic: Claude Sonnet 4", "context_length": 200000, "modality": "text+image->text"},
    {"model_id": "openai/gpt-5", "name": "OpenAI: GPT-5", "context_length": 400000, "modality": "text+image->text"},
    {"model_id": "google/gemini-2.5-pro", "name": "Google: Gemini 2.5 Pro", "context_length": 1048576, "modality": "text+image->text"},
    {"model_id": "deepseek/deepseek-r1", "name": "DeepSeek: R1", "context_length": 128000, "modality": "text->text"},
    {"model_id": "deepseek/deepseek-chat", "name": "DeepSeek: Chat", "context_length": 128000, "modality": "text->text"},
    {"model_id": "qwen/qwen-2.5-72b-instruct", "name": "Qwen: Qwen2.5 72B Instruct", "context_length": 131072, "modality": "text->text"},
    {"model_id": "meta-llama/llama-3.3-70b-instruct", "name": "Meta: Llama 3.3 70B Instruct", "context_length": 131072, "modality": "text->text"},
]


class CatalogError(RuntimeError):
    """Raised when the live catalogue cannot be fetched. Callers fall back
    to the cached/bundled list rather than failing."""


def _to_per_million(value: Any) -> Optional[float]:
    """OpenRouter prices are per-token strings. Convert to USD per 1M
    tokens, or None when the value is absent/unparseable."""
    if value in (None, ""):
        return None
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def _normalize(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    model_id = entry.get("id")
    if not model_id:
        return None
    pricing = entry.get("pricing") or {}
    architecture = entry.get("architecture") or {}
    return {
        "model_id": str(model_id),
        "name": entry.get("name") or str(model_id),
        "context_length": entry.get("context_length"),
        "prompt_price": _to_per_million(pricing.get("prompt")),
        "completion_price": _to_per_million(pricing.get("completion")),
        "modality": architecture.get("modality"),
    }


def fetch_catalog(*, timeout: int = _FETCH_TIMEOUT) -> list[dict[str, Any]]:
    """Fetch and normalize OpenRouter's public model list.

    Raises CatalogError on any network/parse failure — callers decide
    whether to fall back. Never returns a partially-parsed list.
    """
    request = urllib.request.Request(
        CATALOG_URL,
        headers={"Accept": "application/json", "User-Agent": "setu-recon-poc"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CatalogError(f"OpenRouter returned HTTP {exc.code} for the model list.") from exc
    except urllib.error.URLError as exc:
        raise CatalogError(f"Couldn't reach OpenRouter to list models: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise CatalogError(f"Couldn't reach OpenRouter to list models: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"OpenRouter's model list wasn't valid JSON: {exc}") from exc

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise CatalogError("OpenRouter's model list had an unexpected shape.")

    models = [m for m in (_normalize(e) for e in entries if isinstance(e, dict)) if m]
    if not models:
        raise CatalogError("OpenRouter returned an empty model list.")
    return models


def fallback_catalog() -> list[dict[str, Any]]:
    """The bundled list, in the same shape as fetch_catalog()."""
    return [dict(m) for m in FALLBACK_MODELS]
