"""AI Ingestion settings — the frontend control panel for the unified AI
ingestion layer's model, provider endpoint, credential, and confidence
threshold.

This exists because the extraction layer feeds the reconciliation engine
directly: a wrong column mapping here silently corrupts every downstream
match. The model choice therefore deserves to be an explicit, visible,
changeable decision rather than something buried in a YAML file — and the
API key must never be pasted into source or an env file by hand.

Design notes (Setu_Phase2_UI_Foundation_Build_Prompt):
- The credential is stored through C4's vault (real encryption-at-rest) and
  only ever displayed as a masked reference — never revealed, matching the
  vault's own rule that even Admin never sees a raw secret.
- The connection status uses the 3.7 live status chip, paired with the
  masked ref, exactly as the Foundation spec requires.
- A blocked action (saving without a credential) renders its reason inline
  (3.4), never as a tooltip.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from src.auth import service as auth
from src.ai_models import service as ai_models
from src.ingestion_ai import service as ingestion_ai
from src.vault import service as vault
from src.ui.theme import (
    render_confidence_badge,
    render_inline_reason,
    render_live_status_chip,
    render_warning_banner,
)

# The service name used when storing the ingestion key in C4's vault, so the
# settings screen can find it again.
_VAULT_SERVICE = "OpenRouter (AI Ingestion)"


def _model_options(current_model: str) -> list[str]:
    """Model ids for the dropdown, from the central registry's catalogue.

    The currently-configured id is always included even when it isn't in the
    catalogue, so opening this screen never silently changes the value.
    """
    try:
        ids = [m["model_id"] for m in ai_models.model_options()]
    except Exception:  # noqa: BLE001
        ids = []
    if current_model and current_model not in ids:
        return [current_model] + ids
    return ids or [current_model]


def _model_labels() -> dict[str, str]:
    try:
        return {m["model_id"]: ai_models.model_label(m) for m in ai_models.model_options()}
    except Exception:  # noqa: BLE001
        return {}


def _sync_registry(model_id: str, actor: str) -> None:
    """Mirror a model change into the central registry so the two screens
    never disagree. Best-effort — the ingestion setting is already saved."""
    try:
        row = ai_models.get_assignment("ingestion_mapping")
        if not row:
            return
        fallback = row.get("fallback_model") or ""
        if not fallback or fallback == model_id:
            # Keep the registry's own rule intact: a fallback must differ.
            fallback = "anthropic/claude-sonnet-4" if model_id != "anthropic/claude-sonnet-4" else "openai/gpt-5"
        ai_models.set_assignment_models(
            "ingestion_mapping", primary_model=model_id, fallback_model=fallback, actor=actor,
        )
    except Exception:  # noqa: BLE001
        pass


def render_ai_ingestion_settings(current_user: dict[str, Any]) -> None:
    """The AI Ingestion section of the Settings hub."""
    can_manage = auth.has_permission(current_user, "ingestion_ai.llm.manage")
    status = ingestion_ai.llm_status()

    st.caption(
        "The model that reads uploaded files and maps their columns onto the "
        "reconciliation schema. This layer feeds the engine directly, so it runs "
        "at the engine's own tier — a wrong mapping here corrupts every downstream match."
    )

    # --- Current status -------------------------------------------------
    c1, c2 = st.columns([2, 3])
    with c1:
        st.markdown("**Connection**")
        if status["configured"]:
            render_live_status_chip("connected", label="Configured")
        else:
            render_live_status_chip("needs_reauth", label="Not configured")
    with c2:
        st.markdown("**Model in use**")
        st.code(status["model"], language=None)
        if status["key_source"] == "vault":
            st.caption(f"API key: stored in the vault ({status['masked_ref'] or 'masked'})")
        elif status["key_source"] == "env":
            st.caption(f"API key: from the {status['env_var']} environment variable")
        else:
            render_inline_reason(status["reason"] or "No API key configured.")

    if not status["configured"]:
        render_warning_banner(
            "AI ingestion can't run until a model and API key are configured. "
            "Uploads will be blocked with an explanation rather than silently guessed at."
        )

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Model + endpoint ----------------------------------------------
    st.markdown("**Model & provider**")
    st.caption(
        "This is the ingestion touchpoint's model. Every AI touchpoint in the "
        "platform is listed together under **Setup → AI Models**."
    )
    current_model = status["model"]
    options = _model_options(current_model)
    model_choice = st.selectbox(
        "Model", options, index=options.index(current_model),
        format_func=lambda mid: _model_labels().get(mid, mid),
        disabled=not can_manage, key="ai_ing_model_select",
    )
    custom_model = st.text_input(
        "Or enter a model id directly", value="", disabled=not can_manage,
        key="ai_ing_model_custom", placeholder="e.g. anthropic/claude-opus-4.1",
    )
    base_url = st.text_input(
        "Provider endpoint", value=status["base_url"], disabled=not can_manage,
        key="ai_ing_base_url",
    )

    if can_manage and st.button("Save model settings", type="primary", key="ai_ing_save_model"):
        try:
            chosen = (custom_model or "").strip() or model_choice
            ingestion_ai.set_llm_model(chosen, actor=current_user["username"])
            ingestion_ai.set_llm_base_url(base_url, actor=current_user["username"])
            # Keep the central registry in step, so the two screens never
            # disagree about which model the ingestion touchpoint uses.
            _sync_registry(chosen, current_user["username"])
            st.success(f"Model set to `{chosen}`.")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Credential -----------------------------------------------------
    st.markdown("**API key**")
    st.caption(
        "Stored encrypted in the Security & Credential Vault. It is never displayed "
        "again after saving — only a masked reference is shown, even to Admin."
    )

    credentials = _ingestion_credentials()
    if credentials:
        labels = {c["credential_id"]: f"{c['label'] or c['service']} — {c['masked_ref']}" for c in credentials}
        current_id = status["credential_id"]
        options_ids = [None] + list(labels.keys())
        idx = options_ids.index(current_id) if current_id in options_ids else 0
        chosen_id = st.selectbox(
            "Credential to use", options_ids, index=idx,
            format_func=lambda cid: "(use the environment variable instead)" if cid is None else labels[cid],
            disabled=not can_manage, key="ai_ing_cred_select",
        )
        if can_manage and st.button("Use this credential", key="ai_ing_use_cred"):
            try:
                ingestion_ai.set_llm_credential(chosen_id, actor=current_user["username"])
                st.success("Credential selection saved.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
    else:
        st.caption("No stored credentials yet — add one below.")

    with st.expander("Add a new API key", expanded=not credentials):
        new_key = st.text_input(
            "API key", type="password", disabled=not can_manage, key="ai_ing_new_key",
            help="Pasted here, encrypted immediately, and never shown again.",
        )
        if can_manage and st.button("Save key to vault", key="ai_ing_save_key"):
            if not new_key.strip():
                render_inline_reason("Enter an API key before saving.")
            else:
                try:
                    credential_id = vault.add_credential(
                        _VAULT_SERVICE, new_key.strip(), label=_VAULT_SERVICE,
                        actor=current_user["username"],
                    )
                    ingestion_ai.set_llm_credential(credential_id, actor=current_user["username"])
                    st.success("API key stored in the vault and selected for AI ingestion.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Test -----------------------------------------------------------
    st.markdown("**Test the connection**")
    st.caption("Makes one real, minimal call to confirm the model and key actually work.")
    if st.button("Test connection", key="ai_ing_test"):
        with st.spinner("Calling the model\u2026"):
            outcome = ingestion_ai.test_llm_connection()
        if outcome["ok"]:
            st.success(f"Connection OK — `{outcome['model']}` responded in {outcome['latency_ms']} ms.")
        else:
            st.error("The connection test failed.")
            with st.expander("Details"):
                st.code(outcome["error"] or "Unknown error")

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Confidence threshold -------------------------------------------
    st.markdown("**Mapping confidence threshold**")
    st.caption(
        "A field the AI maps at or above this confidence is pre-selected for the user. "
        "Fields below it are the only ones highlighted for manual attention."
    )
    current_threshold = ingestion_ai.get_preselect_threshold()
    threshold = st.slider(
        "Pre-select threshold", min_value=0.0, max_value=1.0, value=float(current_threshold),
        step=0.05, disabled=not can_manage, key="ai_ing_threshold",
    )
    render_confidence_badge("ai", pct=int(round(threshold * 100)), label=f"Pre-select at {int(round(threshold*100))}%")
    if can_manage and st.button("Save threshold", key="ai_ing_save_threshold"):
        try:
            ingestion_ai.set_preselect_threshold(threshold, actor=current_user["username"])
            st.success(f"Pre-select threshold set to {int(round(threshold * 100))}%.")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

    st.markdown('<hr class="setu-section-rule" />', unsafe_allow_html=True)

    # --- Learned file shapes --------------------------------------------
    st.markdown("**Learned file shapes**")
    st.caption(
        "Every file shape the AI has mapped for this firm. A trusted shape skips "
        "both the model call and the manual mapping step on the next file of that shape."
    )
    shapes = ingestion_ai.list_shapes()
    if not shapes:
        st.info("No file shapes learned yet.")
        return

    for s in shapes:
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        with c1:
            st.write(f"`{s['client_ref']}` · {s['source_type']}")
            st.caption(f"{len(s.get('headers') or [])} column(s) · used {s['times_used']} time(s)")
        with c2:
            if s["trusted"]:
                render_confidence_badge("rule", label="Trusted")
            else:
                st.caption("Not trusted")
        with c3:
            st.caption(s.get("model_used") or "—")
        with c4:
            if s["trusted"] and can_manage and st.button("Revoke trust", key=f"ai_ing_revoke_{s['shape_id']}"):
                try:
                    ingestion_ai.revoke_shape_trust(s["shape_id"])
                    st.success("Trust revoked — the next file of this shape will be re-mapped.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))
        st.markdown('<hr style="margin:2px 0;border-color:var(--setu-border);" />', unsafe_allow_html=True)


def _ingestion_credentials() -> list[dict[str, Any]]:
    """Active vault credentials that look like an LLM API key, so the
    picker doesn't list every portal credential in the firm."""
    try:
        rows = vault.list_credentials()
    except Exception:  # noqa: BLE001
        return []
    return [
        r for r in rows
        if "openrouter" in (r.get("service") or "").lower()
        or "ai" in (r.get("service") or "").lower()
        or "llm" in (r.get("service") or "").lower()
    ]