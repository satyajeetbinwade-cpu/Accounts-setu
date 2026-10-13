"""Filesystem helpers for the client-id-keyed data/ uploaded-source layout.

    data/<client_id>/<period>/<source_type>/

This is the ONLY module that may build a path under ``data/``. Every caller
passes an integer ``client_id`` (from ``end_clients``) — never a name. Period
is validated as ``YYYY-MM`` and ``source_type`` against the shared allowlist,
so a ``"../"`` trick, a bad period or an unknown source type is rejected
before it can touch the filesystem. The client id is coerced to a positive
``int``, which also makes traversal impossible.

Folders are created lazily: ``source_data_path`` mkdirs on demand (it is
called on upload/re-file), while ``data_dir`` returns the path without
creating anything (for read paths and existence checks).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from src.shared.periods import PERIOD_UNFILED, is_valid_period
from src.shared.vocab import SOURCE_TYPES, is_valid_source_type

#: The repository's OWN data tree. Never changes: the safety check below compares
#: against it, so it must not follow an override.
REPO_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"

#: ``SETU_DATA_ROOT`` points the whole app at a COPY of the data tree. Used by a
#: local scratch run so a stray write cannot land in the real upload folders.
#: The DEFAULT is unchanged: the repo's data/ when the variable is unset.
DATA_ROOT = Path(os.environ.get("SETU_DATA_ROOT") or REPO_DATA_ROOT)


def default_data_root(db_path=None) -> Path:
    """The data root that BELONGS to a database.

    * a database under a ``db/`` folder owns the ``data/`` folder beside that one
      — the deployed layout (``/app/persist/db/poc.db`` ↔ ``/app/persist/data``)
      and the repo's own (``db/poc.db`` ↔ ``data/``);
    * any other database owns a ``data/`` folder next to itself, which is what a
      copy or a scratch database gets.
    """
    from src import db as recon_db

    path = Path(db_path) if db_path else Path(recon_db.DB_PATH)
    if path.parent.name == "db":
        return path.parent.parent / "data"
    return path.parent / "data"


def assert_app_data_root(db_path=None, *, data_root=None) -> Path:
    """Refuse to serve the app with a data root that does not belong to the DB.

    Called on the app's import path, so ``reflex run`` can never start against a
    mismatched pair. Accepted:

    * the database's OWN data folder (see :func:`default_data_root`);
    * a folder BESIDE the database — which is what a scratch run gets;
    * the repo's real ``data/`` when the database is the repo's own.

    Refused: any other root, and specifically the repo's real ``data/`` while the
    database is a copy. That mismatch is the dangerous one — it would let a
    scratch run migrate or write into the real upload folders.
    """
    from src import db as recon_db

    db = Path(db_path) if db_path else Path(recon_db.DB_PATH)
    resolved = Path(data_root) if data_root else Path(DATA_ROOT)
    db = db.expanduser().resolve()
    resolved = resolved.expanduser().resolve()

    if resolved == REPO_DATA_ROOT.resolve() and db != Path(recon_db.REPO_DB_PATH).resolve():
        raise RuntimeError(
            f"refusing to start: the data root is the repository's real {resolved} but the "
            f"database is {db}. Point SETU_DATA_ROOT at the copy's own data folder "
            f"({default_data_root(db)}) as well."
        )
    accepted = {default_data_root(db).expanduser().resolve(), db.parent}
    if resolved.parent not in accepted and resolved not in accepted:
        raise RuntimeError(
            f"refusing to start: the data root {resolved} does not belong to the database "
            f"{db}. Expected {default_data_root(db)} or a folder beside it."
        )
    return resolved

#: A RESERVED folder under the data root for machine-made artefacts — currently
#: the review screen's rendered page images. The leading underscore keeps it out
#: of the client-id namespace (no client id is ever ``_page_cache``), and
#: ``list_unmapped_folders`` skips reserved names, so a cache can never be
#: reported as a stray client folder. It is git-ignored: it is derived data that
#: can always be re-rendered, and it must never enter the repository.
PAGE_CACHE_DIRNAME = "_page_cache"

# A cache key is built by the caller from an upload id, a page number, a
# rotation, a dpi and a content hash — all digits and hex. Nothing else is
# accepted, so a key cannot carry a path separator or a traversal.
_CACHE_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}")


def page_cache_dir() -> Path:
    """``<data root>/_page_cache/`` (NOT created)."""
    return DATA_ROOT / PAGE_CACHE_DIRNAME


def page_cache_path(key: str) -> Path:
    """The cache file for one validated ``key``.

    The key is checked against a conservative allowlist BEFORE it is joined, so
    a hostile key (``../../etc/passwd``, an absolute path, a separator) can
    never leave the cache folder.
    """
    text = str(key or "")
    if not _CACHE_KEY_RE.fullmatch(text):
        raise ValueError(f"Unsafe cache key {key!r}")
    return page_cache_dir() / f"{text}.png"

# Back-compat re-export: older imports read this set.
VALID_SOURCE_TYPES = set(SOURCE_TYPES)


def use_data_root(path) -> Path:
    """Point every data path at ``path`` instead of the repo's ``data/``.

    For verification harnesses and tests that must never write into the real
    upload tree. ``src.shared.discovery`` binds ``DATA_ROOT`` at import time,
    so it is re-pointed too — otherwise a stray read would resolve against the
    real folder and silently disagree with the writers.
    """
    global DATA_ROOT
    DATA_ROOT = Path(path)
    try:
        from src.shared import discovery
    except ImportError:  # pragma: no cover — discovery always imports cleanly
        pass
    else:
        discovery.DATA_ROOT = DATA_ROOT
    return DATA_ROOT


def use_sandboxed_data_root(source_root=None, *, prefix: str = "setu_sandbox_") -> Path:
    """Redirect data paths into a temp root pre-seeded with the SOURCE slots.

    A verification harness has to *read* the real uploads (the report is built
    from the portal workbook on disk) while never *adding* anything to them
    (regenerating exports is exactly what those harnesses do). So: copy every
    ``<client_id>/<period>/<source_type>/`` slot into a throwaway root, skip
    ``exports/``, then point the data root there. Reads keep working and every
    byte the harness writes lands in the sandbox.

    Returns the new data root. Set ``SETU_VERIFY_DATA_ROOT`` to reuse a root.
    """
    import shutil
    import tempfile

    source = Path(source_root) if source_root else DATA_ROOT
    sandbox = Path(tempfile.mkdtemp(prefix=prefix))
    if source.is_dir():
        for client_dir in source.iterdir():
            if not client_dir.is_dir():
                continue
            for period_dir in client_dir.iterdir():
                if not period_dir.is_dir():
                    continue
                for slot in period_dir.iterdir():
                    # exports/ is OUTPUT: leaving it out is the whole point.
                    if not slot.is_dir() or slot.name == "exports":
                        continue
                    for src_file in slot.iterdir():
                        if not src_file.is_file():
                            continue
                        dest = sandbox / client_dir.name / period_dir.name / slot.name / src_file.name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dest)
    return use_data_root(sandbox)


def require_client_id(client_id) -> int:
    """Coerce and validate a client id. Raises ``ValueError`` otherwise."""
    try:
        cid = int(client_id)
    except (TypeError, ValueError):
        raise ValueError(f"client_id must be an integer, got {client_id!r}") from None
    if cid <= 0:
        raise ValueError(f"client_id must be a positive integer, got {cid!r}")
    return cid


def client_data_root(client_id) -> Path:
    """The ``data/<client_id>/`` root (not created)."""
    return DATA_ROOT / str(require_client_id(client_id))


def data_dir(client_id, period: str, source_type: str) -> Path:
    """Return ``data/<client_id>/<period>/<source_type>/`` WITHOUT creating it.

    Validates every segment. The period must be ``YYYY-MM`` — or the exact
    unfiled sentinel ``"-"``, which is a legitimate *stored* folder for a file
    whose period is not yet known (never a reconcilable period). Anything else
    is rejected, so a ``"../"`` trick can never reach the filesystem.
    """
    cid = require_client_id(client_id)
    if str(period) != PERIOD_UNFILED and not is_valid_period(period):
        raise ValueError(
            f"Invalid period {period!r}; expected 'YYYY-MM' or the unfiled "
            f"sentinel {PERIOD_UNFILED!r}"
        )
    if not is_valid_source_type(source_type):
        raise ValueError(
            f"Unknown source_type {source_type!r}; expected one of {sorted(SOURCE_TYPES)}"
        )
    return DATA_ROOT / str(cid) / str(period) / str(source_type)


def source_data_path(client_id, period: str, source_type: str) -> Path:
    """Return the source-upload directory for a client, creating it if missing.

    The single entry point for building a ``data/`` path. Raises ``ValueError``
    on an invalid client id, period or source type.
    """
    path = data_dir(client_id, period, source_type)
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir(client_id, period: str) -> Path:
    """The ``data/<client_id>/<period>/exports/`` directory (created).

    Reports and workbooks live beside their period's source files, so an
    export is always reachable from the client/period it belongs to.
    """
    cid = require_client_id(client_id)
    if str(period) != PERIOD_UNFILED and not is_valid_period(period):
        raise ValueError(f"Invalid period {period!r}; expected 'YYYY-MM'")
    path = DATA_ROOT / str(cid) / str(period) / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Display-only helpers (never used for lookup)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slugify(name: str, *, max_len: int = 60) -> str:
    """A filesystem-safe slug of a display name, for download filenames ONLY.

    A slug is NEVER parsed back into an identity and NEVER appears in a data
    path — it exists purely so a downloaded report has a human-readable name.
    """
    text = _SLUG_RE.sub("_", str(name or "").strip()).strip("_")
    return (text or "client")[:max_len]


def sanitize_filename(filename: str, *, max_len: int = 200) -> str:
    """Strip path separators and control characters from an uploaded filename.

    The ORIGINAL name is always kept in the DB; this is only what lands on
    disk. Rejects empty results and truncates over-long names (preserving the
    extension where possible).
    """
    name = str(filename or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = name.strip().strip(".")
    if not name:
        raise ValueError("Unsafe or empty filename")
    if len(name) <= max_len:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 10:
        keep = max_len - len(ext) - 1
        return f"{stem[:keep]}.{ext}"
    return name[:max_len]


def list_unmapped_folders() -> list[str]:
    """Read-only health helper: ``data/`` folders whose name is NOT a valid
    client id (a leftover from before the migration, or a stray upload).

    Reserved names (a leading ``_`` or ``.``) are skipped: those are the
    platform's own folders, not client folders somebody forgot to migrate.
    """
    if not DATA_ROOT.exists():
        return []
    return sorted(
        p.name for p in DATA_ROOT.iterdir()
        if p.is_dir() and not p.name.isdigit() and not p.name.startswith(("_", "."))
    )