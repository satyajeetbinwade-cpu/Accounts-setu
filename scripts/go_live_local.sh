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
# cat'ed, and never echoed: nothing in this script may reveal a secret.
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
# The NORMAL boot path, run against the scratch pair only. `python main.py`
# refuses nothing here because the database is explicit (not the repo's own);
# on branches that carry the boot-safety guard the explicit flag for a
# deliberate real-database migration is `SETU_ALLOW_BOOT_MIGRATE=1`.
echo "== boot (migrations + seeds) against the scratch pair =="
"${PY}" main.py

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
echo "== settings =="
"${PY}" -m src.invoice_extract.config set invoice_extract.v2.enabled true

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
