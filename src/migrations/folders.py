"""Move the legacy ``data/<client_name>/`` folders to ``data/<client_id>/``.

Filesystem moves cannot be part of the DB transaction, so this is a
separate, idempotent step run right after the schema migration:

1. plan — list every non-numeric folder under ``data/`` and its target id;
2. apply — rename (same volume) each folder into place, writing a manifest
   (``data/_migration_manifest.json``) as it goes;
3. on any failure the caller rolls the DB back and we reverse the moves
   already listed in the manifest.

Nothing is ever deleted: when the target already exists (a re-run, or a
collision) the source's children are *merged* into the target, skipping any
path that already exists.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MANIFEST_NAME = "_migration_manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path(data_root: Path) -> Path:
    return data_root / MANIFEST_NAME


def plan_folder_moves(mapping, data_root: Path) -> list[dict]:
    """Return the list of planned moves: ``data/<name>`` → ``data/<client_id>``.

    A folder whose name is already numeric is skipped (already migrated).
    """
    entries: list[dict] = []
    if not data_root.exists():
        return entries
    for child in sorted(data_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.isdigit():
            continue
        cid = mapping.id_for(child.name)
        entries.append(
            {
                "old_path": str(child),
                "old_name": child.name,
                "client_id": int(cid) if cid else None,
                "new_path": str(data_root / str(cid)) if cid else None,
                "status": "planned" if cid else "unmapped",
            }
        )
    return entries


def _merge_into(src: Path, dst: Path) -> list[str]:
    """Move every child of ``src`` into ``dst``, skipping existing paths.
    Returns the list of skipped (already-present) relative paths."""
    skipped: list[str] = []
    for item in sorted(src.iterdir()):
        target = dst / item.name
        if target.exists():
            if item.is_dir() and target.is_dir():
                skipped.extend(
                    f"{item.name}/{s}" for s in _merge_into(item, target)
                )
                try:
                    item.rmdir()
                except OSError:
                    pass
            else:
                skipped.append(item.name)
            continue
        os.replace(str(item), str(target))
    return skipped


def apply_folder_moves(
    entries: list[dict], data_root: Path, *, dry_run: bool = False
) -> dict:
    """Execute the planned moves. Returns a summary dict.

    Writes/updates the manifest incrementally so a crash mid-way can still be
    reversed. With ``dry_run=True`` nothing is touched.
    """
    mpath = manifest_path(data_root)
    manifest: dict = {"started_at": _now(), "moves": []}
    done: list[dict] = []
    skipped_all: dict[str, list[str]] = {}

    for entry in entries:
        if entry["status"] != "planned" or not entry["new_path"]:
            manifest["moves"].append(entry)
            continue
        old = Path(entry["old_path"])
        new = Path(entry["new_path"])
        if not old.exists():
            entry = {**entry, "status": "already_moved"}
            manifest["moves"].append(entry)
            continue
        if dry_run:
            manifest["moves"].append({**entry, "status": "would_move"})
            continue

        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            if new.exists():
                skipped = _merge_into(old, new)
                try:
                    old.rmdir()
                except OSError:
                    pass
                status = "merged"
                skipped_all[old.name] = skipped
            else:
                os.replace(str(old), str(new))
                status = "moved"
        except Exception as exc:  # noqa: BLE001 — record, then let caller roll back
            entry = {**entry, "status": "failed", "error": str(exc)}
            manifest["moves"].append(entry)
            if not dry_run:
                mpath.write_text(json.dumps(manifest, indent=2))
            raise

        done.append(entry)
        manifest["moves"].append({**entry, "status": status})

    manifest["finished_at"] = _now()
    if not dry_run and (entries or mpath.exists()):
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(manifest, indent=2))
    return {
        "manifest_path": str(mpath),
        "moved": sum(1 for m in manifest["moves"] if m.get("status") in ("moved", "merged")),
        "would_move": sum(1 for m in manifest["moves"] if m.get("status") == "would_move"),
        "skipped_existing": skipped_all,
        "moves": manifest["moves"],
    }


def rollback(data_root: Path) -> list[str]:
    """Reverse every move recorded in the manifest. Best-effort; returns the
    list of paths restored."""
    mpath = manifest_path(data_root)
    if not mpath.exists():
        return []
    manifest = json.loads(mpath.read_text())
    restored: list[str] = []
    for move in reversed(manifest.get("moves", [])):
        if move.get("status") not in ("moved", "merged"):
            continue
        old = Path(move["old_path"])
        new = Path(move["new_path"])
        if not new.exists():
            continue
        try:
            if move["status"] == "moved" and not old.exists():
                os.replace(str(new), str(old))
            elif move["status"] == "merged":
                old.mkdir(parents=True, exist_ok=True)
                for item in list(new.iterdir()):
                    dest = old / item.name
                    if not dest.exists():
                        shutil.move(str(item), str(dest))
            restored.append(str(old))
        except Exception:  # noqa: BLE001 — keep reversing what we can
            continue
    return restored
