"""The ONE definition of "this upload must not change any more".

An upload that has been exported is part of an immutable export batch: once a
document is filed, its history must not move. That rule was previously written
out in four places — ``confidence.record_correction``, the legacy service's
extract/re-extract/discard/confirm paths, and the review screen's own
``read_only_for_status`` helper — which meant a new caller could easily forget
it, and that changing the rule meant finding them all.

This module owns it. It deliberately imports nothing from the rest of
``invoice_extract`` (only the DB opener), so every one of those places can call
it without an import cycle. Callers raise their own error with their own
message; this module only answers the question.
"""

from __future__ import annotations

from typing import Any, Optional

from src import db as recon_db

#: Upload statuses after which nothing about the upload may be changed.
IMMUTABLE_UPLOAD_STATUSES: tuple[str, ...] = ("exported",)


def is_immutable_status(status: Optional[Any]) -> bool:
    """The rule itself, for a caller that already holds the status."""
    return str(status or "") in IMMUTABLE_UPLOAD_STATUSES


def is_upload_immutable(upload_id: int, *, db_path=None) -> bool:
    """Is this upload part of an immutable export batch?

    False for an unknown upload and for a database that has no
    ``invoice_uploads`` table yet — neither is an immutable batch, and a status
    probe must not be the thing that raises.
    """
    conn = recon_db.get_connection(db_path)
    try:
        try:
            row = conn.execute(
                "SELECT status FROM invoice_uploads WHERE upload_id = ?", (int(upload_id),),
            ).fetchone()
        except Exception:  # noqa: BLE001 — no table yet is simply "not immutable"
            return False
    finally:
        conn.close()
    return row is not None and is_immutable_status(row[0])
