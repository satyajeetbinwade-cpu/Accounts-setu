"""C3-ext v2 — multi-provider AI configuration tests.

Covers the parts that are new and easy to get wrong:
  * provider-aware model-id validation,
  * per-provider request/response shaping (Anthropic vs OpenAI-compatible),
  * the static-catalogue path (Sarvam) and the env-var key fallback,
  * the CORE new behaviour: one provider's key being unusable degrades ONLY the
    touchpoints that use it — with real cross-provider failover and a recorded
    fallback event naming the provider that resolved the call.

Runs against scratch DBs via ``db_path`` — never the app's real db/poc.db.
"""

from __future__ import annotations

import json

import pytest

from src.ai_models import gateway
from src.ai_models import providers as prov
from src.ai_models import service as ai_models
from src.vault import service as vault


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "ai_models.db")
    ai_models.init_ai_models(path)
    vault.init_vault(path)
    return path


@pytest.fixture()
def fake_transport(monkeypatch):
    """Replace the real HTTP call with a recorder that succeeds for a chosen
    set of provider keys and raises for the rest."""
    state = {"ok_providers": set(), "calls": []}

    def _call(spec, api_key, *, model, system, user, images=None,
              max_tokens=2000, temperature=0.0, timeout=120):
        state["calls"].append((spec.key, model))
        if spec.key not in state["ok_providers"]:
            raise prov.ProviderError(f"api_error|simulated outage for {spec.key}")
        return "ok", 11

    monkeypatch.setattr(prov, "call_provider", _call)
    return state


# ---------------------------------------------------------------------------
# Provider transport shaping
# ---------------------------------------------------------------------------


def test_anthropic_request_uses_x_api_key_and_system_field():
    spec = prov.PROVIDER_SPECS["anthropic"]
    url, headers, payload = prov.build_chat_request(
        spec, "secret-key", model="claude-sonnet-4", system="SYS", user="USER",
    )
    assert url == spec.chat_url
    assert headers["x-api-key"] == "secret-key"
    assert "anthropic-version" in headers
    assert "Authorization" not in headers
    assert payload["system"] == "SYS"
    assert payload["messages"][0]["role"] == "user"
    assert payload["max_tokens"] > 0


def test_openai_compatible_request_uses_bearer_and_system_message():
    spec = prov.PROVIDER_SPECS["openrouter"]
    _url, headers, payload = prov.build_chat_request(
        spec, "secret-key", model="anthropic/claude-sonnet-4", system="SYS", user="USER",
    )
    assert headers["Authorization"] == "Bearer secret-key"
    assert payload["messages"][0] == {"role": "system", "content": "SYS"}


def test_anthropic_response_shape_parsed():
    spec = prov.PROVIDER_SPECS["anthropic"]
    raw = json.dumps({"content": [{"type": "text", "text": "hello"}]})
    assert prov.parse_chat_response(spec, raw) == "hello"


def test_openai_response_shape_parsed():
    spec = prov.PROVIDER_SPECS["deepseek"]
    raw = json.dumps({"choices": [{"message": {"content": "hi"}}]})
    assert prov.parse_chat_response(spec, raw) == "hi"


def test_every_provider_has_one_spec_and_distinct_transport():
    assert set(prov.PROVIDER_SPECS) == {"openrouter", "anthropic", "deepseek", "sarvam"}
    urls = {s.chat_url for s in prov.PROVIDER_SPECS.values()}
    assert len(urls) == 4


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_openrouter_requires_vendor_prefixed_id():
    with pytest.raises(ai_models.AIModelsError):
        ai_models.validate_model_id("claude-sonnet-4", provider_key="openrouter")
    assert ai_models.validate_model_id("anthropic/claude-sonnet-4") == "anthropic/claude-sonnet-4"


def test_direct_providers_accept_bare_model_id():
    assert ai_models.validate_model_id("claude-sonnet-4", provider_key="anthropic") == "claude-sonnet-4"
    assert ai_models.validate_model_id("deepseek-chat", provider_key="deepseek") == "deepseek-chat"
    with pytest.raises(ai_models.AIModelsError):
        ai_models.validate_model_id("bad model", provider_key="anthropic")


def test_unknown_provider_rejected():
    with pytest.raises(prov.ProviderError):
        prov.get_spec("nope")


# ---------------------------------------------------------------------------
# Catalogue + keys
# ---------------------------------------------------------------------------


def test_sarvam_uses_static_catalogue_without_network():
    models = prov.fetch_catalog(prov.PROVIDER_SPECS["sarvam"], None)
    assert models and models[0]["model_id"] == "sarvam-m"
    assert prov.PROVIDER_SPECS["sarvam"].catalog_mode == "static"


def test_stored_key_is_masked_and_never_returned(db):
    ai_models.set_provider_key("openrouter", "sk-or-v1-abcdef1234", actor="admin", db_path=db)
    row = ai_models.get_provider_row("openrouter", db_path=db)
    assert row["has_key"] is True
    assert row["masked_ref"] == "sk-••••1234"
    assert "abcdef" not in json.dumps(row)


def test_env_var_key_used_when_no_vault_credential(db, monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "env-secret")
    configured, source = ai_models.provider_key_configured("sarvam", db_path=db)
    assert configured is True and source == "env"
    assert ai_models.resolve_provider_secret(
        "sarvam", actor="tester", purpose="test", db_path=db,
    ) == "env-secret"


def test_replace_key_rotates_the_bound_credential(db):
    first = ai_models.set_provider_key("anthropic", "sk-ant-one-1111", actor="admin", db_path=db)
    second = ai_models.replace_provider_key("anthropic", "sk-ant-two-2222", actor="admin", db_path=db)
    assert first["credential_id"] == second["credential_id"]  # rotated, not duplicated
    assert ai_models.get_provider_row("anthropic", db_path=db)["masked_ref"] == "sk-••••2222"


# ---------------------------------------------------------------------------
# Leg assignment
# ---------------------------------------------------------------------------


def test_legs_may_use_different_providers(db):
    ai_models.set_assignment_legs(
        "ingestion_mapping",
        primary_provider="anthropic", primary_model="claude-opus-4",
        fallback_provider="deepseek", fallback_model="deepseek-chat",
        actor="admin", db_path=db,
    )
    leg = ai_models.effective_leg("ingestion_mapping", db_path=db)
    assert leg["primary"]["provider"] == "anthropic"
    assert leg["fallback"]["provider"] == "deepseek"


def test_identical_legs_rejected(db):
    with pytest.raises(ai_models.AIModelsError):
        ai_models.set_assignment_legs(
            "ingestion_mapping",
            primary_provider="openrouter", primary_model="anthropic/claude-sonnet-4",
            fallback_provider="openrouter", fallback_model="anthropic/claude-sonnet-4",
            actor="admin", db_path=db,
        )


# ---------------------------------------------------------------------------
# The core new behaviour
# ---------------------------------------------------------------------------


def test_failover_records_the_resolved_provider(db, fake_transport):
    ai_models.set_provider_key("openrouter", "sk-or-test-9999", actor="admin", db_path=db)
    fake_transport["ok_providers"] = {"openrouter"}
    ai_models.set_assignment_legs(
        "ingestion_mapping",
        primary_provider="deepseek", primary_model="deepseek-chat",
        fallback_provider="openrouter", fallback_model="anthropic/claude-sonnet-4",
        actor="admin", db_path=db,
    )

    text, meta = gateway.call_touchpoint_text("ingestion_mapping", "sys", "user", db_path=db)
    assert text == "ok"
    assert meta["provider"] == "openrouter"
    assert meta["fallback_used"] is True

    events = ai_models.list_fallback_events(db_path=db)
    assert len(events) == 1
    assert events[0]["touchpoint_key"] == "ingestion_mapping"
    assert events[0]["primary_provider"] == "deepseek"
    assert events[0]["resolved_provider"] == "openrouter"


def test_unusable_provider_degrades_only_its_own_touchpoints(db, fake_transport):
    """The acceptance criterion: one provider's key being unusable degrades ONLY
    the touchpoints that use it on an active leg."""
    ai_models.set_provider_key("openrouter", "sk-or-test-9999", actor="admin", db_path=db)
    fake_transport["ok_providers"] = {"openrouter"}

    # Uses the broken provider on its PRIMARY leg, but has a healthy fallback.
    ai_models.set_assignment_legs(
        "ingestion_mapping",
        primary_provider="deepseek", primary_model="deepseek-chat",
        fallback_provider="openrouter", fallback_model="anthropic/claude-sonnet-4",
        actor="admin", db_path=db,
    )
    # Uses ONLY the healthy provider — must be entirely unaffected.
    ai_models.set_assignment_legs(
        "recon_explanation",
        primary_provider="openrouter", primary_model="deepseek/deepseek-r1",
        fallback_provider="openrouter", fallback_model="qwen/qwen-2.5-72b-instruct",
        actor="admin", db_path=db,
    )
    # Uses ONLY the broken provider on BOTH legs — must degrade.
    ai_models.set_assignment_legs(
        "tds_classification",
        primary_provider="deepseek", primary_model="deepseek-chat",
        fallback_provider="deepseek", fallback_model="deepseek-reasoner",
        actor="admin", db_path=db,
    )

    # Healthy touchpoint unaffected: no fallback, resolved on its primary.
    _text, meta = gateway.call_touchpoint_text("recon_explanation", "s", "u", db_path=db)
    assert meta["provider"] == "openrouter"
    assert meta["fallback_used"] is False

    # Broken-primary touchpoint continues via its own fallback leg.
    _text, meta2 = gateway.call_touchpoint_text("ingestion_mapping", "s", "u", db_path=db)
    assert meta2["fallback_used"] is True

    # Both-legs-broken touchpoint degrades to manual, and ONLY it.
    with pytest.raises(gateway.TouchpointUnavailableError) as exc:
        gateway.call_touchpoint_text("tds_classification", "s", "u", db_path=db)
    assert "AI unavailable" in str(exc.value)

    # A fallback event is recorded only when a fallback actually RESOLVED the
    # call — so exactly one event (for the touchpoint that failed over), and
    # none for the healthy touchpoint or the fully-degraded one.
    events = ai_models.list_fallback_events(db_path=db)
    assert [e["touchpoint_key"] for e in events] == ["ingestion_mapping"]


def test_inactive_touchpoint_is_unavailable(db):
    with pytest.raises(gateway.TouchpointUnavailableError):
        gateway.call_touchpoint_text("corrective_entry_drafting", "s", "u", db_path=db)


def test_placeholder_touchpoints_seeded_inactive(db):
    rows = {r["touchpoint_key"]: r for r in ai_models.list_assignments(db_path=db)}
    for key in ("corrective_entry_drafting", "audit_query_drafting", "anomaly_scoring"):
        assert rows[key]["is_active"] == 0
        assert rows[key]["primary_model"] is None
    # The real wired touchpoints remain active.
    assert rows["ingestion_mapping"]["is_active"] == 1


def test_test_leg_reports_latency_and_sample(db, fake_transport):
    ai_models.set_provider_key("openrouter", "sk-or-test-9999", actor="admin", db_path=db)
    fake_transport["ok_providers"] = {"openrouter"}
    ai_models.set_assignment_legs(
        "ingestion_mapping",
        primary_provider="openrouter", primary_model="anthropic/claude-sonnet-4",
        fallback_provider="deepseek", fallback_model="deepseek-chat",
        actor="admin", db_path=db,
    )
    out = ai_models.test_leg("ingestion_mapping", "primary", actor="admin", db_path=db)
    assert out["ok"] is True
    assert out["latency_ms"] == 11
    assert out["sample"] == "ok"

    bad = ai_models.test_leg("ingestion_mapping", "fallback", actor="admin", db_path=db)
    assert bad["ok"] is False  # deepseek has no key
    assert bad["error"]
