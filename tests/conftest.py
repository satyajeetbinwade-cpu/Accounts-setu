"""Shared test helpers.

Identity is ``end_clients.client_id`` everywhere now, so any test that runs a
reconciliation must first seed a client and lay its fixtures out under
``data/<client_id>/<period>/<source_type>/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def seed_client(
    db_path,
    legal_name: str = "Test Client",
    *,
    pan: str | None = "AAAPL1234C",
    tan: str | None = None,
    gstin: str | None = None,
) -> int:
    """Create one F2 client and return its ``client_id`` (1 on a fresh DB)."""
    from src.clients import service as clients

    return clients.create_client(
        legal_name=legal_name, pan=pan, tan=tan, gstin=gstin,
        actor="test", db_path=db_path,
    )
