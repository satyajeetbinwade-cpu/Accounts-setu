"""Amendment F — make a wrong vision assignment impossible to miss.

Covers, with stubs only (no live calls):
  * a text-only model is REFUSED when saving a vision touchpoint's assignment,
    and nothing is written;
  * the explicit override saves it AND is recorded in the change log;
  * the override is required per-vision-touchpoint only — a text-only
    touchpoint is completely unaffected;
  * "Test leg" on a vision touchpoint sends a generated IMAGE and fails when
    the stub rejects images; a text touchpoint's test is unchanged;
  * the fallback-rate warning chip logic (last 7 days, > half).

Run:

    venv/bin/python -m pytest tests/test_ai_vision_guard.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import db as recon_db
from src.ai_models import gateway, providers as prov, service as ai_models
from src.f4 import service as f4
from src.vault import service as vault

VISION_TP = "invoice_extraction_visual"
TEXT_TP = "ingestion_mapping"


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "ai_vision.db")
    ai_models.init_ai_models(path)
    vault.init_vault(path)
    return path


def _seed_modality(db_path, provider_key, model_id, modality):
    conn = recon_db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ai_provider_models (provider_key, model_id, modality, source) "
            "VALUES (?, ?, ?, 'static')",
            (provider_key, model_id, modality),
        )
        conn.commit()
    finally:
        conn.close()


def _valid_legs(db_path, tp, *, primary="acme/text-only", fallback="google/gemini-2.5-pro", **kw):
    return ai_models.set_assignment_legs(
        tp,
        primary_provider="openrouter", primary_model=primary,
        fallback_provider="openrouter", fallback_model=fallback,
        actor="admin", db_path=db_path, **kw,
    )


def _history(db_path, tp):
    return f4.history_for_record("ai_model_assignment", tp, db_path=db_path)


# ===========================================================================
# 1. Save-time guard
# ===========================================================================


def test_text_only_model_refused_for_vision_touchpoint(db):
    _seed_modality(db, "openrouter", "acme/text-only", "text->text")
    before = ai_models.get_assignment(VISION_TP, db_path=db)["primary_model"]

    with pytest.raises(ai_models.AIModelsError) as exc:
        _valid_legs(db, VISION_TP)

    msg = str(exc.value)
    assert "acme/text-only" in msg                 # names the model
    assert VISION_TP in msg                        # names the touchpoint
    assert "accepts images" in msg                 # tells the admin how to proceed
    # NOTHING was written.
    assert ai_models.get_assignment(VISION_TP, db_path=db)["primary_model"] == before


def test_override_saves_and_is_recorded_in_the_change_log(db):
    _seed_modality(db, "openrouter", "acme/text-only", "text->text")
    _valid_legs(db, VISION_TP, allow_non_vision_override=True)

    assert ai_models.get_assignment(VISION_TP, db_path=db)["primary_model"] == "acme/text-only"

    entries = _history(db, VISION_TP)
    actions = [e["field"] for e in entries]
    assert "vision_override" in actions
    override = next(e for e in entries if e["field"] == "vision_override")
    assert "acme/text-only" in (override.get("new_value") or "")
    assert override.get("changed_by") == "admin"


def test_override_is_not_logged_when_no_refusal_happened(db):
    # A vision-capable model saves with no override entry.
    _seed_modality(db, "openrouter", "acme/vision", "text+image->text")
    _valid_legs(db, VISION_TP, primary="acme/vision")
    assert ai_models.get_assignment(VISION_TP, db_path=db)["primary_model"] == "acme/vision"
    assert "vision_override" not in [e["field"] for e in _history(db, VISION_TP)]


def test_text_only_touchpoint_is_completely_unaffected(db):
    _seed_modality(db, "openrouter", "acme/text-only", "text->text")
    # No override flag, a model the catalogue calls text-only — still saves,
    # because ingestion_mapping is not a vision touchpoint.
    _valid_legs(db, TEXT_TP, fallback="acme/other")
    assert ai_models.get_assignment(TEXT_TP, db_path=db)["primary_model"] == "acme/text-only"
    assert "vision_override" not in [e["field"] for e in _history(db, TEXT_TP)]


def test_unknown_modality_is_not_refused(db):
    # A brand-new model with no catalogue row must NOT be blocked.
    _valid_legs(db, VISION_TP, primary="acme/unlisted-model")
    assert ai_models.get_assignment(VISION_TP, db_path=db)["primary_model"] == "acme/unlisted-model"


def test_allowlist_overrides_an_unknown_modality(db):
    from src.settings import service as settings

    settings.init_settings(db)
    settings.set_setting_str(
        ai_models.VISION_ALLOWLIST_KEY, "acme/vision-ok,acme/vision-ok-2", actor="admin", db_path=db,
    )
    # listed → allowed
    ai_models.set_assignment_legs(
        VISION_TP,
        primary_provider="openrouter", primary_model="acme/vision-ok",
        fallback_provider="openrouter", fallback_model="acme/vision-ok-2",
        actor="admin", db_path=db,
    )
    # not listed → refused even though the modality is unknown
    with pytest.raises(ai_models.AIModelsError):
        _valid_legs(db, VISION_TP, primary="acme/not-listed", fallback="acme/vision-ok")


def test_registry_and_runtime_guard_agree(db):
    """The save-time check and the gateway's runtime check are one rule."""
    _seed_modality(db, "openrouter", "acme/text-only", "text->text")
    save_allowed, save_reason = ai_models.vision_check("openrouter", "acme/text-only", db_path=db)
    run_allowed, run_reason = gateway._vision_allowed("openrouter", "acme/text-only", db)
    assert save_allowed is False and run_allowed is False
    assert save_reason == run_reason


def test_set_assignment_models_is_guarded_too(db):
    """The older two-model setter (used by the Invoice Extraction screen) is
    guarded as well."""
    _seed_modality(db, "openrouter", "acme/text-only", "text->text")
    with pytest.raises(ai_models.AIModelsError):
        ai_models.set_assignment_models(
            VISION_TP, primary_model="acme/text-only", fallback_model="acme/other-2",
            actor="admin", db_path=db,
        )


# ===========================================================================
# 2. The per-leg test call sends an IMAGE on a vision touchpoint
# ===========================================================================


def _with_key(db):
    ai_models.set_provider_key("openrouter", "sk-or-test-0001", actor="admin", db_path=db)


def test_image_test_fails_on_a_stub_that_rejects_images(db, monkeypatch):
    _with_key(db)
    seen: dict = {}

    def _call(spec, api_key, *, model, system, user, images=None, **kw):
        seen["images"] = images
        raise prov.ProviderError("api_error|HTTP 400 this model does not support image input")

    monkeypatch.setattr(prov, "call_provider", _call)

    out = ai_models.test_leg(VISION_TP, "primary", actor="admin", db_path=db)
    assert out["kind"] == "image"
    assert seen["images"], "the vision test must actually send an image"
    assert out["ok"] is False
    assert "does not support image input" in out["error"]   # the provider's error text


def test_image_test_passes_only_when_the_reply_reads_the_word(db, monkeypatch):
    _with_key(db)
    monkeypatch.setattr(prov, "call_provider", lambda spec, api_key, **kw: ("I can read SETU", 9))
    ok = ai_models.test_leg(VISION_TP, "primary", actor="admin", db_path=db)
    assert ok["ok"] is True and ok["kind"] == "image" and ok["latency_ms"] == 9

    # A reply that ignores the image must FAIL — a text-only model can otherwise
    # answer plausibly without ever reading it.
    monkeypatch.setattr(prov, "call_provider", lambda spec, api_key, **kw: ("I cannot see any image.", 9))
    bad = ai_models.test_leg(VISION_TP, "primary", actor="admin", db_path=db)
    assert bad["ok"] is False
    assert "test image" in bad["error"]


def test_generated_test_image_is_a_64x64_png(db):
    png, mime = ai_models._vision_test_image()
    assert mime == "image/png"
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as img:
        assert img.size == (64, 64)


def test_text_touchpoint_test_is_unchanged(db, monkeypatch):
    _with_key(db)
    seen: dict = {}

    def _call(spec, api_key, *, images=None, **kw):
        seen["images"] = images
        return "ok", 11

    monkeypatch.setattr(prov, "call_provider", _call)
    out = ai_models.test_leg(TEXT_TP, "primary", actor="admin", db_path=db)
    assert out["ok"] is True
    assert out["kind"] == "text"
    assert out["sample"] == "ok"
    assert not seen["images"], "a text touchpoint must not send an image"


# ===========================================================================
# 3. Fallback-rate warning chip logic
# ===========================================================================


def _log(db_path, touchpoint, outcome, *, days_ago=0):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = recon_db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO ai_call_log (touchpoint_key, provider, model, outcome, attempt, created_at) "
            "VALUES (?, 'openrouter', 'm', ?, 1, ?)",
            (touchpoint, outcome, ts),
        )
        conn.commit()
    finally:
        conn.close()


def test_fallback_warning_chip_logic(db):
    # 3 of 4 fell back → warn.
    for outcome in ("fallback_used", "fallback_used", "fallback_used", "success"):
        _log(db, VISION_TP, outcome)
    # exactly half (2 of 4) → NOT more than half → no warn.
    for outcome in ("fallback_used", "fallback_used", "success", "success"):
        _log(db, TEXT_TP, outcome)
    # a fallback OUTSIDE the 7-day window must not count.
    _log(db, "recon_explanation", "fallback_used", days_ago=8)

    stats = {s["touchpoint_key"]: s for s in ai_models.fallback_stats(days=7, db_path=db)}

    assert stats[VISION_TP]["calls"] == 4
    assert stats[VISION_TP]["fallbacks"] == 3
    assert stats[VISION_TP]["fallback_rate"] == 0.75
    assert stats[VISION_TP]["warn"] is True

    assert stats[TEXT_TP]["fallbacks"] == 2
    assert stats[TEXT_TP]["warn"] is False          # exactly half is not "more than half"

    assert "recon_explanation" not in stats         # outside the window

    # ordered by fallback count, so the worst offender leads.
    assert ai_models.fallback_stats(days=7, db_path=db)[0]["touchpoint_key"] == VISION_TP


def test_fallback_stats_never_warns_without_calls(db):
    assert ai_models.fallback_stats(days=7, db_path=db) == []


def test_retry_and_error_rows_are_not_counted_as_calls(db):
    # Attempts that never resolved must not dilute the ratio.
    for outcome in ("transport_error", "parse_error", "rejected"):
        _log(db, VISION_TP, outcome)
    _log(db, VISION_TP, "fallback_used")
    stats = {s["touchpoint_key"]: s for s in ai_models.fallback_stats(days=7, db_path=db)}
    assert stats[VISION_TP]["calls"] == 1
    assert stats[VISION_TP]["fallbacks"] == 1
    assert stats[VISION_TP]["warn"] is True
