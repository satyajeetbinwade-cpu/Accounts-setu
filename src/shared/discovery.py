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

RECON_TYPES = ["GST", "TDS", "OTHER"]

# Plain-language labels for each source type, shown in the Run tab's file
# picker instead of the raw folder name.
SOURCE_TYPE_LABELS = {
    "tally": "Books / Purchase Register",
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


def list_clients() -> list[str]:
    """Clients with a data/ folder, plus any client already in the DB
    (in case a run exists for a client whose source files were removed)."""
    on_disk = set()
    if DATA_ROOT.exists():
        on_disk = {p.name for p in DATA_ROOT.iterdir() if p.is_dir()}

    in_db = set()
    try:
        df = queries.list_runs()
        if not df.empty:
            in_db = set(df["client"].unique())
    except Exception:
        pass

    return sorted(on_disk | in_db)


def list_periods(client: str) -> list[str]:
    """Periods (newest first, lexicographic desc works for YYYY-MM) for a
    client, from disk and DB."""
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
