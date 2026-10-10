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

import re
from pathlib import Path

from src.shared.periods import PERIOD_UNFILED, is_valid_period
from src.shared.vocab import SOURCE_TYPES, is_valid_source_type

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"

# Back-compat re-export: older imports read this set.
VALID_SOURCE_TYPES = set(SOURCE_TYPES)


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
    client id (a leftover from before the migration, or a stray upload)."""
    if not DATA_ROOT.exists():
        return []
    return sorted(
        p.name for p in DATA_ROOT.iterdir() if p.is_dir() and not p.name.isdigit()
    )