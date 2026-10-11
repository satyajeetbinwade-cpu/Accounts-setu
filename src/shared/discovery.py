"""Filesystem + DB discovery helpers for populating sidebar selectors.

No matching/query logic — this only looks at what's on disk under data/
and what's already in the DB via queries.list_runs, so the sidebar never
hardcodes client/period/recon_type values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.data_paths import DATA_ROOT
from src import queries
from src.ingestion_ai.periods import (
    PERIOD_UNFILED,
    UNFILED_LABEL,
    is_unfiled,
    is_valid_period,
    period_label,
)

# Which source_type directories are relevant to each recon type. "tally"
# feeds both (books side); portal sources are recon-type specific.
RECON_SOURCE_TYPES = {
    "GST": ["tally", "gstr2b", "ims"],
    "TDS": ["tally", "form26as", "tds"],
    # 2C "Other" — the six sources layered onto the same shared engine.
    "OTHER": ["tally", "bank", "vendor_ledger", "form26as", "opening_balances", "loan_sheet", "salary"],
}

# Books-side source types (always required for a run).
BOOKS_SOURCE_TYPES = ["tally"]

# Books-side NOTE register. A separate document class from the Purchase
# Register, reconciled by its own pass — deliberately NOT in RECON_SOURCE_TYPES
# above, so it can never be picked as the books or portal side of an INVOICE run.
NOTE_SOURCE_TYPES = ["credit_notes"]

RECON_TYPES = ["GST", "TDS", "OTHER"]


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


def list_clients(viewer=None, *, db_path=None) -> list[str]:
    """Clients the viewer may work on.

    The union of three things, which is what makes the picker useful at every
    stage of a client's life: the client's ``data/`` folder, any run recorded
    for it (in case the source files were later removed), and **its client
    record** — so a client created moments ago is selectable before anything
    has been uploaded for it.

    Scoped to ``viewer``: a standard user sees ONLY the clients they own. The
    filter is applied here rather than in the UI, so no page, export or count
    can be handed a client the user may not see. A client name with no owning
    record counts as admin-only, so a folder created before ownership existed
    can never leak.
    """
    from src.auth import access

    on_disk = set()
    if DATA_ROOT.exists():
        on_disk = {p.name for p in DATA_ROOT.iterdir() if p.is_dir()}

    in_db = set()
    try:
        df = queries.list_runs(db_path=db_path)
        if not df.empty:
            in_db = set(df["client"].unique())
    except Exception:
        pass

    owned = access.visible_client_names(viewer, db_path=db_path)
    if owned is None:
        # Admin: everything, including client records with no folder yet.
        names = on_disk | in_db | access.all_client_record_names(db_path=db_path)
    else:
        # A standard user's world IS their owned records. Their folders/runs
        # are a subset of those names, so no separate union is needed.
        names = owned
    return sorted(n for n in names if n)


def list_periods(client: str) -> list[str]:
    """Periods (newest first, lexicographic desc works for YYYY-MM) for a
    client, from disk and DB.

    Includes the unfiled sentinel (``"-"``) when files were filed without a
    period, so those files are never silently invisible — the UI labels it
    via ``period_label()`` and explains why it cannot be reconciled.
    """
    on_disk = set()
    client_dir = DATA_ROOT / client
    if client_dir.exists():
        on_disk = {p.name for p in client_dir.iterdir() if p.is_dir()}

    in_db = set()
    try:
        df = queries.list_runs(client=client)
        if not df.empty:
            in_db = set(df["period"].unique())
    except Exception:
        pass

    return sorted(on_disk | in_db, reverse=True)


def reconcilable_periods(client: str) -> list[str]:
    """Periods that can actually be reconciled — the unfiled sentinel
    excluded. Use this wherever a period is chosen to RUN against."""
    return [p for p in list_periods(client) if is_valid_period(p)]


def list_recon_types(client: str, period: str) -> list[str]:
    """Recon types that have either a run in the DB or at least one
    relevant source directory present for this client/period."""
    available = []
    period_dir = DATA_ROOT / client / period
    for recon_type in RECON_TYPES:
        has_run = False
        try:
            df = queries.list_runs(client=client, period=period, recon_type=recon_type)
            has_run = not df.empty
        except Exception:
            pass
        has_source_dir = False
        if period_dir.exists():
            for source_type in RECON_SOURCE_TYPES[recon_type]:
                if (period_dir / source_type).exists():
                    has_source_dir = True
                    break
        if has_run or has_source_dir:
            available.append(recon_type)
    return available or RECON_TYPES  # fall back to both if nothing found yet


def _find_source_files(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    matches: list[Path] = []
    for pattern in ("*.csv", "*.xlsx", "*.xls"):
        matches.extend(directory.glob(pattern))
    return sorted(m.name for m in matches)


def source_file_status(client: str, period: str, recon_type: str) -> dict[str, dict]:
    """For each source_type relevant to recon_type, report what's on disk.

    Returns {source_type: {"path": str, "files": [str, ...], "present": bool}}
    """
    status: dict[str, dict] = {}
    for source_type in RECON_SOURCE_TYPES[recon_type]:
        directory = DATA_ROOT / client / period / source_type
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
