"""First-boot demo seeding — creates the demo clients and files the bundled
source folders under ``data/<client_id>/...`` via the ONE path builder.

The image ships a pristine copy of the demo folders at ``/app/seed_data``
(name-keyed). Those names are ONLY bundled-asset directory names, never
identity: this script maps each to a real ``end_clients`` row it creates (so
the clients carry their real PAN/GSTIN), then copies each period/slot's files
into place with ``source_data_path`` / ``exports_dir`` — the same functions the
upload path uses. After this runs there is no name-keyed folder anywhere.

Idempotent, and first-boot ONLY: it does nothing at all once any client
       exists, existing clients are reused and destination files are skipped. It
also does nothing when no seed data is present (a real customer volume).

Run:  python -m src.demo_seed
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.clients import service as clients  # noqa: E402
from src.data_paths import DATA_ROOT, exports_dir, source_data_path  # noqa: E402
from src.shared.vocab import is_valid_source_type  # noqa: E402

# (legal_name, pan, gstin, assigned_team) — the demo pair. PAN is supplied so
# the normal create-client rule (PAN-or-TAN required) is satisfied without any
# special-casing.
DEMO_CLIENTS: tuple[tuple[str, str, str, str], ...] = (
    ("Test Client", "AAAPL1234C", "07AAACP9472A1ZJ", "Demo team"),
    ("Meridian Fabrics", "AABCM1234K", "27AABCM1234K1Z9", "Demo team"),
)

SEED_ROOT = Path(os.environ.get("SETU_SEED_DATA", "/app/seed_data"))


def main() -> int:
    if not SEED_ROOT.exists():
        print(f"[demo_seed] no seed data at {SEED_ROOT}; nothing to do")
        return 0

    by_name = {
        c["legal_name"]: int(c["client_id"])
        for c in clients.list_clients(include_inactive=True)
    }

    # A volume that already has clients is NOT a fresh deployment, so this is
    # not the first boot and there is nothing demo-shaped to add.
    #
    # This guard is what makes the docstring's "idempotent, never clobbers real
    # data" actually true. Reuse below matches on legal_name, and a MIGRATED
    # volume can hold the very client this list is looking for under a
    # different name — the live roster has `Test`, not the bundled folder's
    # `Test Client`. Name-matching alone therefore missed the existing row and
    # created a SECOND client for a client that already had one (the GSTIN is
    # no help either: the live row has none, so the unique index did not stop
    # it). Refuse before touching anything.
    if by_name:
        print(
            f"[demo_seed] {len(by_name)} client(s) already exist; "
            "this is not a fresh volume, so no demo seeding"
        )
        return 0

    for legal_name, pan, gstin, team in DEMO_CLIENTS:
        src = SEED_ROOT / legal_name
        if not src.is_dir():
            continue
        if legal_name in by_name:
            cid = by_name[legal_name]
        else:
            cid = int(clients.create_client(
                legal_name=legal_name, pan=pan, gstin=gstin, assigned_team=team,
                actor="seed",
            ))
            by_name[legal_name] = cid

        copied = 0
        for period_dir in sorted(p for p in src.iterdir() if p.is_dir()):
            period = period_dir.name
            for slot in sorted(p for p in period_dir.iterdir() if p.is_dir()):
                if slot.name == "exports":
                    dest = exports_dir(cid, period)
                elif is_valid_source_type(slot.name):
                    dest = source_data_path(cid, period, slot.name)
                else:
                    print(f"[demo_seed] skip {legal_name}/{period}/{slot.name}: not a known slot")
                    continue
                for f in sorted(slot.iterdir()):
                    if f.is_file() and not (dest / f.name).exists():
                        shutil.copy2(f, dest / f.name)
                        copied += 1
        print(f"[demo_seed] {legal_name} -> client_id={cid}: filed {copied} file(s) under data/{cid}/")

    print(f"[demo_seed] done (data root: {DATA_ROOT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
