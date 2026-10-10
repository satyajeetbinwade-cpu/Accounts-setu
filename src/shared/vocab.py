"""Single source of truth for the small controlled vocabularies the engine
validates at its boundaries — recon types and source types.

Keeping these here (rather than scattered across ``data_paths.py`` and
``discovery.py``) means a path builder, a picker and an ingestion service
can never disagree about what a legal value is. ``CHECK`` constraints are
deliberately not relied on for the *legacy* tables (adding them means
rebuilding every table for a rule that belongs at the service boundary);
this module + ``require_*`` validators are the enforcement point.
"""

from __future__ import annotations

# Recon types the guided flow offers.
RECON_TYPES: tuple[str, ...] = ("GST", "TDS", "OTHER")

# Books-side source types (always required for a run).
BOOKS_SOURCE_TYPES: tuple[str, ...] = ("tally",)

# Every source type that may appear as an upload slot.
SOURCE_TYPES: tuple[str, ...] = (
    "tally", "gstr2b", "ims", "form26as", "tds",
    "bank", "vendor_ledger", "opening_balances", "loan_sheet", "salary",
    "credit_notes",
)

# Which source_type directories are relevant to each recon type. "tally"
# feeds both (books side); portal sources are recon-type specific.
RECON_SOURCE_TYPES: dict[str, list[str]] = {
    "GST": ["tally", "gstr2b", "ims"],
    "TDS": ["tally", "form26as", "tds"],
    "OTHER": ["tally", "bank", "vendor_ledger", "form26as", "opening_balances", "loan_sheet", "salary"],
}

# Books-side NOTE register (its own document class; never an INVOICE slot).
NOTE_SOURCE_TYPES: tuple[str, ...] = ("credit_notes",)


def is_valid_recon_type(recon_type: str) -> bool:
    return recon_type in RECON_TYPES


def is_valid_source_type(source_type: str) -> bool:
    return source_type in SOURCE_TYPES


def require_valid_recon_type(recon_type: str) -> str:
    if recon_type not in RECON_TYPES:
        raise ValueError(
            f"Unknown recon_type {recon_type!r}; expected one of {list(RECON_TYPES)}"
        )
    return recon_type


def require_valid_source_type(source_type: str) -> str:
    if source_type not in SOURCE_TYPES:
        raise ValueError(
            f"Unknown source_type {source_type!r}; expected one of {sorted(SOURCE_TYPES)}"
        )
    return source_type
