"""Portal coverage declarations — what a run did NOT reconcile (D7).

A reconciliation that silently omits a whole class of documents is worse than
one that declares the omission. The GSTR-2B carries credit notes (B2B-CDNR)
and the IMS export carries per-document action statuses; neither is matched in
this build (credit-note matching is explicitly out of scope). Rather than let
them vanish, the run records a NOTE naming exactly what was left out and why.

Notes are DECLARATIONS, not caveats: a caveat says "a check could not run"; a
note says "this class of document was deliberately not reconciled". They are
stored on the run and surfaced in the Review screen's data-quality panel.

Pure reads over the source files — no matching, no data-model change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd

# Sheets that hold credit/debit notes in a GSTR-2B export. These are the
# document classes this build does not reconcile. B2B-CDNR is the original
# credit-note register; B2B-CDNRA holds amendments to it.
_CREDIT_NOTE_SHEETS = ("B2B-CDNR",)

# IMS sheets and the column carrying the action status.
_IMS_STATUS_SHEETS = ("B2B", "B2B-CN")
_IMS_STATUS_COLUMN = "Status"


def _read_sheet(path: Path, sheet: str) -> Optional[pd.DataFrame]:
    try:
        return pd.read_excel(path, sheet_name=sheet, header=None, dtype=str, keep_default_na=False)
    except Exception:  # noqa: BLE001
        return None


def _data_rows(raw: pd.DataFrame, header_row: int) -> pd.DataFrame:
    """Rows below the header, with fully blank rows dropped.

    A GSTN export has a TWO-row header (parent + child), so the row directly
    below the parent header is the child header, not data. Data rows always
    carry a GSTIN in the first column, so requiring a non-blank first cell
    skips the child header without hard-coding its position.
    """
    data = raw.iloc[header_row + 1:]
    keep = data.apply(
        lambda r: (
            str(r.iloc[0]).strip() not in ("", "nan", "None")
            and any(str(v).strip() not in ("", "nan", "None") for v in r)
        ),
        axis=1,
    )
    return data.loc[keep]


def _find_header_row(raw: pd.DataFrame, needle: str) -> Optional[int]:
    for i in range(min(20, len(raw))):
        cells = [str(v).strip().lower() for v in raw.iloc[i]]
        if any(needle in c for c in cells):
            return i
    return None


def _note_number_column(raw: pd.DataFrame, header_row: int) -> Optional[int]:
    """Index of the B2B-CDNR sheet's note-number column, if present.

    The label sits on the CHILD header row ("Credit note/Debit note number"),
    not the parent row the header search keys on."""
    for row_idx in (header_row + 1, header_row):
        if row_idx >= len(raw):
            continue
        cells = [str(v).strip().lower() for v in raw.iloc[row_idx]]
        col = next((i for i, c in enumerate(cells)
                    if "note number" in c or "credit note number" in c), None)
        if col is not None:
            return col
    return None


def credit_note_note(path: Path) -> Optional[dict[str, Any]]:
    """A note declaring how many portal CREDIT notes were not reconciled.

    Debit-note rows are excluded: a debit note is a genuine charge that IS
    reconciled against the books (Test Set 3 S3-F1), so counting it here would
    assert an omission that did not happen."""
    total = 0
    for sheet in _CREDIT_NOTE_SHEETS:
        raw = _read_sheet(path, sheet)
        if raw is None or raw.empty:
            continue
        header_row = _find_header_row(raw, "gstin of supplier")
        if header_row is None:
            continue
        num_col = _note_number_column(raw, header_row)
        for _, row in _data_rows(raw, header_row).iterrows():
            if num_col is not None:
                number = str(row.iloc[num_col]).strip().upper()
                if number.startswith("DN"):
                    continue  # debit note — matched as an invoice, not omitted
            total += 1
    if not total:
        return None
    return {
        "code": "credit_notes_not_reconciled",
        "kind": "unreconciled_source",
        "title": f"{total} credit note(s) in the portal file were not reconciled",
        "detail": (
            f"The portal file carries {total} credit note(s) (B2B-CDNR). No books-side "
            "debit/credit-note register was uploaded, and credit-note matching is not "
            "part of this reconciliation, so they were left out entirely rather than "
            "silently dropped."
        ),
        "count": total,
    }


def ims_coverage_note(path: Path) -> Optional[dict[str, Any]]:
    """A note declaring the IMS action coverage of the portal documents."""
    counts: dict[str, int] = {}
    total = 0
    for sheet in _IMS_STATUS_SHEETS:
        raw = _read_sheet(path, sheet)
        if raw is None or raw.empty:
            continue
        header_row = _find_header_row(raw, "gstin of supplier")
        if header_row is None:
            continue
        header_cells = [str(v).strip().lower() for v in raw.iloc[header_row]]
        status_col = next(
            (n for n, c in enumerate(header_cells) if c == _IMS_STATUS_COLUMN.lower()), None
        )
        if status_col is None:
            continue
        for _, row in _data_rows(raw, header_row).iterrows():
            status = str(row.iloc[status_col]).strip() or "Unknown"
            counts[status] = counts.get(status, 0) + 1
            total += 1
    if not total:
        return None
    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    return {
        "code": "ims_coverage",
        "kind": "coverage",
        "title": f"IMS action status covers {total} portal document(s)",
        "detail": (
            f"IMS statuses on the supplied export: {parts}. IMS status is carried "
            "through as reference only — it does not drive matching in this build."
        ),
        "counts": counts,
    }


def portal_run_notes(path: Path, source_type: str) -> list[dict[str, Any]]:
    """Every coverage note a portal export warrants, for the run record."""
    notes: list[dict[str, Any]] = []
    if source_type == "gstr2b":
        note = credit_note_note(path)
        if note:
            notes.append(note)
    if source_type == "ims":
        note = ims_coverage_note(path)
        if note:
            notes.append(note)
    return notes
