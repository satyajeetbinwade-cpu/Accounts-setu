"""Bundled fallback model list — a transport-free convenience.

The live per-provider catalogues are fetched by ``src/ai_models/providers.py``
(and cached in ``ai_provider_models``). This module no longer performs any
network call and holds no provider endpoint: it exists only so the OpenRouter
model dropdown is never EMPTY on a completely fresh database with no network.

Adding a provider endpoint here would re-introduce the hardcoded-provider
reference this extension removed — the platform's provider set lives in
``providers.PROVIDER_SPECS`` and nowhere else.
"""

from __future__ import annotations

from typing import Any

# Bundled fallback — used only when a provider's catalogue has never been
# fetched AND a live fetch fails. Deliberately short: it exists so the
# dropdown is never empty, not to mirror any vendor's full list.
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


def fallback_catalog() -> list[dict[str, Any]]:
    """The bundled list, in the same shape as a live catalogue fetch."""
    return [dict(m) for m in FALLBACK_MODELS]
