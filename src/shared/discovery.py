"""Filesystem + DB discovery helpers for populating sidebar selectors.

Identity is ALWAYS ``end_clients.client_id``. No function here matches a
client by name — the only name → id resolution left in the codebase lives in
``src.migrations.identity`` and runs once, at migration time.

Client options come from the F2 client table (active clients for context
pickers; inactive clients too, labelled "(inactive)", for history/"existing
data" screens). Periods and source-file status come from the
``data/<client_id>/<period>/<source_type>/`` layout via ``src.data_paths``,
plus the runs already in the DB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.data_paths import DATA_ROOT, REPLACED_DIR_NAME, client_data_root, data_dir
from src import queries
from src.shared.periods import (
    PERIOD_UNFILED,
    UNFILED_LABEL,
    EARLIEST_YEAR,
    available_years,
    is_unfiled,
    is_valid_period,
    period_label,
)
from src.shared.vocab import (
    BOOKS_SOURCE_TYPES,
    NOTE_SOURCE_TYPES,
    RECON_SOURCE_TYPES,
    RECON_TYPES,
    is_valid_source_type,
)

# Re-exported for the many callers that import these from discovery.
__all__ = [
    "RECON_TYPES", "RECON_SOURCE_TYPES", "BOOKS_SOURCE_TYPES", "NOTE_SOURCE_TYPES",
    "PERIOD_UNFILED", "UNFILED_LABEL", "is_unfiled", "is_valid_period", "period_label",
    "available_years", "SOURCE_TYPE_LABELS", "SOURCE_TYPE_HINTS",
    "source_type_label", "source_type_hint", "portal_source_types", "note_slot_types",
    "slot_types_for", "list_clients", "client_option", "list_periods",
    "reconcilable_periods", "period_has_source_data", "list_recon_types",
    "source_file_status", "books_side_ready", "portal_side_ready", "run_is_executable",
]



def note_slot_types(recon_type: str) -> list[str]:
    """Books-side NOTE registers that may be supplied for a recon type.

    GST only: the note pass matches the register against the portal's
    B2B-CDNR note sheet. These are OPTIONAL inputs — a run without one still
    reconciles invoices and states in the report that note matching could not
    be attempted.
    """
    return list(NOTE_SOURCE_TYPES) if recon_type == "GST" else []


def slot_types_for(recon_type: str) -> list[str]:
    """Every slot Stage 2 offers, in display order: the REQUIRED invoice sides
    first (books, then the portal sources), then the optional note register.

    Anything that must be selected for a run to be possible comes before
    anything that is merely nice to have. NOTE slots are here but NOT in
    RECON_SOURCE_TYPES, so they can never be chosen as an invoice side.
    """
    return list(RECON_SOURCE_TYPES.get(recon_type, [])) + note_slot_types(recon_type)

# Plain-language labels for each source type, shown in the Run tab's file
# picker instead of the raw folder name.
SOURCE_TYPE_LABELS = {
    "tally": "Books / Purchase Register",
    # The register's own voucher-type column reads "Debit Note" while the rows
    # are credit notes RECEIVED, so the label names both and says "received".
    "credit_notes": "Credit / Debit Note Register (received)",
    "gstr2b": "GSTR-2B",
    "ims": "IMS",
    "form26as": "Form 26AS",
    "tds": "TDS Return / Challan",
    "bank": "Bank Statement",
    "vendor_ledger": "Vendor Ledger",
    "opening_balances": "Opening Balances",
    "loan_sheet": "Loan Sheet",
    "salary": "Salary Register",
}

# One-line explanation of what each slot actually wants, shown beneath the
# label. The books slot in particular is NOT "a Tally export" — Tally is one
# of several formats the same internal books can arrive in.
SOURCE_TYPE_HINTS = {
    "tally": "Your internal books — purchases/sales as recorded in your accounting "
             "system. A Tally export is one format; an ERP export or a prepared "
             "register is equally fine.",
    "credit_notes": "The books-side register of credit notes you RECEIVED from "
                    "suppliers (Tally may label the voucher type 'Debit Note'). "
                    "Optional — a run without it still reconciles invoices, and "
                    "says so in the report.",
    "gstr2b": "The GSTR-2B download from the GST portal.",
    "ims": "The Invoice Management System action export from the GST portal.",
    "form26as": "The Form 26AS download from TRACES.",
    "tds": "The TDS return or challan detail.",
}


def source_type_hint(source_type: str) -> str:
    """One-line explanation of what a slot expects, or '' when none is set."""
    return SOURCE_TYPE_HINTS.get(source_type, "")


def source_type_label(source_type: str) -> str:
    """Plain-language label for a source type, falling back to the raw key."""
    return SOURCE_TYPE_LABELS.get(source_type, source_type)


def portal_source_types(recon_type: str) -> list[str]:
    """The portal-side source types for a recon type (everything except
    the books side)."""
    return [s for s in RECON_SOURCE_TYPES[recon_type] if s not in BOOKS_SOURCE_TYPES]


def _display_name(row: dict, all_rows: list[dict]) -> str:
    """end_clients.legal_name, disambiguated with PAN (or last-4 GSTIN) when
    two clients share the same legal name."""
    name = str(row.get("legal_name") or "")
    peers = [r for r in all_rows if str(r.get("legal_name") or "") == name]
    if len(peers) <= 1:
        return name
    pan = str(row.get("pan") or "").strip()
    if pan:
        return f"{name} · {pan}"
    gstin = str(row.get("gstin") or "").strip()
    if gstin:
        return f"{name} · {gstin[-4:]}"
    return f"{name} · #{row.get('client_id')}"


def list_clients(*, viewer, include_inactive: bool = False, db_path=None) -> list[dict]:
    """F2 clients the VIEWER may work on, as option rows, alphabetical.

    Each row: ``{client_id, legal_name, display_name, is_active, pan, gstin}``.
    Options come from the F2 client table (the single source of truth) — NOT
    from data/ folders. ``include_inactive=True`` (history / "existing data"
    screens) keeps deactivated clients and marks their display name
    "(inactive)"; context pickers use the default (active only).

    ``viewer`` is REQUIRED and is applied in ``src.clients.service``, so no
    page, export or count can be handed a client the user may not see. A
    client with no owning row is admin-only, so a folder created before
    ownership existed can never leak.
    """
    from src.clients import service as clients_service

    rows = clients_service.list_clients(
        viewer=viewer, include_inactive=include_inactive, db_path=db_path
    )
    out = []
    for r in rows:
        display = _display_name(r, rows)
        if not r.get("is_active", 1):
            display = f"{display} (inactive)"
        out.append(
            {
                "client_id": int(r["client_id"]),
                "legal_name": r.get("legal_name") or "",
                "display_name": display,
                "is_active": bool(r.get("is_active", 1)),
                "pan": r.get("pan"),
                "gstin": r.get("gstin"),
            }
        )
    out.sort(key=lambda o: o["display_name"].lower())
    return out


def client_option(client_id, *, viewer, db_path=None) -> Optional[dict]:
    """A single client option row by id, or None (also None when the id is
    not one this viewer owns)."""
    if not client_id:
        return None
    for c in list_clients(viewer=viewer, include_inactive=True, db_path=db_path):
        if c["client_id"] == int(client_id):
            return c
    return None


def list_periods(client_id) -> list[str]:
    """Periods (newest first, lexicographic desc works for YYYY-MM) for a
    client, from disk and DB.

    Includes the unfiled sentinel (``"-"``) when files were filed without a
    period, so those files are never silently invisible — the UI labels it
    via ``period_label()`` and explains why it cannot be reconciled.
    """
    on_disk = set()
    if client_id:
        client_dir = client_data_root(client_id)
        if client_dir.exists():
            on_disk = {p.name for p in client_dir.iterdir() if p.is_dir()}

    in_db = set()
    try:
        df = queries.list_runs(client_id=int(client_id)) if client_id else None
        if df is not None and not df.empty:
            in_db = set(df["period"].unique())
    except Exception:
        pass

    return sorted(on_disk | in_db, reverse=True)


def reconcilable_periods(client_id) -> list[str]:
    """Periods that can actually be reconciled — the unfiled sentinel
    excluded. Use this wherever a period is chosen to RUN against."""
    return [p for p in list_periods(client_id) if is_valid_period(p)]


def period_has_source_data(client_id, period: str) -> bool:
    """Whether ``data/<client_id>/<period>/<source_type>/`` holds any files.

    Used by read-only screens to show the "no data uploaded for this period
    yet" empty state instead of erroring when the period itself is valid but
    nothing has been filed against it.

    ``_replaced/`` — the archive a same-name re-upload pushes copies into — is
    a SIBLING of the slots, not one of them, and holds no CURRENT source file.
    Reading it as data would make a period look populated on the strength of a
    file the user already replaced.
    """
    if not (client_id and period):
        return False
    period_dir = client_data_root(client_id) / period
    if not period_dir.is_dir():
        return False
    for source_dir in period_dir.iterdir():
        if not source_dir.is_dir() or source_dir.name == REPLACED_DIR_NAME:
            continue
        if any(f.is_file() for f in source_dir.iterdir()):
            return True
    return False


def list_recon_types(client_id, period: str) -> list[str]:
    """Recon types that have either a run in the DB or at least one
    relevant source directory present for this client/period."""
    available = []
    period_dir = client_data_root(client_id) / period if client_id else None
    for recon_type in RECON_TYPES:
        has_run = False
        try:
            df = queries.list_runs(client_id=int(client_id), period=period, recon_type=recon_type)
            has_run = not df.empty
        except Exception:
            pass
        has_source_dir = False
        if period_dir and period_dir.exists():
            for source_type in RECON_SOURCE_TYPES[recon_type]:
                if (period_dir / source_type).exists():
                    has_source_dir = True
                    break
        if has_run or has_source_dir:
            available.append(recon_type)
    return available or list(RECON_TYPES)  # fall back to all if nothing found yet


def _find_source_files(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    matches: list[Path] = []
    for pattern in ("*.csv", "*.xlsx", "*.xls"):
        matches.extend(directory.glob(pattern))
    return sorted(m.name for m in matches)


def source_file_status(client_id, period: str, recon_type: str) -> dict[str, dict]:
    """For each source_type relevant to recon_type, report what's on disk.

    Returns {source_type: {"path": str, "files": [str, ...], "present": bool}}
    """
    from src.shared.vocab import require_valid_recon_type

    require_valid_recon_type(recon_type)
    status: dict[str, dict] = {}
    for source_type in RECON_SOURCE_TYPES[recon_type]:
        directory = data_dir(client_id, period, source_type)
        files = _find_source_files(directory)
        status[source_type] = {
            "path": str(directory),
            "files": files,
            "present": len(files) > 0,
        }
    return status



def books_side_ready(status: dict[str, dict]) -> bool:
    return status.get("tally", {}).get("present", False)


def portal_side_ready(status: dict[str, dict], recon_type: str) -> bool:
    portal_types = [s for s in RECON_SOURCE_TYPES[recon_type] if s != "tally"]
    return any(status.get(s, {}).get("present", False) for s in portal_types)


def run_is_executable(status: dict[str, dict], recon_type: str) -> bool:
    return books_side_ready(status) and portal_side_ready(status, recon_type)
