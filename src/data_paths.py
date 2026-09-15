"""Filesystem helpers for the data/ uploaded-source layout.

data/{client}/{period}/{source_type}/
"""

from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"

VALID_SOURCE_TYPES = {
    "tally", "gstr2b", "ims", "form26as", "tds",
    # 2C "Other" sources (Module 2).
    "bank", "vendor_ledger", "opening_balances", "loan_sheet", "salary",
}


def source_data_path(client: str, period: str, source_type: str) -> Path:
    """Return the directory for a source upload, creating it if missing.

    Raises ValueError if source_type is not one of the known types.
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(
            f"Unknown source_type {source_type!r}; expected one of "
            f"{sorted(VALID_SOURCE_TYPES)}"
        )
    path = DATA_ROOT / client / period / source_type
    path.mkdir(parents=True, exist_ok=True)
    return path