"""F4 — shared History loader for the reusable "View History" component.

The single place every Reflex state reads a record's edit history from, so
the component in ``setu/foundation/components.py`` stays presentation-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.f4 import service as f4

logger = logging.getLogger(__name__)


@dataclass
class HistoryEntry:
    """One edit-history entry, pre-formatted for the panel."""

    plain_language: str
    timestamp: str
    field: str
    changed_by: str


def load_history(
    record_type: str, record_id, *, client_id: int | None = None
) -> list[HistoryEntry]:
    """Newest-first history for one record, each entry in plain language.

    Degrades to an empty history rather than raising. Callers embed the result
    directly in ``rx.foreach`` renderers and also build per-record dicts from
    it, so a read failure must yield ``[]`` — never abort mid-load and leave
    the owning collection partially built. That exact failure mode crashed the
    whole page with "Cannot read properties of undefined (reading 'length')"
    when the underlying table was missing.
    """
    try:
        rows = f4.history_for_record(record_type, record_id, client_id=client_id)
    except Exception as exc:  # noqa: BLE001 — degrade to empty, log loudly
        logger.warning(
            "Edit history unavailable for %s %r: %s", record_type, record_id, exc
        )
        return []
    return [
        HistoryEntry(
            plain_language=r.get("plain_language") or "",
            timestamp=str(r.get("created_at") or "").replace("T", " ").split(".")[0],
            field=r.get("field") or "",
            changed_by=r.get("changed_by") or "",
        )
        for r in rows
    ]


def history_count(record_type: str, record_id, *, client_id: int | None = None) -> int:
    return len(load_history(record_type, record_id, client_id=client_id))
