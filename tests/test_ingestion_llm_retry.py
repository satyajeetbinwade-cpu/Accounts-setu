"""Ingestion LLM layer — truncated-reply recovery.

WHY THIS EXISTS (a real production defect): the ingestion mapping prompt asks
for one JSON entry per canonical field, and the books slot maps the GST+TDS
UNION (21 fields). A reply that overruns the output-token ceiling is cut off
mid-object, so `parse_json_response` raised
``parse_error|unbalanced JSON object`` — which escaped
``normalize_source_file`` BEFORE anything was persisted. The upload then sat on
disk for ever and the Reconcile slot read "not ingested yet", with no stored
result to explain why.

The two guards pinned here:
  1. the default budget is large enough for the widest prompt this layer sends;
  2. a TRUNCATED reply is re-asked once with a bigger budget, because the model
     was reached and its answer was valid — it simply never ended.

Runs against scratch DBs via ``db_path`` — never the app's real db/poc.db.
"""

from __future__ import annotations

import pytest

from src.ai_models import providers as prov
from src.ai_models import service as ai_models
from src.ingestion_ai import llm
from src.vault import service as vault

_TRUNCATED = (
    '{"mappings": [{"canonical_field": "gstin", "source_column": "GSTIN/UIN", '
    '"confidence": 0.92, "reason": "the header names the GSTIN colu'
)
_COMPLETE = (
    '{"mappings": [{"canonical_field": "gstin", "source_column": "GSTIN/UIN", '
    '"confidence": 0.92, "reason": "the header names the GSTIN column"}]}'
)
_MALFORMED = "I could not work out a mapping for this file."


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "llm_retry.db")
    ai_models.init_ai_models(path)
    vault.init_vault(path)
    # A stored key is what the gateway resolves; the transport is stubbed, so
    # the value itself is never sent anywhere.
    ai_models.set_provider_key("openrouter", "sk-or-test-0001", actor="admin", db_path=path)
    return path


@pytest.fixture()
def replies(monkeypatch):
    """Queue of canned provider replies, recording the budget each attempt
    asked for."""
    state = {"queue": [], "calls": []}

    def _call(spec, api_key, *, model, system, user, images=None,
              max_tokens=2000, temperature=0.0, timeout=120):
        state["calls"].append({"model": model, "max_tokens": max_tokens})
        if not state["queue"]:
            raise prov.ProviderError("api_error|no canned reply left in the queue")
        return state["queue"].pop(0), 7

    monkeypatch.setattr(prov, "call_provider", _call)
    return state


def test_the_default_budget_covers_the_widest_ingestion_prompt():
    """A regression to the old 2000 would silently strand every wide file
    again, so the floor is pinned rather than left to drift."""
    assert llm._FALLBACK_MAX_TOKENS >= 8192


def test_a_truncated_reply_is_retried_once_with_a_bigger_budget(db, replies):
    replies["queue"] = [_TRUNCATED, _COMPLETE]

    parsed, _latency, _model, _raw = llm.call_llm_json(
        "SYS", "USER", db_path=db,
    )

    assert parsed["mappings"][0]["source_column"] == "GSTIN/UIN"
    assert len(replies["calls"]) == 2, "a truncated reply must be re-asked exactly once"
    assert replies["calls"][1]["max_tokens"] > replies["calls"][0]["max_tokens"]
    assert replies["calls"][1]["max_tokens"] >= 8192


def test_a_second_truncated_reply_raises_rather_than_looping(db, replies):
    """The retry is bounded: a model that can never finish must surface the
    error, not spin."""
    replies["queue"] = [_TRUNCATED, _TRUNCATED]

    with pytest.raises(llm.LLMError) as excinfo:
        llm.call_llm_json("SYS", "USER", db_path=db)

    assert llm.TRUNCATED_REPLY_MARKER in str(excinfo.value)
    assert len(replies["calls"]) == 2


def test_a_malformed_reply_is_not_retried(db, replies):
    """A reply that is wrong (rather than cut off) is a real error — retrying
    it with more room would only cost another call."""
    replies["queue"] = [_MALFORMED]

    with pytest.raises(llm.LLMError) as excinfo:
        llm.call_llm_json("SYS", "USER", db_path=db)

    assert "no JSON object found" in str(excinfo.value)
    assert len(replies["calls"]) == 1


def test_the_reported_latency_and_model_come_from_the_successful_attempt(db, replies):
    replies["queue"] = [_TRUNCATED, _COMPLETE]

    _parsed, latency, model, _raw = llm.call_llm_json("SYS", "USER", db_path=db)

    assert latency == 7
    assert model == replies["calls"][-1]["model"]


def test_the_vision_json_path_shares_the_retry(db, replies):
    """One implementation of the recovery, so the vision touchpoint cannot
    drift away from the text one."""
    replies["queue"] = [_TRUNCATED, _COMPLETE]

    parsed, _latency, _model, _raw = llm.call_llm_vision_json(
        "SYS", "USER", [("image/png", b"\x89PNG\r\n\x1a\n")], db_path=db,
    )

    assert parsed["mappings"][0]["source_column"] == "GSTIN/UIN"
    assert len(replies["calls"]) == 2
