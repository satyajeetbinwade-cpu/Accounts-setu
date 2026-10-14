#!/usr/bin/env bash
# Get the v2 invoice pipeline running LOCALLY on a fresh scratch database.
#
#   bash scripts/go_live_local.sh
#
# The repository's own db/ and data/ are NEVER used: everything lives in
# ../setu_live_test (outside the repo), and the app is pointed at it with
# SETU_DB_PATH / SETU_DATA_ROOT — the two overrides the app itself enforces.
#
# Keys are read from ~/.setu_keys. That file is SOURCED, never printed, never
# cat'ed, and never echoed: nothing in this script may reveal a secret. It is
# also bound into the scratch database's vault (again: never printed), then the
# app's own live IMAGE test decides which vision models the v2 pipeline may use.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO_ROOT}/venv/bin/python"
LIVE_ROOT="${LIVE_ROOT:-$(dirname "${REPO_ROOT}")/setu_live_test}"

echo "== setu go-live (local) =="
echo "repo:        ${REPO_ROOT}"
echo "scratch root: ${LIVE_ROOT}"

# ---------------------------------------------------------------- secrets
if [[ -f "${HOME}/.setu_keys" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${HOME}/.setu_keys"
  set +a
  echo "keys:        sourced ~/.setu_keys (contents never printed)"
else
  echo "keys:        ~/.setu_keys NOT FOUND — model calls will fail until it exists"
fi

# ------------------------------------------------------------- scratch tree
mkdir -p "${LIVE_ROOT}/db" "${LIVE_ROOT}/data"
export SETU_DB_PATH="${LIVE_ROOT}/db/poc.db"
export SETU_DATA_ROOT="${LIVE_ROOT}/data"
export FRONTEND_PORT="${FRONTEND_PORT:-3311}"
echo "database:    ${SETU_DB_PATH}"
echo "data root:   ${SETU_DATA_ROOT}"

# ------------------------------------------------------------- boot + seed
# The NORMAL boot path, run against the scratch pair only. The boot-safety
# guard makes a bare `python main.py` refuse — migrations must be ASKED for —
# so this explicit --migrate is the command (the same sequence the container
# CMD runs via SETU_ALLOW_BOOT_MIGRATE=1).
echo "== boot (migrations + seeds) against the scratch pair =="
"${PY}" main.py --migrate

# ------------------------------------------------------------------ client
# Idempotent: the client is created only when this GSTIN is not already there,
# so re-running the script on an existing scratch folder is harmless.
echo "== client =="
"${PY}" - <<'PY'
from src.clients import service as clients

NAME, GSTIN = "KMAP Capital Limited", "07AAECK9054D1ZK"
# The service requires PAN or TAN: the PAN is characters 3-12 of the GSTIN.
PAN = GSTIN[2:12]

existing = [c for c in clients.list_clients() if (c.get("gstin") or "").strip().upper() == GSTIN]
if existing:
    cid = existing[0].get("client_id")
    print(f"client exists: {NAME} (client_id={cid}, GSTIN={GSTIN}) — nothing created")
else:
    cid = clients.create_client(
        legal_name=NAME, gstin=GSTIN, pan=PAN, actor="cli:go-live",
    )
    print(f"client created: {NAME} (client_id={cid}, GSTIN={GSTIN}, PAN={PAN})")
PY

# ------------------------------------------------------------ v2 pipeline ON
# The ONLY settings the v2 pipeline gates on are the two enabled flags; every
# other key (dpi, concurrency, timeout, retries, max_cost_usd, …) has a code
# default in src/invoice_extract/{triage,extract}.py, so nothing else needs
# seeding for a run. `config set` prints old -> new.
echo "== settings =="
"${PY}" -m src.invoice_extract.config set invoice_extract.v2.enabled true
"${PY}" -m src.invoice_extract.config set invoice_extract.triage.enabled true

# ------------------------------------------------------------- AI models
# Bind the keys (from the environment the block above exported), refresh the
# catalogues, then let the app's OWN image test decide the vision legs:
#   * invoice_triage / invoice_extraction_visual — cheapest PASSING model,
#     fallback = a passing model from a different vendor;
#   * invoice_extraction_verify — a passing model on a DIFFERENT PROVIDER
#     from read A's primary (two reads from one provider are not independent).
# Candidates: the DeepSeek model that served VISUAL reads in production
# (deepseek-flash — recorded in the deployed DB's invoice_uploads.ai_model),
# plus three cheap OpenRouter vision models. Anthropic and any Pro-class model
# are never candidates and never assigned.
# Idempotent: once all three touchpoints carry both legs, re-runs skip the
# (paid, tiny) candidate tests unless FORCE_MODEL_TEST=1 is set.
echo "== AI models =="
"${PY}" - <<'PY'
import os
import sqlite3
import sys

from src import db as recon_db
from src.ai_models import service as ai_models

ACTOR = "cli:go-live"
DB = recon_db.DB_PATH
TOUCHPOINTS = ("invoice_triage", "invoice_extraction_visual", "invoice_extraction_verify")

# ---- 1. bind ~/.setu_keys exports to the providers in the scratch DB -------
for provider_key, env_name in (
    ("openrouter", "OPENROUTER_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("sarvam", "SARVAM_API_KEY"),
):
    row = ai_models.get_provider_row(provider_key)
    if row and row.get("has_key"):
        print(f"key: {provider_key:10s} already bound in the scratch DB")
        continue
    secret = (os.environ.get(env_name) or "").strip()
    if not secret:
        print(f"key: {provider_key:10s} {env_name} is NOT set — no key bound")
        continue
    ai_models.set_provider_key(provider_key, secret, actor=ACTOR)
    print(f"key: {provider_key:10s} bound from {env_name}")  # value never printed

# ---- 2. skip the paid tests when everything is already assigned ------------
def _legs_ready():
    for key in TOUCHPOINTS:
        legs = ai_models.effective_leg(key)
        if not legs or not legs["is_active"]:
            return False
        if not legs["primary"]["model"] or not legs["fallback"]["model"]:
            return False
    return True

if _legs_ready() and os.environ.get("FORCE_MODEL_TEST") != "1":
    print("models: all three v2 touchpoints are already assigned — skipping "
          "the candidate image tests (FORCE_MODEL_TEST=1 re-runs them)")
    for key in TOUCHPOINTS:
        legs = ai_models.effective_leg(key)
        print(f"assigned: {key}: primary {legs['primary']['provider']}/{legs['primary']['model']} · "
              f"fallback {legs['fallback']['provider']}/{legs['fallback']['model']}")
    sys.exit(0)

# ---- 3. refresh the catalogues the prices + modality checks come from ------
for provider_key in ("openrouter", "deepseek"):
    try:
        info = ai_models.refresh_provider_catalog(provider_key, actor=ACTOR)
        print(f"catalogue: {provider_key:10s} {info['count']} model(s) ({info['source']})")
    except Exception as exc:  # noqa: BLE001 — reported, never fatal here
        print(f"catalogue: {provider_key:10s} REFRESH FAILED: {exc}")

def catalogue_price(provider: str, model: str):
    """The model's $/1M in/out from the app's own catalogue tables."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT prompt_price, completion_price FROM ai_provider_models "
            "WHERE provider_key = ? AND model_id = ?", (provider, model),
        ).fetchone()
        if (row is None or (row[0] is None and row[1] is None)) and provider != "openrouter":
            # A provider called directly does not price its models; the same
            # model on OpenRouter (vendor-prefixed id) carries the price.
            row = conn.execute(
                "SELECT prompt_price, completion_price FROM ai_provider_models "
                "WHERE provider_key = 'openrouter' AND model_id LIKE ?",
                (f"%/{model}",),
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()

# ---- 4. pick the DeepSeek candidate (the "DeepSeek V4.1 Flash" model) ------
def deepseek_candidate():
    ids = [m["model_id"] for m in ai_models.models_for_provider("deepseek")]
    if not ids:
        # The provider's list endpoint gave nothing (or was not reachable);
        # test the id production actually served visual reads with, and let
        # the live image test decide.
        print("deepseek: provider catalogue empty — testing deepseek-flash anyway")
        return "deepseek-flash"
    if "deepseek-v4.1-flash" in ids:
        return "deepseek-v4.1-flash"
    for hints in (("v4.1", "flash"), ("vision",), ("flash",)):
        for model_id in ids:
            if all(h in model_id.lower() for h in hints):
                return model_id
    print("deepseek: no v4.1/vision/flash id in the provider catalogue — testing "
          "deepseek-flash anyway")
    return "deepseek-flash"

candidates = [("deepseek", deepseek_candidate())] if ai_models.provider_key_configured("deepseek")[0] else []
if not candidates:
    print("deepseek: no usable key — candidate skipped (read B needs a second provider)")
candidates += [
    ("openrouter", "qwen/qwen3.7-flash"),
    ("openrouter", "mistralai/ministral-3b-2512"),
    ("openrouter", "google/gemini-3-flash-preview"),
]

# ---- 5. the app's own live image test, one candidate at a time -------------
def placeholder_fallback(candidate):  # any OTHER valid leg for the test call
    if candidate == ("openrouter", "google/gemini-3-flash-preview"):
        return ("openrouter", "qwen/qwen3.7-flash")
    return ("openrouter", "google/gemini-3-flash-preview")

results = []
for provider, model in candidates:
    price_in, price_out = catalogue_price(provider, model)
    low = model.lower()
    if provider == "anthropic" or any(m in low for m in ("pro", "opus", "ultra")):
        ok, reason = False, "excluded: Anthropic / Pro-class"
    else:
        fp, fm = placeholder_fallback((provider, model))
        try:
            ai_models.set_assignment_legs(
                "invoice_triage",
                primary_provider=provider, primary_model=model,
                fallback_provider=fp, fallback_model=fm,
                actor=ACTOR, allow_non_vision_override=True,
            )
            res = ai_models.test_leg("invoice_triage", "primary", actor=ACTOR)
            ok = bool(res.get("ok"))
            reason = "" if ok else str(res.get("error") or "image test failed")
        except Exception as exc:  # noqa: BLE001 — an invalid id must not abort
            ok, reason = False, str(exc)
    results.append({"provider": provider, "model": model, "ok": ok,
                    "price_in": price_in, "price_out": price_out, "reason": reason})
    print(f"tested: {provider}/{model} -> {'PASS' if ok else 'FAIL'}")

print()
print(f"{'model':<44} {'provider':<12} {'$/1M in':>8} {'$/1M out':>9}  test")
print("-" * 86)
for r in results:
    pin = f"{r['price_in']:.4f}" if r["price_in"] is not None else "—"
    pout = f"{r['price_out']:.4f}" if r["price_out"] is not None else "—"
    print(f"{r['model']:<44} {r['provider']:<12} {pin:>8} {pout:>9}  {'PASS' if r['ok'] else 'FAIL'}")
for r in results:
    if not r["ok"]:
        print(f"  FAILED: {r['provider']}/{r['model']} — {r['reason'][:200]}")

# ---- 6. assign the three v2 touchpoints ------------------------------------
# A model can PASS the image test and still be unusable for the pipeline's
# JSON reads: qwen3.7-flash spends ~1900 tokens reasoning, so the triage
# call's 2000-token output cap truncates its JSON — 35 of 45 real triage
# calls on the 31-page bundle came back parse_error. Excluded from
# assignment below; the reason lives here so the exclusion is auditable.
UNUSABLE = {
    "qwen/qwen3.7-flash": (
        "truncated JSON at the pipeline's output caps (reasoning eats the "
        "2000-token triage budget; 35/45 real calls failed to parse)"
    ),
}
passing = [r for r in results if r["ok"] and r["model"] not in UNUSABLE]
for r in results:
    if r["ok"] and r["model"] in UNUSABLE:
        print(f"excluded from assignment: {r['provider']}/{r['model']} — {UNUSABLE[r['model']]}")
providers_passed = sorted({r["provider"] for r in passing})
if len(providers_passed) < 2:
    print(f"STOP: fewer than two providers passed the image test "
          f"({len(passing)} model(s), providers: {providers_passed or 'none'}) — nothing assigned.")
    sys.exit(3)

def vendor(r):
    if r["provider"] == "openrouter" and "/" in r["model"]:
        return r["model"].split("/", 1)[0].lower()  # the OpenRouter vendor
    return r["provider"]

def order(r):
    pin = r["price_in"] if r["price_in"] is not None else float("inf")
    pout = r["price_out"] if r["price_out"] is not None else float("inf")
    return (pin, pout, r["provider"], r["model"])

ranked = sorted(passing, key=order)
cheapest = ranked[0]
different_vendor = next((r for r in ranked if vendor(r) != vendor(cheapest)), None)
different_provider = next((r for r in ranked if r["provider"] != cheapest["provider"]), None)
if different_vendor is None or different_provider is None:
    print("STOP: the passing models cannot satisfy the vendor/provider diversity rules.")
    sys.exit(3)

assignments = {
    "invoice_triage": (cheapest, different_vendor),
    "invoice_extraction_visual": (cheapest, different_vendor),
    "invoice_extraction_verify": (different_provider,
                                  next(r for r in ranked if r is not different_provider)),
}
print()
for key, (prim, fb) in assignments.items():
    try:
        ai_models.set_assignment_legs(
            key, primary_provider=prim["provider"], primary_model=prim["model"],
            fallback_provider=fb["provider"], fallback_model=fb["model"],
            actor=ACTOR,
        )
        guard_note = ""
    except Exception as exc:  # noqa: BLE001 — the live test passed; save anyway
        ai_models.set_assignment_legs(
            key, primary_provider=prim["provider"], primary_model=prim["model"],
            fallback_provider=fb["provider"], fallback_model=fb["model"],
            actor=ACTOR, allow_non_vision_override=True,
        )
        guard_note = f" (catalogue guard overridden: {exc})"
    print(f"assigned: {key}: primary {prim['provider']}/{prim['model']} · "
          f"fallback {fb['provider']}/{fb['model']}{guard_note}")
PY

# ------------------------------------------------------------------- admin
echo "== admin user =="
"${PY}" - <<'PY'
from src import db as recon_db
from src.auth import seed as auth_seed

conn = recon_db.get_connection()
try:
    row = conn.execute("SELECT username FROM users ORDER BY user_id LIMIT 1").fetchone()
finally:
    conn.close()
if row is None:
    raise SystemExit("no admin user was seeded — the boot path did not run the auth seed")
print(f"login: {row[0]} / {auth_seed.DEFAULT_ADMIN_PASSWORD}")
print("(change this password after the first sign-in — the login page no longer advertises it)")
PY

# --------------------------------------------------------------------- app
echo "== starting the app =="
echo "URL: http://localhost:${FRONTEND_PORT}"
echo "stop:  kill \$(pgrep -f 'reflex run' | head -1)"
echo "again: bash ${BASH_SOURCE[0]}   # boots, seeds, then serves"
exec env SETU_DB_PATH="${SETU_DB_PATH}" SETU_DATA_ROOT="${SETU_DATA_ROOT}" \
  FRONTEND_PORT="${FRONTEND_PORT}" "${REPO_ROOT}/venv/bin/reflex" run --env dev
