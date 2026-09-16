"""F4 — shared History loader for the reusable "View History" component.

The single place every Reflex state reads a record's edit history from, so
the component in ``setu/foundation/components.py`` stays presentation-only.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.f4 import service as f4


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
    """Newest-first history for one record, each entry in plain language."""
    rows = f4.history_for_record(record_type, record_id, client_id=client_id)
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
